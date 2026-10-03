"""Web search, for the model to call when it does not know something.

Three backends, tried in order of how much they can be trusted:

  Tavily and Brave are real search APIs. They need a key, they return clean
  JSON, and they do not change shape underneath us.

  DuckDuckGo needs no key, which is why it is here — the feature works the
  moment it is switched on, with nothing to sign up for. It is also scraped
  HTML, so it will break one day without warning. That is an acceptable
  default and a poor foundation, hence the order.

A failure here is never an error anywhere else. The model asked a question and
gets "no results" back, which it can say out loud; the alternative is a whole
answer lost because a search engine was slow.
"""
import asyncio
import dataclasses
import html
import logging
import os
import re

import aiohttp

log = logging.getLogger("rula.search")

# ---------------------------------------------------------------------------
# Set to False to take the search tool away from the model entirely.
# SEARCH=off in the environment does the same.
# ---------------------------------------------------------------------------
ENABLED = True

TIMEOUT = 10.0
MAX_RESULTS = 5
# Snippets are what the model actually reads. Long enough to be worth having,
# short enough that five of them do not crowd out the conversation.
SNIPPET_CHARS = 400

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


@dataclasses.dataclass
class Result:
    title: str
    url: str
    snippet: str


def enabled() -> bool:
    raw = os.environ.get("SEARCH", "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return ENABLED


def backend() -> str:
    if os.environ.get("TAVILY_API_KEY", "").strip():
        return "Tavily"
    if os.environ.get("BRAVE_API_KEY", "").strip():
        return "Brave"
    return "DuckDuckGo"


async def _tavily(session, query: str, count: int) -> list[Result]:
    key = os.environ["TAVILY_API_KEY"].strip()
    async with session.post("https://api.tavily.com/search", json={
            "api_key": key, "query": query, "max_results": count,
            "search_depth": "basic"}) as resp:
        if resp.status != 200:
            raise RuntimeError(f"tavily {resp.status}: {(await resp.text())[:120]}")
        data = await resp.json(content_type=None)
    return [Result(r.get("title", ""), r.get("url", ""), r.get("content", ""))
            for r in (data.get("results") or [])]


async def _brave(session, query: str, count: int) -> list[Result]:
    key = os.environ["BRAVE_API_KEY"].strip()
    async with session.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": str(count)},
            headers={"X-Subscription-Token": key,
                     "Accept": "application/json"}) as resp:
        if resp.status != 200:
            raise RuntimeError(f"brave {resp.status}: {(await resp.text())[:120]}")
        data = await resp.json(content_type=None)
    return [Result(r.get("title", ""), r.get("url", ""), r.get("description", ""))
            for r in ((data.get("web") or {}).get("results") or [])]


# The lite endpoint returns a table rather than a page of layout, which is both
# smaller and far less likely to be restyled. Parsed with regex rather than a
# DOM library on purpose: there is no HTML parser in the standard library worth
# the name, and adding a dependency for a fallback would be the wrong trade.
_DDG_ANCHOR = re.compile(r"<a\s+([^>]*)>(.*?)</a>", re.I | re.S)
_HREF = re.compile(r"""href\s*=\s*["']([^"']+)""", re.I)
_DDG_SNIPPET = re.compile(
    r"""<td[^>]*class\s*=\s*["'][^"']*result-snippet[^"']*["'][^>]*>(.*?)</td>""",
    re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")


def _ddg_links(page: str):
    """(url, title) for each result anchor, whatever order its attributes are in.

    Matched as "an anchor whose attributes mention result-link" rather than as
    one pattern over the whole tag. A single pattern has to assume an order for
    href and class, and the real markup puts them the other way round — which
    finds nothing, silently, and looks exactly like a search returning no hits.
    """
    for attrs, inner in _DDG_ANCHOR.findall(page):
        if "result-link" not in attrs:
            continue
        href = _HREF.search(attrs)
        if href:
            yield href.group(1), inner


def _clean(fragment: str) -> str:
    return html.unescape(_TAGS.sub("", fragment or "")).strip()


async def _duckduckgo(session, query: str, count: int) -> list[Result]:
    async with session.post("https://lite.duckduckgo.com/lite/",
                            data={"q": query}) as resp:
        if resp.status != 200:
            raise RuntimeError(f"duckduckgo {resp.status}")
        page = await resp.text()
    links = list(_ddg_links(page))
    snippets = _DDG_SNIPPET.findall(page)
    out = []
    for i, (url, title) in enumerate(links[:count]):
        snippet = _clean(snippets[i]) if i < len(snippets) else ""
        out.append(Result(_clean(title), html.unescape(url), snippet))
    return out


async def search(query: str, count: int = MAX_RESULTS) -> list[Result]:
    """Search the web. Returns [] rather than raising, whatever went wrong."""
    query = (query or "").strip()
    if not query or not enabled():
        return []
    engines = []
    if os.environ.get("TAVILY_API_KEY", "").strip():
        engines.append(("Tavily", _tavily))
    if os.environ.get("BRAVE_API_KEY", "").strip():
        engines.append(("Brave", _brave))
    engines.append(("DuckDuckGo", _duckduckgo))

    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": _UA},
                                         timeout=timeout) as session:
            for name, engine in engines:
                try:
                    results = await engine(session, query, count)
                except (aiohttp.ClientError, asyncio.TimeoutError,
                        RuntimeError, ValueError, KeyError) as e:
                    log.warning("%s の検索に失敗しました: %s", name, e)
                    continue
                results = [r for r in results if r.url]
                if results:
                    log.info("検索(%s): %s -> %d件", name, query[:60], len(results))
                    return results[:count]
                log.info("検索(%s): %s -> 0件", name, query[:60])
    except Exception:
        log.exception("検索に失敗しました")
    return []


def as_text(query: str, results: list[Result]) -> str:
    """The results as the model will read them.

    Numbered and with the URL on its own line, so an answer can cite one. The
    instruction at the end is here rather than in the system prompt because it
    is about these results specifically — a model told once at the top of a long
    conversation stops honouring it by the time it matters.
    """
    if not results:
        return (f"「{query}」の検索結果はありませんでした。"
                "検索できなかったことを述べ、推測で答えないでください。")
    lines = [f"「{query}」の検索結果:"]
    for i, r in enumerate(results, 1):
        lines.append(f"\n[{i}] {r.title}\n{r.url}\n{r.snippet[:SNIPPET_CHARS]}")
    lines.append("\n上の結果だけを根拠に答えてください。"
                 "結果に無いことは書かず、参照した番号を示してください。")
    return "\n".join(lines)


# The tool as the model sees it. The description is doing real work: it is the
# only thing deciding whether a question about today's weather triggers a search
# or an invented answer.
TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current or factual information. Use this "
            "whenever the answer depends on recent events, prices, news, "
            "schedules, sports results, software versions, or anything that "
            "may have changed since training. Also use it when asked about a "
            "specific person, product or place you are not certain about. "
            "Do not use it for opinions, translation, arithmetic, or casual "
            "conversation."),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The search query. Use the language the answer is "
                        "likely written in — Japanese for Japanese topics."),
                },
            },
            "required": ["query"],
        },
    },
}
