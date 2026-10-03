"""Voice playback for the Rula bot.

Kept apart from discord_bot.py because it shares almost nothing with it. The chat
side is a thin client over a local HTTP API; this side owns a background task per
server, an ffmpeg subprocess per track, and a queue that outlives any single
command. The only thing the two have in common is how a message is formatted,
which is why both import ui.

yt-dlp and ffmpeg are looked up rather than assumed. A server missing one of them
should lose music, not the whole bot, so the import is guarded and the commands
say what is missing instead of raising into the command handler.

Commands are prefixed R! and listed in HELP_LINES at the bottom.
"""
import asyncio
import ctypes.util
import dataclasses
import functools
import html
import json
import logging
import os
import random
import re
import shutil
import socket
import struct
import sys
import tempfile
import time
import urllib.parse
from collections import deque

import aiohttp
import discord
from discord.ext import commands

import ui

log = logging.getLogger("rula.music")

try:
    from gtts import gTTS
except ImportError:
    gTTS = None

try:
    import yt_dlp
except ImportError:  # reported by the commands, not at startup
    yt_dlp = None

try:
    # discord.py encrypts every voice packet with this. Without it VoiceClient
    # refuses to be constructed at all, raising a bare RuntimeError from its
    # __init__ — which reads as an internal fault rather than a missing package.
    import nacl.secret  # noqa: F401
    HAS_NACL = True
except ImportError:
    HAS_NACL = False

# How long a resolved media URL is trusted. YouTube signs these with an expiry —
# usually hours — but a track sitting behind a long queue can outlive one, and a
# stale URL fails as a 403 in ffmpeg rather than anywhere we could catch it. Well
# under any real expiry, and re-resolving costs about half a second.
STREAM_TTL = 1800
# Left alone this long with nothing queued, the bot hangs up. Sitting in an empty
# channel forever is how a music bot ends up holding a voice connection for days.
IDLE_TIMEOUT = 300
# And this long once the last human leaves, so that stepping out to another
# channel for a moment does not end the queue.
ALONE_TIMEOUT = 60
# A ceiling on a queue, so that one mis-pasted 10,000-track playlist cannot sit
# in memory for the life of the process.
MAX_QUEUE = 500
MAX_VOLUME = 2.0


def _env_float(name: str, fallback: float, low: float, high: float) -> float:
    """A number from the environment, or the default, but never a crash.

    These are read at import. An unparseable value raising here would take the
    whole module with it, and a music bot that fails to import because someone
    typed MUSIC_VOLUME=1,0 is a worse outcome than one that ignores them.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback
    try:
        return min(high, max(low, float(raw)))
    except ValueError:
        log.warning("%s が数値ではありません (%r)。%s を使います。", name, raw, fallback)
        return fallback


# Full scale by default. PCMVolumeTransformer multiplies every sample, and at 1.0
# that multiplication is the identity — the audio reaches the encoder exactly as
# it was decoded. Anything less throws away signal before the one lossy step in
# the chain, so quieter is a choice to make per server, not a default.
DEFAULT_VOLUME = _env_float("MUSIC_VOLUME", 1.0, 0.0, MAX_VOLUME)

# A track that ends this quickly never really started: the usual cause is a media
# URL that expired between being resolved and being opened, which ffmpeg reports
# by writing nothing at all rather than by failing.
MIN_PLAYBACK = 3.0
# How often the loop looks up from waiting to check the connection is still there.
WATCHDOG_INTERVAL = 15
# How long to wait for a voice connection. Shorter than it was: the handshake
# either completes in about a second or does not complete at all, so the rest of
# the wait only delays telling someone that it failed.
VOICE_TIMEOUT = float(os.environ.get("MUSIC_VOICE_TIMEOUT", "12"))
# Join self-deafened, meaning Discord does not send the bot the voice traffic of
# everyone in the channel. It is the ordinary thing for a music bot: nothing here
# listens, so receiving it would be bandwidth spent on nothing — and on a host
# where voice UDP is the scarce resource, that is worth having.
#
# It does not affect what the bot sends. Deafening only silences the inbound
# direction; the audio still goes out. Discord's own client draws a crossed-out
# microphone next to a deafened member as well as a crossed-out headphone, which
# is why it reads as muted when it is not. MUSIC_SELF_DEAF=0 turns it off for
# anyone who would rather not see that.
SELF_DEAF = os.environ.get("MUSIC_SELF_DEAF", "1").strip().lower() not in (
    "0", "false", "no")

FFMPEG_BEFORE = "-nostdin -hide_banner -loglevel warning -probesize 64k -analyzeduration 500000"
# Streams are long and connections to a CDN do drop; without these a momentary
# blip ends the track silently, mid-song. Added only for http inputs, because
# they are options of the http protocol rather than global ones: handed to
# ffmpeg for anything else it exits with "Option reconnect not found" before it
# has opened the file at all.
#
# -reconnect                   : reconnect on EOF/error
# -reconnect_streamed          : allow reconnect mid-stream (live/VOD both)
# -reconnect_at_eof            : reconnect when the server sends EOF early
# -reconnect_on_network_error  : reconnect on TCP/TLS resets (Connection reset by peer)
# -reconnect_on_http_error 4xx,5xx : reconnect when YouTube returns 4xx/5xx
# -reconnect_delay_max 5       : up to 5 s between retries (was 3)
# -multiple_requests 1         : reuse the HTTP connection (Keep-Alive)
# -timeout 15000000            : 15-second socket read timeout (µs) to detect hangs
FFMPEG_RECONNECT = (
    "-reconnect 1"
    " -reconnect_streamed 1"
    " -reconnect_at_eof 1"
    " -reconnect_on_network_error 1"
    " -reconnect_on_http_error 4xx,5xx"
    " -reconnect_delay_max 5"
    " -multiple_requests 1"
    " -timeout 15000000"
)
FFMPEG_OPTIONS = "-vn"

_URL = re.compile(r"https?://\S+")

# Services that publish what a song is but will not let anyone stream it. A link
# to one is a request for that song, not for that particular file, so the title
# is read off the page and the song is found somewhere it can be played from.
#
# Nothing here touches their audio. Spotify's stream is DRM-protected and stays
# that way; only the title and artist are read, from the same public metadata
# the link preview in Discord is built from.
_METADATA_ONLY = re.compile(
    r"^https?://(?:[\w-]+\.)*("
    r"spotify\.com|spotify\.link|music\.apple\.com|music\.amazon\.[\w.]+"
    r"|deezer\.com|tidal\.com|music\.line\.me|awa\.fm)/", re.I)

# oEmbed, where the service publishes one: a documented JSON endpoint beats
# scraping a page of HTML that can be redesigned at any time.
_OEMBED = (
    (re.compile(r"spotify\.(com|link)", re.I), "https://open.spotify.com/oembed"),
    (re.compile(r"music\.apple\.com", re.I), "https://music.apple.com/api/oembed"),
    (re.compile(r"deezer\.com", re.I), "https://api.deezer.com/oembed"),
)

_META_TAG = re.compile(r"<meta\s+([^>]+?)/?>", re.I)
_ATTR_PROP = re.compile(r"(?:property|name)\s*=\s*[\"']([^\"']+)", re.I)
_ATTR_CONTENT = re.compile(r"content\s*=\s*[\"']([^\"']*)", re.I)
# Discordbot UA ensures music platforms like Spotify return rich Open Graph metadata.
_BROWSER_UA = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"


def _meta_tags(html_text: str) -> dict[str, str]:
    """og: and friends, tolerant of attribute order.

    Written as two small searches per tag rather than one big pattern because
    content= comes before property= as often as after, and a pattern that
    assumes an order silently finds nothing on half the sites.
    """
    tags: dict[str, str] = {}
    for attrs in _META_TAG.findall(html_text):
        prop, content = _ATTR_PROP.search(attrs), _ATTR_CONTENT.search(attrs)
        if prop and content:
            tags.setdefault(prop.group(1).lower(), html.unescape(content.group(1)))
    return tags


def _artist_from(description: str, title: str) -> str:
    """The performer, out of a description written for humans.

    Spotify writes "The Stranglers · Song · 1981", sometimes behind a "Listen to
    X on Spotify." lead. Anything that is plainly a year, a format word, or the
    title repeated back is not the artist.
    """
    text = re.sub(r"^(?:Listen to|Watch|Play)\s+.*?\s+on\s+\w+[.\s]*", "",
                  description or "", flags=re.I).strip()
    for part in (p.strip() for p in re.split(r"[·•|]", text)):
        if (part and not re.fullmatch(r"\d{4}", part)
                and part.lower() not in ("song", "single", "album", "ep", "track")
                and part.lower() != (title or "").lower()):
            return part
    return ""


async def describe_link(url: str) -> str:
    """A search phrase for a link we cannot stream from, or "" if unreadable.

    Both sources are tried and the results merged, because neither is reliable
    on its own: oEmbed gives a clean title but usually no artist, while the page
    carries the artist in a description meant for humans. Either alone is enough
    to find the song; together they find the right recording of it.
    """
    title = artist = ""
    timeout = aiohttp.ClientTimeout(total=15)
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    try:
        async with aiohttp.ClientSession(
                connector=connector,
                headers={"User-Agent": _BROWSER_UA}, timeout=timeout) as session:
            for pattern, endpoint in _OEMBED:
                if not pattern.search(url):
                    continue
                try:
                    async with session.get(endpoint, params={"url": url}) as resp:
                        if resp.status == 200:
                            data = json.loads(await resp.text())
                            title = (data.get("title") or "").strip()
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                    log.debug("oEmbed に失敗しました: %s", e)
                break
            try:
                async with session.get(url) as resp:
                    tags = _meta_tags(await resp.text(errors="ignore"))
            except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError) as e:
                log.debug("ページを取得できませんでした: %s", e)
                tags = {}
    except Exception:
        log.exception("リンクの情報を取得できませんでした")
        return ""

    title = tags.get("og:title") or title
    artist = (tags.get("music:musician_description")
              or _artist_from(tags.get("og:description", ""), title))
    phrase = f"{artist} {title}".strip() if artist else title.strip()
    if phrase:
        log.info("リンクから曲を特定しました: %s", phrase)
    return ui.clip(phrase, 120)


COLOR_MUSIC = 0x5865F2    # the left edge of anything to do with playback


def duration(seconds: float | None) -> str:
    """h:mm:ss, or mm:ss when it fits. Nothing at all means a live stream."""
    if not seconds:
        return "LIVE"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def progress_bar(elapsed: float, total: float | None, width: int = 24) -> str:
    """A text progress bar. ASCII, because the obvious alternative is a pictograph."""
    if not total:
        return "[" + "-" * width + f"] {duration(elapsed)} / LIVE"
    filled = max(0, min(width - 1, int(width * elapsed / total)))
    bar = "=" * filled + ">" + "-" * (width - filled - 1)
    return f"[{bar}] {duration(elapsed)} / {duration(total)}"


class MusicError(RuntimeError):
    """Something the person who typed the command should be told about."""


def find_ffmpeg() -> str | None:
    """Locate an ffmpeg binary, preferring an explicit one.

    The fallback is imageio-ffmpeg, which this project already depends on for
    video work and which ships a full build. It means playback works on a fresh
    Windows machine with nothing on PATH, which is the usual state of one.
    """
    explicit = os.environ.get("FFMPEG_PATH", "").strip().strip('"')
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        log.warning("FFMPEG_PATH のファイルが見つかりません: %s", explicit)
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG_EXE = find_ffmpeg()


def _load_opus() -> bool:
    """Make sure libopus is loaded, which is what actually encodes the audio.

    discord.py ships the DLL for Windows and finds it unaided; everywhere else it
    is a system package. On a container without it the failure surfaces as
    OpusNotLoaded raised from inside VoiceClient.play — several layers below
    anything that could explain it, and after the bot has already joined the
    channel. Resolved once here so it can be reported as what it is.
    """
    if discord.opus.is_loaded():
        return True
    # The bundled Windows DLL, via the same private helper discord.py uses.
    loader = getattr(discord.opus, "_load_default", None)
    if loader is not None:
        try:
            loader()
        except Exception:
            pass
        if discord.opus.is_loaded():
            return True
    for name in ("opus", "libopus.so.0", "libopus.so", "libopus.0.dylib"):
        try:
            discord.opus.load_opus(ctypes.util.find_library(name) or name)
        except Exception:
            continue
        if discord.opus.is_loaded():
            return True
    return False


# Resolved once at import: ctypes lookups are not free, and the answer cannot
# change while the process runs.
OPUS_READY = _load_opus()


def unavailable() -> str | None:
    """Why music cannot run, in Japanese, or None if it can."""
    missing = []
    if yt_dlp is None:
        missing.append("yt-dlp")
    if not HAS_NACL:
        missing.append("PyNaCl")
    if FFMPEG_EXE is None:
        missing.append("ffmpeg")
    if not missing and not OPUS_READY:
        return ("libopus を読み込めませんでした。音声を送信できません。\n"
                "  Linux では apt install libopus0、"
                "Debian系のコンテナなら apt-get install -y libopus0 で入ります。")
    if not missing:
        return None
    return ("音楽機能に必要なものが足りません: " + ", ".join(missing) + "\n"
            "  pip install yt-dlp PyNaCl imageio-ffmpeg\n"
            "  ffmpeg を別に入れている場合は .env に FFMPEG_PATH=... を書けます。")


# Discord hands back voice endpoints like "c-nrt16-x.discord.media:2096", and
# discord.py connects the voice websocket to exactly that — port and all. 2096 is
# one of Cloudflare's alternative HTTPS ports, and a host that only allows
# outbound 443 blocks it: the endpoint is found, the websocket never opens, and
# the attempt dies on a timeout with nothing to show for it.
#
# Setting MUSIC_VOICE_PORT=443 rewrites the port before discord.py uses it. It is
# off by default because the port Discord chose is the one Discord expects to
# serve; this exists for hosts that will not let it be reached.
VOICE_PORT = os.environ.get("MUSIC_VOICE_PORT", "").strip()


@functools.lru_cache(maxsize=1)
def _port_voice_client():
    """A VoiceClient that rewrites the endpoint port, or None if unavailable.

    Built on demand behind a guard rather than at import, because it subclasses
    VoiceConnectionState, which is not public API. Defined at module level, a
    discord.py release that moves or renames that class would stop music.py from
    importing at all — disabling every music command in order to support a
    workaround that almost no host needs. Failing to build this costs the
    override; failing to import costs everything.
    """
    try:
        base = discord.voice_state.VoiceConnectionState
    except AttributeError:
        log.warning("MUSIC_VOICE_PORT はこの discord.py では使えません"
                    "(内部APIが変更されています)。無視して通常どおり接続します。")
        return None

    class _PortRewriteState(base):
        # Applied to the payload rather than to self.endpoint afterwards, because
        # discord.py begins connecting inside voice_server_update: by the time a
        # subclass could edit the attribute, it has already been read.
        async def voice_server_update(self, data) -> None:
            endpoint = data.get("endpoint")
            if endpoint:
                host = endpoint.removeprefix("wss://").rsplit(":", 1)[0]
                data = dict(data, endpoint=f"{host}:{VOICE_PORT}")
                log.info("音声エンドポイントのポートを %s に変更しました: %s",
                         VOICE_PORT, data["endpoint"])
            await super().voice_server_update(data)

    class _PortVoiceClient(discord.VoiceClient):
        def create_connection_state(self):
            return _PortRewriteState(self)

    return _PortVoiceClient


# Discord's voice close codes. discord.py handles 4014, 4015, 4021 and 4022
# itself and lets the rest through as a bare ConnectionClosed, which surfaces as
# a number and a traceback with nothing to act on.
VOICE_CLOSE_HINTS = {
    4004: "音声の認証に失敗しました。DISCORD_TOKEN を確認してください。",
    4006: "セッションが無効と判断されました (4006)。",
    4009: "セッションがタイムアウトしました。もう一度試してください。",
    4011: "音声サーバーが見つかりませんでした。"
          "サーバー設定でボイスチャンネルのリージョンを変えると直ることがあります。",
    4016: "対応していない暗号化方式です。discord.py を更新してください。",
}


def voice_close_help(code: int | None) -> str:
    said = VOICE_CLOSE_HINTS.get(code or 0,
                                 f"音声サーバーに接続できませんでした (コード {code})。")
    if code == 4006 and VOICE_PORT:
        # Worth spelling out, because the workaround and the symptom look
        # unrelated: the port is part of how Discord routes to the instance
        # holding the session, so reaching a different port can mean reaching an
        # instance that has never heard of us. Changing the port trades a
        # timeout for this.
        said += ("\nMUSIC_VOICE_PORT を設定しているのが原因の可能性が高いです。"
                 f"\n一度 MUSIC_VOICE_PORT を外して試してください。"
                 "\n元のポートが塞がれている場合は、ホスト側で開けてもらう必要があります。")
    return said


def voice_client_class():
    """The VoiceClient to connect with, plain unless a port override is set."""
    if not VOICE_PORT.isdigit():
        return discord.VoiceClient
    return _port_voice_client() or discord.VoiceClient


def encoder_options(channel) -> dict:
    """Opus settings for this channel, at the best it will carry.

    Discord will not pass more than the channel's own bitrate — 96k on an
    unboosted server, up to 384k on a boosted one — so matching it is genuinely
    the maximum, and discord.py's flat default of 128 is either wasteful or a
    needless ceiling depending on which side of it the channel sits.
    """
    bitrate = getattr(channel, "bitrate", None) or 128_000
    return {
        "bitrate": max(16, min(512, bitrate // 1000)),
        # The encoder otherwise optimises for intelligible speech, which means
        # spending its bits below 8kHz. This is the single largest audible
        # difference between a music bot that sounds good and one that does not.
        "signal_type": "music",
        "bandwidth": "full",        # the whole 20kHz, not a speech-width band
        "application": "audio",     # quality over latency
        "fec": True,
        # discord.py assumes 15% packet loss and hands that much of the bitrate
        # to redundancy. A voice connection that bad is rare, and on a good one
        # those bits are simply thrown away. Clamped because the encoder rejects
        # anything outside this range, and it does so from inside play() —
        # a typo in the environment would otherwise kill playback, not the value.
        "expected_packet_loss": _PACKET_LOSS,
    }


# The encoder rejects anything outside (0, 1] and does so from inside play(),
# where it would read as playback breaking rather than as a bad setting.
_PACKET_LOSS = _env_float("MUSIC_PACKET_LOSS", 0.02, 0.001, 1.0)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

# IPv6 ranges belonging to hosting providers are the ones YouTube throttles
# hardest, so forcing v4 is the usual fix for "works on my PC, 403 on the VPS".
# It is wrong on an IPv6-only host, hence the escape hatch.
_FORCE_IPV4 = os.environ.get("MUSIC_FORCE_IPV4", "1").strip().lower() not in (
    "0", "false", "no")

class _YdlLog:
    """Route yt-dlp's own output into the log instead of stderr.

    quiet and no_warnings silence most of it, but errors are still written
    straight to stderr — which is how the untimestamped, untranslated [DRM]
    notice appeared in the log while the bot was separately explaining the same
    thing in Japanese. Everything is logged at debug: an error here is also
    raised as an exception, and _explain turns that into the message anyone
    actually reads.
    """

    def debug(self, message: str):
        log.debug("yt-dlp: %s", message)

    info = warning = error = debug


_YDL_COMMON = {
    "quiet": True,
    "no_warnings": True,
    "logger": _YdlLog(),
    "skip_download": True,
    "cachedir": False,
    "socket_timeout": 8,
    "retries": 3,
    "geo_bypass": True,
    "format": "bestaudio[ext=m4a]/bestaudio/best",
    "format_sort": ["abr", "asr", "acodec"],
    "default_search": "ytsearch",
    "lazy_playlist": True,
    "no_color": True,
    "check_formats": False,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web", "ios"],
            "player_skip": ["configs", "webpage"],
            "skip": ["hls", "dash", "translated_subs"],
        }
    },
}
if _FORCE_IPV4:
    _YDL_COMMON["source_address"] = "0.0.0.0"

# An exported cookies.txt, for the one thing no amount of retrying gets past:
# age-restricted videos, which YouTube will not serve to a signed-out client at
# all. Optional, and off unless the file is actually there — a path that points
# at nothing would otherwise fail every extraction rather than none.
_COOKIES = os.environ.get("YTDLP_COOKIES", "").strip().strip('"')
if _COOKIES:
    if os.path.isfile(_COOKIES):
        _YDL_COMMON["cookiefile"] = _COOKIES
        log.info("yt-dlp のCookieを使用します: %s", _COOKIES)
    else:
        log.warning("YTDLP_COOKIES のファイルが見つかりません: %s", _COOKIES)

# Queueing only needs a title and a link. extract_flat stops yt-dlp from opening
# every entry of a 200-track playlist, which is the difference between a queue
# appearing at once and one appearing over several minutes.
_YDL_LIST = _YDL_COMMON | {"extract_flat": "in_playlist", "noplaylist": False}
# Playback needs the media URL itself, so this one goes all the way in — and only
# for the single track that is about to start.
_YDL_ONE = _YDL_COMMON | {"extract_flat": False, "noplaylist": True}


@dataclasses.dataclass
class Track:
    title: str
    url: str                      # the page, which stays valid and can be re-resolved
    duration: float | None        # None for a live stream
    uploader: str
    requester: int
    stream: str | None = None     # the media itself, short-lived
    resolved_at: float = 0.0
    # Other results for the same search, kept to fall back on. Only a keyword
    # search has these: a URL is a request for that video and nothing else.
    thumbnail: str | None = None
    alternatives: list["Track"] = dataclasses.field(default_factory=list)

    @property
    def fresh(self) -> bool:
        return bool(self.stream) and time.time() - self.resolved_at < STREAM_TTL

    def label(self, limit: int = 70) -> str:
        """The title, safe to drop into a message.

        Escaped because a song title is someone else's text: an underscore or a
        stray backtick in one would otherwise reformat the rest of the queue.
        """
        return discord.utils.escape_markdown(ui.clip(self.title, limit))


def _media_url(info: dict) -> str | None:
    if info.get("url"):
        return info["url"]
    for fmt in info.get("requested_formats") or ():
        if fmt.get("url"):
            return fmt["url"]
    return None


def _to_track(info: dict, requester: int) -> Track | None:
    # A flat playlist entry carries the watch page in "url"; a full extraction
    # carries the media there and the page in "webpage_url". Telling them apart
    # matters, because queueing a media URL means queueing something that expires.
    flat = info.get("_type") == "url"
    page = info.get("webpage_url") or info.get("url")
    if not page:
        return None
    stream = None if flat else _media_url(info)
    return Track(
        title=info.get("title") or "(タイトル不明)",
        url=page,
        duration=None if info.get("is_live") else info.get("duration"),
        uploader=info.get("uploader") or info.get("channel") or "",
        requester=requester,
        stream=stream,
        resolved_at=time.time() if stream else 0.0,
        thumbnail=info.get("thumbnail") or None)


def _extract(query: str, opts: dict) -> dict:
    """Blocking; always called through asyncio.to_thread."""
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(query, download=False)


# yt-dlp says why in English, at length, and asks for a bug report that is not
# warranted. These are the refusals people actually hit, answered in a sentence.
#
# Ordered most specific first and matched first-wins. A geo-blocked video is also
# reported as "Video unavailable", so the generic lines have to come last or they
# would swallow every reason worth telling apart.
_YDL_HINTS = (
    ("confirm your age", "年齢制限があるため再生できません。"),
    ("age-restricted", "年齢制限があるため再生できません。"),
    # Both word orders: YouTube uses "Private video." on its own and "This video
    # is private" when it follows "Video unavailable".
    ("private video", "非公開の動画です。"),
    ("is private", "非公開の動画です。"),
    ("members-only", "メンバー限定の動画です。"),
    ("join this channel", "メンバー限定の動画です。"),
    # Covers both "is not available in your country" and YouTube's own
    # "The uploader has not made this video available in your country".
    ("available in your country", "この地域からは再生できません。"),
    ("blocked it in your country", "この地域からは再生できません。"),
    ("live event will begin", "まだ開始していないライブ配信です。"),
    ("premieres in", "まだ公開されていないプレミア公開です。"),
    ("copyright", "著作権上の理由で再生できません。"),
    ("has been removed", "この動画は削除されています。"),
    ("account associated with this video has been terminated",
     "投稿者のアカウントが削除されています。"),
    ("sign in", "ログインが必要なため再生できません。"),
    # Spotify and the like. Reached only when the link slipped past
    # _METADATA_ONLY, so it names a service we have not listed yet.
    ("drm", "このサイトの曲はDRM保護のため再生できません。"
            "曲名で検索すると再生できることがあります。"),
    ("unsupported url", "対応していないURLです。"),
    ("no video formats", "音声を取得できる形式が見つかりませんでした。"),
    ("unable to download", "接続できませんでした。時間をおいて試してください。"),
    # Three phrasings of the same thing: "Video unavailable" on its own, and
    # "This video is unavailable" / "no longer available" in a sentence. The
    # first needle does not match the others — the word "is" sits in the middle.
    ("video unavailable", "この動画は利用できません。"),
    ("is unavailable", "この動画は利用できません。"),
    ("no longer available", "この動画は利用できません。"),
)


def _explain(message: str) -> str:
    plain = re.sub(r"\x1b\[[0-9;]*m", "", message)
    lowered = plain.lower()
    for needle, said in _YDL_HINTS:
        if needle in lowered:
            return said
    # Nothing recognised: the first clause of yt-dlp's own message beats a
    # generic failure, even in English.
    return ui.clip(plain.split(";")[0].replace("ERROR: ", "").strip(), 300)


async def _run_ydl(query: str, opts: dict) -> dict:
    try:
        return await asyncio.to_thread(_extract, query, opts)
    except yt_dlp.utils.DownloadError as e:
        raise MusicError(_explain(str(e))) from None
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("yt-dlp が失敗しました")
        raise MusicError(f"取得に失敗しました ({type(e).__name__})") from None


async def search(query: str, requester: int, limit: int = 1,
                 alternatives: int = 0) -> list[Track]:
    """Resolve a URL or a search phrase into tracks, cheaply.

    Only metadata: the media URL is fetched track by track as each one starts,
    so that a queue of fifty does not hold fifty URLs that expire while waiting.

    Asking for a few more results than are wanted costs nothing — a search is one
    request whether it returns one row or five — and they are worth having. See
    Track.alternatives.
    """
    query = query.strip()
    is_url = bool(_URL.match(query))
    is_playlist = is_url and any(k in query.lower() for k in ("list=", "playlist?", "/sets/"))

    # Fast-path for single video URLs: resolve directly with _YDL_ONE to avoid a second yt-dlp call!
    if is_url and not is_playlist:
        info = await _run_ydl(query, _YDL_ONE)
        if not info:
            raise MusicError("何も見つかりませんでした。")
        track = _to_track(info, requester)
        if not track:
            raise MusicError("再生できるものが見つかりませんでした。")
        return [track]

    if not is_url:
        query = f"ytsearch{limit + alternatives}:{query}"
    info = await _run_ydl(query, _YDL_LIST)
    if not info:
        raise MusicError("何も見つかりませんでした。")
    entries = info["entries"] if info.get("_type") == "playlist" else [info]
    tracks = [t for t in (_to_track(e, requester) for e in entries if e) if t]
    if not tracks:
        raise MusicError("再生できるものが見つかりませんでした。")
    if not is_url and alternatives:
        wanted = tracks[:limit]
        wanted[0].alternatives = tracks[limit:limit + alternatives]
        return wanted
    return tracks


async def search_popular(genre: str, count: int, requester: int) -> list[Track]:
    """Search YouTube for popular songs in a genre ordered by view count."""
    genre = genre.strip()
    encoded = urllib.parse.quote(f"{genre}")
    # sp=CAMSAhAB specifies sort by view count on YouTube
    search_url = f"https://www.youtube.com/results?search_query={encoded}&sp=CAMSAhAB"

    fetch_limit = min(50, max(count * 3, 15))
    opts = _YDL_LIST | {
        "playlist_items": f"1:{fetch_limit}",
    }
    info = await _run_ydl(search_url, opts)
    if not info:
        raise MusicError(f"ジャンル「{genre}」の曲が見つかりませんでした。")
    entries = info.get("entries") or [info]

    filtered_tracks: list[Track] = []
    fallback_tracks: list[Track] = []

    for e in entries:
        if not e:
            continue
        track = _to_track(e, requester)
        if not track:
            continue
        # Avoid extremely long compilation/BGM videos (>20 mins) if possible
        dur = track.duration
        if dur is not None and dur > 1200:
            fallback_tracks.append(track)
            continue
        filtered_tracks.append(track)
        if len(filtered_tracks) >= count:
            break

    # If we still need more tracks, backfill with fallback
    if len(filtered_tracks) < count:
        for t in fallback_tracks:
            filtered_tracks.append(t)
            if len(filtered_tracks) >= count:
                break

    if not filtered_tracks:
        raise MusicError(f"ジャンル「{genre}」で再生できる曲が見つかりませんでした。")

    return filtered_tracks[:count]


# --------------------------------------------------------------------------- #
# Stream Cache & Similar Tracks Search
# --------------------------------------------------------------------------- #

# URL -> (stream_url, resolved_at, duration, title, thumbnail)
_RESOLVE_CACHE: dict[str, tuple[str, float, float | None, str, str | None]] = {}
_CACHE_MAX_ENTRIES = 200
_CACHE_TTL = 900.0  # 15分（YouTube CDN URLの有効期限内）

_YT_ID_RE = re.compile(r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})")


def _extract_video_id(url_or_id: str) -> str | None:
    """YouTube動画URLまたはIDから11桁のVideo IDを抽出する。"""
    if len(url_or_id) == 11 and re.match(r"^[a-zA-Z0-9_-]{11}$", url_or_id):
        return url_or_id
    m = _YT_ID_RE.search(url_or_id)
    return m.group(1) if m else None


def _get_cached_stream(url: str) -> tuple[str, float | None, str, str | None] | None:
    now = time.time()
    item = _RESOLVE_CACHE.get(url)
    if not item:
        return None
    stream, resolved_at, dur, title, thumb = item
    if now - resolved_at < _CACHE_TTL:
        return stream, dur, title, thumb
    _RESOLVE_CACHE.pop(url, None)
    return None


def _put_cached_stream(url: str, stream: str, dur: float | None, title: str, thumb: str | None):
    now = time.time()
    if len(_RESOLVE_CACHE) >= _CACHE_MAX_ENTRIES:
        to_del = [k for k, v in _RESOLVE_CACHE.items() if now - v[1] >= _CACHE_TTL]
        if not to_del:
            to_del = list(_RESOLVE_CACHE.keys())[:20]
        for k in to_del:
            _RESOLVE_CACHE.pop(k, None)
    _RESOLVE_CACHE[url] = (stream, now, dur, title, thumb)


async def search_similar(query: str, count: int, requester: int) -> tuple[Track | None, list[Track]]:
    """指定された楽曲名・アーティスト・URLから類似楽曲（YouTube Mix）を検索して取得する。
    
    戻り値: (seed_track, similar_tracks)
    """
    query = query.strip()
    vid = _extract_video_id(query)
    seed_track: Track | None = None

    if vid:
        seed_url = f"https://www.youtube.com/watch?v={vid}"
        try:
            seed_info = await _run_ydl(seed_url, _YDL_ONE)
            if seed_info:
                seed_track = _to_track(seed_info, requester)
        except Exception as e:
            log.debug("シード曲の詳細取得に失敗 (無視可能): %s", e)
    else:
        # キーワードからシード曲を特定
        try:
            seed_tracks = await search(query, requester, limit=1)
            if seed_tracks:
                seed_track = seed_tracks[0]
                vid = _extract_video_id(seed_track.url)
        except Exception as e:
            log.warning("シード曲の検索に失敗: %s", e)

    similar_tracks: list[Track] = []

    # 1. YouTube Mix (RD{vid}) を用いた公式レコメンド類似曲の取得
    if vid:
        mix_url = f"https://www.youtube.com/watch?v={vid}&list=RD{vid}"
        fetch_limit = min(50, max(count * 3, 20))
        opts = _YDL_LIST | {
            "playlist_items": f"1:{fetch_limit}",
        }
        try:
            info = await _run_ydl(mix_url, opts)
            entries = info.get("entries") or [] if info else []
            seen_ids = {vid}
            seen_titles = {seed_track.title.lower()} if seed_track else set()

            for e in entries:
                if not e:
                    continue
                eid = e.get("id")
                if eid and eid in seen_ids:
                    continue
                t = _to_track(e, requester)
                if not t:
                    continue
                t_lower = t.title.lower()
                if t_lower in seen_titles:
                    continue
                # 極端に長い動画 (20分以上) は除外
                if t.duration and t.duration > 1200:
                    continue
                if eid:
                    seen_ids.add(eid)
                seen_titles.add(t_lower)
                similar_tracks.append(t)
                if len(similar_tracks) >= count:
                    break
        except Exception as e:
            log.warning("YouTube Mix (RD) の取得に失敗しました: %s", e)

    # 2. フォールバック: Mixで十分な曲数が得られなかった場合は人気曲/関連検索で補完
    if len(similar_tracks) < count:
        fallback_query = query
        if seed_track and seed_track.uploader:
            fallback_query = f"{seed_track.uploader} {query}"
        try:
            more_tracks = await search_popular(fallback_query, count, requester)
            seen_urls = {t.url for t in similar_tracks}
            if seed_track:
                seen_urls.add(seed_track.url)
            for t in more_tracks:
                if t.url not in seen_urls:
                    similar_tracks.append(t)
                    seen_urls.add(t.url)
                if len(similar_tracks) >= count:
                    break
        except Exception as e:
            log.debug("類似曲フォールバック検索失敗: %s", e)

    if not similar_tracks and not seed_track:
        raise MusicError(f"「{query}」に類似する楽曲が見つかりませんでした。")

    return seed_track, similar_tracks[:count]


async def resolve(track: Track) -> str:
    """The media URL for a track, fetched now if the stored one has gone stale."""
    if track.fresh:
        return track.stream

    # キャッシュチェック（高速化: 同一曲の再取得を0msに短縮）
    cached = _get_cached_stream(track.url)
    if cached:
        stream, dur, title, thumb = cached
        track.stream, track.resolved_at = stream, time.time()
        if track.duration is None and dur is not None:
            track.duration = dur
        if title:
            track.title = title
        if not track.thumbnail and thumb:
            track.thumbnail = thumb
        log.debug("ストリームURLキャッシュを利用しました: %s", track.title)
        return stream

    info = await _run_ydl(track.url, _YDL_ONE)
    stream = _media_url(info or {})
    if not stream:
        raise MusicError("音声ストリームを取得できませんでした。")
    track.stream, track.resolved_at = stream, time.time()
    # A flat entry has no duration; now that the full record is here, take it.
    if track.duration is None and not info.get("is_live"):
        track.duration = info.get("duration")
    if info.get("title"):
        track.title = info["title"]
    if not track.thumbnail and info.get("thumbnail"):
        track.thumbnail = info["thumbnail"]
    _put_cached_stream(track.url, stream, track.duration, track.title, track.thumbnail)
    return stream


# --------------------------------------------------------------------------- #
# Per-server playback
# --------------------------------------------------------------------------- #

class GuildPlayer:
    """One queue and one background task per server.

    The task is the whole design: commands only ever touch the queue and the
    flags, and this loop decides what plays next. Nothing else calls
    VoiceClient.play, so there is one place where a track can start and one
    place where the loop mode is read.
    """

    def __init__(self, cog: "Music", guild: discord.Guild,
                 channel: discord.abc.Messageable):
        self.cog = cog
        self.guild = guild
        self.channel = channel      # where "now playing" goes
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.loop_mode = "off"      # off | one | all
        self.volume = DEFAULT_VOLUME

        self._added = asyncio.Event()
        self._finished = asyncio.Event()
        self._skipped = False
        self._replay_at: float | None = None
        self._leaving = False
        self._announced: Track | None = None
        # Said once per player, not once per track: a server mute stays until
        # somebody clears it, and repeating it every song would be noise on top
        # of silence.
        self._warned_mute = False
        # Playback position, which Discord does not report and ffmpeg cannot be
        # asked for, so it is kept by hand: when the track started, how far into
        # it that was, and how much of the wall clock since was spent paused.
        self._start = 0.0
        self._offset = 0.0
        self._paused_at: float | None = None
        self._paused_total = 0.0

        self._task = asyncio.create_task(self._run(), name=f"player-{guild.id}")

    # -- state ------------------------------------------------------------- #

    @property
    def voice(self) -> discord.VoiceClient | None:
        return self.guild.voice_client

    @property
    def playing(self) -> bool:
        vc = self.voice
        return bool(vc and (vc.is_playing() or vc.is_paused()))

    def elapsed(self) -> float:
        if not self._start:
            return 0.0
        now = self._paused_at or time.monotonic()
        return max(0.0, self._offset + (now - self._start) - self._paused_total)

    def total_duration(self) -> float | None:
        """Seconds of queued audio, or None if anything in it is a live stream."""
        seconds = 0.0
        for track in self.queue:
            if track.duration is None:
                return None
            seconds += track.duration
        return seconds

    def add(self, tracks: list[Track]) -> int:
        room = MAX_QUEUE - len(self.queue)
        added = tracks[:max(0, room)]
        self.queue.extend(added)
        if added:
            self._added.set()
            if self.playing:
                asyncio.create_task(self._prefetch_next())
        return len(added)

    async def _prefetch_next(self):
        """Pre-fetch stream URL for the next queued track in background."""
        try:
            if self.queue:
                next_track = self.queue[0]
                if not next_track.fresh:
                    await resolve(next_track)
                    log.debug("次の曲を事前解決しました: %s", next_track.title)
        except Exception as e:
            log.debug("プリフェッチ失敗 (無視可能): %s", e)

    # -- the loop ---------------------------------------------------------- #

    async def _run(self):
        try:
            while not self._leaving:
                seeking = self._replay_at is not None
                if seeking:
                    offset, self._replay_at = self._replay_at, None
                    track = self.current
                else:
                    track = self._next_track() or await self._wait_for_track()
                    if track is None:
                        await self._say("しばらく再生がなかったので退出します。")
                        return
                    self.current, offset = track, 0.0
                if track is None:      # seek raced with stop; nothing to replay
                    continue
                if not await self._play(track, offset, announce=not seeking):
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("プレイヤーが停止しました (guild=%s)", self.guild.id)
            await self._say("再生処理が停止しました。もう一度 R!play を試してください。",
                            error=True)
        finally:
            await self.cog.dispose(self)

    async def _play(self, track: Track, offset: float, announce: bool) -> bool:
        """Play one track through to its end. False when the connection is gone.

        Tried twice at most. The retry is not for a track that fails loudly —
        that raises and is reported — but for one that fails silently: an expired
        media URL makes ffmpeg exit without writing a byte, which reaches us as a
        song that finished in a fraction of a second, and is indistinguishable
        from success unless the clock is checked.
        """
        for attempt in (0, 1):
            vc = self.voice
            if vc is None or not vc.is_connected():
                return False
            try:
                source = await self._source(track, offset, fresh=attempt > 0)
            except MusicError as e:
                await self._say(f"再生できませんでした: {track.label()}\n{e}", error=True)
                self.current = None
                return True

            self._finished.clear()
            try:
                vc.play(source, after=self._after, **encoder_options(vc.channel))
            except discord.opus.OpusNotLoaded:
                source.cleanup()
                await self._say(unavailable() or "音声を送信できません。", error=True)
                return False
            except discord.ClientException as e:
                # Something is already on the wire. Take the slot rather than
                # abandoning the track, but only once.
                log.warning("再生を開始できませんでした: %s", e)
                source.cleanup()
                vc.stop()
                if attempt:
                    return True
                continue

            self._mark_started(offset)
            asyncio.create_task(self._prefetch_next())
            await self._warn_if_muted()
            if announce and self._announced is not track:
                self._announced = track
                await self._announce(track)
            if not await self._wait_until_done():
                return False

            played = max(0.0, self.elapsed() - offset)
            remaining = None if track.duration is None else track.duration - offset
            cut_short = played < MIN_PLAYBACK and (
                remaining is None or remaining > MIN_PLAYBACK * 2)
            if not cut_short:
                return True
            if attempt == 0:
                log.warning("再生が即座に終了しました。URLを取り直します: %s", track.title)
                continue
            await self._say(
                f"再生できませんでした: {track.label()}\n"
                "URLの期限切れか、配信元に拒否された可能性があります。", error=True)
        return True

    async def _warn_if_muted(self):
        """Say so if the bot has been server-muted.

        This is the mute that matters. Self-deafening looks alarming and changes
        nothing about what goes out; a server mute is set by a moderator on the
        member and stops the audio dead. Everything else keeps working — the
        queue advances, the position counter runs, the embed says 再生中 — and
        nobody hears a thing, which is a miserable thing to debug from the
        outside.
        """
        if self._warned_mute:
            return
        me = self.guild.me
        state = me.voice if me else None
        if state is None or not state.mute:
            return
        self._warned_mute = True
        log.warning("サーバーミュートされています (guild=%s)", self.guild.id)
        await self._say(
            "サーバーミュートされているため、音声が誰にも届きません。\n"
            "Botを右クリック → 「サーバーミュート」のチェックを外してください。",
            error=True)

    async def _wait_until_done(self) -> bool:
        """Wait out the current track. False if the connection went away.

        Not a bare wait on the event. The callback that sets it runs on the audio
        thread, and a voice connection that drops takes that thread with it —
        waiting forever on an event nobody is left to set is how a player goes
        quiet and then ignores every command it is given.
        """
        while not self._finished.is_set():
            try:
                await asyncio.wait_for(self._finished.wait(), WATCHDOG_INTERVAL)
            except asyncio.TimeoutError:
                vc = self.voice
                if vc is None or not vc.is_connected():
                    return False
                if not (vc.is_playing() or vc.is_paused()):
                    log.warning("再生は終わっていますが通知がありません (guild=%s)",
                                self.guild.id)
                    return True
        return True

    def _next_track(self) -> Track | None:
        """Apply the loop mode to the track that just finished, then take one.

        Skipping overrides repeat-one — asking for the next track and being given
        the same one again is the sort of thing that gets a bot muted.
        """
        finished, self.current = self.current, None
        if finished is not None:
            if self.loop_mode == "one" and not self._skipped:
                self._skipped = False
                return finished
            if self.loop_mode == "all":
                self.queue.append(finished)
        self._skipped = False
        return self.queue.popleft() if self.queue else None

    async def _wait_for_track(self) -> Track | None:
        """Block until something is queued, or give up and let the caller leave.

        The event is cleared before the queue is checked, never after: clearing
        afterwards would drop a track added in between and leave the player
        asleep with a full queue.
        """
        while not self._leaving:
            self._added.clear()
            if self.queue:
                return self.queue.popleft()
            try:
                await asyncio.wait_for(self._added.wait(), IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                return None
        return None

    def _after(self, error: Exception | None):
        # Runs on the audio thread, so it may not touch the loop directly.
        if error:
            log.warning("再生中のエラー (guild=%s): %s", self.guild.id, error)
        try:
            self.cog.bot.loop.call_soon_threadsafe(self._finished.set)
        except RuntimeError:
            # The loop closed under us during shutdown. Nothing is waiting on
            # the event any more, so there is nothing to salvage.
            pass

    async def _fallback_resolve(self, track: Track) -> str:
        """Resolve, stepping to the next search result if this one refuses.

        A keyword search returns the most popular upload, which for Japanese
        music is very often the official MV — and those are routinely
        age-restricted, which cannot be resolved without an account. The next
        result down is usually the same song from an upload that can be.

        The working track's details are copied onto the queued one rather than
        replacing it, so the queue entry, the loop mode and the announcement all
        stay attached to the same object.
        """
        try:
            return await resolve(track)
        except MusicError as refused:
            while track.alternatives:
                candidate = track.alternatives.pop(0)
                try:
                    stream = await resolve(candidate)
                except MusicError:
                    continue
                log.info("代替候補に切り替えました: %s -> %s",
                         track.title, candidate.title)
                track.title, track.url = candidate.title, candidate.url
                track.duration = candidate.duration
                track.uploader = candidate.uploader
                track.stream, track.resolved_at = stream, candidate.resolved_at
                return stream
            raise refused

    async def _source(self, track: Track, offset: float,
                      fresh: bool = False) -> discord.AudioSource:
        if fresh:
            track.stream = None      # discard the URL that just failed
        stream = await self._fallback_resolve(track)
        before = FFMPEG_BEFORE
        if stream.startswith(("http://", "https://")):
            before = f"{before} {FFMPEG_RECONNECT}"
        if offset > 0:
            # Ahead of -i, so ffmpeg seeks the input by keyframe instead of
            # decoding and discarding everything up to the mark.
            before = f"-ss {offset:.3f} {before}"
        audio = discord.FFmpegPCMAudio(stream, executable=FFMPEG_EXE,
                                       before_options=before, options=FFMPEG_OPTIONS)
        return discord.PCMVolumeTransformer(audio, volume=self.volume)

    def _mark_started(self, offset: float):
        self._start = time.monotonic()
        self._offset = offset
        self._paused_at = None
        self._paused_total = 0.0

    # -- commands reach in here -------------------------------------------- #

    def skip(self) -> Track | None:
        # Only when something is actually playing: setting the flag while the
        # loop is idle would leave it armed, and it would then eat the repeat on
        # whatever gets queued next.
        vc = self.voice
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return None
        skipped, self._skipped = self.current, True
        vc.stop()               # the after-callback wakes the loop
        return skipped

    def pause(self) -> bool:
        vc = self.voice
        if not vc or not vc.is_playing():
            return False
        vc.pause()
        self._paused_at = time.monotonic()
        return True

    def resume(self) -> bool:
        vc = self.voice
        if not vc or not vc.is_paused():
            return False
        if self._paused_at:
            self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None
        vc.resume()
        return True

    def set_volume(self, value: float):
        self.volume = value
        vc = self.voice
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = value   # takes effect mid-track

    def seek(self, seconds: float) -> bool:
        """Restart the current track at an offset.

        Done by replaying rather than by talking to ffmpeg, which has no way to
        be told to move once it is running. The flag is what stops the loop from
        treating the stop below as the track having finished.
        """
        if self.current is None:
            return False
        self._replay_at = max(0.0, seconds)
        if self.voice:
            self.voice.stop()
        return True

    def halt(self):
        """Empty the queue and end the current track, but stay in the channel.

        What R!stop does, as against R!leave. The loop finds nothing waiting and
        goes back to idling, so the next R!play starts straight away instead of
        reconnecting — and the idle timeout still gets the bot out eventually if
        nothing follows.
        """
        self.queue.clear()
        self.current = None
        self._announced = None
        self._replay_at = None
        if self.voice:
            self.voice.stop()

    async def stop(self):
        """Empty the queue, hang up, and end the task.

        The player is dropped from the cog here rather than in the task's own
        cleanup, which only runs on the next pass through the event loop: for
        that moment a stopped player would still answer to R!play, and the track
        queued into it would never start.
        """
        self.cog.players.pop(self.guild.id, None)
        self._leaving = True
        self.queue.clear()
        self.current = None
        self._added.set()          # so a waiting loop notices _leaving
        vc = self.voice
        if vc:
            vc.stop()
            await vc.disconnect(force=True)
        if self._task and not self._task.done():
            self._task.cancel()

    # -- talking ------------------------------------------------------------ #

    async def _say(self, text: str, error: bool = False, title: str | None = None):
        body = ui.embed(text, title,
                        ui.COLOR_ERROR if error else COLOR_MUSIC)
        try:
            await self.channel.send(**ui.payload(self.channel, body))
        except ui.SEND_ERRORS as e:
            log.warning("音楽の通知を送れませんでした: %s", ui.short_error(e))

    async def _announce(self, track: Track):
        who = self.guild.get_member(track.requester)
        lines = [f"**{track.label(90)}**",
                 f"長さ: {duration(track.duration)}"]
        if track.uploader:
            lines.append(f"投稿者: {discord.utils.escape_markdown(track.uploader)}")
        if who:
            lines.append(f"リクエスト: {who.display_name}")
        if self.loop_mode != "off":
            lines.append(f"リピート: {self.loop_mode}")
        embed = ui.embed("\n".join(lines), title="再生中", color=COLOR_MUSIC)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        try:
            await self.channel.send(**ui.payload(self.channel, embed))
        except ui.SEND_ERRORS as e:
            log.warning("音楽の通知を送れませんでした: %s", ui.short_error(e))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def _parse_time(raw: str) -> float:
    """Accept 90, 1:30 or 1:02:03 as a number of seconds."""
    parts = raw.strip().split(":")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise MusicError("時間は 90 や 1:30 の形式で指定してください。") from None
    if len(values) > 3:
        raise MusicError("時間は 90 や 1:30 の形式で指定してください。")
    seconds = 0.0
    for value in values:
        seconds = seconds * 60 + value
    return seconds


# Every one of these is required to play anything, and "チャンネルを見る" is the
# one people miss: denying it on a channel takes connect and speak with it, so
# the permission tab can show both as allowed while neither applies.
_NEEDED_PERMS = (
    ("view_channel", "チャンネルを見る"),
    ("connect", "接続"),
    ("speak", "発言"),
)


def _missing(channel, member) -> list[str]:
    """The names of the permissions this member lacks on this channel."""
    if member is None:
        # No member object to reason about. Say nothing is missing and let the
        # connection attempt be the judge, rather than refusing on no evidence.
        return []
    perms = channel.permissions_for(member)
    return [label for attr, label in _NEEDED_PERMS if not getattr(perms, attr, False)]


def _permission_help(channel, missing: list[str]) -> str:
    return (f"「{channel.name}」で次の権限がありません: {'、'.join(missing)}\n"
            "・チャンネルを右クリック → チャンネルの編集 → 権限 で確認してください。\n"
            "・サーバー全体で許可していても、チャンネル個別の設定が優先されます。\n"
            "・`R!perms` で、Bot から実際に何が見えているか確認できます。")


class SearchView(discord.ui.View):
    """A dropdown of search results.

    A menu rather than numbered reactions, which is how this is usually done and
    which would mean ten emoji per search.
    """

    def __init__(self, cog: "Music", ctx: commands.Context, tracks: list[Track]):
        super().__init__(timeout=60)
        self.cog, self.ctx, self.tracks = cog, ctx, tracks
        self.message: discord.Message | None = None
        self.add_item(_SearchSelect(tracks))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "この検索を実行した人だけが選べます。", ephemeral=True)
        return False

    async def on_timeout(self):
        self.clear_items()
        if self.message:
            try:
                await self.message.edit(view=self)
            except ui.SEND_ERRORS:
                pass


class _SearchSelect(discord.ui.Select):
    def __init__(self, tracks: list[Track]):
        super().__init__(
            placeholder="再生する曲を選んでください",
            options=[discord.SelectOption(
                label=ui.clip(ui.strip_emoji(t.title), 100) or "(無題)",
                description=ui.clip(
                    f"{duration(t.duration)} / {ui.strip_emoji(t.uploader)}", 100),
                value=str(i)) for i, t in enumerate(tracks[:25])])

    async def callback(self, interaction: discord.Interaction):
        view: SearchView = self.view
        track = view.tracks[int(self.values[0])]
        view.clear_items()
        await interaction.response.edit_message(view=view)
        view.stop()
        # Caught here because a component callback has no error handler behind
        # it: the cog's would never see this, and the person who chose a song
        # would be left with a dead menu and no reason given.
        try:
            await view.cog.enqueue(view.ctx, [track])
        except MusicError as e:
            await view.cog.say(view.ctx, str(e), error=True)


class Music(commands.Cog, name="音楽"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self._alone: dict[int, asyncio.Task] = {}
        # TTS（読み上げ）の状態管理
        self.tts_channels: dict[int, int] = {}        # guild_id -> target_channel_id
        self.tts_queues: dict[int, asyncio.Queue] = {} # guild_id -> Queue[str]
        self.tts_tasks: dict[int, asyncio.Task] = {}  # guild_id -> WorkerTask
        self.tts_max_queue = 25

    async def cog_unload(self):
        for task in list(self._alone.values()):
            task.cancel()
        for task in list(self.tts_tasks.values()):
            task.cancel()
        for player in list(self.players.values()):
            await player.stop()

    async def dispose(self, player: "GuildPlayer"):
        """Called by a player's task as it ends, so the next command builds a new one.

        Identity is checked rather than just the guild id. A task's cleanup runs
        one turn of the event loop after it is cancelled, and R!leave followed by
        R!play fits in that gap: matching on the id alone would let a dying
        player evict — and hang up on — the live one that replaced it.
        """
        if self.players.get(player.guild.id) is not player:
            return
        del self.players[player.guild.id]
        vc = player.guild.voice_client
        if vc:
            await vc.disconnect(force=True)

    # -- guards ------------------------------------------------------------- #

    # Diagnostics have to work when the thing they diagnose does not, or they
    # are unavailable exactly when they are needed.
    _ALWAYS_ALLOWED = frozenset({"perms"})

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.CheckFailure("音楽コマンドはサーバー内でのみ使えます。")
        if ctx.command.name in self._ALWAYS_ALLOWED:
            return True
        reason = unavailable()
        if reason:
            raise commands.CheckFailure(reason)
        return True

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.CommandInvokeError):
            error = error.original
        if isinstance(error, ui.NETWORK_ERRORS):
            # Logged as one line, not a traceback: the stack for a failed DNS
            # lookup is twenty frames of aiohttp internals and says nothing the
            # first line does not. The reply below will very likely fail too,
            # which is fine — say() swallows that now instead of raising back
            # into the handler that called it.
            log.warning("ネットワークエラーで %s を実行できませんでした: %s",
                        ctx.command, ui.short_error(error))
            await self.say(ctx, "Discordに接続できませんでした。"
                                "ネットワークが不安定なようです。"
                                "少し待ってからもう一度どうぞ。", error=True)
            return
        if isinstance(error, commands.CommandOnCooldown):
            await self.say(ctx, f"少し待ってからどうぞ ({error.retry_after:.0f}秒)。",
                           error=True)
            return
        if isinstance(error, commands.BadArgument):
            # The default text is English and describes a failed int() cast,
            # which tells the person nothing about what to type instead.
            await self.say(ctx, f"引数の形式が正しくありません。"
                                f"`R!help` で {ctx.command} の使い方を確認してください。",
                           error=True)
            return
        if isinstance(error, (MusicError, commands.CheckFailure,
                              commands.UserInputError)):
            await self.say(ctx, str(error), error=True)
            return
        if isinstance(error, discord.Forbidden):
            await self.say(ctx, "権限が足りないため実行できませんでした。", error=True)
            return
        log.exception("音楽コマンドが失敗しました: %s", ctx.command, exc_info=error)
        await self.say(ctx, "コマンドの実行に失敗しました。", error=True)

    async def _leave_quietly(self, guild: discord.Guild):
        """Undo a half-finished connection.

        Joining has two halves and only the first is TCP. When the audio half
        fails, the first has already succeeded — so the bot is left sitting in
        the channel: visible, silent, self-deafened, looking like it is about to
        start at any moment. Clearing the voice state is what makes a failed
        attempt actually look like one.
        """
        vc = guild.voice_client
        if vc is not None:
            try:
                # Bounded: disconnecting a half-open connection can wait on a
                # handshake that is never going to finish, and this runs on the
                # path of the command someone is waiting on.
                await asyncio.wait_for(vc.disconnect(force=True), timeout=8)
            except (asyncio.TimeoutError, Exception):
                log.debug("切断に失敗しました", exc_info=True)

        # Unconditionally, and this is the important line. disconnect() drops the
        # client from discord.py's registry as part of its cleanup, but only if
        # it gets that far — and a connection that failed midway often does not.
        # What is left behind is invisible (guild.voice_client can still read as
        # None) yet makes every future connect() raise "Already connected to a
        # voice channel", which is how one bad attempt bricks the bot until it is
        # restarted. That is the loop we were in.
        try:
            guild._state._remove_voice_client(guild.id)
        except Exception:
            log.debug("ボイスクライアントを除去できませんでした", exc_info=True)

        # Discord can still have us in the channel even with nothing local to
        # show for it. Asking the gateway for no channel is the only way out.
        try:
            await guild.change_voice_state(channel=None)
        except Exception:
            log.debug("ボイス状態を戻せませんでした", exc_info=True)

    async def _open_voice(self, ctx: commands.Context, channel):
        """Connect to a voice channel, clearing the way first and once again.

        Two attempts, and the second is not optimism. The only thing
        channel.connect() raises ClientException for is a leftover VoiceClient,
        and _leave_quietly removes exactly that — so a failure on the first
        attempt is repaired by the cleanup between them, not waited out. Without
        this the bot needed a restart to recover from one bad connection.
        """
        last = ""
        for attempt in (0, 1):
            if ctx.guild.voice_client is not None or attempt:
                log.info("以前の音声接続を片付けます (試行 %d)", attempt + 1)
                await self._leave_quietly(ctx.guild)
            try:
                # reconnect=False: discord.py otherwise retries internally, which
                # triples how long a doomed attempt takes and leaves more state
                # behind each time. Retrying is this method's business.
                await channel.connect(timeout=VOICE_TIMEOUT, reconnect=False,
                                      self_deaf=SELF_DEAF,
                                      cls=voice_client_class())
                return
            except discord.ConnectionClosed as e:
                # Must be caught before ClientException, which it subclasses. The
                # other way round every close code is mistaken for a leftover
                # client, retried on that theory, and reported as one.
                log.warning("音声WebSocketが閉じられました (試行 %d, コード %s)",
                            attempt + 1, e.code)
                last = f"コード {e.code}"
                # 4006 means the voice server has no record of this session, and
                # 4009 that it expired. A full reset is the only thing that can
                # produce a new one — so one more attempt, and no more.
                if attempt == 0 and e.code in (4006, 4009):
                    continue
                await self._leave_quietly(ctx.guild)
                raise MusicError(f"「{channel.name}」に接続できませんでした。\n"
                                 + voice_close_help(e.code)) from None
            except discord.ClientException as e:
                # "Already connected to a voice channel" — recoverable, and the
                # cleanup at the top of the next pass is the recovery.
                last = str(e)
                log.warning("接続に失敗しました (試行 %d): %s", attempt + 1, e)
                continue
            except asyncio.TimeoutError:
                # Joining is negotiated over the gateway, but the voice websocket
                # is a separate connection to a different host and port, and the
                # audio after it is UDP. Getting this far and no further means one
                # of those two is not reachable — never a permissions problem,
                # because permissions are checked before we get here.
                log.warning("音声接続がタイムアウトしました (guild=%s ch=%s)。"
                            "TCP 2096 か UDP が遮断されている可能性があります。",
                            ctx.guild.id, channel.id)
                await self._leave_quietly(ctx.guild)
                raise MusicError(
                    f"「{channel.name}」の音声サーバーに接続できませんでした。\n"
                    "チャンネルには入れているので、権限ではなく通信の問題です。\n"
                    "・`voice_check.py` を実行すると原因が分かります。\n"
                    "・TCP 2096 が塞がれている場合は "
                    "`MUSIC_VOICE_PORT=443` で回避できることがあります。") from None
            except discord.opus.OpusNotLoaded:
                await self._leave_quietly(ctx.guild)
                raise MusicError(unavailable() or "音声を送信できません。") from None
            except discord.DiscordException as e:
                log.warning("接続に失敗しました: %s", e)
                await self._leave_quietly(ctx.guild)
                raise MusicError(f"ボイスチャンネルに接続できませんでした: {e}") from None
        await self._leave_quietly(ctx.guild)
        # The reason is included rather than logged and hidden: "try again"
        # without saying what happened is what sent us looking at permissions
        # for an hour when the problem was a stale connection object.
        raise MusicError(f"接続に失敗しました: {last}\n"
                         "接続状態を掃除しても解消しませんでした。"
                         "Botを再起動すると直ることがあります。")

    async def missing_perms(self, guild: discord.Guild, channel) -> list[str]:
        """Which of the permissions we need are absent, asking Discord if unsure.

        The cached member is consulted first because it is free. When it says no,
        the question is put again to a freshly fetched one — and that second look
        is the whole point of this method.

        permissions_for() works from the member's roles, and without the members
        intent the bot is never told that its own roles have changed. A role
        granted after login is therefore invisible to it: guild.me still carries
        the roles it had when it connected, and the bot goes on insisting it has
        no permission to join a channel anyone can see it has been given. Since
        fixing permissions is exactly the thing people do while a bot is already
        running, that stale answer is the one it would give most often.
        """
        stale = _missing(channel, guild.me)
        if not stale:
            return []
        try:
            fresh = await guild.fetch_member(self.bot.user.id)
        except ui.SEND_ERRORS as e:
            log.warning("自分のメンバー情報を取得できませんでした: %s",
                        ui.short_error(e))
            return stale
        missing = _missing(channel, fresh)
        if not missing:
            log.info("キャッシュ上は権限不足でしたが、取得し直したら足りていました "
                     "(guild=%s ch=%s)", guild.id, getattr(channel, "id", "?"))
        return missing

    def player(self, ctx: commands.Context) -> GuildPlayer:
        """The player for this server, which must already be connected."""
        player = self.players.get(ctx.guild.id)
        if player is None or ctx.guild.voice_client is None:
            raise MusicError("いまは何も再生していません。")
        return player

    async def connect(self, ctx: commands.Context) -> GuildPlayer:
        """Join the caller's channel, or confirm we are already in it."""
        voice = ctx.author.voice
        if voice is None or voice.channel is None:
            raise MusicError("先にボイスチャンネルに入ってください。")
        channel = voice.channel

        missing = await self.missing_perms(ctx.guild, channel)
        if missing:
            raise MusicError(_permission_help(channel, missing))

        vc = ctx.guild.voice_client
        if vc and vc.is_connected():
            if vc.channel.id != channel.id:
                # Only moved when nobody is left behind to listen: dragging the
                # bot out of a channel other people are using is not a request
                # any one of them should be able to make.
                if any(not m.bot for m in vc.channel.members):
                    raise MusicError(f"いま {vc.channel.name} で再生中です。")
                await vc.move_to(channel)
        else:
            await self._open_voice(ctx, channel)

        player = self.players.get(ctx.guild.id)
        if player is None:
            player = GuildPlayer(self, ctx.guild, ctx.channel)
            self.players[ctx.guild.id] = player
        else:
            player.channel = ctx.channel   # follow the conversation
        return player

    # -- output ------------------------------------------------------------- #

    async def say(self, ctx: commands.Context, text: str, title: str | None = None,
                  error: bool = False, thumbnail_url: str | None = None) -> discord.Message | None:
        body = ui.embed(text, title, ui.COLOR_ERROR if error else COLOR_MUSIC)
        if thumbnail_url:
            body.set_thumbnail(url=thumbnail_url)
        try:
            return await ctx.reply(**ui.payload(ctx.channel, body),
                                   mention_author=False)
        except ui.NETWORK_ERRORS as e:
            # Discord is unreachable. There is nowhere to send an explanation of
            # why nothing can be sent, and the fallback below would fail the same
            # way — noisily, from inside an error handler.
            log.warning("Discordに接続できず返信できませんでした: %s",
                        ui.short_error(e))
            return None
        except discord.HTTPException:
            try:
                return await ctx.channel.send(**ui.payload(ctx.channel, body))
            except ui.SEND_ERRORS as e:
                log.warning("返信できませんでした: %s", ui.short_error(e))
                return None

    async def enqueue(self, ctx: commands.Context, tracks: list[Track],
                      player: "GuildPlayer | None" = None):
        player = player or await self.connect(ctx)
        was_idle = not player.playing and not player.queue
        added = player.add(tracks)
        if not added:
            raise MusicError(f"キューがいっぱいです (上限 {MAX_QUEUE} 曲)。")
        if len(tracks) > added:
            await self.say(ctx, f"キューの上限のため {added} 曲だけ追加しました。")
        elif added > 1:
            await self.say(ctx, f"{added} 曲をキューに追加しました。", "追加")
        elif not was_idle:
            position = len(player.queue)
            await self.say(ctx, f"**{tracks[0].label(90)}**\n"
                                f"長さ: {duration(tracks[0].duration)} / "
                                f"キュー {position} 番目", "追加")

    # -- playback ----------------------------------------------------------- #

    # "check" deliberately not an alias here: R!scan owns it, and a name that
    # could plausibly mean either is worse than no shorthand at all.
    @commands.command(name="perms", aliases=["permissions"])
    async def perms(self, ctx: commands.Context):
        """Botに見えている権限を表示します。"""
        # Deliberately reports the fetched member rather than the cached one, so
        # that what it prints is what the connection attempt will actually use.
        try:
            me = await ctx.guild.fetch_member(self.bot.user.id)
        except ui.SEND_ERRORS:
            me = ctx.guild.me
        if me is None:
            raise MusicError("Bot自身のメンバー情報を取得できませんでした。")

        roles = "、".join(r.name for r in me.roles if r.name != "@everyone")
        lines = [f"ロール: {discord.utils.escape_markdown(roles) or '(なし)'}"]
        if me.guild_permissions.administrator:
            lines.append("管理者権限: あり (通常はすべて許可されます)")
        if me.is_timed_out():
            lines.append("タイムアウト中です。解除するまで発言できません。")

        # Permissions are not the only way to be silenced. A server mute is set
        # per member and overrides everything below it: the bot connects, the
        # queue advances, and not one packet reaches anybody.
        state = me.voice
        if state is not None:
            if state.mute:
                lines.append("**サーバーミュートされています。**"
                             "右クリック → ミュートを解除してください。")
            if state.deaf:
                lines.append("サーバースピーカーミュートされています。")
            if state.self_deaf:
                lines.append("自分でスピーカーミュートしています "
                             "(受信のみ無効。送信には影響しません)。")

        voice = ctx.author.voice
        channel = voice.channel if voice else None
        if channel is None:
            lines.append("\nボイスチャンネルに入ってから実行すると、"
                         "そのチャンネルの権限も確認できます。")
        else:
            perms = channel.permissions_for(me)
            lines.append(f"\n**{discord.utils.escape_markdown(channel.name)}**")
            for attr, label in _NEEDED_PERMS:
                lines.append(f"`{'OK  ' if getattr(perms, attr, False) else 'なし'}`"
                             f" {label}")
            cached = _missing(channel, ctx.guild.me)
            live = _missing(channel, me)
            if cached != live:
                # Worth saying out loud: it means the bot was working from roles
                # it no longer has, which is the failure this command exists for.
                lines.append("\nキャッシュが古くなっていました。"
                             "いまの状態で判定します。")
        await self.say(ctx, "\n".join(lines), "権限の確認")

    @commands.command(name="join", aliases=["connect", "come"])
    async def join(self, ctx: commands.Context):
        """ボイスチャンネルに参加します。"""
        player = await self.connect(ctx)
        await self.say(ctx, f"{player.voice.channel.name} に参加しました。")

    # Each extraction is a round trip to YouTube, and a queue command held open
    # by someone leaning on the enter key is how a host gets rate-limited.
    @commands.cooldown(3, 10.0, commands.BucketType.user)
    @commands.command(name="play", aliases=["p"])
    async def play(self, ctx: commands.Context, *, query: str = ""):
        """URLか検索語で再生します。"""
        if not query:
            # Bare R!play is almost always meant as "carry on".
            player = self.players.get(ctx.guild.id)
            if player and player.resume():
                await self.say(ctx, "再生を再開しました。")
                return
            raise MusicError("URLか検索したい言葉を続けて入力してください。")
        async with ui.typing(ctx):
            connect_task = asyncio.create_task(self.connect(ctx))
            try:
                query = await self._playable_query(ctx, query)
                tracks = await search(query, ctx.author.id, alternatives=3)
                resolve_task = None
                if tracks and not tracks[0].fresh:
                    resolve_task = asyncio.create_task(resolve(tracks[0]))
                player = await connect_task
                if resolve_task:
                    try:
                        await resolve_task
                    except Exception:
                        pass
            except Exception:
                connect_task.cancel()
                raise
        await self.enqueue(ctx, tracks, player=player)

    async def _playable_query(self, ctx: commands.Context, query: str) -> str:
        """Turn a link we cannot stream from into one we can find the song by.

        Spotify and the rest hold their audio behind DRM, and yt-dlp says so and
        stops. But the link still names a song, and the song is almost always on
        YouTube — so the name is read from the link's public metadata and used as
        a search. The substitution is announced, because playing a different
        recording than the one linked is not something to do quietly.
        """
        if not _METADATA_ONLY.match(query.strip()):
            return query
        phrase = await describe_link(query.strip())
        if not phrase:
            raise MusicError(
                "曲の情報を取得できませんでした。\n"
                "`R!play 曲名 アーティスト名` で検索してください。")
        return phrase

    @commands.cooldown(2, 10.0, commands.BucketType.user)
    @commands.command(name="search", aliases=["find"])
    async def search_cmd(self, ctx: commands.Context, *, query: str = ""):
        """検索結果から選んで再生します。"""
        if not query:
            raise MusicError("検索したい言葉を続けて入力してください。")
        async with ui.typing(ctx):
            tracks = await search(query, ctx.author.id, limit=8)
        lines = [f"`{i + 1}.` {t.label()} `[{duration(t.duration)}]`"
                 for i, t in enumerate(tracks)]
        view = SearchView(self, ctx, tracks)
        body = ui.embed("\n".join(lines), f"「{ui.clip(query, 60)}」の検索結果",
                        COLOR_MUSIC)
        try:
            view.message = await ctx.reply(**ui.payload(ctx.channel, body),
                                           view=view, mention_author=False)
        except ui.SEND_ERRORS as e:
            log.warning("検索結果を送れませんでした: %s", ui.short_error(e))

    @commands.cooldown(2, 10.0, commands.BucketType.user)
    @commands.command(name="plist", aliases=["Plist", "similar", "sim", "radio", "playlist_genre", "genre", "ジャンル", "類似曲"])
    async def plist(self, ctx: commands.Context, *, args: str = ""):
        """指定した楽曲に似た曲（類似楽曲）やジャンル人気曲をまとめて連続再生します。
        例: R!plist Homage Funk 4
        例: R!plist (例： Homage Funk） 4
        例: R!plist J-POP 5
        """
        raw = args.strip()
        if not raw:
            raise MusicError(
                "曲名・URL・ジャンルと曲数を指定してください。\n"
                "例: `R!plist Homage Funk 4` (類似曲を4曲追加)\n"
                "例: `R!plist (例： Homage Funk） 4`\n"
                "例: `R!plist J-POP 5` (人気曲を5曲追加)"
            )

        # 柔軟な引数パース（括弧や「例：」の除去）
        text = raw
        text = re.sub(r"^[（\(]\s*(?:例\s*[:：]\s*)?", "", text)
        text = re.sub(r"[）\)]\s*(?=\d+$)", " ", text)
        text = re.sub(r"[）\)]$", "", text).strip()

        parts = text.split()
        count = 5  # デフォルト曲数
        if len(parts) >= 2 and parts[-1].isdigit():
            count = int(parts[-1])
            query = " ".join(parts[:-1]).strip()
        elif len(parts) == 1 and parts[0].isdigit():
            raise MusicError("曲名またはジャンル名を指定してください。\n例: `R!plist Homage Funk 4`")
        else:
            query = text

        query = query.strip("\"' \t\r\n")
        if not query:
            raise MusicError("曲名またはジャンル名を指定してください。\n例: `R!plist Homage Funk 4`")

        # 1〜25曲の範囲に制限
        count = max(1, min(count, 25))

        async with ui.typing(ctx):
            seed_track, tracks = await search_similar(query, count, ctx.author.id)

        if not tracks:
            if seed_track:
                tracks = [seed_track]
            else:
                raise MusicError(f"「{query}」の類似楽曲が見つかりませんでした。")

        await self.enqueue(ctx, tracks)

        # 追加結果のEmbed生成
        track_list = [f"`{i + 1}.` {t.label(60)} `[{duration(t.duration)}]`" for i, t in enumerate(tracks)]
        lines = []
        if seed_track:
            lines.append(f"**元曲:** {seed_track.label(55)}")
        lines.append(f"**追加曲数:** {len(tracks)}曲 (類似楽曲リスト)")
        lines.append("")
        lines.extend(track_list)

        title = f"「{ui.clip(seed_track.title if seed_track else query, 35)}」の類似曲を追加"
        embed = ui.embed("\n".join(lines), title, COLOR_MUSIC)
        if seed_track and seed_track.thumbnail:
            embed.set_thumbnail(url=seed_track.thumbnail)

        try:
            await ctx.reply(**ui.payload(ctx.channel, embed), mention_author=False)
        except ui.SEND_ERRORS:
            pass

    # ----------------------------------------------------------------------- #
    # TTS (Text-to-Speech) 読み上げ機能
    # ----------------------------------------------------------------------- #

    def _clean_text_simple(self, text: str) -> str:
        """単発テキスト用の簡易整形"""
        text = re.sub(r"https?://\S+", "URL省略", text)
        text = re.sub(r"<@!?\d+>", "メンション", text)
        text = re.sub(r"<@&\d+>", "ロール", text)
        text = re.sub(r"<#\d+>", "チャンネル", text)
        text = re.sub(r"<a?:\w+:\d+>", "", text)
        text = re.sub(r"```[\s\S]*?```", "コード省略", text)
        text = re.sub(r"`.*?`", "コード省略", text)
        text = re.sub(r"[wWｗＷ]{3,}", "わら", text)
        text = re.sub(r"[!！?？]{3,}", "！", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 80:
            text = text[:80] + "、以下略"
        return text

    def _clean_tts_text(self, message: discord.Message) -> str:
        """チャット自動読み上げ用テキストの整形・安全対策"""
        text = message.clean_content or message.content or ""
        if not text.strip() and message.attachments:
            if any(a.content_type and "image" in a.content_type for a in message.attachments):
                return "画像"
            return "ファイル添付"

        # URL置換
        text = re.sub(r"https?://\S+", "URL省略", text)
        # メンション置換
        text = re.sub(r"<@!?\d+>", "メンション", text)
        text = re.sub(r"<@&\d+>", "ロール", text)
        text = re.sub(r"<#\d+>", "チャンネル", text)
        text = text.replace("@everyone", "エブリワン").replace("@here", "ヒア")
        # 絵文字除去
        text = re.sub(r"<a?:\w+:\d+>", "", text)
        # コードブロック
        text = re.sub(r"```[\s\S]*?```", "コード省略", text)
        text = re.sub(r"`.*?`", "コード省略", text)
        # スポイラー
        text = re.sub(r"\|\|.*?\|\|", "ネタバレ", text)
        # 連続記号
        text = re.sub(r"[wWｗＷ]{3,}", "わら", text)
        text = re.sub(r"[笑]{2,}", "わら", text)
        text = re.sub(r"[!！?？]{3,}", "！", text)

        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 80:
            text = text[:80] + "、以下略"

        if not text and message.attachments:
            return "ファイル添付"
        return text

    async def _enqueue_tts(self, guild: discord.Guild, text: str):
        """TTS読み上げキューにテキストを追加し、ワーカーを起動する。"""
        if not gTTS or not text:
            return
        q = self.tts_queues.setdefault(guild.id, asyncio.Queue())
        if q.qsize() >= self.tts_max_queue:
            try:
                q.get_nowait()
            except (asyncio.QueueEmpty, ValueError):
                pass
        await q.put(text)

        task = self.tts_tasks.get(guild.id)
        if task is None or task.done():
            self.tts_tasks[guild.id] = asyncio.create_task(self._tts_worker(guild))

    async def _tts_worker(self, guild: discord.Guild):
        """TTSキューからメッセージを順次取り出し、VCで音声再生する。"""
        q = self.tts_queues.get(guild.id)
        if not q:
            return
        try:
            while True:
                try:
                    text = await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    break

                vc = guild.voice_client
                if not vc or not vc.is_connected():
                    break

                # 音声ファイル生成 (非同期実行)
                def _generate():
                    tts = gTTS(text=text, lang="ja")
                    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
                    tts.write_to_fp(tmp)
                    name = tmp.name
                    tmp.close()
                    return name

                try:
                    tmp_path = await asyncio.to_thread(_generate)
                except Exception as e:
                    log.warning("TTS音声の生成に失敗しました: %s", e)
                    q.task_done()
                    continue

                done_event = asyncio.Event()

                def _after(err):
                    if err:
                        log.warning("TTS再生エラー: %s", err)
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
                    self.bot.loop.call_soon_threadsafe(done_event.set)

                # 音楽再生中の場合は一時停止して割り込み
                was_music_playing = vc.is_playing()
                if was_music_playing:
                    vc.pause()

                try:
                    source = discord.FFmpegPCMAudio(tmp_path, executable=FFMPEG_EXE, options="-vn")
                    volume_source = discord.PCMVolumeTransformer(source, volume=0.85)
                    vc.play(volume_source, after=_after)
                    await done_event.wait()
                except Exception as e:
                    log.warning("TTS再生中に例外が発生しました: %s", e)
                    _after(None)
                finally:
                    # 音楽が一時停止されていた場合は再開
                    if was_music_playing and vc.is_connected() and vc.is_paused():
                        vc.resume()
                    q.task_done()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("TTSワーカーでエラーが発生しました (guild=%s): %s", guild.id, e)
        finally:
            self.tts_tasks.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """読み上げチャンネルのチャットをVCで自動読み上げする。"""
        if not message.guild or message.author.bot:
            return
        guild_id = message.guild.id
        target_ch_id = self.tts_channels.get(guild_id)
        if not target_ch_id or message.channel.id != target_ch_id:
            return

        content = message.content.strip()
        if not content and not message.attachments:
            return

        # コマンドプレフィックスで始まるものは読み上げない
        prefixes = ("r!", "R!", "!", "/", "?", ";", ".", "$")
        if any(content.startswith(p) for p in prefixes):
            return

        vc = message.guild.voice_client
        if not vc or not vc.is_connected():
            self.tts_channels.pop(guild_id, None)
            return

        clean_text = self._clean_tts_text(message)
        if clean_text:
            await self._enqueue_tts(message.guild, clean_text)

    @commands.cooldown(2, 5.0, commands.BucketType.user)
    @commands.command(name="tts", aliases=["TTS", "yomiage", "読み上げ"])
    async def tts_cmd(self, ctx: commands.Context, *, args: str = ""):
        """チャットの読み上げ、または指定テキストを読み上げます。
        ・R!tts : チャット自動読み上げを開始/終了（トグル）
        ・R!tts <文章> : 入力したテキストをVCで読み上げ
        ・R!tts stop / 切断 : 読み上げを終了して退出
        """
        if not gTTS:
            raise MusicError("読み上げ機能（gTTS）が利用できません。")

        args = args.strip()

        # 停止・終了
        if args.lower() in ("stop", "leave", "end", "off", "終了", "切断", "停止", "bye"):
            self.tts_channels.pop(ctx.guild.id, None)
            q = self.tts_queues.get(ctx.guild.id)
            if q:
                while not q.empty():
                    try:
                        q.get_nowait()
                        q.task_done()
                    except (asyncio.QueueEmpty, ValueError):
                        break
            vc = ctx.guild.voice_client
            if vc and ctx.guild.id not in self.players:
                await vc.disconnect(force=True)
                await self.say(ctx, "読み上げを終了し、ボイスチャンネルから退出しました。")
            else:
                await self.say(ctx, "チャット読み上げを終了しました。")
            return

        # ステータス確認
        if args.lower() in ("status", "state", "ステータス", "状態"):
            is_active = ctx.guild.id in self.tts_channels
            target_ch = self.bot.get_channel(self.tts_channels.get(ctx.guild.id, 0))
            vc = ctx.guild.voice_client
            lines = [
                f"読み上げ状態: **{'有効 (読み上げ中)' if is_active else '無効'}**",
                f"読み上げチャンネル: {target_ch.mention if target_ch else '未設定'}",
                f"接続中VC: **{vc.channel.name if vc and vc.channel else '未接続'}**",
            ]
            await self.say(ctx, "\n".join(lines), title="読み上げ (TTS) ステータス")
            return

        # VC接続の確認と自動接続
        voice = ctx.author.voice
        if voice is None or voice.channel is None:
            raise MusicError("先にボイスチャンネルに入ってください。")
        channel = voice.channel

        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            missing = await self.missing_perms(ctx.guild, channel)
            if missing:
                raise MusicError(_permission_help(channel, missing))
            await self._open_voice(ctx, channel)
            vc = ctx.guild.voice_client
        elif vc.channel.id != channel.id:
            if not any(not m.bot for m in vc.channel.members):
                await vc.move_to(channel)
            else:
                raise MusicError(f"いま別のVC（{vc.channel.name}）で使用中です。")

        # 1. テキスト指定のワンショット読み上げ
        if args:
            clean_text = self._clean_text_simple(args)
            if not clean_text:
                raise MusicError("読み上げるテキストを入力してください。")
            await self._enqueue_tts(ctx.guild, clean_text)
            try:
                await ctx.message.add_reaction("📢")
            except (discord.HTTPException, discord.Forbidden):
                pass
            return

        # 2. 引数なし: チャット読み上げのトグル（開始 / 終了）
        current_target = self.tts_channels.get(ctx.guild.id)
        if current_target == ctx.channel.id:
            self.tts_channels.pop(ctx.guild.id, None)
            await self.say(
                ctx,
                "このチャンネルのチャット読み上げを終了しました。\n"
                "VCから退出する場合は `R!tts stop` または `R!leave` と送信してください。",
                title="読み上げ (TTS) 終了"
            )
            return

        self.tts_channels[ctx.guild.id] = ctx.channel.id
        await self.say(
            ctx,
            f"チャット読み上げを開始しました。\n"
            f"・対象チャンネル: {ctx.channel.mention}\n"
            f"・接続VC: **{channel.name}**\n"
            f"終了するには `R!tts` または `R!tts stop` と送信してください。",
            title="読み上げ (TTS) 開始"
        )
        await self._enqueue_tts(ctx.guild, "読み上げを開始しました")

    # ----------------------------------------------------------------------- #
    # r!edit  —  YouTube Edit版検索
    # ----------------------------------------------------------------------- #

    @commands.cooldown(3, 15.0, commands.BucketType.user)
    @commands.command(name="edit", aliases=["Edit", "エディット"])
    async def edit_cmd(self, ctx: commands.Context, *, query: str = ""):
        """曲名/URL/SpotifyリンクからYouTubeのEdit版（sped up, slowed reverb, phonk edit等）を検索します。
        例: R!edit Homage Funk
        例: R!edit https://open.spotify.com/track/...
        """
        if not query:
            raise MusicError(
                "曲名、URL、SpotifyリンクなどをR!editの後に入力してください。\n"
                "例: `R!edit Homage Funk`\n"
                "例: `R!edit https://open.spotify.com/track/...`"
            )

        async with ui.typing(ctx):
            # SpotifyなどDRM系リンクは曲名を解決してから検索
            base_query = query.strip()
            if _METADATA_ONLY.match(base_query):
                phrase = await describe_link(base_query)
                if not phrase:
                    raise MusicError(
                        "リンクから曲名を取得できませんでした。\n"
                        "曲名を直接入力してください: `R!edit 曲名 アーティスト名`"
                    )
                base_query = phrase
                await self.say(ctx, f"Spotifyリンクから曲名を特定しました: **{ui.clip(base_query, 60)}**")

            # Edit系キーワードリスト（人気・代表的なもの）
            _EDIT_KEYWORDS = [
                "edit",
                "sped up",
                "slowed reverb",
                "slowed",
                "phonk",
                "nightcore",
            ]

            # 各キーワードで並列検索し、まとめてリストアップ
            seen_ids: set[str] = set()
            all_tracks: list[Track] = []

            async def _fetch_one(kw: str):
                q = f"ytsearch5:{base_query} {kw}"
                try:
                    info = await _run_ydl(q, _YDL_LIST)
                    entries = info.get("entries") or [] if info else []
                    for e in entries:
                        if not e:
                            continue
                        t = _to_track(e, ctx.author.id)
                        if not t:
                            continue
                        vid = _extract_video_id(t.url)
                        key = vid or t.url
                        if key in seen_ids:
                            continue
                        seen_ids.add(key)
                        # 極端に短い動画（10秒未満）や長すぎるもの（15分超）を除外
                        if t.duration is not None and (t.duration < 10 or t.duration > 900):
                            continue
                        all_tracks.append(t)
                except Exception as e:
                    log.debug("edit検索失敗 (kw=%s): %s", kw, e)

            # 最初の3キーワードを並列に取得（レートリミット対策で全部は並列にしない）
            primary_kws = _EDIT_KEYWORDS[:3]
            await asyncio.gather(*[_fetch_one(kw) for kw in primary_kws])

            # 候補が少なければ残りのキーワードでも補完
            if len(all_tracks) < 5:
                for kw in _EDIT_KEYWORDS[3:]:
                    await _fetch_one(kw)
                    if len(all_tracks) >= 8:
                        break

        if not all_tracks:
            raise MusicError(
                f"「{ui.clip(base_query, 50)}」のEdit版が見つかりませんでした。\n"
                "別のキーワードで試してみてください。"
            )

        # 最大8件に絞って選択式で表示
        result_tracks = all_tracks[:8]
        lines = [
            f"`{i + 1}.` {t.label()} `[{duration(t.duration)}]`"
            for i, t in enumerate(result_tracks)
        ]
        view = SearchView(self, ctx, result_tracks)
        body = ui.embed(
            "\n".join(lines),
            f"「{ui.clip(base_query, 40)}」のEdit版 検索結果",
            COLOR_MUSIC,
        )
        try:
            view.message = await ctx.reply(**ui.payload(ctx.channel, body),
                                           view=view, mention_author=False)
        except ui.SEND_ERRORS as e:
            log.warning("edit検索結果を送れませんでした: %s", ui.short_error(e))

    @commands.command(name="skip", aliases=["s", "next"])
    async def skip(self, ctx: commands.Context):
        """いまの曲を飛ばします。"""
        skipped = self.player(ctx).skip()
        if skipped is None:
            raise MusicError("再生中ではありません。")
        await self.say(ctx, f"スキップしました: {skipped.label()}")

    @commands.command(name="stop")
    async def stop(self, ctx: commands.Context):
        """再生を止めてキューを空にします (退出はしません)。"""
        self.player(ctx).halt()
        await self.say(ctx, "再生を停止してキューを空にしました。"
                            "退出させるには R!leave を使ってください。")

    @commands.command(name="leave", aliases=["disconnect", "dc", "bye"])
    async def leave(self, ctx: commands.Context):
        """ボイスチャンネルから退出します。"""
        player = self.players.get(ctx.guild.id)
        if player:
            await player.stop()
        elif ctx.guild.voice_client:
            await ctx.guild.voice_client.disconnect(force=True)
        else:
            raise MusicError("ボイスチャンネルに参加していません。")
        await self.say(ctx, "退出しました。")

    @commands.command(name="pause")
    async def pause(self, ctx: commands.Context):
        """一時停止します。"""
        if not self.player(ctx).pause():
            raise MusicError("再生中ではありません。")
        await self.say(ctx, "一時停止しました。R!resume で再開します。")

    @commands.command(name="resume", aliases=["unpause"])
    async def resume(self, ctx: commands.Context):
        """一時停止を解除します。"""
        if not self.player(ctx).resume():
            raise MusicError("一時停止していません。")
        await self.say(ctx, "再生を再開しました。")

    @commands.command(name="volume", aliases=["vol"])
    async def volume(self, ctx: commands.Context, value: str = ""):
        """音量を 0〜200 で設定します。"""
        player = self.player(ctx)
        if not value:
            await self.say(ctx, f"いまの音量は {round(player.volume * 100)} です。")
            return
        try:
            percent = float(value.rstrip("%"))
        except ValueError:
            raise MusicError("音量は 0 から 200 までの数字で指定してください。") from None
        if not 0 <= percent <= MAX_VOLUME * 100:
            raise MusicError(f"音量は 0 から {int(MAX_VOLUME * 100)} までです。")
        player.set_volume(percent / 100)
        await self.say(ctx, f"音量を {round(percent)} にしました。")

    @commands.command(name="seek")
    async def seek(self, ctx: commands.Context, position: str = ""):
        """再生位置を移動します (例: R!seek 1:30)。"""
        player = self.player(ctx)
        if not position:
            raise MusicError("移動先を指定してください (例: R!seek 1:30)。")
        seconds = _parse_time(position)
        track = player.current
        if track and track.duration and seconds >= track.duration:
            raise MusicError(f"この曲は {duration(track.duration)} までです。")
        if not player.seek(seconds):
            raise MusicError("再生中ではありません。")
        await self.say(ctx, f"{duration(seconds)} から再生します。")

    # -- the queue ---------------------------------------------------------- #

    @commands.command(name="queue", aliases=["q", "list"])
    async def queue(self, ctx: commands.Context, page: int = 1):
        """キューを表示します。"""
        player = self.player(ctx)
        lines = []
        if player.current:
            lines.append("**再生中**")
            lines.append(f"{player.current.label(80)} "
                         f"`[{duration(player.current.duration)}]`")
            lines.append("")
        if not player.queue:
            lines.append("キューは空です。")
        else:
            per_page = 10
            pages = max(1, -(-len(player.queue) // per_page))
            page = max(1, min(page, pages))
            start = (page - 1) * per_page
            lines.append(f"**キュー {len(player.queue)} 曲** "
                         f"(ページ {page}/{pages})")
            for offset, track in enumerate(
                    list(player.queue)[start:start + per_page], start + 1):
                lines.append(f"`{offset}.` {track.label()} "
                             f"`[{duration(track.duration)}]`")
            total = player.total_duration()
            if total:
                lines.append(f"\n合計 {duration(total)}")
        if player.loop_mode != "off":
            lines.append(f"リピート: {player.loop_mode}")
        await self.say(ctx, "\n".join(lines), "キュー")

    @commands.command(name="np", aliases=["nowplaying", "now"])
    async def now_playing(self, ctx: commands.Context):
        """いま再生している曲を表示します。"""
        player = self.player(ctx)
        track = player.current
        if track is None:
            raise MusicError("いまは何も再生していません。")
        who = ctx.guild.get_member(track.requester)
        lines = [f"**{track.label(90)}**",
                 f"`{progress_bar(player.elapsed(), track.duration)}`"]
        if track.uploader:
            lines.append(f"投稿者: {discord.utils.escape_markdown(track.uploader)}")
        if who:
            lines.append(f"リクエスト: {who.display_name}")
        lines.append(f"音量: {round(player.volume * 100)} / リピート: {player.loop_mode}")
        vc = player.voice
        if vc and vc.channel:
            # Shown because it is the one quality setting that is not ours to
            # choose: it follows the channel, and raising it is a server setting.
            lines.append(f"音質: {encoder_options(vc.channel)['bitrate']} kbps "
                         f"({vc.channel.name})")
        lines.append(f"<{track.url}>")
        await self.say(ctx, "\n".join(lines),
                       "一時停止中" if player.voice and player.voice.is_paused()
                       else "再生中",
                       thumbnail_url=track.thumbnail)

    @commands.command(name="loop", aliases=["repeat"])
    async def loop(self, ctx: commands.Context, mode: str = ""):
        """リピートを off / one / all で切り替えます。"""
        player = self.player(ctx)
        mode = mode.strip().lower()
        if not mode:
            # No argument cycles, because that is what people expect of a button
            # they press repeatedly.
            order = ["off", "one", "all"]
            mode = order[(order.index(player.loop_mode) + 1) % len(order)]
        aliases = {"off": "off", "none": "off", "no": "off",
                   "one": "one", "single": "one", "track": "one", "song": "one",
                   "all": "all", "queue": "all", "q": "all"}
        if mode not in aliases:
            raise MusicError("リピートは off / one / all のどれかです。")
        player.loop_mode = aliases[mode]
        await self.say(ctx, {
            "off": "リピートを解除しました。",
            "one": "いまの曲をリピートします。",
            "all": "キュー全体をリピートします。",
        }[player.loop_mode])

    @commands.command(name="shuffle")
    async def shuffle(self, ctx: commands.Context):
        """キューをシャッフルします。"""
        player = self.player(ctx)
        if len(player.queue) < 2:
            raise MusicError("シャッフルするほど曲がありません。")
        order = list(player.queue)
        random.shuffle(order)
        player.queue = deque(order)
        await self.say(ctx, f"{len(order)} 曲をシャッフルしました。")

    @commands.command(name="remove", aliases=["rm"])
    async def remove(self, ctx: commands.Context, index: int = 0):
        """キューの n 番目を削除します。"""
        player = self.player(ctx)
        if not player.queue:
            raise MusicError("キューは空です。")
        if not 1 <= index <= len(player.queue):
            raise MusicError(f"1 から {len(player.queue)} までの番号を指定してください。"
                             "番号は R!queue で確認できます。")
        track = player.queue[index - 1]
        del player.queue[index - 1]
        await self.say(ctx, f"削除しました: {track.label()}")

    @commands.command(name="clear")
    async def clear(self, ctx: commands.Context):
        """キューを空にします (再生中の曲はそのまま)。"""
        player = self.player(ctx)
        count = len(player.queue)
        player.queue.clear()
        await self.say(ctx, f"キューの {count} 曲を削除しました。")

    # -- housekeeping -------------------------------------------------------- #

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState,
                                    after: discord.VoiceState):
        """Leave once the last listener does, and clean up if we are disconnected.

        Not immediately: people hop channels, and a bot that hangs up the instant
        a channel empties loses the queue to a five-second absence.
        """
        guild = member.guild
        player = self.players.get(guild.id)
        is_tts = guild.id in self.tts_channels
        if player is None and not is_tts and guild.voice_client is None:
            return

        if member.id == self.bot.user.id and after.channel is None:
            # 切断された場合はTTSも解除
            self.tts_channels.pop(guild.id, None)
            if player:
                await player.stop()
            return

        vc = guild.voice_client
        if vc is None or vc.channel is None:
            self.tts_channels.pop(guild.id, None)
            return
        listeners = [m for m in vc.channel.members if not m.bot]
        existing = self._alone.pop(guild.id, None)
        if existing:
            existing.cancel()
        if not listeners:
            self._alone[guild.id] = asyncio.create_task(self._leave_if_alone(guild.id))

    async def _leave_if_alone(self, guild_id: int):
        try:
            await asyncio.sleep(ALONE_TIMEOUT)
            guild = self.bot.get_guild(guild_id)
            vc = guild.voice_client if guild else None
            if vc and vc.channel and not any(not m.bot for m in vc.channel.members):
                self.tts_channels.pop(guild_id, None)
                player = self.players.get(guild_id)
                if player:
                    await player._say("誰もいなくなったので退出します。")
                    await player.stop()
                else:
                    await vc.disconnect(force=True)
        except asyncio.CancelledError:
            pass
        finally:
            self._alone.pop(guild_id, None)


HELP_LINES = [
    ("R!play <URL|検索語>", "再生、またはキューに追加します"),
    ("R!edit <曲名|URL>", "Edit版（sped up / slowed等）をYouTubeで検索して再生します"),
    ("R!plist <曲名|ジャンル> [数]", "指定曲に似た曲（類似楽曲）や人気曲をまとめて再生します"),
    ("R!tts [テキスト]", "チャット読み上げを開始/終了、またはテキストを読み上げます"),
    ("R!search <検索語>", "検索結果から選んで再生します"),
    ("R!skip", "いまの曲を飛ばします"),
    ("R!pause / R!resume", "一時停止と再開"),
    ("R!seek <1:30>", "再生位置を移動します"),
    ("R!queue [ページ]", "キューを表示します"),
    ("R!np", "いまの曲と再生位置を表示します"),
    ("R!loop [off|one|all]", "リピートを切り替えます"),
    ("R!shuffle", "キューをシャッフルします"),
    ("R!remove <番号> / R!clear", "キューから削除します"),
    ("R!volume <0-200>", "音量を変えます"),
    ("R!stop / R!leave", "停止、または退出します"),
    ("R!perms", "Botに見えている権限を確認します"),
]


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))


# --------------------------------------------------------------------------- #
# Voice Environment Diagnostics (Integrated from voice_check)
# --------------------------------------------------------------------------- #

STUN_HOST, STUN_PORT = "stun.l.google.com", 19302
DNS_HOST, DNS_PORT = "8.8.8.8", 53


def _diag_line(label: str, ok: bool | None, detail: str = ""):
    mark = {True: "OK  ", False: "NG  ", None: "--  "}[ok]
    print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))


def check_libraries() -> bool:
    print("\n1. ライブラリ")
    ok = True
    try:
        import discord
        _diag_line(f"discord.py {discord.__version__}", True)
    except ImportError as e:
        _diag_line("discord.py", False, str(e))
        return False
    try:
        import nacl.secret  # noqa: F401
        _diag_line("PyNaCl (音声の暗号化)", True)
    except ImportError:
        _diag_line("PyNaCl (音声の暗号化)", False, "pip install PyNaCl  ← 音声接続に必須です")
        ok = False
    try:
        import yt_dlp
        _diag_line(f"yt-dlp {yt_dlp.version.__version__}", True)
    except ImportError:
        _diag_line("yt-dlp", False, "pip install yt-dlp")
        ok = False

    _diag_line("libopus (音声の符号化)", OPUS_READY, "" if OPUS_READY else "apt install libopus0")
    _diag_line("ffmpeg", FFMPEG_EXE is not None, FFMPEG_EXE or "見つかりません")
    return ok and OPUS_READY and FFMPEG_EXE is not None


def _udp_probe(host: str, port: int, payload: bytes, timeout: float = 5.0) -> bool:
    try:
        addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
    except socket.gaierror:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(payload, addr)
        sock.recvfrom(2048)
        return True
    except (socket.timeout, OSError):
        return False
    finally:
        sock.close()


def _tcp_probe(host: str, port: int, timeout: float = 6.0) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def check_tcp() -> bool:
    print("\n2. TCP通信 (音声の制御に使います)")
    ok_443 = _tcp_probe("cloudflare.com", 443)
    _diag_line("TCP cloudflare.com:443", ok_443)
    ok_2096 = _tcp_probe("cloudflare.com", 2096)
    _diag_line("TCP cloudflare.com:2096 (音声と同じポート)", ok_2096)
    return ok_2096


def check_udp() -> bool:
    print("\n3. UDP通信 (音声データはこれで流れます)")
    dns = (struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
           + b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1))
    basic = _udp_probe(DNS_HOST, DNS_PORT, dns)
    _diag_line(f"UDP {DNS_HOST}:{DNS_PORT} (DNS)", basic)

    stun = struct.pack(">HHI12s", 0x0001, 0, 0x2112A442, os.urandom(12))
    high = _udp_probe(STUN_HOST, STUN_PORT, stun)
    _diag_line(f"UDP {STUN_HOST}:{STUN_PORT} (高ポート)", high)
    return high


def run_voice_diagnostics() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    print("=" * 60)
    print("音声機能の診断")
    print("=" * 60)
    libs = check_libraries()
    tcp = check_tcp()
    udp = check_udp()

    print("\n4. 環境")
    _diag_line("FFMPEG_PATH", None, os.environ.get("FFMPEG_PATH", "(未設定)"))
    _diag_line("MUSIC_VOICE_PORT", None, os.environ.get("MUSIC_VOICE_PORT", "(未設定)"))
    _diag_line("MUSIC_VOLUME", None, os.environ.get("MUSIC_VOLUME", "(未設定 = 1.0)"))
    print("\n" + "=" * 60)
    if not libs:
        print("結論: 不足しているライブラリがあります。上の NG を確認してください。")
    elif not tcp:
        print("結論: 音声用ポート (TCP 2096) が塞がれています。MUSIC_VOICE_PORT=443 をお試しください。")
    elif not udp:
        print("結論: このホストはUDPを通していません。UDP通信が許可された環境が必要です。")
    else:
        print("結論: 音声通信の前提条件を満たしています。")
    print("=" * 60)
    return 0


# 後方互換性
sys.modules["voice_check"] = sys.modules[__name__]

if __name__ == "__main__":
    sys.exit(run_voice_diagnostics())

