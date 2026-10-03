"""Check links and attachments posted in chat for the shapes an attack takes.

The base layer is offline: string and structure checks, no lookups against a
threat feed. That limit is worth stating plainly — it cannot know that a
brand-new domain is malicious, only that a link is built like the ones that
are. In exchange it costs nothing, works on a host whose DNS is unreliable, and
never leaves the machine.

VirusTotal is layered on top when a key is configured, and only ever adds to
that base. It is the opposite trade: a real multi-engine verdict, at the cost of
sending the URL to a third party — and useless against exactly the case the
offline checks are for, since a domain registered this morning to imitate
Discord is unknown to every scanner precisely because it is new.

The switch is ENABLED, immediately below.
"""
import asyncio
import base64
import dataclasses
import hashlib
import ipaddress
import json
import logging
import os
import re
import time
import unicodedata
from urllib.parse import unquote, urlsplit

import aiohttp

import ui

log = logging.getLogger("rula.linkguard")

# ---------------------------------------------------------------------------
# The switch. Set to False to turn link checking off completely; nothing else
# needs changing. LINKGUARD=off in the environment overrides it without an edit.
# ---------------------------------------------------------------------------
ENABLED = True

# What to do about a link that looks dangerous.
#   "warn"   — reply in the channel saying why. The default.
#   "log"    — record it and say nothing, for watching before trusting.
#   "delete" — remove the message and say why. Needs Manage Messages, and is
#              off by default: these are heuristics, and a wrong deletion of
#              someone's message costs more than a wrong warning.
ACTION = "warn"

# Warn about links that are merely odd, not just ones built like an attack.
# Off by default: a checker that cries about every shortened link is one people
# learn to ignore, and an ignored warning is worse than none.
WARN_ON_SUSPICIOUS = False


# ---------------------------------------------------------------------------
# VirusTotal. Off unless VIRUSTOTAL_API_KEY is set, and set to False here to
# disable it even when a key is present.
#
# Worth understanding before turning it on: a lookup sends the URL to
# VirusTotal. The address someone posted in this server is shared with a third
# party, and VirusTotal keeps what it is sent. That is the trade — a real
# multi-engine verdict in exchange for the link no longer being private — and it
# is why this needs a key rather than working out of the box.
#
# The offline checks run either way. This only ever adds to them, so a missing
# key, a rate limit, or the API being unreachable costs the extra opinion and
# nothing else.
# ---------------------------------------------------------------------------
VIRUSTOTAL = True

# How many engines must call a URL malicious before it is reported as dangerous.
# One is often a false positive; two agreeing rarely is.
VT_MALICIOUS_FOR_DANGER = 2
# Submit unknown URLs to be scanned, rather than only reading existing reports.
# Off by default: submitting publishes the URL to VirusTotal's corpus, which is
# a much larger step than looking one up, and it spends the daily quota fast.
VT_SUBMIT = False

VT_API = "https://www.virustotal.com/api/v3"
VT_TIMEOUT = 6.0          # a warning that arrives a minute late is not a warning
VT_CACHE_TTL = 3600       # a verdict on a URL does not change by the minute
VT_CACHE_MAX = 512
# The free tier allows four requests a minute. Tracked as a sliding window and
# skipped rather than queued when it is full: waiting out a rate limit would
# stall the message handler for a link the offline checks have already judged.
VT_PER_MINUTE = 4
# At most this many lookups for one message, so a wall of links cannot spend the
# entire minute's allowance in one go.
VT_MAX_PER_MESSAGE = 2


def _flag(name: str, fallback: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("1", "true", "on", "yes"):
        return True
    if raw in ("0", "false", "off", "no"):
        return False
    return fallback


def reload_settings():
    """Re-read the environment, and again once .env has been merged in.

    This module is imported before main() reads .env, so the values taken at
    import come from the real environment alone — LINKGUARD=off written in .env
    would have been read too early and silently ignored, which is the same trap
    the API base URL was documented for. Called a second time after the file is
    loaded, so both places work.
    """
    global ENABLED, WARN_ON_SUSPICIOUS, ACTION
    ENABLED = _flag("LINKGUARD", _DEFAULTS["enabled"])
    WARN_ON_SUSPICIOUS = _flag("LINKGUARD_SUSPICIOUS", _DEFAULTS["suspicious"])
    ACTION = (os.environ.get("LINKGUARD_ACTION", "").strip().lower()
              or _DEFAULTS["action"])
    if ACTION not in ("warn", "log", "delete"):
        log.warning("LINKGUARD_ACTION は warn / log / delete のいずれかです"
                    "(受け取った値: %s)。warn を使います。", ACTION)
        ACTION = "warn"


# The values written at the top of this file, kept so the environment can be
# re-read without them having been overwritten by the first pass.
_DEFAULTS = {"enabled": ENABLED, "suspicious": WARN_ON_SUSPICIOUS,
             "action": ACTION}
reload_settings()

DANGEROUS = "dangerous"
SUSPICIOUS = "suspicious"
SAFE = "safe"

# Registrable domains that are the real thing. Checked before any lookalike
# test, so that Discord's own several domains do not get reported as imitations
# of each other.
SAFE_DOMAINS = frozenset("""
discord.com discord.gg discordapp.com discordapp.net discordstatus.com
discord.media discord.co discordcdn.com discordmerch.com
youtube.com youtu.be google.com google.co.jp gstatic.com googleusercontent.com
googlevideo.com ytimg.com
twitter.com x.com t.co fxtwitter.com vxtwitter.com
github.com githubusercontent.com github.io gitlab.com
spotify.com apple.com icloud.com microsoft.com live.com office.com
amazon.co.jp amazon.com steampowered.com steamcommunity.com
steamstatic.com steamgames.com steamdeck.com
roblox.com rbxcdn.com roblox.io
minecraft.net minecraftservices.com mojang.com
nintendo.co.jp nintendo.com playstation.com xbox.com
wikipedia.org twitch.tv reddit.com nicovideo.jp pixiv.net
soundcloud.com bandcamp.com deezer.com tidal.com
paypal.com stripe.com line.me yahoo.co.jp
""".split())

# Names worth impersonating. A host that contains one of these but does not
# belong to it is the single strongest signal available offline.
BRANDS = ("discord", "steam", "youtube", "google", "paypal", "amazon",
          "apple", "microsoft", "nintendo", "playstation", "twitch",
          "spotify", "github", "nicovideo", "pixiv", "roblox", "minecraft")

# Free registries and file-shaped TLDs, both heavily used for throwaway
# phishing. .zip and .mov matter because a "document.zip" link reads as a file.
RISKY_TLDS = frozenset("""
tk ml ga cf gq zip mov cam click link work fit rest surf quest
""".split())

# Extensions nobody sends as a normal chat link.
EXECUTABLE = frozenset("""
exe scr bat cmd com msi apk jar vbs vbe js ps1 dll lnk iso img hta reg
""".split())

SHORTENERS = frozenset("""
bit.ly tinyurl.com goo.gl t.co ow.ly is.gd buff.ly cutt.ly rb.gy
shorturl.at rebrand.ly bl.ink t.ly x.gd urlz.fr v.gd tiny.cc
""".split())

# Words that turn up in the pitch rather than in the site's real name.
BAIT = ("nitro", "free", "gift", "giveaway", "airdrop", "claim", "reward",
        "bonus", "verify", "login", "signin", "unlock", "prize", "winner")

# Multi-label public suffixes common enough to matter here. Not the full Public
# Suffix List — that is a downloaded file that would go stale — but enough that
# co.jp and co.uk are not mistaken for registrable domains.
_MULTI_SUFFIX = frozenset("""
co.jp ne.jp or.jp ac.jp go.jp ed.jp lg.jp co.uk org.uk ac.uk gov.uk
com.au net.au org.au com.br com.cn com.tw co.kr co.nz com.mx com.sg
""".split())

# Characters that end a URL because the sentence has resumed: CJK punctuation,
# kana, kanji and full-width forms. Built from codepoints so the source stays
# ASCII. Without them "ここ(https://example.com/x)です" takes the closing bracket
# and the two kana after it as part of the path, and every check downstream is
# then looking at a hostname that was never posted.
_SENTENCE = "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in (
    (0x3000, 0x303F),   # 、。「」etc
    (0x3040, 0x30FF),   # hiragana and katakana
    (0x4E00, 0x9FFF),   # kanji
    (0xFF00, 0xFFEF),   # full-width forms
))
# Matches a bare URL. ASCII brackets are trimmed afterwards rather than excluded
# here, because a URL may legitimately end in one.
_URL_RE = re.compile(rf"https?://[^\s<>\"'`|\\^{{}}{_SENTENCE}]+", re.I)
# [label](target) — the shape a link takes when the words and the destination
# are allowed to disagree.
_MARKDOWN_RE = re.compile(r"\[([^\]]{1,200})\]\(\s*(https?://[^\s)]+)\s*\)", re.I)


@dataclasses.dataclass
class Verdict:
    url: str
    level: str                       # SAFE | SUSPICIOUS | DANGEROUS
    reasons: list[str] = dataclasses.field(default_factory=list)

    @property
    def bad(self) -> bool:
        return self.level == DANGEROUS

    @property
    def worth_saying(self) -> bool:
        return self.bad or (self.level == SUSPICIOUS and WARN_ON_SUSPICIOUS)


def registrable(host: str) -> str:
    """example.co.jp from www.shop.example.co.jp, near enough.

    Approximate by design: the exact answer needs the Public Suffix List, which
    is a file that has to be fetched and kept current. The pairs that actually
    come up in Japanese and English servers are listed in _MULTI_SUFFIX, and
    everything else is treated as a two-label domain.
    """
    labels = host.lower().strip(".").split(".")
    if len(labels) < 2:
        return host.lower()
    if ".".join(labels[-2:]) in _MULTI_SUFFIX and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _normalize_lookalike(s: str) -> str:
    """Normalize common character-substitution tricks used in typosquatting.

    Attackers replace character sequences that look identical or nearly identical:
    rn → m  (rn looks like m in sans-serif)
    cl → d  (cl looks like d)
    vv → w  (vv looks like w)
    0  → o  (zero for letter)
    1  → l  (one for ell)
    3  → e  (three for e)
    5  → s  (five for s)
    """
    s = s.lower()
    # multi-char substitutions first so single-char ones do not break them
    for old, new in (("rn", "m"), ("cl", "d"), ("vv", "w")):
        s = s.replace(old, new)
    for old, new in (("1", "l"), ("0", "o"), ("3", "e"), ("5", "s")):
        s = s.replace(old, new)
    return s


def _edit_distance(a: str, b: str, limit: int = 3) -> int:
    """Levenshtein, stopped early once it cannot matter."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _mixed_scripts(host: str) -> bool:
    """Whether the host mixes alphabets, which ordinary domains do not.

    A Cyrillic 'а' among Latin letters is invisible in a proxy and is the whole
    trick behind a homograph domain.
    """
    scripts = set()
    for ch in host:
        if ch.isalpha():
            name = unicodedata.name(ch, "")
            for script in ("LATIN", "CYRILLIC", "GREEK", "ARMENIAN", "HEBREW"):
                if name.startswith(script):
                    scripts.add(script)
                    break
    return len(scripts) > 1


def inspect(url: str, *, label: str | None = None) -> Verdict:
    """Judge one link. `label` is the text it was hidden behind, if any."""
    verdict = Verdict(url=url, level=SAFE)
    try:
        parts = urlsplit(url)
    except ValueError:
        verdict.level = SUSPICIOUS
        verdict.reasons.append("URLとして解釈できません")
        return verdict

    raw_host = parts.hostname or ""
    if not raw_host:
        verdict.level = SUSPICIOUS
        verdict.reasons.append("ホスト名がありません")
        return verdict

    host = raw_host.lower()
    domain = registrable(host)
    known_good = domain in SAFE_DOMAINS
    danger: list[str] = []
    odd: list[str] = []

    # Everything before an @ is credentials, and browsers show the part after
    # it. "https://discord.com@evil.example" goes to evil.example.
    if "@" in parts.netloc:
        danger.append("`@` を使って本当の行き先を隠しています")

    # Punycode and mixed alphabets: two ways to spell a name that reads as a
    # brand but is not one.
    if host.startswith("xn--") or ".xn--" in host:
        danger.append("国際化ドメイン(punycode)で、見た目を偽装できます")
    if _mixed_scripts(raw_host):
        danger.append("複数の文字体系が混ざっています(なりすましの手口です)")

    try:
        ipaddress.ip_address(host)
        odd.append("ドメイン名ではなくIPアドレスを直接指しています")
    except ValueError:
        pass

    if not known_good:
        # Split host into individual labels, stripping hyphens and underscores,
        # so that brand checks apply per-label rather than per-character.
        # This stops "snapple.com" (apple is a suffix, not a prefix/label) and
        # "pineapple.com" from being flagged, while still catching
        # "apple-gifts.com" (first label starts with "apple") and
        # "discord-gifts.com" (first label starts with "discord").
        host_labels_clean = [re.sub(r"[-_]", "", lbl)
                             for lbl in host.split(".")]
        for brand in BRANDS:
            if any(lbl == brand or lbl.startswith(brand)
                   for lbl in host_labels_clean):
                danger.append(
                    f"`{brand}` を名乗っていますが公式のドメインではありません")
                break
        else:
            # Not spelling the brand out exactly, but visually close.
            # Two separate checks:
            #   1. Character-substitution normalisation catches "discorcl"
            #      (cl→d) and "d1sc0rd" (1→l, 0→o) in one pass.
            #   2. Edit-distance catches single-key typos ("discrod", "discorb").
            #      Limit raised to 2 for names ≥ 7 chars to catch longer squats.
            mine = domain.split(".")[0]
            norm_mine = _normalize_lookalike(mine)
            for safe in SAFE_DOMAINS:
                name = safe.split(".")[0]
                if len(name) < 5:
                    continue
                norm_name = _normalize_lookalike(name)
                # Exact match after normalisation → substitution squatting
                if norm_mine == norm_name and mine != name:
                    danger.append(
                        f"`{safe}` によく似た紛らわしいドメインです"
                        f"（文字の置換で偽装しています）")
                    break
                # Edit distance: 1 for any brand, 2 for longer ones
                limit = 2 if len(name) >= 7 else 1
                dist = _edit_distance(mine, name, limit)
                if 0 < dist <= limit:
                    danger.append(
                        f"`{safe}` によく似た紛らわしいドメインです")
                    break


    tld = domain.rsplit(".", 1)[-1]
    if tld in RISKY_TLDS:
        odd.append(f"`.{tld}` は詐欺サイトに多用されるドメインです")

    path = unquote(parts.path or "")
    extension = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    if extension in EXECUTABLE:
        danger.append(f"実行ファイル(`.{extension}`)への直接リンクです")

    if domain in SHORTENERS:
        odd.append("短縮URLのため、実際の行き先が分かりません")

    if not known_good:
        haystack = f"{host}{path}".lower()
        hits = [w for w in BAIT if w in haystack]
        if len(hits) >= 2:
            danger.append("「無料」「配布」などの誘い文句が並んでいます")
        elif hits:
            odd.append(f"誘い文句(`{hits[0]}`)が含まれています")

    # The link text claims one destination and the href goes to another. Only
    # counted when the text is itself domain-shaped, so ordinary prose used as
    # link text is not treated as a lie.
    if label:
        claimed = re.search(r"\b((?:[\w-]+\.)+[a-z]{2,})\b", label, re.I)
        if claimed and registrable(claimed.group(1)) != domain:
            danger.append(f"表示は `{claimed.group(1)}` ですが "
                          f"`{domain}` に飛びます")

    if parts.scheme == "http" and not known_good:
        odd.append("暗号化されていない通信(http)です")

    if danger:
        verdict.level, verdict.reasons = DANGEROUS, danger + odd
    elif odd:
        verdict.level, verdict.reasons = SUSPICIOUS, odd
    return verdict


class _VirusTotal:
    """A thin client with a cache and its own rate-limit accounting.

    Everything it can go wrong with — no key, no network, quota exhausted, a
    malformed reply — returns None, which leaves the offline verdict standing.
    A link checker that stops checking because a third party is down would be
    worse than one that never called out at all.
    """

    def __init__(self):
        self._cache: dict[str, tuple[float, tuple[str, list[str]] | None]] = {}
        self._calls: list[float] = []
        self._lock = asyncio.Lock()

    @property
    def key(self) -> str:
        return os.environ.get("VIRUSTOTAL_API_KEY", "").strip()

    @property
    def ready(self) -> bool:
        return bool(VIRUSTOTAL and self.key)

    def _budget(self) -> bool:
        """Whether another request fits in this minute's allowance."""
        now = time.monotonic()
        self._calls = [t for t in self._calls if now - t < 60]
        return len(self._calls) < VT_PER_MINUTE

    def _cached(self, url: str):
        entry = self._cache.get(url)
        if entry and time.monotonic() - entry[0] < VT_CACHE_TTL:
            return entry[1]
        return None

    def _remember(self, url: str, result):
        if len(self._cache) >= VT_CACHE_MAX:
            # Cheapest possible eviction. The cache exists to stop the same link
            # being looked up repeatedly as people repost it, not to be a
            # long-lived store worth managing carefully.
            self._cache.clear()
        self._cache[url] = (time.monotonic(), result)

    @staticmethod
    def _url_id(url: str) -> str:
        """VirusTotal identifies a URL by its unpadded base64url form."""
        return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")

    async def lookup_hash(self, sha256: str, *, cache_key: str
                          ) -> tuple[str, list[str]] | None:
        """The same question about a file, asked by hash."""
        return await self._ask(f"{VT_API}/files/{sha256}", cache_key)

    async def lookup(self, url: str) -> tuple[str, list[str]] | None:
        """(level, reasons) from VirusTotal, or None if it could not say."""
        return await self._ask(f"{VT_API}/urls/{self._url_id(url)}",
                               url, submit=url)

    async def _ask(self, endpoint: str, cache_key: str,
                   submit: str | None = None):
        if not self.ready:
            return None
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        async with self._lock:
            # Re-checked inside the lock: several links from one message arrive
            # together, and the first through may have answered for all of them.
            cached = self._cached(cache_key)
            if cached is not None:
                return cached
            if not self._budget():
                log.debug("VirusTotal のレート上限のため今回は照会しません")
                return None
            self._calls.append(time.monotonic())
            result = await self._request(endpoint, submit)
        self._remember(cache_key, result)
        return result

    async def _request(self, endpoint: str,
                       submit: str | None) -> tuple[str, list[str]] | None:
        headers = {"x-apikey": self.key, "Accept": "application/json"}
        timeout = aiohttp.ClientTimeout(total=VT_TIMEOUT)
        try:
            async with aiohttp.ClientSession(headers=headers,
                                             timeout=timeout) as session:
                async with session.get(endpoint) as resp:
                    if resp.status == 404:
                        # Never seen. Submitting is a separate, louder act, and
                        # only ever applies to a URL — a file is not uploaded.
                        if VT_SUBMIT and submit:
                            await self._submit(session, submit)
                        return None
                    if resp.status == 401:
                        log.warning("VIRUSTOTAL_API_KEY が拒否されました。"
                                    "キーを確認してください。")
                        return None
                    if resp.status == 429:
                        log.warning("VirusTotal の利用上限に達しました。"
                                    "しばらくオフライン判定のみで動作します。")
                        return None
                    if resp.status != 200:
                        log.debug("VirusTotal が %s を返しました", resp.status)
                        return None
                    body = json.loads(await resp.text())
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.debug("VirusTotal に照会できませんでした: %s", e)
            return None
        except Exception:
            log.exception("VirusTotal の処理に失敗しました")
            return None
        return self._read(body)

    async def _submit(self, session: aiohttp.ClientSession, url: str):
        try:
            async with session.post(f"{VT_API}/urls", data={"url": url}) as resp:
                log.debug("VirusTotal に解析を依頼しました (%s)", resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.debug("VirusTotal に送信できませんでした: %s", e)

    @staticmethod
    def _read(body: dict) -> tuple[str, list[str]] | None:
        stats = (((body or {}).get("data") or {}).get("attributes") or {}
                 ).get("last_analysis_stats")
        if not isinstance(stats, dict):
            return None
        malicious = int(stats.get("malicious") or 0)
        suspicious = int(stats.get("suspicious") or 0)
        clean = int(stats.get("harmless") or 0) + int(stats.get("undetected") or 0)
        if malicious >= VT_MALICIOUS_FOR_DANGER:
            return DANGEROUS, [f"VirusTotal: {malicious}社が危険と判定しています"]
        if malicious or suspicious >= 2:
            return SUSPICIOUS, [
                f"VirusTotal: {malicious + suspicious}社が問題を指摘しています"]
        if clean:
            # Said plainly. "No engine objected" is not "this is safe", and the
            # difference matters most to whoever is deciding whether to click.
            return SAFE, [f"VirusTotal: {clean}社が検査して問題は出ていません"]
        return None


virustotal = _VirusTotal()


def _tidy(url: str) -> str:
    """Drop punctuation that belongs to the sentence, not the link."""
    url = url.rstrip(".,!?;:'\"")
    while url.endswith((")", "]", ">")) and url.count(url[-1]) > url.count(
            {")": "(", "]": "[", ">": "<"}[url[-1]]):
        url = url[:-1]
    return url


def scan(text: str, limit: int = 8) -> list[Verdict]:
    """Every link in a message, judged. Worst first."""
    if not text:
        return []
    seen: dict[str, str | None] = {}
    for label, target in _MARKDOWN_RE.findall(text):
        seen.setdefault(_tidy(target), label)
    for found in _URL_RE.findall(text):
        seen.setdefault(_tidy(found), None)

    return _rank([inspect(url, label=label)
                  for url, label in list(seen.items())[:limit]])


# --------------------------------------------------------------------------
# Attachments. Link checking only ever looked at URLs, and a file posted
# directly is the other half of the same problem — arguably the worse half,
# since it arrives already downloaded.
# --------------------------------------------------------------------------
ATTACHMENTS = True

# Only files up to here are hashed for VirusTotal: reading the attachment back
# out of Discord's CDN costs bandwidth, and malware small enough to be posted in
# chat is almost never large.
VT_FILE_MAX_BYTES = 8 * 1024 * 1024
# Types worth spending a lookup on. Images and video are the bulk of what gets
# posted and almost never the problem, so they are not hashed — the quota is
# four a minute and belongs to the things that execute.
WORTH_HASHING = frozenset("""
exe scr bat cmd com msi apk jar vbs vbe js ps1 dll lnk iso img hta reg
zip rar 7z gz tar cab ace
doc docx xls xlsx ppt pptx pdf rtf docm xlsm pptm
""".split())

# A name ending in a real extension followed by an executable one. The icon
# Discord draws comes from the last extension, but people read the first.
_DOUBLE_EXTENSION = re.compile(
    r"\.(jpe?g|png|gif|webp|bmp|txt|pdf|docx?|xlsx?|mp[34]|wav|mov|csv|json)"
    r"\s*\.(" + "|".join(sorted(EXECUTABLE)) + r")$", re.I)

# Bidirectional overrides. "photo‮gnp.exe" renders as "photoexe.png": the
# characters are reordered for display only, and the file still runs.
_BIDI = re.compile("[" + "".join(chr(c) for c in (
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x2066, 0x2067, 0x2068, 0x2069)) + "]")


def _extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def inspect_file(name: str, size: int = 0) -> Verdict:
    """Judge one attachment by its name alone."""
    verdict = Verdict(url=name, level=SAFE)
    danger, odd = [], []

    if _BIDI.search(name):
        danger.append("ファイル名に表示を反転させる文字が埋め込まれています")
    if _DOUBLE_EXTENSION.search(name):
        danger.append("二重拡張子です(画像や文書に見せかけた実行ファイル)")

    extension = _extension(_BIDI.sub("", name))
    if extension in EXECUTABLE:
        danger.append(f"実行ファイル(`.{extension}`)です")
    elif extension in ("zip", "rar", "7z") and size and size < 2000:
        odd.append("中身のほとんど無い圧縮ファイルです")

    lowered = name.lower()
    if any(w in lowered for w in BAIT) and extension in EXECUTABLE | {"zip", "rar"}:
        danger.append("誘い文句を含むファイル名です")

    if danger:
        verdict.level, verdict.reasons = DANGEROUS, danger + odd
    elif odd:
        verdict.level, verdict.reasons = SUSPICIOUS, odd
    return verdict


async def _file_report(url: str, size: int) -> tuple[str, list[str]] | None:
    """Hash the attachment and ask VirusTotal about it.

    The file is fetched and hashed locally; only the hash is sent. Unlike a URL
    lookup, this tells VirusTotal nothing it did not already know — a SHA-256 of
    a file it has never seen is not the file.
    """
    if not virustotal.ready or size <= 0 or size > VT_FILE_MAX_BYTES:
        return None
    cached = virustotal._cached(url)
    if cached is not None:
        return cached
    try:
        timeout = aiohttp.ClientTimeout(total=VT_TIMEOUT * 2)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                digest = hashlib.sha256()
                read = 0
                async for block in resp.content.iter_chunked(64 * 1024):
                    read += len(block)
                    if read > VT_FILE_MAX_BYTES:
                        return None       # larger than it claimed
                    digest.update(block)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.debug("添付ファイルを取得できませんでした: %s", e)
        return None
    return await virustotal.lookup_hash(digest.hexdigest(), cache_key=url)


async def scan_attachments(attachments) -> list[Verdict]:
    """Judge every attachment on a message. Worst first."""
    if not ENABLED or not ATTACHMENTS or not attachments:
        return []
    verdicts = []
    hashed = 0
    for attachment in list(attachments)[:8]:
        name = getattr(attachment, "filename", "") or ""
        size = int(getattr(attachment, "size", 0) or 0)
        verdict = inspect_file(name, size)
        if (hashed < VT_MAX_PER_MESSAGE
                and _extension(name) in WORTH_HASHING):
            try:
                result = await _file_report(getattr(attachment, "url", ""), size)
            except Exception:
                log.exception("添付ファイルの照会に失敗しました")
                result = None
            if result:
                hashed += 1
                level, reasons = result
                verdict.reasons.extend(reasons)
                if level == DANGEROUS or (level == SUSPICIOUS
                                          and verdict.level == SAFE):
                    verdict.level = level
        verdicts.append(verdict)
    return _rank(verdicts)


def _rank(verdicts: list[Verdict]) -> list[Verdict]:
    order = {DANGEROUS: 0, SUSPICIOUS: 1, SAFE: 2}
    return sorted(verdicts, key=lambda v: order[v.level])


async def scan_online(text: str, limit: int = 8) -> list[Verdict]:
    """scan(), then a second opinion from VirusTotal where it is worth asking.

    Never downgrades. If the structure of a link says it is an attack, no number
    of engines reporting it clean changes that — a domain registered this
    morning to imitate Discord is unknown to every scanner precisely because it
    is new, and that is the case this exists to catch.
    """
    verdicts = scan(text, limit)
    if not verdicts or not virustotal.ready:
        return verdicts

    asked = 0
    for verdict in verdicts:
        if asked >= VT_MAX_PER_MESSAGE:
            break
        host = urlsplit(verdict.url).hostname or ""
        if registrable(host) in SAFE_DOMAINS:
            continue        # no sense spending the quota on youtube.com
        try:
            result = await virustotal.lookup(verdict.url)
        except Exception:
            # The offline verdicts are already decided, and they are the ones
            # that catch a domain registered this morning. Losing all of them
            # because a third party misbehaved would invert the whole point of
            # treating VirusTotal as an addition rather than the checker.
            log.exception("VirusTotal の照会に失敗しました")
            break
        if result is None:
            continue
        asked += 1
        level, reasons = result
        verdict.reasons.extend(reasons)
        if level == DANGEROUS or (level == SUSPICIOUS
                                  and verdict.level == SAFE):
            verdict.level = level
    return _rank(verdicts)


async def scan_message(text: str, attachments=()) -> list[Verdict]:
    """Everything posted in one message — links and files — judged together."""
    verdicts = await scan_online(text or "")
    verdicts += await scan_attachments(attachments)
    return _rank(verdicts)


def summary(verdicts: list[Verdict]) -> tuple[str, str]:
    """(title, body) describing what was found."""
    worst = verdicts[0]
    shown = [v for v in verdicts if v.level != SAFE] or verdicts[:1]
    lines = []
    for v in shown[:3]:
        head = "危険" if v.bad else ("注意" if v.level == SUSPICIOUS else "問題なし")
        if v.url.lower().startswith(("http://", "https://")):
            # The host only. Reposting the full URL makes the warning itself a
            # working copy of the link it is warning about.
            what = urlsplit(v.url).hostname or v.url
        else:
            what = f"ファイル: {v.url}"
        lines.append(f"**{head}** `{ui.clip(what, 90)}`")
        lines += [f"・{r}" for r in v.reasons[:4]]
    if worst.bad:
        lines.append("\n心当たりのないリンクは開かないでください。"
                     "ログイン情報を入力するよう求められたら詐欺です。")
    title = "危険なリンクの可能性" if worst.bad else "リンクの確認"
    return title, "\n".join(lines)
