"""Discord bot for Rula.

Talks to the local API rather than holding its own Engine. The API already
serialises generation behind a lock — one GPU, one answer at a time — and owns
the load/unload dance the vision model needs to fit on an 8GB card. A second
process with its own Engine would race it for VRAM, so the bot is an ordinary
client: it needs the API running and an API key, nothing more.

Mention it or reply to it to talk. The answer streams into the message as it is
produced, because with reasoning switched on a turn takes tens of seconds and
nearly all of that is thinking — worth watching, unlike a spinner.

Everything else is a command prefixed R! — R!help lists them. Music lives in
music.py, which this file only loads; the two share nothing but ui.py.

No emoji anywhere. Not by convention but by construction: every message leaves
through ui.embed, which strips them, so the rule holds for the model's answers
and for song titles as well as for the wording here.
"""
import asyncio
import difflib
import json
import logging
import os
import re
import socket
import sys
import time
import uuid

import aiohttp
import discord
from discord.ext import commands

import assist
import botdetect
import freegames
import linkguard
import llm
import media_extract
import songlink
import streamnotify
import svcstatus
import timeoutwatch
import ui
import websearch

try:
    import music
except ImportError:
    # Music is optional, and its absence is a deployment choice rather than a
    # fault: delete music.py and this becomes a chat bot, with no edit to any
    # other file. That is the whole mechanism behind the Rula_Chan build — one
    # copy of this file serving both, instead of two copies drifting apart.
    music = None
from ui import (COLOR_ANSWER, COLOR_ERROR, COLOR_WORKING, MAX_EMBED_CHARS, clip,
                embed)

log = logging.getLogger("rula.discord")

# Defaults only. The environment is read in main(), after .env has been loaded —
# reading it here would be too early, and RULA_API_BASE written in .env would be
# silently ignored in favour of this value.
DEFAULT_API_BASE = "http://127.0.0.1:8000"
DEFAULT_MODEL = "rula-1.2-4b"

# What every command starts with. Full-width variants are included because a
# Japanese keyboard in kana mode produces them without the typist noticing, and
# being told "unknown command" over an invisible difference is maddening.
PREFIXES = ("R!", "r!", "R！", "r！")

# ---------------------------------------------------------------------------
# Set to False to run without the music feature even where music.py is present.
# MUSIC=off in the environment does the same without an edit, and deleting
# music.py does it a third way — the import is optional.
#
# Three ways because the builds are deployed differently: Rula_Chan simply has
# no music.py, while a hosting panel where deleting files is awkward can set the
# variable instead.
# ---------------------------------------------------------------------------
MUSIC_ENABLED = True

# Everything is sent as an embed, so this is the limit that binds (see ui.py).
# Embeds also cannot ping anyone, whatever the model wrote — a second guard
# behind AllowedMentions.none().
# Reserve for a code fence reopened across a split (see _balance_fences).
SPLIT_LIMIT = MAX_EMBED_CHARS - 16
# A plain message holds far less. Only used where Embed Links is missing, so the
# fallback splits to fit rather than losing the tail of the answer.
PLAIN_SPLIT_LIMIT = 2000 - 16
# The API's own cap. Trimming here gives a plain answer instead of a 422.
MAX_INPUT_CHARS = 4000
# How often the live message is rewritten. Discord allows roughly five edits per
# five seconds per channel; discord.py waits out the limit rather than failing,
# so editing faster would not error — it would silently fall behind the model.
EDIT_INTERVAL = 1.5
# How much reasoning to show while it is being written. The tail, not the head:
# what is being thought right now is the part that reads as alive. Roomier than
# the plain-text version could afford, now that 4096 characters are available.
THINKING_TAIL = 800

# Longer than the API's own 300s generation timeout, so a slow turn ends with the
# API's error rather than the socket giving up first and hiding it.
READ_TIMEOUT = 330
# Paused before exiting on a startup failure, so a supervisor that restarts on
# exit cannot turn one into a login flood. See _fatal.
RESTART_COOLDOWN = int(os.environ.get("DISCORD_RESTART_COOLDOWN", "30"))

# When DISCORD_THINK=auto, the questions worth spending ~20 extra seconds on:
# ones with a derivation behind the answer. A heuristic, and a cheap one — the
# cost of a wrong guess either way is latency, not a wrong answer, and "on" and
# "off" are there for anyone who would rather not have it guessed at all.
_NEEDS_THOUGHT = re.compile(
    r"[0-9０-９]\s*[+\-*/×÷=]\s*[0-9０-９]"
    r"|(何分|何時間|何日|何個|何人|何円|何倍|何%|何パーセント|いくつ|いくら)"
    r"|(なぜ|なぜか|どうして|理由|根拠)"
    r"|(比較|違い|どっち|どちら|メリット|デメリット|向いてる|選ぶ)"
    r"|(計算|求めて|見積|証明|論理|矛盾|検証|考えて|検討)")


# Shown when the API cannot be reached at all. By far the usual cause is the bot
# running somewhere other than the machine the API is on — a hosting service, a
# VPS — where 127.0.0.1 is that host, not the PC with the GPU on it.
_UNREACHABLE_HINT = (
    "別のホストでBotを動かしている場合、127.0.0.1 はそのホスト自身を指します。\n"
    "  トンネルの公開URLを指定してください: RULA_API_BASE=https://api.example.com\n"
    "  同じPCで動かしている場合は、run_api.ps1 が起動しているか確認してください。")


# Cloudflare answers with its own HTML page when it cannot reach the origin, and
# the number on that page says which way the path is broken. Worth translating:
# the bot cannot read HTML, and the status code alone (530, for most of these)
# says only "something upstream went wrong" — which is the least useful part.
_GATEWAY_HINTS = {
    "1033": "Cloudflare トンネルが接続されていません。\n"
            "  APIを動かしているPCで cloudflared が起動しているか確認してください。\n"
            "  (PCがスリープしている場合もこれになります)",
    "1016": "トンネルの向き先が見つかりません。Cloudflare のDNS設定を確認してください。",
    "521": "APIサーバーが接続を拒否しました。run_api.ps1 が起動しているか確認してください。",
    "522": "APIサーバーへの接続がタイムアウトしました。",
    "523": "APIサーバーに到達できません。",
    "524": "APIサーバーの応答がタイムアウトしました。生成に時間がかかりすぎた可能性があります。",
}


def _gateway_reason(status: int, body: str) -> str | None:
    """Translate a proxy's error page, if that is what came back."""
    found = re.search(r"[Ee]rror\s*(\d{3,4})", body or "")
    if found and found.group(1) in _GATEWAY_HINTS:
        return f"Cloudflare エラー {found.group(1)}: {_GATEWAY_HINTS[found.group(1)]}"
    if str(status) in _GATEWAY_HINTS:
        return _GATEWAY_HINTS[str(status)]
    return None


class ApiError(RuntimeError):
    """A non-200 from the API, kept with its status so it can be explained."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status

    def as_japanese(self) -> str:
        return {
            401: "APIキーが正しくありません。.env の RULA_API_KEY を確認してください。",
            429: "リクエストが多すぎます。少し待ってからもう一度どうぞ。",
            503: "モデルを読み込み中です。少し待ってからもう一度どうぞ。",
            504: "生成が時間切れになりました。もう少し短い質問だと通るかもしれません。",
        }.get(self.status, f"APIがエラーを返しました({self.status})。")


class RulaClient:
    """The OpenAI-compatible endpoint, as an async generator of deltas."""

    def __init__(self, base: str, key: str, model: str = DEFAULT_MODEL):
        self.base = base.rstrip("/")
        self.key = key
        self.model = model
        self._session: aiohttp.ClientSession | None = None

    @property
    def configured(self) -> bool:
        """Whether chat can work at all. LLMClient answers the same question."""
        return bool(self.key)

    def forget(self, conversation: str) -> bool:
        """History lives on the server here, so there is nothing local to drop."""
        return False

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.key}"},
            # The same DNS cache as the Discord side. The API is reached over
            # the internet too when the bot is hosted away from the GPU, so it
            # is exposed to exactly the same flaky resolver.
            connector=_make_connector(),
            # No total timeout: a thinking turn on a busy card legitimately runs
            # for minutes. sock_read is the real guard — it fires when the server
            # has stopped sending, which is the failure worth catching.
            timeout=aiohttp.ClientTimeout(total=None, sock_read=READ_TIMEOUT))
        return self

    async def __aexit__(self, *_exc):
        await self._session.close()

    async def health(self) -> tuple[dict | None, str]:
        """(status, reason). The dict on success, or why not on failure.

        Never raises, and never fatal. Everything that can go wrong between here
        and the GPU — an asleep PC, a tunnel that has dropped, a proxy serving
        its own error page — is something that comes back on its own, and none
        of it is a reason to stop a bot that also plays music.
        """
        try:
            async with self._session.get(f"{self.base}/health") as resp:
                status, body = resp.status, await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return None, f"APIに接続できません: {ui.short_error(e)}"

        try:
            data = json.loads(body)
        except ValueError:
            # HTML, almost certainly: something in front of the API answered
            # instead of the API. Cloudflare says which way it is broken in a
            # number on that page, and that number is the useful part.
            return None, (_gateway_reason(status, body)
                          or f"{self.base} がRula APIの応答を返しませんでした "
                             f"(HTTP {status})。RULA_API_BASE を確認してください。")
        if not isinstance(data, dict) or "status" not in data:
            return None, f"{self.base} はRula APIではなさそうです。"
        return data, ""

    async def preflight(self) -> dict | None:
        """Check the server and the key before connecting to Discord.

        A rejected key is fatal: it will never start working on its own, and
        every mention would be answered with an error until someone noticed.

        Nothing else is. The bot may well be running somewhere the API is not —
        on a hosting service, with the API behind a tunnel on a PC that is
        currently asleep — and that machine will come back. Warn, start anyway,
        and let each turn report the problem if it persists.
        """
        health, reason = await self.health()
        if health is None:
            log.warning("%s", reason)
            log.warning("%s", _UNREACHABLE_HINT)
            log.warning("会話機能は復帰するまで使えません(音楽は動きます)。")
            return None

        if health.get("status") != "ok":
            log.warning("APIがまだモデルを読み込んでいます。復帰すれば答えられるようになります。")
            return None

        try:
            async with self._session.get(f"{self.base}/v1/models") as resp:
                if resp.status == 401:
                    # The one thing here that never comes right on its own.
                    raise RuntimeError(
                        "APIキーが正しくありません。RULA_API_KEY を確認してください。\n"
                        "キーが手元にない場合は python make_api_key.py で再発行できます"
                        "(古いキーは無効になります)。")
                if resp.status != 200:
                    # A gateway between here and the API, having a bad moment.
                    # /health just answered, so the API itself is alive.
                    log.warning("APIの確認に失敗しました(%s)。%s", resp.status,
                                _gateway_reason(resp.status, await resp.text()) or "")
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("APIキーを確認できませんでした: %s", ui.short_error(e))
            return None
        return health

    async def ask(self, prompt: str, *, think: bool = False) -> str:
        """A one-shot answer, with no memory of it kept either side.

        History lives on the server keyed by `user`, so a fresh id per call is
        what stops a translation being answered in the context of whatever the
        channel was chatting about a minute ago — and stops the translation
        turning up in that conversation afterwards. Every task here wants that:
        they are transformations of a given text, not a continuing exchange.
        """
        conversation = f"task-{uuid.uuid4().hex}"
        parts = []
        async for kind, chunk in self.stream(prompt, conversation, think=think):
            if kind == "answer":
                parts.append(chunk)
        return "".join(parts).strip()

    async def stream(self, message: str, conversation: str,
                     image: str | None = None, think: bool = False):
        """Yield ("thinking" | "answer", chunk) as the API produces them."""
        content: list[dict] = [{"type": "text", "text": message}]
        if image:
            content.append({"type": "image_url", "image_url": {"url": image}})
        payload = {
            "model": self.model,
            # Only the newest turn: history lives on the server, keyed by `user`.
            "messages": [{"role": "user", "content": content}],
            "user": conversation,
            "stream": True,
            "think": think,
        }
        async with self._session.post(f"{self.base}/v1/chat/completions",
                                      json=payload) as resp:
            if resp.status != 200:
                raise ApiError(resp.status, await _detail(resp))
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
                    continue  # a malformed frame is not worth losing the turn over
                # Reasoning arrives on its own field, so it can be shown as
                # working rather than pasted into the answer.
                if delta.get("reasoning_content"):
                    yield "thinking", delta["reasoning_content"]
                if delta.get("content"):
                    yield "answer", delta["content"]


async def _detail(resp) -> str:
    try:
        body = await resp.json()
        return str(body.get("detail") or body)[:200]
    except Exception:
        return (await resp.text())[:200]


def split_message(text: str, limit: int = SPLIT_LIMIT) -> list[str]:
    """Break an answer into Discord-sized pieces, preferring paragraph breaks."""
    parts, rest = [], text.strip()
    while len(rest) > limit:
        cut = _cut_point(rest, limit)
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        parts.append(rest)
    return _balance_fences(parts)


def _cut_point(text: str, limit: int) -> int:
    window = text[:limit]
    for sep in ("\n\n", "\n", "。", "、", " "):
        idx = window.rfind(sep)
        # Not in the opening fifth: cutting there leaves a stub and shoves almost
        # the whole part into the next message.
        if idx > limit // 5:
            return idx + len(sep)
    return limit


def _balance_fences(parts: list[str]) -> list[str]:
    """Close and reopen code fences across a split.

    A fence left hanging at a split point turns the remainder of the answer —
    and whatever anyone posts next — into one long code block.
    """
    out, language = [], ""
    for part in parts:
        if language:
            part = f"```{language}\n{part}"
        fences = re.findall(r"^```(\w*)", part, re.M)
        if len(fences) % 2:
            language = fences[-1] or ""
            part = part.rstrip() + "\n```"
        else:
            language = ""
        out.append(part)
    return out


# The labels the live message cycles through. Words rather than the hourglass
# and thought-bubble that were here before: with emoji out, the title carries the
# state on its own, and the embed colour still says it at a glance.
WORKING_TITLE = "考えています…"
QUEUED_TITLE = "順番待ちです…"
SEARCH_TITLE = "検索しています…"
ERROR_TITLE = "エラー"

# Shown when there is no API key, which on a hosting service is the normal state
# until the API on the home PC has been given a public address. Says what to set
# rather than just that something is missing, because the person who sees this in
# a channel is usually not the person who deployed the bot.
# Shown where music is welcome but the model is not. Says what still works,
# because a bare refusal in a server the bot is otherwise active in reads like
# the bot is broken.
CHAT_ELSEWHERE = (
    "会話機能はこのサーバーでは使えません。\n"
    "音楽コマンドは使えます。R!help で一覧が出ます。")

CHAT_DISABLED = (
    "このBotでは会話機能が設定されていません。音楽コマンドは使えます (R!help)。\n"
    "有効にするには RULA_API_KEY と、外部ホストなら RULA_API_BASE に "
    "APIの公開URLを設定してください。")


def _live_view(thinking: str, answer: str,
               searching: str = "") -> tuple[str | None, str, int]:
    """(title, description, colour) for the message while it is being written.

    Returned as plain values rather than an Embed so the caller can tell whether
    anything actually changed since the last edit — two Embeds built from the
    same text are different objects, and rewriting on every token would put the
    bot straight into Discord's edit rate limit.
    """
    if answer:
        # Once the answer starts it replaces the reasoning outright: it is what
        # was actually asked for, and it is seconds from being final.
        return None, clip(answer, MAX_EMBED_CHARS - 4) + " ▌", COLOR_ANSWER
    if searching:
        # Shown ahead of the reasoning, because a search or tool is the one part of a
        # slow turn that has an obvious cause — and being told what is being
        # looked up is more reassuring than being told it is thinking.
        title = "猫語を処理中…" if "猫語" in searching else SEARCH_TITLE
        return title, f"> {clip(searching, 200)}", COLOR_WORKING
    if thinking:
        tail = thinking[-THINKING_TAIL:].replace("\n", " ").strip()
        lead = "…" if len(thinking) > THINKING_TAIL else ""
        return WORKING_TITLE, f"> {lead}{tail}", COLOR_WORKING
    return WORKING_TITLE, "", COLOR_WORKING


def _first_image(message: discord.Message) -> str | None:
    for attachment in message.attachments:
        if (attachment.content_type or "").startswith("image/"):
            return attachment.url
    return None


class RulaBot(commands.Bot):
    def __init__(self, rula: RulaClient, think_mode: str,
                 chat_guilds: set[int] | None,
                 connector: aiohttp.BaseConnector | None = None):
        intents = discord.Intents.default()
        # Privileged, and the whole point of the bot: without it every message
        # arrives with empty content, so there is nothing to answer and no
        # prefix to find either.
        intents.message_content = True
        # In Intents.default() already, but named here because music depends on
        # it: without voice states the bot cannot see which channel someone is
        # in, and R!play has nowhere to go.
        intents.voice_states = True
        # Also already on, and named for the same reason: this is what delivers
        # on_audit_log_entry_create, the route to noticing timeouts that needs
        # no Developer Portal change.
        intents.moderation = True
        # The other route. Off unless asked for, because requesting a privileged
        # intent that has not been enabled in the portal stops the bot from
        # starting at all — a worse outcome than missing the reason on a
        # timeout announcement. RULA_MEMBERS_INTENT=1 turns it on.
        if os.environ.get("RULA_MEMBERS_INTENT", "").strip().lower() in (
                "1", "true", "on", "yes"):
            intents.members = True
            log.info("SERVER MEMBERS INTENT を要求します"
                     "(Developer Portal で有効化されている必要があります)")
        super().__init__(
            # R! only. A mention is deliberately not a prefix as well: "@Rula
            # help me with this" would then run the help command instead of
            # being the question it obviously is.
            command_prefix=list(PREFIXES),
            intents=intents,
            # The model's output is untrusted text. Without this, an answer that
            # happens to contain @everyone would ping the server.
            allowed_mentions=discord.AllowedMentions.none(),
            # The built-in one paginates into plain messages and is in English.
            # R!help below replaces it.
            help_command=None,
            # Carries the DNS cache. Every HTTP call discord.py makes goes
            # through this, which is where the repeated lookups were coming from.
            connector=connector)
        self.rula = rula
        self.think_mode = think_mode
        # Applies to the conversation only. Music is answered in every server.
        self.chat_guilds = chat_guilds
        # One turn at a time. The API serialises anyway; queueing here as well is
        # what lets the wait be shown to the person waiting.
        self._turn = asyncio.Semaphore(1)
        # Said once, not once per message, when Embed Links turns out to be missing.
        self._warned_embed = False

    async def setup_hook(self):
        await self.add_cog(Chat(self))
        await self.add_cog(assist.Assist(self))
        await self.add_cog(timeoutwatch.TimeoutWatch(self))
        await self.add_cog(songlink.SongLink(self))
        await self.add_cog(media_extract.MediaExtract(self))
        await self.add_cog(media_extract.UrlSummary(self))
        await self.add_cog(freegames.FreeGames(self))
        await self.add_cog(streamnotify.StreamNotify(self))
        await self.add_cog(botdetect.BotDetect(self))
        if music is None:
            log.info("音楽機能: 未搭載 (music.py がありません)")
            return
        if not MUSIC_ENABLED:
            log.info("音楽機能: 無効 (MUSIC_ENABLED / MUSIC=off)")
            return
        await self.add_cog(music.Music(self))
        reason = music.unavailable()
        if reason:
            # A warning, not a failure. Chat is the older half of this bot and
            # has no business being held up by a missing audio dependency.
            log.warning("音楽機能は無効です。\n%s", reason)
        else:
            log.info("音楽機能: 有効 (ffmpeg=%s)", music.FFMPEG_EXE)

    async def on_ready(self):
        log.info("接続しました: %s (%d サーバー)", self.user, len(self.guilds))
        # Named with their ids, so the right value for RULA_CHAT_GUILDS can be
        # copied out of the log instead of hunted for in the Discord client.
        for guild in self.guilds:
            log.info("  参加中: %s (%d)", guild.name, guild.id)
        log.info("音楽機能: 全サーバーで利用可能")
        if self.chat_guilds is None:
            log.info("会話機能: 全サーバーで利用可能"
                     "(RULA_CHAT_GUILDS で制限できます)")
            return
        log.info("会話機能: %s のみ",
                 ", ".join(str(g) for g in sorted(self.chat_guilds)))
        # An allowlist matching nothing is silent by design: every mention is
        # dropped with no error anywhere. Said out loud here, because otherwise
        # the only symptom is a bot that looks online and ignores every question
        # while still playing music, which reads as a broken model rather than a
        # configured restriction.
        if not self.chat_guilds & {g.id for g in self.guilds}:
            log.warning("会話許可サーバーのIDが、参加中のどのサーバーとも一致しません。"
                        "このままでは全ての質問を無視します(音楽は動きます)。")
            log.warning("上の「参加中」行のIDに直すか、"
                        "RULA_CHAT_GUILDS / DISCORD_GUILDS を外してください。")

    async def on_message(self, message: discord.Message):
        """Commands first, conversation second.

        Overriding this means process_commands is no longer called for us, so the
        dispatch below is the only route into any R! command. Doing it by hand is
        what lets one gate — _allowed — cover the commands and the chat alike,
        instead of the allowlist quietly applying to only one of them.
        """
        if not self._allowed(message):
            return
        # Before commands: a warning about a link is worth giving whether or not
        # the message was also a command, and whether or not it is understood.
        await self._check_links(message)
        ctx = await self.get_context(message)
        if ctx.prefix is not None:
            await self._run_command(ctx)
            return
        if not self._addressed(message):
            return
        text = self._strip_mention(message)[:MAX_INPUT_CHARS]
        image = _first_image(message)
        if not text and not image:
            await self._say(
                embed("聞きたいことを続けて書いてください。R!help でコマンド一覧が出ます。",
                      "呼びましたか?", COLOR_WORKING), message)
            return
        await self._answer(message, text or "この画像について説明してください。", image)

    async def _check_links(self, message: discord.Message):
        """Warn about a link that is built like an attack.

        Deliberately not gated on the chat allowlist: a phishing link is a
        problem in any server, and this costs no GPU and no network — it is
        string inspection, and it runs before anything can go wrong with it.
        """
        if not linkguard.ENABLED:
            return
        if "http" not in message.content and not message.attachments:
            return
        try:
            verdicts = await linkguard.scan_message(message.content,
                                                    message.attachments)
        except Exception:
            # A checker that throws must not take the message with it: the
            # person posted a link, not a request to be disconnected.
            log.exception("リンクの検査に失敗しました")
            return
        flagged = [v for v in verdicts if v.worth_saying]
        if not flagged:
            return

        log.warning("危険なリンク ch=%s user=%s: %s", message.channel.id,
                    message.author.id,
                    " / ".join(f"{v.level}:{v.url[:80]}" for v in flagged[:3]))
        if linkguard.ACTION == "log":
            return

        deleted = False
        if linkguard.ACTION == "delete":
            try:
                await message.delete()
                deleted = True
            except discord.Forbidden:
                log.warning("メッセージを削除する権限がありません。警告のみ行います。")
            except ui.SEND_ERRORS as e:
                log.warning("メッセージを削除できませんでした: %s", ui.short_error(e))

        title, body = linkguard.summary(flagged)
        body = f"{body}\n\n投稿者: {message.author.display_name}"
        if deleted:
            # Replying to a message that no longer exists fails, and the warning
            # matters more here than anywhere else — it is the only trace left
            # of what was removed.
            body += " (メッセージは削除されました)"
            try:
                await message.channel.send(
                    **self._payload(message.channel,
                                    embed(body, title, COLOR_ERROR)))
            except ui.SEND_ERRORS as e:
                log.warning("警告を送れませんでした: %s", ui.short_error(e))
        else:
            await self._say(embed(body, title, COLOR_ERROR), message)

    async def _run_command(self, ctx: commands.Context):
        """Invoke an R! command, or explain that there is no such thing.

        The unknown-command reply is held back unless the word after the prefix
        looks like one: "R!" is a plausible thing to type mid-sentence in
        Japanese, and a bot that answers every one of those is a bot people mute.
        """
        if ctx.command is not None:
            await self.invoke(ctx)
            return
        name = ctx.invoked_with or ""
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,19}", name):
            return
        close = difflib.get_close_matches(name.lower(), self._command_names(), 1)
        hint = f"もしかして: R!{close[0]}\n" if close else ""
        await self._say(embed(f"{hint}R!help でコマンド一覧が出ます。",
                              f"「{ctx.prefix}{ui.clip(name, 20)}」は知らないコマンドです",
                              COLOR_ERROR), ctx.message)

    def _command_names(self) -> list[str]:
        names = []
        for command in self.commands:
            names.append(command.name)
            names.extend(command.aliases)
        return names

    def _allowed(self, message: discord.Message) -> bool:
        """Whether this bot will react to this message at all.

        No longer a per-server allowlist. Music is answered wherever the bot has
        been invited; only the conversation is restricted, by _chat_allowed.
        """
        if message.author.bot:
            return False
        # DMs are ignored outright, without so much as a refusal: anyone who
        # shares a server with the bot can open one, and a reply there is a
        # conversation nobody else in the server can see. Music needs a server
        # regardless — there is no voice channel in a DM.
        return message.guild is not None

    def _chat_allowed(self, guild: discord.Guild | None) -> bool:
        """Whether the model may be talked to here.

        Separate from _allowed because the two halves cost different things. A
        song is streamed from YouTube and costs this process some bandwidth; a
        turn of conversation occupies the single GPU behind the API, and every
        server added would be queueing against the same one.
        """
        return (self.chat_guilds is None
                or (guild is not None and guild.id in self.chat_guilds))

    def _addressed(self, message: discord.Message) -> bool:
        # Silently, where the conversation is not allowed. A mention is ambient
        # — people write the bot's name in passing — and answering every one of
        # them with a refusal would be worse than not answering. R!ask says so
        # out loud instead, because that was typed deliberately.
        if not self._chat_allowed(message.guild):
            return False
        # @everyone technically mentions this bot too. Answering it would mean
        # replying to every announcement in the server.
        if message.mention_everyone:
            return False
        if self.user in message.mentions:
            return True
        # A reply to something Rula said continues that exchange; making people
        # re-mention it every turn is friction nobody expects from a chat bot.
        replied = message.reference.resolved if message.reference else None
        return (isinstance(replied, discord.Message)
                and replied.author.id == self.user.id)

    def _strip_mention(self, message: discord.Message) -> str:
        text = re.sub(rf"<@!?{self.user.id}>", " ", message.content)
        return re.sub(r"[ \t]+", " ", text).strip()

    def _wants_thinking(self, text: str) -> bool:
        if self.think_mode == "on":
            return True
        if self.think_mode == "off":
            return False
        return bool(_NEEDS_THOUGHT.search(text))

    async def _answer(self, message: discord.Message, text: str, image: str | None):
        # The one gate both routes into chat pass through: a mention and R!ask
        # both land here, so this is said once rather than at each entrance.
        # Mentions never reach it — _addressed drops those quietly — so in
        # practice this answers R!ask, which was typed on purpose.
        if not self._chat_allowed(message.guild):
            await self._say(embed(CHAT_ELSEWHERE, "会話機能は使えません",
                                  COLOR_WORKING), message)
            return
        # An unconfigured API is explained once too, rather than turning into a
        # 401 dressed up as a generation failure.
        if not self.rula.configured:
            await self._say(embed(CHAT_DISABLED, "会話機能は無効です", COLOR_WORKING),
                            message)
            return
        # Said before the queue is joined, so someone waiting behind another turn
        # is told that rather than watching an idle message.
        queued = self._turn.locked()
        placeholder = await self._say(embed(
            "", QUEUED_TITLE if queued else WORKING_TITLE, COLOR_WORKING), message)
        if placeholder is None:
            return
        async with self._turn:
            if queued:
                # The wait is over, and with thinking off nothing streams until
                # the answer lands — leaving "順番待ち" up would misreport it.
                await self._edit(placeholder,
                                 embed("", WORKING_TITLE, COLOR_WORKING))
            try:
                await self._stream_into(placeholder, message, text, image)
            except (ApiError, llm.LLMError) as e:
                log.warning("AI %s: %s", getattr(e, "status", "-"), e)
                await self._fail(placeholder, e.as_japanese())
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("APIに接続できません: %s", e)
                await self._fail(placeholder,
                                 "APIサーバーに接続できません。起動しているか確認してください。")
            except Exception:
                log.exception("回答に失敗しました")
                await self._fail(placeholder, "回答の生成に失敗しました。")

    async def run_task(self, message: discord.Message, prompt: str, *,
                       title: str, think: bool = False):
        """Run a one-shot AI task and reply with the result.

        Shares the turn semaphore with ordinary chat on purpose. There is one
        GPU behind the API, and a translation queued behind a long answer has to
        wait for it either way — going through the same gate is what lets the
        person waiting be told so, instead of watching nothing happen.
        """
        if not self._chat_allowed(message.guild):
            await self._say(embed(CHAT_ELSEWHERE, "会話機能は使えません",
                                  COLOR_WORKING), message)
            return
        if not self.rula.configured:
            await self._say(embed(CHAT_DISABLED, "会話機能は無効です",
                                  COLOR_WORKING), message)
            return

        queued = self._turn.locked()
        placeholder = await self._say(embed(
            "", QUEUED_TITLE if queued else WORKING_TITLE, COLOR_WORKING), message)
        if placeholder is None:
            return
        async with self._turn:
            if queued:
                await self._edit(placeholder,
                                 embed("", WORKING_TITLE, COLOR_WORKING))
            try:
                answer = await self.rula.ask(prompt, think=think)
            except (ApiError, llm.LLMError) as e:
                log.warning("AI %s: %s", getattr(e, "status", "-"), e)
                await self._fail(placeholder, e.as_japanese())
                return
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("APIに接続できません: %s", ui.short_error(e))
                await self._fail(placeholder,
                                 "APIサーバーに接続できません。")
                return
            except Exception:
                log.exception("%s に失敗しました", title)
                await self._fail(placeholder, "生成に失敗しました。")
                return

        if not answer:
            await self._fail(placeholder, "うまく生成できませんでした。")
            return
        parts = split_message(answer, SPLIT_LIMIT
                              if ui.can_embed(placeholder.channel)
                              else PLAIN_SPLIT_LIMIT)
        await self._edit(placeholder, embed(parts[0], title))
        for part in parts[1:]:
            try:
                await placeholder.channel.send(
                    **self._payload(placeholder.channel, embed(part)))
            except ui.SEND_ERRORS as e:
                log.warning("続きを送れませんでした: %s", ui.short_error(e))
                return

    async def _fail(self, placeholder: discord.Message, reason: str):
        await self._edit(placeholder, embed(reason, ERROR_TITLE, COLOR_ERROR))

    async def _stream_into(self, placeholder: discord.Message,
                           message: discord.Message, text: str, image: str | None):
        think = self._wants_thinking(text)
        # Per channel, not per user: in a channel the exchange is shared, and a
        # follow-up like "じゃあその2つ目は?" means the previous turn whoever asked it.
        conversation = f"discord-{message.channel.id}"
        started = time.monotonic()
        thinking, answer = [], []
        searching = ""
        shown, last_edit = None, 0.0

        async for kind, chunk in self.rula.stream(text, conversation, image, think):
            if kind == "searching":
                # Not accumulated: only the query being run right now matters,
                # and it should be shown at once rather than at the next tick.
                searching, last_edit = chunk, 0.0
            elif kind == "thinking":
                thinking.append(chunk)
            else:
                answer.append(chunk)
            now = time.monotonic()
            if now - last_edit < EDIT_INTERVAL:
                continue
            view = _live_view("".join(thinking), "".join(answer), searching)
            if view != shown:
                shown, last_edit = view, now
                title, description, color = view
                await self._edit(placeholder, embed(description, title, color))

        await self._finish(placeholder, "".join(answer), "".join(thinking))
        # The channel and the timings, not the message: this is someone's
        # conversation, and api.py does not log message bodies either.
        log.info("answer ch=%s %.1fs think=%s %d字", message.channel.id,
                 time.monotonic() - started, think, len("".join(answer)))

    async def _finish(self, placeholder: discord.Message, answer: str, thinking: str):
        answer = answer.strip()
        if not answer:
            # The API recovers from reasoning that ate its own budget; if it still
            # comes back empty there is nothing to show but the fact.
            log.warning("空の回答 (thinking=%d字)", len(thinking))
            answer = "うまく答えられませんでした。もう一度聞いてもらえますか。"
        # Sized for where it is actually going: a plain-text fallback holds 2000
        # characters, and splitting for 4096 would lose the tail of a long answer.
        parts = split_message(answer, SPLIT_LIMIT
                              if ui.can_embed(placeholder.channel)
                              else PLAIN_SPLIT_LIMIT)
        await self._edit(placeholder, embed(parts[0]))
        for part in parts[1:]:
            try:
                await placeholder.channel.send(
                    **self._payload(placeholder.channel, embed(part)))
            except ui.SEND_ERRORS as e:
                log.warning("続きを送れませんでした: %s", ui.short_error(e))
                return

    def _payload(self, channel, body: discord.Embed) -> dict:
        """ui.payload, plus a one-off complaint when embeds are not allowed.

        Said once per process rather than once per message: without Embed Links
        every reply falls back to plain text, and a log line for each of them
        would bury everything else.
        """
        if not ui.can_embed(channel) and not self._warned_embed:
            self._warned_embed = True
            log.warning("このチャンネルで埋め込みを送れません(Embed Links 権限が不足)。"
                        "平文で応答します。")
            log.warning("サーバー設定 → 連携サービス → RulaAI、または"
                        "チャンネルの権限で「埋め込みリンク」を許可してください。")
        return ui.payload(channel, body)

    async def _say(self, body: discord.Embed,
                   message: discord.Message) -> discord.Message | None:
        try:
            return await message.reply(
                **self._payload(message.channel, body), mention_author=False)
        except ui.SEND_ERRORS as e:
            # A deleted message, no permission to post here, or Discord simply
            # out of reach. None of the three is recoverable from in here, and
            # raising would only take down whatever was trying to speak.
            log.warning("返信できませんでした: %s", ui.short_error(e))
            return None

    async def _edit(self, placeholder: discord.Message, body: discord.Embed):
        try:
            await placeholder.edit(**self._payload(placeholder.channel, body))
        except ui.SEND_ERRORS as e:
            log.warning("編集できませんでした: %s", ui.short_error(e))

    async def on_command_error(self, ctx: commands.Context, error: Exception):
        """The backstop, for commands whose cog does not handle its own.

        Discord.py dispatches here even after a cog has answered, so a cog with
        a handler is skipped outright — otherwise every music error would be
        reported to the channel twice.
        """
        if ctx.cog and commands.Cog._get_overridden_method(ctx.cog.cog_command_error):
            return
        if isinstance(error, commands.CommandInvokeError):
            error = error.original
        if isinstance(error, ui.NETWORK_ERRORS):
            # One line rather than a traceback, and the reply is attempted but
            # not depended on: _say swallows the failure it will probably hit.
            log.warning("ネットワークエラーで %s を実行できませんでした: %s",
                        ctx.command, ui.short_error(error))
            await self._say(embed("Discordに接続できませんでした。"
                                  "少し待ってからもう一度どうぞ。",
                                  ERROR_TITLE, COLOR_ERROR), ctx.message)
            return
        if isinstance(error, commands.CommandOnCooldown):
            await self._say(embed(f"少し待ってからどうぞ ({error.retry_after:.0f}秒)。",
                                  ERROR_TITLE, COLOR_ERROR), ctx.message)
            return
        if isinstance(error, discord.Forbidden):
            log.warning("権限不足: %s", error)
            await self._say(embed("権限が足りないため実行できませんでした。",
                                  ERROR_TITLE, COLOR_ERROR), ctx.message)
            return
        if isinstance(error, (commands.UserInputError, commands.CheckFailure)):
            await self._say(embed(str(error), ERROR_TITLE, COLOR_ERROR), ctx.message)
            return
        log.exception("コマンドが失敗しました: %s", ctx.command, exc_info=error)
        await self._say(embed("コマンドの実行に失敗しました。", ERROR_TITLE, COLOR_ERROR),
                        ctx.message)


# Kept beside the music list rather than inside the help command, so that adding
# a command and forgetting to document it is a visible omission in one place.
HELP_CHAT = [
    ("@Rula <質問>", "メンションで話しかけます (返信でも続けられます)"),
    ("R!ask <質問>", "コマンドとして質問します"),
    ("R!svc <サービス名>", "外部サービスの現在ステータスを確認します"),
    ("R!scan <URL>", "リンクが危険でないか検査します"),
    ("R!reset", "このチャンネルの会話の記憶を消します"),
    ("R!status", "AIの状態とAPIキーの残量を表示します"),
    ("R!ping", "応答速度を表示します"),
    ("R!help", "この一覧を表示します"),
]


def get_help_pages() -> list[dict]:
    """Return categorized pages for the help command."""
    return [
        {
            "category": "会話・AI・要約",
            "description": "AIとの対話、質問、翻訳、動画やWeb記事の要約機能",
            "sections": [
                ("会話", HELP_CHAT),
                ("AI機能", assist.HELP_LINES),
                ("動画・記事要約", media_extract.SUMMARY_HELP_LINES),
            ],
        },
        {
            "category": "音楽・メディア抽出",
            "description": "VCでの音楽再生、plist人気曲再生、MP3/MP4抽出、曲リンク",
            "sections": [
                ("音楽", music.HELP_LINES if music is not None else [("(音楽機能は未搭載です)", "")]),
                ("メディア抽出", media_extract.EXTRACT_HELP_LINES),
                ("曲リンク", songlink.HELP_LINES),
            ],
        },
        {
            "category": "通知・外部サービス",
            "description": "無料配布ゲーム通知、YouTube/Twitch配信通知、稼働状況確認",
            "sections": [
                ("無料ゲーム通知", freegames.HELP_LINES),
                ("配信開始通知", streamnotify.HELP_LINES),
                ("外部サービス稼働状況", [
                    ("R!svc <サービス名>", "外部サービスの障害・稼働状況を確認します"),
                    ("R!svc list", "確認できるサービス一覧を表示します"),
                ]),
            ],
        },
        {
            "category": "管理・セキュリティ",
            "description": "タイムアウトモデレーション監視、危険リンク検査",
            "sections": [
                ("モデレーション", timeoutwatch.HELP_LINES),
                ("リンク安全検査", [
                    ("R!scan <URL>", "リンクが危険でないか検査します (返信でも可)"),
                ]),
                ("外部Bot検知", [
                    ("R!botdetect", "外部Bot・ユーザーアプリ検知の状態を確認します"),
                ]),
            ],
        },
    ]


class HelpSelect(discord.ui.Select):
    def __init__(self, pages: list[dict], current_index: int):
        options = [
            discord.SelectOption(
                label=f"{i + 1}. {p['category']}",
                description=p['description'][:50],
                value=str(i),
                default=(i == current_index)
            )
            for i, p in enumerate(pages)
        ]
        super().__init__(placeholder="カテゴリを選んでジャンプ", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view: HelpView = self.view
        view.current_page = int(self.values[0])
        view.update_components()
        await interaction.response.edit_message(embed=view.get_current_embed(), view=view)


class HelpView(discord.ui.View):
    def __init__(self, bot: "RulaBot", ctx: commands.Context, pages: list[dict], start_page: int = 0):
        super().__init__(timeout=120)
        self.bot = bot
        self.ctx = ctx
        self.pages = pages
        self.current_page = max(0, min(start_page, len(pages) - 1))
        self.message: discord.Message | None = None
        self.update_components()

    def update_components(self):
        self.clear_items()
        self.add_item(HelpSelect(self.pages, self.current_page))

        prev_btn = discord.ui.Button(
            label="◀ 前へ",
            style=discord.ButtonStyle.secondary,
            disabled=(self.current_page == 0)
        )
        prev_btn.callback = self.prev_callback
        self.add_item(prev_btn)

        next_btn = discord.ui.Button(
            label="次へ ▶",
            style=discord.ButtonStyle.primary,
            disabled=(self.current_page >= len(self.pages) - 1)
        )
        next_btn.callback = self.next_callback
        self.add_item(next_btn)

    async def prev_callback(self, interaction: discord.Interaction):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_components()
            await interaction.response.edit_message(embed=self.get_current_embed(), view=self)

    async def next_callback(self, interaction: discord.Interaction):
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            self.update_components()
            await interaction.response.edit_message(embed=self.get_current_embed(), view=self)

    def get_current_embed(self) -> discord.Embed:
        page = self.pages[self.current_page]
        lines = []
        for heading, entries in page["sections"]:
            lines.append(f"**{heading}**")
            lines += [f"`{name}` — {what}" for name, what in entries if name]
            lines.append("")

        reason = music.unavailable() if music is not None else None
        if self.current_page == 1 and reason:
            lines.append(f"(音楽機能は現在使えません: {reason.splitlines()[0]})")
        if self.current_page == 0 and not self.bot.rula.configured:
            lines.append("(会話機能は現在無効です: RULA_API_KEY が未設定)")

        lines.append("大文字小文字と全角の `R！` も同じように使えます。")

        body_embed = embed("\n".join(lines), f"コマンド一覧: {page['category']}", COLOR_ANSWER)
        body_embed.set_footer(text=f"ページ {self.current_page + 1} / {len(self.pages)} • ボタンまたはドロップダウンで切替")
        return body_embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "このメニューはコマンドを実行した人のみ操作できます。自分で `R!help` を実行してください。",
            ephemeral=True
        )
        return False

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except ui.SEND_ERRORS:
                pass


class Chat(commands.Cog, name="会話"):
    """The R! side of what the bot could already do by mention."""

    def __init__(self, bot: RulaBot):
        self.bot = bot

    @commands.command(name="help", aliases=["h", "commands", "cmd"])
    async def help(self, ctx: commands.Context, *, target: str = ""):
        """コマンド一覧をページごとに表示します。R!help [ページ番号またはカテゴリ]"""
        pages = get_help_pages()
        start_page = 0
        target = target.strip().lower()

        if target:
            if target.isdigit():
                start_page = int(target) - 1
            elif any(k in target for k in ("会話", "ai", "要約", "chat")):
                start_page = 0
            elif any(k in target for k in ("音楽", "music", "メディア", "曲", "mp3", "mp4")):
                start_page = 1
            elif any(k in target for k in ("通知", "ゲーム", "game", "配信", "stream", "svc")):
                start_page = 2
            elif any(k in target for k in ("管理", "モデレーション", "安全", "scan", "link")):
                start_page = 3

        view = HelpView(self.bot, ctx, pages, start_page=start_page)
        first_embed = view.get_current_embed()
        try:
            view.message = await ctx.reply(**ui.payload(ctx.channel, first_embed), view=view, mention_author=False)
        except ui.SEND_ERRORS:
            view.message = await self.bot._say(first_embed, ctx.message)

    @commands.cooldown(3, 15.0, commands.BucketType.user)
    @commands.command(name="svc", aliases=["service", "svcs", "services"])
    async def svc(self, ctx: commands.Context, *, name: str = ""):
        """外部サービスのステータスを確認します。R!svc <サービス名> / R!svc list"""
        name = name.strip()
        if not name or name.lower() in ("list", "一覧"):
            services = svcstatus.list_services()
            # Show in groups of 8, joined with spaces
            groups = [", ".join(services[i:i + 8]) for i in range(0, len(services), 8)]
            body = (
                "使い方: `R!svc <サービス名>`\n\n"
                "**サービス一覧:**\n"
                + "\n".join(f"`{g}`" for g in groups)
            )
            await self.bot._say(embed(body, "対応サービス一覧", COLOR_ANSWER),
                                ctx.message)
            return

        async with ui.typing(ctx):
            result = await svcstatus.fetch(name)

        if result is None:
            known = ", ".join(svcstatus.list_services()[:12]) + " ..."
            raise commands.UserInputError(
                f"サービス `{name}` は未対応です。\n"
                f"対応サービス一覧は `R!svc list` で確認できます。\n"
                f"例: {known}")

        lines = [f"{result.emoji} **{result.description}**"]
        if result.indicator == "unknown":
            lines.append(f"🔗 {result.url}")
        color = result.color
        await self.bot._say(
            embed("\n".join(lines), result.name, color),
            ctx.message)

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context):
        """Discordとの往復にかかっている時間を表示します。"""
        latency = self.bot.latency
        # nan until the first heartbeat has come back.
        shown = "計測中" if latency != latency else f"{round(latency * 1000)} ms"
        await self.bot._say(embed(f"Discord との遅延: {shown}", "ping", COLOR_ANSWER),
                            ctx.message)

    @commands.command(name="status", aliases=["api"])
    async def status(self, ctx: commands.Context):
        """つないでいるAPIの状態を表示します。"""
        if not self.bot.rula.configured:
            await self.bot._say(embed(CHAT_DISABLED, "会話機能は無効です", COLOR_WORKING),
                                ctx.message)
            return
        health, reason = await self.bot.rula.health()
        if health is None:
            where = getattr(self.bot.rula, "base", "")
            await self.bot._say(
                embed(f"{reason}" + (f"\n\n接続先: {where}" if where else "")
                      + "\n音楽コマンドはこのまま使えます。",
                      ERROR_TITLE, COLOR_ERROR), ctx.message)
            return
        lines = [f"状態: {health.get('status')}",
                 f"モデル: {health.get('weights')}",
                 f"バックエンド: {health.get('backend')}"]
        if health.get("keys"):
            lines.append(f"APIキー: {health['keys']} 使用可")
        if health.get("thinking") is not None:
            lines.append(f"思考: {health.get('thinking')}")
        lines.append(f"思考モード設定: {self.bot.think_mode}")
        pool = getattr(self.bot.rula, "pool", None)
        if pool is not None:
            lines.append("")
            lines.append(pool.report())
        if music is None:
            lines.append("音楽: 未搭載")
        elif music.unavailable() is None:
            lines.append(f"音楽: 有効 (再生中 {len(self.bot.get_cog('音楽').players)} サーバー)")
        else:
            lines.append("音楽: 無効")
        if linkguard.ENABLED:
            vt = ("VirusTotal 併用" if linkguard.virustotal.ready
                  else "オフライン判定のみ")
            lines.append(f"リンク検査: 有効 (動作: {linkguard.ACTION} / {vt})")
        else:
            lines.append("リンク検査: 無効")
        lines.append("検索ツール: " + (f"有効 ({websearch.backend()})"
                                  if websearch.enabled() else "無効"))
        watch = self.bot.get_cog("タイムアウト")
        if watch is not None:
            lines.append("To監視: " + (f"有効 (検知: {watch.routes()})"
                                     if timeoutwatch.enabled() else "無効"))
        lines.append("曲リンク: " + ("有効" if songlink.enabled() else "無効"))
        await self.bot._say(embed("\n".join(lines), "状態", COLOR_ANSWER), ctx.message)

    @commands.command(name="reset", aliases=["forget", "リセット"])
    async def reset(self, ctx: commands.Context):
        """このチャンネルの会話の記憶を消します。"""
        # Only does anything on the external backends, where the history is kept
        # in this process. The local API holds its own and has no such button.
        if self.bot.rula.forget(f"discord-{ctx.channel.id}"):
            body = "このチャンネルの会話の記憶を消しました。"
        else:
            body = ("消す記憶がありませんでした。\n"
                    "(ローカルAPI利用時は、履歴はAPI側にあります)")
        await self.bot._say(embed(body, "リセット", COLOR_ANSWER), ctx.message)

    @commands.command(name="scan", aliases=["check", "link"])
    async def scan(self, ctx: commands.Context, *, target: str = ""):
        """リンクが安全か検査します。"""
        if not linkguard.ENABLED:
            raise commands.UserInputError(
                "リンク検査は無効になっています(linkguard.py の ENABLED)。")
        # Falls back to the message being replied to, so a suspicious link can
        # be checked without copying it out — which is the moment people are
        # most likely to click it by accident.
        if not target and ctx.message.reference:
            replied = ctx.message.reference.resolved
            if isinstance(replied, discord.Message):
                target = replied.content
        if not target:
            raise commands.UserInputError(
                "調べたいURLを続けて入力するか、対象メッセージに返信してください。")
        async with ui.typing(ctx):
            verdicts = await linkguard.scan_online(target)
        if not verdicts:
            raise commands.UserInputError("URLが見つかりませんでした。")
        title, body = linkguard.summary(verdicts)
        colour = COLOR_ERROR if verdicts[0].bad else COLOR_ANSWER
        if verdicts[0].level == linkguard.SAFE:
            body += "\n\n既知の手口には当てはまりませんでした。"
            body += "\n安全の保証ではありません。心当たりのないリンクは開かないでください。"
        await self.bot._say(embed(body, title, colour), ctx.message)

    @commands.command(name="ask", aliases=["a", "chat"])
    async def ask(self, ctx: commands.Context, *, question: str = ""):
        """メンションの代わりにコマンドとして質問します。"""
        text = question.strip()[:MAX_INPUT_CHARS]
        image = _first_image(ctx.message)
        if not text and not image:
            raise commands.UserInputError("聞きたいことを続けて書いてください。")
        await self.bot._answer(ctx.message,
                               text or "この画像について説明してください。", image)


def _load_env():
    """Read .env beside this file.

    Deliberately standalone rather than imported from api.py: that module pulls
    in FastAPI and the engine, which the bot has no use for.

    Reads this folder first, then RulaAI's — which is where the shared .env
    actually lives, holding the API key the bot authenticates with. A DisBot/.env
    is optional and wins where the two overlap, since setdefault keeps whichever
    value was seen first.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for folder in (here, os.path.dirname(here)):
        path = os.path.join(folder, ".env")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    name, value = line.split("=", 1)
                    os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


def _chat_guilds() -> set[int] | None:
    """Servers where the model may be talked to. None means anywhere.

    This restricts the conversation only. Music is not gated: the bot answers
    R!play in every server it is in, because playing audio costs nothing but
    bandwidth, while a turn of generation occupies the one GPU the API has.

    DISCORD_GUILDS is still read, and now means this narrower thing. It used to
    gate the whole bot, so an existing value carries over to the half that is
    still worth restricting rather than silently ceasing to do anything.
    """
    raw = (os.environ.get("RULA_CHAT_GUILDS", "").strip()
           or os.environ.get("DISCORD_GUILDS", "").strip())
    return {int(x) for x in re.findall(r"\d+", raw)} if raw else None


SETUP_HELP = """\
DISCORD_TOKEN が未設定です。RulaAI\\.env(このフォルダの1つ上)に追記してください。

  1. https://discord.com/developers/applications で New Application
  2. Bot タブ → Reset Token → 出てきた文字列を控える
  3. 同じ Bot タブの Privileged Gateway Intents で
     MESSAGE CONTENT INTENT を ON にする(これが無いと本文を読めません)
  4. .env に  DISCORD_TOKEN=<控えた文字列>
  5. OAuth2 → URL Generator で scopes=bot、権限は
     Send Messages / Read Message History / Embed Links と、
     音楽を使うなら Connect / Speak も選び、出たURLでサーバーに招待
"""


# How long a session has to last before its ending counts as "it was working"
# rather than "it cannot start". Resets the backoff, so an outage after an hour
# does not inherit the delay earned by a stumble at startup.
STABLE_SESSION = 120
RECONNECT_FIRST = 5
RECONNECT_MAX = 300


GATEWAY_HOST = "gateway.discord.gg"

# aiohttp forgets a resolved address after ten seconds, so on a host with an
# unreliable resolver very nearly every request is a fresh opportunity to fail —
# which is what turned one bad minute into a typing indicator, a reply and a
# gateway connection all dying separately. Ten minutes instead: the addresses of
# discord.com and the gateway do not move anything like that often, and a lookup
# that does not happen cannot fail.
DNS_CACHE_TTL = int(os.environ.get("RULA_DNS_TTL", "600"))


def _make_connector() -> aiohttp.TCPConnector:
    """A connector that remembers what it resolved.

    Built fresh for each attempt, because discord.py hands it to a ClientSession
    that closes it on shutdown — one kept across retries would be closed by the
    first of them.

    RULA_DNS_SERVERS overrides the system resolver, for a host whose own is the
    thing at fault. It needs aiodns, which is not a dependency, so an unusable
    setting degrades to the default rather than stopping the bot.
    """
    resolver = None
    servers = [s for s in re.split(r"[,\s]+",
                                   os.environ.get("RULA_DNS_SERVERS", "").strip()) if s]
    if servers:
        try:
            resolver = aiohttp.AsyncResolver(nameservers=servers)
            log.info("DNSサーバーを指定します: %s", ", ".join(servers))
        except Exception as e:
            log.warning("RULA_DNS_SERVERS を使えません(aiodns が必要です): %s",
                        ui.short_error(e))
    # Asking for A records only halves the queries on a resolver that is
    # struggling. Off by default, because it is wrong on an IPv6-only host.
    family = (socket.AF_INET
              if os.environ.get("RULA_FORCE_IPV4", "").strip().lower()
              in ("1", "true", "yes") else socket.AF_UNSPEC)
    return aiohttp.TCPConnector(use_dns_cache=True, ttl_dns_cache=DNS_CACHE_TTL,
                                resolver=resolver, family=family)


async def _wait_for_dns(host: str = GATEWAY_HOST):
    """Block until the gateway's name resolves.

    Not politeness — a workaround for a bug in discord.py. When the very first
    gateway connection fails, its reconnect handler reads self.ws.sequence on a
    self.ws that was never assigned, and dies with AttributeError instead of
    retrying. On a host with flaky DNS that turns a momentary lookup failure
    into a dead process.

    Resolving here first means bot.start() is only called when the network is
    actually up, which keeps us out of that path. It cannot be a guarantee — DNS
    can fail in the moment between this check and the connection — so
    _supervise catches it as well.
    """
    delay = RECONNECT_FIRST
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            return
        except OSError as e:
            log.warning("%s を名前解決できません: %s", host, e)
            log.info("%.0f秒後に再試行します。", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)


async def _supervise(token: str, key: str, base: str, model: str,
                     think_mode: str, guilds: set[int] | None):
    """Keep the bot running across network failures, without exiting.

    A dropped connection used to end the process: a DNS lookup for
    gateway.discord.gg fails for a few seconds, the error reaches main(), and the
    bot exits. A hosting panel that restarts on exit then turns every network
    hiccup into a fresh login — and Discord answers a stream of those by locking
    the whole host out of discord.com for up to an hour (Cloudflare 1015). A five
    second outage could cost sixty minutes.

    Retrying here keeps one process and one login session. Only transient network
    faults are retried; a bad token or a missing intent arrives as RuntimeError
    and still stops the bot, because those never fix themselves.
    """
    delay = RECONNECT_FIRST
    while True:
        await _wait_for_dns()
        started = time.monotonic()
        try:
            await _run(token, key, base, model, think_mode, guilds)
            return                      # a clean shutdown; nothing to retry
        except RuntimeError:
            # Configuration: a bad token, a missing intent, a rate-limited host.
            # None of those come right by trying again.
            raise
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            # socket.gaierror — the name resolution failure seen in the wild —
            # is an OSError, and aiohttp wraps it in ClientConnectorDNSError.
            # Both land here. One line, because the stack is aiohttp internals.
            reason = ui.short_error(e)
        except Exception as e:
            # Deliberately broad. This caught discord.py dereferencing a
            # websocket it never managed to open, which is an AttributeError
            # raised from inside its own reconnect logic — nothing this bot
            # could have anticipated by type, and no reason to die for. Logged
            # in full, because unlike the network errors above, a traceback here
            # might be pointing at a real bug.
            log.exception("予期しないエラーで切断されました")
            reason = ui.short_error(e)
        if time.monotonic() - started > STABLE_SESSION:
            delay = RECONNECT_FIRST
        log.warning("接続が切れました: %s", reason)
        log.info("%.0f秒後に再接続します(プロセスは終了しません)。", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX)


def _make_client(base: str, key: str, model: str):
    """Whichever backend is configured, preferring the hosted ones.

    Groq and OpenRouter win when keys for them exist, because that is the only
    reason to have set them. The local API stays as the fallback rather than
    being removed: it is the one that runs when nothing else is reachable, and
    on a machine with the GPU it is both faster and free.
    """
    external = llm.LLMClient(system=os.environ.get("RULA_SYSTEM", "").strip())
    if external.configured:
        log.info("AIバックエンド: %s (キー %d本)",
                 "/".join(external.pool.providers), len(external.pool))
        return external
    log.info("AIバックエンド: ローカルAPI %s", base)
    return RulaClient(base, key, model)


async def _run(token: str, key: str, base: str, model: str, think_mode: str,
               guilds: set[int] | None):
    async with _make_client(base, key, model) as rula:
        # Without anything configured there is nothing to preflight, and a bot
        # about to run perfectly well for music should not be stopped by it.
        health = await rula.preflight() if rula.configured else None
        if health:
            log.info("AI: %s (%s) think_mode=%s", health.get("weights"),
                     health.get("backend"), think_mode)
        elif rula.configured:
            # Connected to Discord regardless, so the bot is at least present and
            # can say what is wrong when someone asks, rather than being absent.
            log.warning("AIが応答しない状態で起動します(復帰すれば自動的に答えます)。")
        bot = RulaBot(rula, think_mode, guilds, connector=_make_connector())
        try:
            await bot.start(token)
        except discord.PrivilegedIntentsRequired:
            raise RuntimeError(
                "MESSAGE CONTENT INTENT が無効です。Discord Developer Portal の "
                "Bot → Privileged Gateway Intents で有効にしてください。") from None
        except discord.HTTPException as e:
            if e.status != 429:
                raise
            # Cloudflare 1015, not Discord's own per-route limit: the host's IP is
            # shut out of discord.com for logging in too often. Restarting is what
            # causes it and what extends it, so the only move is to stop.
            raise RuntimeError(
                "Discord がこのホストからのログインを一時的に制限しています "
                "(Cloudflare Error 1015)。\n"
                "  短時間に何度も起動したのが原因です。数分〜1時間ほどで解けます。\n"
                "  再起動を繰り返すと制限が延びます。しばらく待ってから起動して"
                "ください。") from None
        except discord.LoginFailure:
            raise RuntimeError("DISCORD_TOKEN が正しくありません。") from None
        finally:
            await bot.close()


def _force_utf8():
    """Make both streams UTF-8, where that is possible at all.

    A Windows console left at cp932 turns every Japanese log line into mojibake,
    and logging writes to stderr rather than stdout, so both need it.

    Guarded because neither stream is guaranteed to be a real file: a hosting
    panel that captures output can substitute an object with no reconfigure() on
    it, and crashing on startup over an encoding preference would be a poor
    trade — the bot runs fine either way.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _fatal(message: str) -> int:
    """Report, wait, then exit non-zero.

    The wait is the point. A hosting service that restarts the process whenever it
    exits turns a startup failure into a login attempt every few seconds, and
    Discord answers a stream of those by locking the whole host out of
    discord.com (Cloudflare Error 1015) — which is how a missing environment
    variable becomes an hour of downtime. It costs nothing when the failure is
    permanent, and breaks the spin when the supervisor is eager.
    """
    print(f"\n{message}")
    print(f"({RESTART_COOLDOWN}秒待ってから終了します。"
          "再起動の連打でDiscordから締め出されるのを防ぐためです)")
    time.sleep(RESTART_COOLDOWN)
    return 1


def main():
    _force_utf8()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _load_env()

    # Both of these are read at import as well, which happens before the line
    # above. Read again here so a value written in .env counts the same as one
    # set in the hosting panel.
    global MUSIC_ENABLED
    switch = os.environ.get("MUSIC", "").strip().lower()
    if switch in ("0", "false", "off", "no"):
        MUSIC_ENABLED = False
    elif switch in ("1", "true", "on", "yes"):
        MUSIC_ENABLED = True
    linkguard.reload_settings()

    if os.environ.get("MUSIC_DEBUG", "").strip().lower() in ("1", "true", "yes"):
        # discord.py narrates the voice handshake at DEBUG and says nothing at
        # INFO, so a connection that dies partway through leaves no trace at all.
        # This is the difference between "接続できませんでした" and knowing which
        # step stopped.
        for name in ("discord.voice_client", "discord.voice_state",
                     "discord.gateway"):
            logging.getLogger(name).setLevel(logging.DEBUG)
        log.info("MUSIC_DEBUG: 音声接続の詳細ログを有効にしました。")

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        return _fatal(SETUP_HELP)
    key = os.environ.get("RULA_API_KEY", "").strip()
    if not key:
        # Not fatal any more. The two halves of this bot have different needs —
        # music wants nothing but a Discord token, chat wants a reachable API —
        # and refusing to start over the second one means a bot that could be
        # playing music sits in a restart loop instead. Chat says so when asked.
        log.warning("RULA_API_KEY が未設定です。会話機能を無効にして起動します。")
        log.warning("(.env の SALT / HASH は照合用で、キーそのものではありません。"
                    " 手元に無ければ python make_api_key.py で再発行できます)")
        log.warning("音楽機能はこのままでも使えます。")
    think_mode = os.environ.get("DISCORD_THINK", "auto").strip().lower()
    if think_mode not in ("on", "off", "auto"):
        return _fatal(
            f"DISCORD_THINK は on / off / auto のどれかです(受け取った値: {think_mode})")
    # Read here rather than at import, so a value in .env is actually used.
    base = os.environ.get("RULA_API_BASE", DEFAULT_API_BASE).strip() or DEFAULT_API_BASE
    model = os.environ.get("RULA_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    try:
        asyncio.run(_supervise(token, key, base, model, think_mode,
                               _chat_guilds()))
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        return _fatal(str(e))
    except aiohttp.ClientError as e:
        # Anything _supervise did not consider retryable. A bare traceback here
        # says nothing useful about a misconfigured address.
        return _fatal(f"APIに接続できません: {base}\n  {e}\n{_UNREACHABLE_HINT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
