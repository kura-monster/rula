"""External service status checker.

Fetches current status from various services' status APIs and pages.
Where no JSON API exists, scraping or XML parsing is used.

Commands:
  R!svc <name>  — Check a service's current status
  R!svc list    — List all supported service names
"""
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, Awaitable

import aiohttp

log = logging.getLogger("rula.svcstatus")

TIMEOUT = aiohttp.ClientTimeout(total=10.0)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

COLOR_OK      = 0x57F287   # green
COLOR_WARN    = 0xFEE75C   # yellow
COLOR_ERROR   = 0xED4245   # red
COLOR_UNKNOWN = 0x99AAB5   # grey


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class StatusResult:
    name: str
    indicator: str      # "ok" | "warn" | "error" | "unknown"
    description: str
    url: str

    @property
    def emoji(self) -> str:
        return {"ok": "🟢", "warn": "🟡", "error": "🔴", "unknown": "⚪"}.get(self.indicator, "⚪")

    @property
    def color(self) -> int:
        return {"ok": COLOR_OK, "warn": COLOR_WARN, "error": COLOR_ERROR,
                "unknown": COLOR_UNKNOWN}.get(self.indicator, COLOR_UNKNOWN)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _sp_indicator(raw: str) -> str:
    """Convert statuspage.io indicator value to our indicator."""
    raw = (raw or "").lower()
    if raw in ("none", "ok"):
        return "ok"
    if raw == "minor":
        return "warn"
    if raw in ("major", "critical"):
        return "error"
    return "unknown"


async def _get(session: aiohttp.ClientSession, url: str,
               accept: str = "text/html,application/json,*/*") -> tuple[int | None, bytes]:
    try:
        async with session.get(
            url,
            headers={"User-Agent": UA, "Accept": accept, "Accept-Encoding": "identity"},
            timeout=TIMEOUT,
        ) as resp:
            return resp.status, await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return None, str(e).encode()


def _short(err: bytes | str) -> str:
    s = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
    return s[:80]


# ---------------------------------------------------------------------------
# Fetchers — one per service type
# ---------------------------------------------------------------------------

async def _fetch_statuspage(session: aiohttp.ClientSession,
                             name: str, page: str, api: str) -> StatusResult:
    """Standard statuspage.io /api/v2/status.json"""
    st, body = await _get(session, api, "application/json")
    if st != 200:
        return StatusResult(name, "unknown", f"HTTP {st}", page)
    try:
        data = json.loads(body)
        s = data.get("status") or {}
        indicator = _sp_indicator(s.get("indicator", ""))
        desc = s.get("description") or "不明"
        return StatusResult(name, indicator, desc, page)
    except Exception as e:
        return StatusResult(name, "unknown", f"解析失敗: {e}", page)


async def _fetch_slack(session: aiohttp.ClientSession) -> StatusResult:
    page = "https://status.slack.com"
    st, body = await _get(session, "https://status.slack.com/api/v2.0.0/current",
                          "application/json")
    if st != 200:
        return StatusResult("Slack", "unknown", f"HTTP {st}", page)
    try:
        data = json.loads(body)
        s = (data.get("status") or "").lower()
        active = data.get("active_incidents") or []
        if s == "ok" and not active:
            return StatusResult("Slack", "ok", "All Systems Operational", page)
        if active:
            return StatusResult("Slack", "warn", active[0].get("title", "Incident")[:80], page)
        return StatusResult("Slack", "warn", s, page)
    except Exception as e:
        return StatusResult("Slack", "unknown", f"解析失敗: {e}", page)


async def _fetch_gcp(session: aiohttp.ClientSession) -> StatusResult:
    page = "https://status.cloud.google.com"
    st, body = await _get(session, "https://status.cloud.google.com/incidents.json",
                          "application/json")
    if st != 200:
        return StatusResult("Google Cloud", "unknown", f"HTTP {st}", page)
    try:
        data = json.loads(body)
        active = [i for i in (data or []) if not i.get("end")]
        if not active:
            return StatusResult("Google Cloud", "ok", "All Services Operational", page)
        desc = active[0].get("external_desc", "Incident")
        if len(desc) > 80:
            desc = desc[:77] + "..."
        sev = (active[0].get("severity") or "").lower()
        ind = "error" if any(k in sev for k in ("high", "critical")) else "warn"
        return StatusResult("Google Cloud", ind,
                            f"{len(active)}件のインシデント: {desc}", page)
    except Exception as e:
        return StatusResult("Google Cloud", "unknown", f"解析失敗: {e}", page)


async def _fetch_azure(session: aiohttp.ClientSession) -> StatusResult:
    page = "https://azure.status.microsoft/ja-jp/status/"
    st, body = await _get(session, "https://azure.status.microsoft/en-us/status/feed/",
                          "application/xml,text/xml,*/*")
    if st != 200:
        return StatusResult("Azure", "unknown", f"HTTP {st}", page)
    text = body.decode("utf-8", "replace")
    # Items in the RSS = active advisories
    items = re.findall(r"<item>.*?</item>", text, re.DOTALL)
    if items:
        title_m = re.search(r"<title><!\[CDATA\[(.+?)\]\]>|<title>([^<]+)</title>",
                            items[0], re.DOTALL)
        title = (title_m.group(1) or title_m.group(2) or "Advisory").strip() if title_m else "Advisory"
        return StatusResult("Azure", "warn", title[:80], page)
    return StatusResult("Azure", "ok", "All Services Operational", page)


async def _fetch_aws(session: aiohttp.ClientSession) -> StatusResult:
    """AWS Health via status.aws.amazon.com/data.json (UTF-16)."""
    page = "https://health.aws.amazon.com"
    st, body = await _get(session, "https://status.aws.amazon.com/data.json",
                          "application/json,*/*")
    if st != 200:
        return StatusResult("AWS", "unknown", f"HTTP {st}", page)
    try:
        data = json.loads(body.decode("utf-16"))
        if not isinstance(data, list):
            return StatusResult("AWS", "unknown", "不明な形式", page)
        active = [e for e in data if not e.get("endTime")]
        if not active:
            return StatusResult("AWS", "ok", "All Services Operational", page)
        e0 = active[0]
        region = e0.get("region_name") or e0.get("region", "")
        summary = e0.get("summary") or e0.get("service", "Issue")
        desc = f"{len(active)}件の障害 [{region}] {summary[:60]}"
        return StatusResult("AWS", "error", desc, page)
    except Exception as ex:
        return StatusResult("AWS", "unknown", f"解析失敗: {ex}", page)


async def _fetch_xbox(session: aiohttp.ClientSession) -> StatusResult:
    """Xbox Live via XML API."""
    page = "https://support.xbox.com/xbox-live-status"
    st, body = await _get(session, "https://xnotify.xboxlive.com/servicestatusv6/US/en-US",
                          "application/xml,*/*")
    if st != 200:
        return StatusResult("Xbox Live", "unknown", f"HTTP {st}", page)
    xml = body.decode("utf-8", "replace")
    # <Status><State>None</State><Id>1</Id>…</Status>
    # Id 1 = OK, anything else = issue
    overall_id = re.search(r"<Overall>.*?<Id>(\d+)</Id>", xml, re.DOTALL)
    oid = overall_id.group(1) if overall_id else "?"
    if oid == "1":
        return StatusResult("Xbox Live", "ok", "All Services Operational", page)
    # Count degraded core services
    core_ids = re.findall(r"<CoreServices>.*?<Id>(\d+)</Id>", xml, re.DOTALL)
    degraded = [i for i in core_ids if i != "1"]
    if degraded:
        return StatusResult("Xbox Live", "warn",
                            f"{len(degraded)}個のサービスに問題あり", page)
    return StatusResult("Xbox Live", "warn", f"ステータス異常 (Id={oid})", page)


async def _fetch_keitodaze(session: aiohttp.ClientSession) -> StatusResult:
    """keitodaze via Uptime Kuma status page API."""
    page = "https://status.keitodaze.net/status/default"
    api  = "https://status.keitodaze.net/api/status-page/default"
    st, body = await _get(session, api, "application/json")
    if st != 200:
        return StatusResult("keitodaze", "unknown", f"HTTP {st}", page)
    try:
        data = json.loads(body)
        incidents = data.get("incidents") or []
        maintenance = data.get("maintenanceList") or []
        if incidents:
            title = incidents[0].get("title", "Incident")
            return StatusResult("keitodaze", "warn", title[:80], page)
        if maintenance:
            title = maintenance[0].get("title", "Maintenance")
            return StatusResult("keitodaze", "warn", f"[メンテ] {title[:70]}", page)
        # Check individual monitor heartbeats
        groups = data.get("publicGroupList") or []
        monitor_ids = [m["id"] for g in groups for m in g.get("monitorList", [])]
        if not monitor_ids:
            return StatusResult("keitodaze", "ok", "All Services Operational", page)
        # Fetch up to 4 monitors to check status
        down: list[str] = []
        for mid in monitor_ids[:8]:
            hst, hbody = await _get(session,
                f"https://status.keitodaze.net/api/status-page/heartbeat/{mid}",
                "application/json")
            if hst == 200:
                hdata = json.loads(hbody)
                hbeats = hdata.get("heartbeatList") or {}
                # Find the monitor name from groups
                name = next(
                    (m["name"] for g in groups for m in g.get("monitorList", [])
                     if m["id"] == mid),
                    str(mid))
                # heartbeatList is keyed by monitorId, value is list of heartbeats
                all_beats = []
                if isinstance(hbeats, dict):
                    for v in hbeats.values():
                        if isinstance(v, list):
                            all_beats.extend(v)
                # Check if latest beat is down (status != 1)
                if all_beats:
                    latest = all_beats[-1]
                    if latest.get("status") not in (1, True, "1"):
                        down.append(name)
        if down:
            return StatusResult("keitodaze", "error",
                                f"Down: {', '.join(down)}", page)
        return StatusResult("keitodaze", "ok", "All Services Operational", page)
    except Exception as ex:
        return StatusResult("keitodaze", "unknown", f"解析失敗: {ex}", page)


async def _fetch_psn(session: aiohttp.ClientSession) -> StatusResult:
    """PlayStation Network — scrape status page for JSON data."""
    page = "https://status.playstation.com"
    # PSN status page is a SPA, but they expose a JSON feed via another domain
    for api_url in [
        "https://status.playstation.com/api/public/incidents",
        "https://status.playstation.com/api/v2/status.json",
        "https://api.direct.playstation.com/commercewebservices/ps-direct-us/psn/status",
    ]:
        st, body = await _get(session, api_url, "application/json")
        if st == 200:
            try:
                data = json.loads(body)
                if isinstance(data, dict) and "status" in data:
                    s = data["status"]
                    indicator = _sp_indicator(s.get("indicator", ""))
                    return StatusResult("PlayStation", indicator,
                                       s.get("description", "不明"), page)
                if isinstance(data, list):
                    active = [i for i in data if not i.get("resolved")]
                    if not active:
                        return StatusResult("PlayStation", "ok",
                                           "All Services Operational", page)
                    return StatusResult("PlayStation", "warn",
                                       active[0].get("message", "Incident")[:80], page)
            except Exception:
                pass
    # Fallback: HTML scrape for status keywords
    st, body = await _get(session, page)
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    if re.search(r"(?i)(outage|disruption|degraded|unavailable)", text):
        return StatusResult("PlayStation", "warn",
                            "障害の可能性あり — ページを確認してください", page)
    if re.search(r"(?i)(operational|all\s+services\s+up)", text):
        return StatusResult("PlayStation", "ok", "All Services Operational", page)
    return StatusResult("PlayStation", "unknown",
                        "APIなし — ステータスページを確認してください", page)


async def _fetch_steam(session: aiohttp.ClientSession) -> StatusResult:
    """Steam — scrape steamstat.us (blocks bots) or use Valve Web API."""
    page = "https://steamstat.us"
    # Valve Web API for Steam status
    api = "https://api.steampowered.com/ISteamWebAPIUtil/GetSupportedAPIList/v1/"
    # Try Valve's CSR endpoint
    for url in [
        "https://steamcommunity.com/",
        "https://store.steampowered.com/",
    ]:
        st, body = await _get(session, url)
        if st in (200, 302):
            # If they return 200 without redirection, Steam is up
            return StatusResult("Steam", "ok", "Steam は到達可能です", page)
        if st is None:
            return StatusResult("Steam", "error", "Steamに接続できません", page)
    return StatusResult("Steam", "unknown", "APIなし — steamstat.us で確認してください", page)


async def _fetch_facebook(session: aiohttp.ClientSession) -> StatusResult:
    """Facebook / Meta — scrape status.fb.com."""
    page = "https://status.fb.com"
    st, body = await _get(session, page)
    if st != 200:
        return StatusResult("Facebook / Meta", "unknown", f"HTTP {st}", page)
    text = body.decode("utf-8", "replace")
    # Look for incident text
    if re.search(r"(?i)(outage|disruption|incident|degraded|unavailable)", text):
        # Try to extract message
        msg = re.search(r"(?i)([\w\s]+ (outage|disruption|incident)[\w\s,\.\-]*)", text)
        desc = msg.group(0)[:80].strip() if msg else "障害が検出されました"
        return StatusResult("Facebook / Meta", "warn", desc, page)
    if re.search(r"(?i)(no\s+issues|all\s+good|fully\s+operational)", text):
        return StatusResult("Facebook / Meta", "ok", "All Systems Operational", page)
    return StatusResult("Facebook / Meta", "unknown",
                        "ステータス確認ページを参照してください", page)


async def _fetch_notion(session: aiohttp.ClientSession) -> StatusResult:
    """Notion — the /api/v2/status.json path returns HTML; try direct scrape."""
    page = "https://status.notion.so"
    # Try undocumented endpoint
    for api_url in [
        "https://notionstatuspage.notion.site/api/v2/status.json",
        "https://status.notion.so/api/v2/status.json",
    ]:
        st, body = await _get(session, api_url, "application/json")
        if st == 200 and body.strip().startswith(b"{"):
            try:
                data = json.loads(body)
                s = data.get("status") or {}
                return StatusResult("Notion", _sp_indicator(s.get("indicator", "")),
                                    s.get("description") or "不明", page)
            except Exception:
                pass
    # Fallback HTML scrape
    st, body = await _get(session, page)
    if st != 200:
        return StatusResult("Notion", "unknown", f"HTTP {st}", page)
    text = body.decode("utf-8", "replace")
    if re.search(r"(?i)(outage|incident|degraded)", text):
        return StatusResult("Notion", "warn", "障害の可能性 — ページを確認してください", page)
    if re.search(r"(?i)(operational|all systems)", text):
        return StatusResult("Notion", "ok", "All Systems Operational", page)
    return StatusResult("Notion", "unknown", "ページで確認してください", page)


async def _fetch_twitter(session: aiohttp.ClientSession) -> StatusResult:
    """X (Twitter) — no public status API; scrape or try unofficial."""
    page = "https://api.x.com/status"
    # Try the twitterstat.us unofficial endpoint
    for url in [
        "https://www.isitdownrightnow.com/api.x.com.json",
    ]:
        st, body = await _get(session, url, "application/json")
        if st == 200 and body.strip().startswith(b"{"):
            try:
                data = json.loads(body)
                server_status = str(data.get("server_status_code", "?"))
                if server_status == "200":
                    return StatusResult("X (Twitter)", "ok", "X API is reachable", page)
                return StatusResult("X (Twitter)", "warn",
                                    f"HTTP {server_status}", page)
            except Exception:
                pass
    # Try to reach api.x.com directly
    st, _ = await _get(session, "https://api.x.com/1.1/help/configuration.json",
                       "application/json")
    if st in (200, 403, 401):
        # 401/403 = server is up but auth required
        return StatusResult("X (Twitter)", "ok", "X API は到達可能です", page)
    if st is None:
        return StatusResult("X (Twitter)", "error", "X APIへ接続できません", page)
    return StatusResult("X (Twitter)", "unknown",
                        f"HTTP {st} — ページで確認してください", page)


async def _fetch_downdetector(session: aiohttp.ClientSession) -> StatusResult:
    """Downdetector JP — returns 403 to bots; link only."""
    page = "https://downdetector.jp"
    return StatusResult("DownDetector JP", "unknown",
                        "APIなし — ページで各サービスの障害報告を確認できます", page)


# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------

def _sp(name: str, domain: str, status_page: str | None = None):
    api  = f"https://{domain}/api/v2/status.json"
    pg   = status_page or f"https://{domain}"
    async def _f(s: aiohttp.ClientSession) -> StatusResult:
        return await _fetch_statuspage(s, name, pg, api)
    return _f


SERVICES: dict[str, Callable[[aiohttp.ClientSession], Awaitable[StatusResult]]] = {
    # --- statuspage.io ---
    "discord":     _sp("Discord",    "discordstatus.com"),
    "github":      _sp("GitHub",     "www.githubstatus.com"),
    "openai":      _sp("OpenAI",     "status.openai.com"),
    "anthropic":   _sp("Anthropic",  "status.anthropic.com"),
    "vercel":      _sp("Vercel",     "www.vercel-status.com"),
    "figma":       _sp("Figma",      "status.figma.com"),
    "docker":      _sp("Docker",     "www.dockerstatus.com"),
    "npm":         _sp("npm",        "status.npmjs.org"),
    "atlassian":   _sp("Atlassian",  "status.atlassian.com"),
    "epicgames":   _sp("Epic Games", "status.epicgames.com"),
    "zoom":        _sp("Zoom",       "status.zoom.us"),
    "cloudflare":  _sp("Cloudflare", "www.cloudflarestatus.com"),
    "spotify":     _sp("Spotify",    "www.spotifystatus.com", "https://status.spotify.com"),
    # --- custom fetchers ---
    "slack":       _fetch_slack,
    "gcp":         _fetch_gcp,
    "googlecloud": _fetch_gcp,
    "azure":       _fetch_azure,
    "aws":         _fetch_aws,
    "xbox":        _fetch_xbox,
    "keitodaze":   _fetch_keitodaze,
    "playstation": _fetch_psn,
    "steam":       _fetch_steam,
    "facebook":    _fetch_facebook,
    "notion":      _fetch_notion,
    "twitter":     _fetch_twitter,
    "x":           _fetch_twitter,
    "downdetector": _fetch_downdetector,
}

ALIASES: dict[str, str] = {
    "cf":          "cloudflare",
    "gh":          "github",
    "epic":        "epicgames",
    "google":      "gcp",
    "ps":          "playstation",
    "psn":         "playstation",
    "fb":          "facebook",
    "meta":        "facebook",
    "slack":       "slack",
    "discord":     "discord",
    "notion":      "notion",
    "figma":       "figma",
    "zoom":        "zoom",
    "steam":       "steam",
    "spotify":     "spotify",
    "npm":         "npm",
    "xboxlive":    "xbox",
}


def resolve(name: str) -> str | None:
    key = name.strip().lower()
    if key in SERVICES:
        return key
    return ALIASES.get(key)


async def fetch(name: str) -> StatusResult | None:
    key = resolve(name)
    if key is None:
        return None
    factory = SERVICES[key]
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    async with aiohttp.ClientSession(connector=connector) as session:
        return await factory(session)


def list_services() -> list[str]:
    shown = sorted(set(SERVICES.keys()) | set(ALIASES.keys()))
    return shown
