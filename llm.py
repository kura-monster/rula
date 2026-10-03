"""Chat completions over Groq and OpenRouter, with a pool of rotating keys.

Both providers speak the OpenAI protocol, so one streaming implementation
covers them and the local Rula API alike. What differs is everything around it:

  Keys.    Free tiers are rate limited per key, so the point of this module is
           to hold many of them and move between them. Rotation is on every
           request, not only on failure — spreading load is what keeps any one
           key below its limit, whereas using a key until it breaks guarantees
           that it does.

  History. The Rula API kept the conversation server-side, keyed by `user`.
           These do not: every request must carry the whole exchange, so the
           history lives here now. See Memory.

  Failover happens before the first token and not after. Once part of an answer
  has been shown, retrying elsewhere would repeat it — a duplicated half-answer
  is worse than the error, so at that point the error is what is reported.
"""
import asyncio
import dataclasses
import json
import logging
import os
import random
import re
import sys
import time
from collections import deque

import aiohttp

import websearch

log = logging.getLogger("rula.llm")

# Long enough for a slow model to finish thinking; the guard that matters is
# sock_read, which fires when the server has stopped sending mid-stream.
READ_TIMEOUT = 180
# How many keys to try for one request before giving up.
MAX_ATTEMPTS = 6
# Parameters not every model accepts. When one is named in a 400 the request is
# retried once without them, on the same key.
_OPTIONAL_PARAMS = ("reasoning_effort",)
# Parsed out of "Limit 1000, Requested 1092" in a too-large-output 429. Not
# Groq-specific by name — several providers phrase this error the same way —
# but the number is: it is the per-minute output-token budget for this
# account, model and tier, whatever it happens to be.
_LIMIT_RE = re.compile(r"[Ll]imit\s+(\d+)[,\s]+[Rr]equested\s+(\d+)")


def _shrink_hint(detail: str) -> int | None:
    """A max_tokens worth retrying with, if this 429 was about the size of a
    single reply rather than about how many replies have gone out recently.

    The distinction matters because the two need opposite responses. An
    ordinary rate limit gets better if a different key or a little time is
    given — this one does not: the same request will hit the same wall on
    every key on this tier, for as long as it keeps asking for an output the
    tier cannot deliver in one request. Only capping the request fixes it.
    """
    if "reduce max_tokens" not in detail and "reduce max_completion_tokens" not in detail:
        return None
    match = _LIMIT_RE.search(detail)
    if not match:
        return 512  # the phrase was there; a conservative guess beats none
    limit = int(match.group(1))
    # Margin below the stated ceiling: reasoning models spend part of the same
    # per-minute budget on reasoning the caller is not shown, so asking for
    # exactly the limit still overshoots it on the next turn.
    return max(256, int(limit * 0.8))
# How many times the model may search before it has to answer. Two is enough to
# look something up and then check one detail of it; more than that is usually a
# model that has decided searching is easier than answering.
TOOL_ROUNDS = 2


def _tool_query(request: dict) -> str:
    """The search phrase out of a tool call, whatever state it arrived in.

    The arguments are a JSON string the model wrote, so they can be malformed —
    truncated by a length limit, or not JSON at all. A bad call becomes a
    literal search for whatever it said rather than an exception: a poor search
    is recoverable and a crashed answer is not.
    """
    raw = (request.get("function") or {}).get("arguments") or ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return str(parsed.get("query") or "").strip()[:200]
    except ValueError:
        found = re.search(r'"query"\s*:\s*"([^"]{1,200})', raw)
        if found:
            return found.group(1).strip()
    return raw.strip()[:200]
# After a 429 with no Retry-After, and the ceiling for one.
DEFAULT_COOLDOWN = 60
MAX_COOLDOWN = 900

# Conversation kept per channel. Two ceilings because either alone fails: a
# turn count lets one pasted essay fill the context, and a character budget
# alone would keep forty one-word replies.
HISTORY_TURNS = 12
HISTORY_CHARS = 8000
# Trimmed before sending. Providers reject an over-long request outright, and
# the newest turn is the one worth keeping whole.
MAX_MESSAGE_CHARS = 6000

# What to ask a reasoning model for. Overridable because the accepted values are
# not standard: "none" is valid on some models and a 400 on Groq's gpt-oss,
# which accepts only low, medium and high.
REASONING_LOW = os.environ.get("REASONING_LOW", "low").strip() or "low"
REASONING_HIGH = os.environ.get("REASONING_HIGH", "medium").strip() or "medium"


@dataclasses.dataclass(frozen=True)
class Provider:
    name: str
    base: str
    model: str
    env_keys: str
    env_model: str
    headers: dict = dataclasses.field(default_factory=dict)


# Defaults are current, free-tier-reachable models. Both are overridable,
# because a provider retiring a model name is a matter of when rather than if,
# and that should be an environment variable rather than an edit.
PROVIDERS = (
    Provider(
        name="Groq",
        base="https://api.groq.com/openai/v1",
        model="llama-3.3-70b-versatile",
        env_keys="GROQ_API_KEYS",
        env_model="GROQ_MODEL",
    ),
    Provider(
        name="OpenRouter",
        base="https://openrouter.ai/api/v1",
        model="meta-llama/llama-3.3-70b-instruct:free",
        env_keys="OPENROUTER_API_KEYS",
        env_model="OPENROUTER_MODEL",
        # OpenRouter attributes usage to these and rate-limits unattributed
        # traffic harder. Neither is secret.
        headers={"HTTP-Referer": "https://github.com/", "X-Title": "RulaChan"},
    ),
)


class LLMError(RuntimeError):
    """Something the person waiting for an answer should be told."""

    def __init__(self, message: str, japanese: str | None = None):
        super().__init__(message)
        self.japanese = japanese or message

    def as_japanese(self) -> str:
        return self.japanese


class _Retryable(LLMError):
    """A failure, and what it says about the key that produced it.

    The distinction that matters is whether another key could do better. A rate
    limit or a dead server: yes. A request the provider will not parse: no —
    every key would be told the same thing, and sweeping the pool to find that
    out costs every one of them a cooldown for a fault that was never theirs.
    """

    def __init__(self, message: str, cooldown: float = 0.0,
                 fatal_key: bool = False, fatal_request: bool = False,
                 drop_optional: bool = False, shrink_to: int | None = None):
        super().__init__(message)
        self.cooldown = cooldown
        self.fatal_key = fatal_key          # this key is finished
        self.fatal_request = fatal_request  # no key can serve this request
        self.drop_optional = drop_optional  # worth one retry without extras
        self.shrink_to = shrink_to          # worth one retry with less output


@dataclasses.dataclass
class Key:
    value: str
    provider: Provider
    index: int
    cooldown_until: float = 0.0
    failures: int = 0
    uses: int = 0
    disabled: str = ""          # non-empty is the reason it was retired

    @property
    def usable(self) -> bool:
        return not self.disabled and time.monotonic() >= self.cooldown_until

    @property
    def label(self) -> str:
        """Enough to identify it in a log, not enough to use it."""
        return f"{self.provider.name}#{self.index + 1}"


def _split_keys(raw: str) -> list[str]:
    """Keys from one variable, however they were separated.

    Commas, whitespace and newlines all work: a list of thirty keys is usually
    pasted from somewhere, and which separator survived that is not something
    to make anyone think about.
    """
    seen, out = set(), []
    for candidate in re.split(r"[,\s;]+", raw or ""):
        candidate = candidate.strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


class KeyPool:
    """Every key from every provider, handed out in turn.

    Round-robin rather than "use the first until it fails": the free tiers are
    per-key-per-minute, so spreading requests is what keeps all of them under
    the limit. Using one until it 429s means one key does all the work, hits
    the limit every time, and the rest sit idle.
    """

    def __init__(self):
        self.keys: list[Key] = []
        self._next = 0
        for provider in PROVIDERS:
            values = _split_keys(os.environ.get(provider.env_keys, ""))
            model = os.environ.get(provider.env_model, "").strip()
            if model:
                provider = dataclasses.replace(provider, model=model)
            for i, value in enumerate(values):
                self.keys.append(Key(value=value, provider=provider, index=i))
        # Interleaved, so the rotation alternates between providers instead of
        # working through one and then the other.
        random.shuffle(self.keys)

    def __len__(self) -> int:
        return len(self.keys)

    @property
    def providers(self) -> list[str]:
        return sorted({k.provider.name for k in self.keys})

    def usable(self) -> list[Key]:
        return [k for k in self.keys if k.usable]

    def acquire(self, tried: set[int]) -> Key | None:
        """The next key not already tried for this request."""
        for _ in range(len(self.keys)):
            key = self.keys[self._next % len(self.keys)]
            self._next += 1
            if key.usable and id(key) not in tried:
                return key
        return None

    def penalise(self, key: Key, seconds: float):
        seconds = min(max(seconds or DEFAULT_COOLDOWN, 1.0), MAX_COOLDOWN)
        key.cooldown_until = time.monotonic() + seconds
        key.failures += 1
        log.info("%s を %.0f秒 休ませます (失敗 %d回目)",
                 key.label, seconds, key.failures)

    def disable(self, key: Key, reason: str):
        key.disabled = reason
        log.warning("%s を無効にしました: %s", key.label, reason)

    def succeed(self, key: Key):
        key.failures = 0
        key.uses += 1

    def report(self) -> str:
        if not self.keys:
            return "APIキーが設定されていません"
        lines = []
        for provider in self.providers:
            mine = [k for k in self.keys if k.provider.name == provider]
            live = sum(1 for k in mine if k.usable)
            resting = sum(1 for k in mine if not k.disabled and not k.usable)
            dead = sum(1 for k in mine if k.disabled)
            model = mine[0].provider.model
            part = f"{provider}: {live}/{len(mine)} 使用可"
            if resting:
                part += f" (休止 {resting})"
            if dead:
                part += f" (無効 {dead})"
            lines.append(f"{part} — {model}")
        return "\n".join(lines)


class Memory:
    """The conversation, per channel, kept here because the providers do not.

    Bounded twice over. A context window that overflows is rejected by the
    provider as one long error rather than degrading, so the trimming has to
    happen before the request rather than being discovered by it.
    """

    def __init__(self):
        self._turns: dict[str, deque] = {}

    def history(self, key: str) -> list[dict]:
        return list(self._turns.get(key, ()))

    def add(self, key: str, role: str, content):
        turns = self._turns.setdefault(key, deque(maxlen=HISTORY_TURNS * 2))
        turns.append({"role": role, "content": content})
        self._trim(turns)

    def clear(self, key: str) -> bool:
        return self._turns.pop(key, None) is not None

    def __len__(self) -> int:
        return len(self._turns)

    @staticmethod
    def _trim(turns: deque):
        def size(entry) -> int:
            body = entry["content"]
            if isinstance(body, str):
                return len(body)
            return sum(len(p.get("text", "")) for p in body if isinstance(p, dict))

        while len(turns) > 2 and sum(size(t) for t in turns) > HISTORY_CHARS:
            turns.popleft()
            # Never leave an assistant reply as the opening turn: a request
            # that begins with someone else's answer reads as though the model
            # said it unprompted, and some providers reject the shape outright.
            if turns and turns[0]["role"] == "assistant":
                turns.popleft()


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", "") for p in content if isinstance(p, dict))


class LLMClient:
    """The same surface RulaClient had, over whichever keys are configured."""

    def __init__(self, system: str = ""):
        self.pool = KeyPool()
        self.memory = Memory()
        self.system = system.strip()
        self._session: aiohttp.ClientSession | None = None
        # A max_tokens learned from a provider's own complaint that requests
        # were too large for it, keyed by provider name. Learned once and kept
        # for the life of the process, so the correction sticks instead of
        # being rediscovered — and its cooldown paid — on every single message.
        self._max_tokens: dict[str, int] = {}

    @property
    def configured(self) -> bool:
        return len(self.pool) > 0

    def forget(self, conversation: str) -> bool:
        """Drop one channel's history. Only meaningful now that it is kept here."""
        return self.memory.clear(conversation)

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_read=READ_TIMEOUT))
        return self

    async def __aexit__(self, *_exc):
        if self._session:
            await self._session.close()

    # -- the OpenAI request ------------------------------------------------ #

    def _messages(self, conversation: str, message: str,
                  image: str | None) -> list[dict]:
        body: list[dict] = []
        if self.system:
            body.append({"role": "system", "content": self.system})
        body.extend(self.memory.history(conversation))
        if image:
            body.append({"role": "user", "content": [
                {"type": "text", "text": message[:MAX_MESSAGE_CHARS]},
                {"type": "image_url", "image_url": {"url": image}}]})
        else:
            body.append({"role": "user", "content": message[:MAX_MESSAGE_CHARS]})
        return body

    async def _open(self, key: Key, messages: list[dict], think: bool,
                    optional: bool = True, tools: bool = False,
                    max_tokens: int | None = None):
        payload = {
            "model": key.provider.model,
            "messages": messages,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = [websearch.TOOL_SPEC]
            payload["tool_choice"] = "auto"
        if optional:
            # Read only by the reasoning models. The accepted values differ by
            # model — Groq's gpt-oss takes low/medium/high and rejects the
            # "none" that others accept — so anything unrecognised is dropped
            # and retried rather than guessed at. See _classify.
            payload["reasoning_effort"] = REASONING_HIGH if think else REASONING_LOW
        headers = {"Authorization": f"Bearer {key.value}",
                   "Content-Type": "application/json", **key.provider.headers}
        return await self._session.post(
            f"{key.provider.base}/chat/completions",
            json=payload, headers=headers)

    @staticmethod
    async def _classify(resp) -> _Retryable | None:
        """Turn a non-200 into something the pool can act on."""
        if resp.status == 200:
            return None
        detail = (await resp.text())[:300]
        if resp.status == 429:
            shrink = _shrink_hint(detail)
            if shrink:
                # Not a "wait it out" 429: retried immediately, with a smaller
                # ask, rather than costing every key in the pool a cooldown for
                # a problem cooldowns cannot fix. See _shrink_hint.
                return _Retryable(f"output too large: {detail}", shrink_to=shrink)
            wait = resp.headers.get("Retry-After", "")
            try:
                cooldown = float(wait)
            except ValueError:
                cooldown = DEFAULT_COOLDOWN
            return _Retryable(f"rate limited: {detail}", cooldown=cooldown)
        if resp.status in (401, 403):
            return _Retryable(f"auth failed: {detail}", fatal_key=True)
        if resp.status == 404:
            # The model name, not the key.
            return _Retryable(f"model not found: {detail}", fatal_request=True)
        if resp.status >= 500:
            return _Retryable(f"server error {resp.status}: {detail}", cooldown=15)
        if resp.status in (400, 422):
            # A parameter the model does not accept. Worth one retry without the
            # optional ones, on the same key — this is not its fault.
            if any(name in detail for name in _OPTIONAL_PARAMS):
                return _Retryable(f"unsupported parameter: {detail}",
                                  drop_optional=True)
            return _Retryable(f"bad request: {detail}", fatal_request=True)
        return _Retryable(f"http {resp.status}: {detail}", fatal_request=True)

    async def stream(self, message: str, conversation: str,
                     image: str | None = None, think: bool = False,
                     tools: bool | None = None):
        """Yield ("thinking" | "searching" | "answer", chunk).

        Runs the tool loop: the model may answer, or it may ask for a search
        first and answer once it has the results. Each round is a fresh request
        carrying everything from the last, which is how a stateless API holds a
        train of thought.
        """
        if not self.configured:
            raise LLMError("no keys", "APIキーが設定されていません。")
        use_tools = websearch.enabled() if tools is None else tools
        messages = self._messages(conversation, message, image)
        original = messages[-1]["content"]
        collected: list[str] = []

        for round_ in range(TOOL_ROUNDS + 1):
            # The last round goes without tools, so a model that keeps asking to
            # search is made to answer rather than looping until the cap.
            offer = use_tools and round_ < TOOL_ROUNDS
            calls: dict[int, dict] = {}
            async for kind, chunk in self._attempt(messages, think, offer,
                                                   calls, collected):
                yield kind, chunk
            if not calls:
                break
            requests = list(calls.values())
            messages.append({"role": "assistant", "content": None,
                             "tool_calls": requests})
            for request in requests:
                query = _tool_query(request)
                yield "searching", query
                results = await websearch.search(query)
                messages.append({
                    "role": "tool",
                    "tool_call_id": request.get("id") or "call",
                    "name": request["function"]["name"],
                    "content": websearch.as_text(query, results)})
        else:
            log.info("ツール呼び出しが上限に達しました")

        # Only the question and the final answer are remembered. Keeping the
        # search results would fill the history with pages of snippets that are
        # stale by the next turn and were only ever scaffolding for one answer.
        text = "".join(collected).strip()
        if text:
            self.memory.add(conversation, "user", original)
            self.memory.add(conversation, "assistant", text)

    async def _attempt(self, messages: list[dict], think: bool,
                       tools: bool, calls: dict, answer_out: list):
        """One request, across as many keys as it takes.

        The answer is appended to answer_out rather than kept on self: two
        overlapping calls would otherwise share it, and the second would inherit
        the first one's text.
        """
        tried: set[int] = set()
        last: _Retryable | None = None
        optional = True

        for _ in range(MAX_ATTEMPTS):
            key = self.pool.acquire(tried)
            if key is None:
                break
            tried.add(id(key))
            started = False
            answer: list[str] = []
            cap = self._max_tokens.get(key.provider.name)
            try:
                resp = await self._open(key, messages, think, optional, tools, cap)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last = _Retryable(f"connect failed: {e}", cooldown=10)
                self.pool.penalise(key, last.cooldown)
                continue

            async with resp:
                problem = await self._classify(resp)
                if problem is not None:
                    last = problem
                    if problem.drop_optional and optional:
                        # Not this key's doing. Put it back and try it again
                        # without the parameter the provider objected to.
                        log.info("%s: 未対応のパラメータを外して再試行します",
                                 key.label)
                        optional = False
                        tried.discard(id(key))
                        continue
                    if problem.shrink_to and (cap is None or problem.shrink_to < cap):
                        # Also not this key's doing — every key on this
                        # provider shares the same per-minute output budget, so
                        # the fix is remembered for the provider, not spent as
                        # a cooldown on the key that happened to ask first.
                        log.info("%s: 出力上限を%d語相当に制限して再試行します",
                                 key.provider.name, problem.shrink_to)
                        self._max_tokens[key.provider.name] = problem.shrink_to
                        tried.discard(id(key))
                        continue
                    if problem.fatal_request:
                        # No key can serve this. Penalising the pool for it is
                        # how one malformed request cost five keys a cooldown.
                        raise LLMError(str(problem), self._request_fault(problem))
                    if problem.fatal_key:
                        self.pool.disable(key, str(problem)[:120])
                    else:
                        self.pool.penalise(key, problem.cooldown)
                    continue
                try:
                    async for kind, chunk in self._read(resp, calls):
                        if kind == "answer":
                            answer.append(chunk)
                        started = True
                        yield kind, chunk
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    if started:
                        # Half an answer is already on screen. Repeating it from
                        # another key would be worse than stopping here.
                        log.warning("%s: 応答が途中で切れました: %s", key.label, e)
                        break
                    calls.clear()   # a half-built tool call is not a tool call
                    last = _Retryable(f"stream broke: {e}", cooldown=10)
                    self.pool.penalise(key, last.cooldown)
                    continue

            self.pool.succeed(key)
            answer_out.extend(answer)
            return

        raise self._exhausted(last)

    @staticmethod
    def _request_fault(problem: _Retryable) -> str:
        """Said in Japanese, and pointed at the setting that is wrong.

        These are configuration mistakes, not outages, so the message names the
        variable to change rather than suggesting the person wait and retry.
        """
        text = str(problem)
        if "model not found" in text:
            return ("モデル名が正しくありません。"
                    "GROQ_MODEL / OPENROUTER_MODEL を確認してください。")
        return ("リクエストが拒否されました(設定の誤りです)。\n"
                f"{text[:200]}")

    def _exhausted(self, last: _Retryable | None) -> LLMError:
        usable = len(self.pool.usable())
        log.warning("全てのキーで失敗しました (使用可 %d/%d): %s",
                    usable, len(self.pool), last)
        if last and last.fatal_key:
            return LLMError(str(last),
                            "APIキーまたはモデル名が正しくありません。"
                            "設定を確認してください。")
        if not usable:
            return LLMError("all keys cooling",
                            "全てのAPIキーが上限に達しています。"
                            "少し待ってからもう一度どうぞ。")
        return LLMError(str(last or "unknown"),
                        "AIサーバーに接続できませんでした。"
                        "少し待ってからもう一度どうぞ。")

    @staticmethod
    async def _read(resp, calls: dict | None = None):
        """Server-sent events into ("thinking" | "answer", text).

        Reasoning arrives under three different names depending on provider and
        model, and inline in <think> tags on a fourth. All of them are shown as
        thinking rather than pasted into the answer.

        Tool calls are collected into `calls` rather than yielded, because they
        arrive in fragments: the name comes in one delta and the arguments over
        several more, keyed by an index that is the only thing tying them
        together. Nothing can be done with half a call, so the caller reads them
        once the stream has finished.
        """
        in_think = False
        async for raw in resp.content:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                delta = (json.loads(data).get("choices")
                         or [{}])[0].get("delta") or {}
            except (ValueError, IndexError, AttributeError):
                continue
            if calls is not None:
                for fragment in delta.get("tool_calls") or ():
                    slot = calls.setdefault(
                        fragment.get("index", 0),
                        {"id": "", "type": "function",
                         "function": {"name": "", "arguments": ""}})
                    if fragment.get("id"):
                        slot["id"] = fragment["id"]
                    piece = fragment.get("function") or {}
                    if piece.get("name"):
                        slot["function"]["name"] = piece["name"]
                    if piece.get("arguments"):
                        slot["function"]["arguments"] += piece["arguments"]
            for field in ("reasoning_content", "reasoning"):
                if delta.get(field):
                    yield "thinking", delta[field]
            piece = delta.get("content")
            if not piece:
                continue
            # Some models put reasoning inline instead of in its own field.
            while piece:
                if in_think:
                    head, tag, rest = piece.partition("</think>")
                    if head:
                        yield "thinking", head
                    if not tag:
                        break
                    in_think, piece = False, rest
                else:
                    head, tag, rest = piece.partition("<think>")
                    if head:
                        yield "answer", head
                    if not tag:
                        break
                    in_think, piece = True, rest

    # -- the rest of the RulaClient surface -------------------------------- #

    async def ask(self, prompt: str, *, think: bool = False) -> str:
        """A one-shot answer, with no history read or written."""
        conversation = f"task-{time.monotonic_ns()}"
        parts = []
        async for kind, chunk in self.stream(prompt, conversation, think=think):
            if kind == "answer":
                parts.append(chunk)
        self.memory.clear(conversation)
        return "".join(parts).strip()

    async def health(self) -> tuple[dict | None, str]:
        if not self.configured:
            return None, ("APIキーが設定されていません。"
                          "GROQ_API_KEYS か OPENROUTER_API_KEYS を設定してください。")
        usable = self.pool.usable()
        if not usable:
            return None, "全てのAPIキーが休止中か無効です。\n" + self.pool.report()
        return {"status": "ok", "backend": "/".join(self.pool.providers),
                "weights": usable[0].provider.model,
                "keys": f"{len(usable)}/{len(self.pool)}"}, ""

    async def preflight(self) -> dict | None:
        health, reason = await self.health()
        if health is None:
            log.warning("%s", reason)
            return None
        log.info("AI: %s", self.pool.report().replace("\n", " / "))
        return health


# --------------------------------------------------------------------------- #
# Cat language (nya) encode / decode tools for AI Function Calling
# --------------------------------------------------------------------------- #

NYA_ENCODE_URL = "https://nya-api.keitodaze.net/v1/encode"
NYA_DECODE_URL = "https://nya-api.keitodaze.net/v1/decode"
NYA_TIMEOUT = aiohttp.ClientTimeout(total=15.0)


async def _nya_post_json(url: str, payload: dict) -> dict:
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    async with aiohttp.ClientSession(connector=connector, timeout=NYA_TIMEOUT) as session:
        async with session.post(url, json=payload, headers={"Content-Type": "application/json"}) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {body[:120]}")
            return await resp.json(content_type=None)


async def nya_encode(text: str) -> str:
    """Encode normal text into cat language (nya) via API."""
    text = (text or "").strip()
    if not text:
        return "変換するテキストが空です。"
    try:
        data = await _nya_post_json(NYA_ENCODE_URL, {"text": text})
        return data.get("text", "")
    except Exception as e:
        log.exception("nya encode error: %s", e)
        return f"猫語への変換に失敗しました: {e}"


async def nya_decode(nya_text: str) -> str:
    """Decode cat language (nya) back into human text via API."""
    nya_text = (nya_text or "").strip()
    if not nya_text:
        return "解読する猫語テキストが空です。"
    try:
        data = await _nya_post_json(NYA_DECODE_URL, {"nya": nya_text})
        return data.get("text", "")
    except Exception as e:
        log.exception("nya decode error: %s", e)
        return f"猫語の解読に失敗しました: {e}"


def _nya_parse_arg(raw: str, key: str) -> str:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and key in parsed:
            return str(parsed[key] or "").strip()
    except ValueError:
        match = re.search(rf'"{key}"\s*:\s*"([^"]+)', raw)
        if match:
            return match.group(1).strip()
    return raw.strip()


async def nya_call_tool(name: str, raw_arguments: str) -> str:
    if name == "nya_encode":
        text = _nya_parse_arg(raw_arguments, "text")
        result = await nya_encode(text)
        return f"猫語変換結果:\n{result}"
    elif name == "nya_decode":
        nya_text = _nya_parse_arg(raw_arguments, "nya")
        result = await nya_decode(nya_text)
        return f"猫語解読結果:\n{result}"
    return f"不明なツール名です: {name}"


NYA_TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "nya_encode",
            "description": (
                "テキスト（日本語など）を猫語（nya暗号）に変換・翻訳します。"
                "ユーザーから「猫語にして」「猫語に翻訳して」「猫語で言って」「猫語で要約して」"
                "などと求められた時に必ずこのツールを使用して猫語を生成してください。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "猫語に変換したい文章（要約した文章や返答したいメッセージなど）",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nya_decode",
            "description": (
                "猫語（nya暗号: ニャ、ニョ、ニュ、ニェ等で構成された暗号文字列）を解読し、"
                "元の日本語などのテキストに復号・翻訳します。"
                "ユーザーが猫語で話しかけてきた時や、猫語の内容を理解・要約・翻訳する時に"
                "必ずこのツールを使用して解読してください。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nya": {
                        "type": "string",
                        "description": "解読したい猫語テキスト",
                    },
                },
                "required": ["nya"],
            },
        },
    },
]

# 後方互換性エイリアス
encode = nya_encode
decode = nya_decode
call_tool = nya_call_tool
TOOL_SPECS = NYA_TOOL_SPECS
sys.modules["nya"] = sys.modules[__name__]

