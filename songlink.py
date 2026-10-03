"""R!m <曲名> — search links for the song on every major streaming service.

Used to go through Odesli (song.link): give it one link, it looks up the same
recording on every other platform it tracks. That meant depending on Odesli
staying free, staying up, and staying unauthenticated — and it stopped being
free without warning (PUBLIC_API_ACCESS_DEPRECATED, one afternoon, no notice).
An aggregator is someone else's business decision away from breaking this
command, and it broke.

What replaced it calls nothing outside this process. Each platform gets a
search-page link built from the query text with a URL template — no lookup, no
key, no quota, nothing that can be deprecated because nothing is being asked
of anyone. The cost is precision: a search page is not a guaranteed exact
track, the way an Odesli link was. Where one exact link already exists — the
song was found on YouTube via music.py, or a direct platform link was found by
a plain web search, the same way R!play resolves a title — that link is used
in place of the search page for that one platform. Everything else is a
search link, and is labelled as one.
"""
import logging
import os
import re
from urllib.parse import parse_qs, quote, urlsplit

import discord
from discord.ext import commands

import ui
import websearch

try:
    import music
except ImportError:
    music = None

log = logging.getLogger("rula.songlink")

# ---------------------------------------------------------------------------
# Set to False to remove the command entirely. SONGLINK=off does the same.
# ---------------------------------------------------------------------------
ENABLED = True

_URL_RE = re.compile(r"^https?://", re.I)

# Search-page URLs, one per platform. %20 throughout (quote, not quote_plus):
# a couple of these place the query in the path rather than the query string,
# where a literal "+" is not reliably read back as a space.
#
# Kept to platforms whose search URL has been stable for years and is widely
# relied on elsewhere (browser redirect extensions, other bots). A guessed
# URL that turns out wrong is worse than one platform fewer.
SEARCH_TEMPLATES: dict[str, str] = {
    "spotify": "https://open.spotify.com/search/{q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "youtubeMusic": "https://music.youtube.com/search?q={q}",
    "appleMusic": "https://music.apple.com/us/search?term={q}",
    "deezer": "https://www.deezer.com/search/{q}",
    "amazonMusic": "https://music.amazon.com/search/{q}",
    "soundcloud": "https://soundcloud.com/search?q={q}",
}

# Domains recognised as "this link already names one exact recording" — either
# pasted directly, or found by resolve_source. Checked by suffix, so a country
# subdomain (music.amazon.co.jp) still matches its entry.
_DOMAIN_TO_PLATFORM: dict[str, str] = {
    "open.spotify.com": "spotify",
    "spotify.com": "spotify",
    "music.apple.com": "appleMusic",
    "music.youtube.com": "youtubeMusic",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "music.amazon.com": "amazonMusic",
    "music.amazon.co.jp": "amazonMusic",
    "deezer.com": "deezer",
    "tidal.com": "tidal",
    "soundcloud.com": "soundcloud",
}

PLATFORM_LABELS: dict[str, str] = {
    "spotify": "Spotify",
    "youtube": "YouTube",
    "youtubeMusic": "YouTube Music",
    "appleMusic": "Apple Music",
    "deezer": "Deezer",
    "amazonMusic": "Amazon Music",
    "soundcloud": "SoundCloud",
    "tidal": "TIDAL",
}
# Every key ever shown, in the order shown. TIDAL has no search template (its
# current search URL was not confident enough to include) so it only ever
# appears when resolve_source already found an exact link there.
_DISPLAY_ORDER = ["spotify", "appleMusic", "youtubeMusic", "youtube",
                  "amazonMusic", "deezer", "soundcloud", "tidal"]


def enabled() -> bool:
    raw = os.environ.get("SONGLINK", "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return ENABLED


def _platform_for_url(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    for domain, platform in _DOMAIN_TO_PLATFORM.items():
        if host == domain or host.endswith("." + domain):
            return platform
    return None


# -- finding one exact link, best-effort ------------------------------------ #

async def _via_music(query: str) -> str | None:
    """A YouTube link via the same search R!play uses, if this build has it."""
    if music is None or getattr(music, "yt_dlp", None) is None:
        return None
    try:
        tracks = await music.search(query, requester=0, limit=1)
    except music.MusicError:
        return None
    except Exception:
        log.exception("music.search に失敗しました")
        return None
    return tracks[0].url if tracks else None


async def _via_web_search(query: str) -> str | None:
    """The first result that is plainly a streaming link, from a plain search."""
    try:
        results = await websearch.search(query, count=8)
    except Exception:
        log.exception("楽曲リンクの検索に失敗しました")
        return None
    for domain in _DOMAIN_TO_PLATFORM:
        for result in results:
            host = (urlsplit(result.url).hostname or "").lower()
            if host == domain or host.endswith("." + domain):
                return result.url
    return None


async def resolve_source(query: str) -> tuple[str | None, str]:
    """(url, how) — one confirmed link for the song, if one can be found.

    Never required for the command to answer — build_links() works from the
    query text alone — so a failure here costs one upgraded link, not the
    whole reply. See the module docstring.
    """
    query = query.strip()
    if _URL_RE.match(query) and _platform_for_url(query):
        return query, "指定されたリンク"
    url = await _via_music(query)
    if url:
        return url, "検索結果"
    if not websearch.enabled():
        return None, ""
    url = await _via_web_search(query)
    if url:
        return url, "検索結果"
    return None, ""


# -- building the links, with nothing to call to do it ---------------------- #

def _youtube_id(url: str) -> str | None:
    """The video id out of a youtube.com or youtu.be link, if there is one."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in ("youtu.be", "www.youtu.be"):
        return parts.path.strip("/").split("/")[0] or None
    if host.endswith("youtube.com"):
        values = parse_qs(parts.query).get("v")
        return values[0] if values else None
    return None


def build_links(query: str, source_url: str | None) -> tuple[dict[str, str], set[str]]:
    """(links, exact) — every platform's link, and which of them are confirmed.

    A search link for everything SEARCH_TEMPLATES knows, unconditionally: this
    is the part with nothing to fail. Anything in `exact` was pasted, found by
    resolve_source, or — for YouTube Music specifically — inferred from a
    confirmed YouTube id, since an official upload's video id plays back
    identically on both services in the large majority of cases.
    """
    encoded = quote(query, safe="")
    links = {platform: template.format(q=encoded)
            for platform, template in SEARCH_TEMPLATES.items()}
    exact: set[str] = set()

    if source_url:
        platform = _platform_for_url(source_url)
        if platform:
            links[platform] = source_url
            exact.add(platform)
            if platform == "youtube":
                video_id = _youtube_id(source_url)
                if video_id:
                    links["youtubeMusic"] = (
                        f"https://music.youtube.com/watch?v={video_id}")
                    exact.add("youtubeMusic")
    return links, exact


def summary(links: dict[str, str], exact: set[str]) -> str:
    lines = []
    for key in _DISPLAY_ORDER:
        if key not in links:
            continue
        label = PLATFORM_LABELS.get(key, key)
        if key in exact:
            lines.append(f"・[{label}]({links[key]})")
        else:
            lines.append(f"・[{label} (検索)]({links[key]})")
    if not lines:
        return "リンクを作成できませんでした。"
    body = "\n".join(lines)
    if len(exact) < len(lines):
        body += "\n\n「(検索)」は検索結果ページです。曲を選んでください。"
    return body


# -- the command --------------------------------------------------------------- #

class SongLink(commands.Cog, name="曲リンク"):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.CheckFailure("サーバー内でのみ使えます。")
        if not enabled():
            raise commands.CheckFailure("この機能は無効になっています。")
        return True

    def _query(self, ctx: commands.Context, given: str) -> str:
        text = given.strip()
        if not text and ctx.message.reference:
            replied = ctx.message.reference.resolved
            if isinstance(replied, discord.Message):
                text = (replied.content or "").strip()
        return text[:300]

    @commands.cooldown(4, 30.0, commands.BucketType.user)
    @commands.command(name="m", aliases=["songlink", "曲リンク", "funk"])
    async def m(self, ctx: commands.Context, *, query: str = ""):
        """曲を各配信サービスのリンクで表示します。R!m <曲名> [アーティスト名]"""
        query = self._query(ctx, query)
        if not query:
            raise commands.UserInputError(
                "曲名を入力するか、リンクを含むメッセージに返信してください。\n"
                "例: `R!m 夜に駆ける YOASOBI`")

        async with ui.typing(ctx):
            source, how = await resolve_source(query)

        links, exact = build_links(query, source)
        embed = ui.embed(summary(links, exact), ui.clip(query, 256),
                         ui.COLOR_ANSWER)
        embed.set_footer(text=f"確認済みリンクあり ({how})" if source else "検索リンク")
        try:
            await ctx.reply(**ui.payload(ctx.channel, embed), mention_author=False)
        except ui.SEND_ERRORS as e:
            log.warning("返信できませんでした: %s", ui.short_error(e))


HELP_LINES = [
    ("R!m <曲名>", "各配信サービスのリンクをまとめて表示します"),
]


async def setup(bot):
    await bot.add_cog(SongLink(bot))
