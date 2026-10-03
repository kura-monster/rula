"""Announce timeouts in the channel, and keep a tally.

Two ways of noticing a timeout, both listened for, because neither is strictly
better — they need different things and give back different amounts:

  on_audit_log_entry_create needs Intents.moderation, which is in
  Intents.default() already and is not privileged, plus the View Audit Log
  permission in the server. It carries the reason and the moderator.

  on_member_update needs no permission at all, but needs Intents.members —
  which is privileged, so it has to be switched on in the Developer Portal.
  It says only that the timeout happened: no reason, no moderator.

Whichever arrives is used, and _seen() stops the same timeout being announced
twice when both do. The member update usually arrives first, so it waits a
moment for the audit log to overtake it: the version with a reason in it is
worth a two second delay.

The counter is kept in a JSON file beside this one. It is the only state in the
whole bot that is supposed to outlive a restart, so it is written carefully:
to a temporary file first, then moved over the real one, so a process killed
mid-write leaves the previous tally intact rather than an empty file.
"""
import asyncio
import datetime
import json
import logging
import os
import tempfile
import time

import discord
from discord.ext import commands

import ui

log = logging.getLogger("rula.timeout")

# ---------------------------------------------------------------------------
# Set to False to stop announcing. TIMEOUT_WATCH=off does the same.
# ---------------------------------------------------------------------------
ENABLED = True

# Where to announce. Unset means the channel the timeout happened in cannot be
# known — a timeout has no channel — so it falls back to the server's system
# channel, then to the first channel the bot may write in.
CHANNEL_ENV = "TIMEOUT_CHANNEL"

STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "timeout_counts.json")

# Audit log entries arrive for every member edit — nickname changes, role
# grants. Only the ones that set a timeout are of interest.
_TIMEOUT_FIELD = "timed_out_until"

# How long the member-update route waits for the audit log entry to overtake it.
# The entry carries the reason; two seconds is worth paying for that.
AUDIT_GRACE = 2.0
# How long an announced timeout is remembered, so the second route recognises it
# as already handled. Only has to outlast the gap between the two events.
DEDUPE_TTL = 60.0


def enabled() -> bool:
    raw = os.environ.get("TIMEOUT_WATCH", "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return ENABLED


class Counter:
    """How many times each person has been timed out, across restarts."""

    def __init__(self, path: str = STORE):
        self.path = path
        self._counts: dict[str, int] = {}
        self._dirty = False
        self._lock = asyncio.Lock()
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._counts = {k: int(v) for k, v in data.items()
                                if isinstance(v, (int, float))}
            log.info("タイムアウト履歴を読み込みました (%d件)", len(self._counts))
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as e:
            # A corrupt file must not stop the bot. Starting from zero is a
            # worse tally but a working one.
            log.warning("タイムアウト履歴を読めませんでした: %s", e)

    @staticmethod
    def _key(guild_id: int, user_id: int) -> str:
        return f"{guild_id}:{user_id}"

    def count(self, guild_id: int, user_id: int) -> int:
        return self._counts.get(self._key(guild_id, user_id), 0)

    async def bump(self, guild_id: int, user_id: int) -> int:
        key = self._key(guild_id, user_id)
        total = self._counts.get(key, 0) + 1
        self._counts[key] = total
        await self._save()
        return total

    async def reset(self, guild_id: int, user_id: int) -> int:
        had = self._counts.pop(self._key(guild_id, user_id), 0)
        if had:
            await self._save()
        return had

    async def _save(self):
        async with self._lock:
            try:
                await asyncio.to_thread(self._write)
            except OSError as e:
                # Some hosting panels give a read-only or ephemeral filesystem.
                # The tally then lasts as long as the process, which is worth
                # having; refusing to announce over it would not be.
                log.warning("タイムアウト履歴を保存できませんでした: %s", e)

    def _write(self):
        folder = os.path.dirname(self.path) or "."
        handle, temporary = tempfile.mkstemp(dir=folder, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as f:
                json.dump(self._counts, f, ensure_ascii=False)
            # Atomic on both platforms, so a crash mid-write cannot leave a
            # half-written file where the tally used to be.
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def describe_duration(seconds: float) -> str:
    """Roughly how long, in the units a person would use.

    Rounded, not truncated. The figure is the gap between now and the moment the
    timeout ends, and a fraction of a second is always already gone by the time
    it is read — so a ten minute timeout arrives as 599.98 seconds and truncating
    reports it as nine minutes. Discord only offers a handful of durations, and
    every one of them is a round number.
    """
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds}秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}時間{minutes}分" if minutes else f"{hours}時間"
    days, hours = divmod(hours, 24)
    return f"{days}日{hours}時間" if hours else f"{days}日"


def ordinal(count: int) -> str:
    if count <= 1:
        return "初犯です"
    return f"通算 {count}回目"


class TimeoutWatch(commands.Cog, name="タイムアウト"):
    def __init__(self, bot):
        self.bot = bot
        self.counter = Counter()
        # Timeouts already announced, so the two routes cannot both report one.
        # Keyed by when the timeout ends, which is the one value both events
        # agree on exactly — the member id alone would collide with a second
        # timeout on the same person.
        self._announced: dict[tuple, float] = {}

    def _seen(self, guild_id: int, user_id: int,
              until: datetime.datetime) -> bool:
        """True if this exact timeout has already been announced.

        Claims it as a side effect, so the check and the mark cannot be
        interleaved by the other listener between them.
        """
        now = time.monotonic()
        for key, when in list(self._announced.items()):
            if now - when > DEDUPE_TTL:
                del self._announced[key]
        key = (guild_id, user_id, int(until.timestamp()))
        if key in self._announced:
            return True
        self._announced[key] = now
        return False

    def routes(self) -> str:
        """Which detection routes are actually available, for R!status."""
        parts = []
        if self.bot.intents.moderation:
            parts.append("監査ログ")
        if self.bot.intents.members:
            parts.append("メンバー更新")
        return " + ".join(parts) or "なし"

    # -- finding somewhere to say it --------------------------------------- #

    def _channels(self, guild: discord.Guild) -> list[discord.TextChannel]:
        """Where the announcement goes.

        Accepts multiple comma-separated channel IDs in TIMEOUT_CHANNEL.
        Falls back to the server's own system channel, or the first writable channel.
        """
        raw = os.environ.get(CHANNEL_ENV, "").strip()
        channels = []
        if raw:
            for item in raw.split(","):
                part = item.strip()
                if part.isdigit():
                    ch = guild.get_channel(int(part))
                    if ch is not None and self._can_post(ch):
                        channels.append(ch)
                    else:
                        log.warning("%s のチャンネルに投稿できません: %s", CHANNEL_ENV, part)

        if channels:
            return channels
        if guild.system_channel and self._can_post(guild.system_channel):
            return [guild.system_channel]
        for channel in guild.text_channels:
            if self._can_post(channel):
                return [channel]
        return []

    @staticmethod
    def _can_post(channel) -> bool:
        try:
            perms = channel.permissions_for(channel.guild.me)
            return perms.send_messages and perms.view_channel
        except AttributeError:
            return False

    # -- the event ---------------------------------------------------------- #

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        if not enabled() or entry.action != discord.AuditLogAction.member_update:
            return
        # Every nickname change and role grant arrives here too. Only an entry
        # that carries a new timeout is one.
        after = getattr(entry.after, _TIMEOUT_FIELD, None)
        before = getattr(entry.before, _TIMEOUT_FIELD, None)
        if after is None or after == before:
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        if after <= now:
            return      # a timeout being lifted, or one already expired
        if entry.guild is None or entry.target is None:
            return
        if self._seen(entry.guild.id, entry.target.id, after):
            return
        try:
            await self._announce(
                entry.guild, entry.target, (after - now).total_seconds(),
                reason=entry.reason, moderator=entry.user)
        except Exception:
            log.exception("タイムアウトの通知に失敗しました")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member,
                               after: discord.Member):
        """The route that needs no permission, only the privileged intent.

        Waits before claiming the timeout. This event normally beats the audit
        log entry, and the audit log is the one that knows why — so the wait
        buys the reason. If the entry never comes, nothing is lost but two
        seconds and the word "不明".
        """
        if not enabled():
            return
        until = after.timed_out_until
        if until is None or until == before.timed_out_until:
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        if until <= now:
            return
        await asyncio.sleep(AUDIT_GRACE)
        if self._seen(after.guild.id, after.id, until):
            return      # the audit log got there first, with more to say
        try:
            await self._announce(after.guild, after,
                                 (until - now).total_seconds())
        except Exception:
            log.exception("タイムアウトの通知に失敗しました")

    async def _announce(self, guild, target, seconds: float,
                        reason: str | None = None, moderator=None):
        if guild is None or target is None:
            return
        channels = self._channels(guild)
        if not channels:
            log.warning("投稿できるチャンネルが見つかりませんでした (guild=%s)",
                        guild.id)
            return

        total = await self.counter.bump(guild.id, target.id)
        reason = (reason or "").strip() or "不明"
        # A mention rather than a name: it renders as a mention and highlights
        # for the person concerned, while AllowedMentions.none() on the client
        # keeps it from actually pinging them. Being named in public is the
        # point; being notified about it is not.
        lines = [
            f"うおっとw {target.mention} がToされましたよww",
            "",
            f"対象: {target.mention}",
            f"時間: {describe_duration(seconds)}",
            f"理由: {ui.clip(reason, 200)}",
            f"回数: {ordinal(total)}",
        ]
        if moderator is not None and moderator.id != self.bot.user.id:
            lines.append(f"執行: {moderator.display_name}")

        embed = ui.embed("\n".join(lines), "To通知", ui.COLOR_ERROR)
        for channel in channels:
            try:
                await channel.send(**ui.payload(channel, embed))
            except ui.SEND_ERRORS as e:
                log.warning("通知を送れませんでした (ch=%s): %s", channel.id, ui.short_error(e))
        log.info("タイムアウト通知 guild=%s user=%s %.0f秒 (通算%d回, %dチャンネルに送信)",
                 guild.id, target.id, seconds, total, len(channels))

    # -- looking it up ------------------------------------------------------ #

    @commands.command(name="tocount", aliases=["to", "To回数"])
    async def tocount(self, ctx: commands.Context,
                      member: discord.Member | None = None):
        """Toされた回数を表示します。R!tocount [@ユーザー]"""
        if ctx.guild is None:
            raise commands.CheckFailure("サーバー内でのみ使えます。")
        who = member or ctx.author
        total = self.counter.count(ctx.guild.id, who.id)
        if total:
            body = f"{who.mention} は {ordinal(total)} です。"
        else:
            body = f"{who.mention} はまだToされていません。"
        await self.bot._say(ui.embed(body, "To記録", ui.COLOR_ANSWER), ctx.message)

    @commands.command(name="toreset")
    @commands.has_permissions(manage_messages=True)
    async def toreset(self, ctx: commands.Context, member: discord.Member):
        """Toの記録を消します (メッセージ管理権限が必要)。"""
        had = await self.counter.reset(ctx.guild.id, member.id)
        body = (f"{member.mention} の記録({had}回)を消しました。" if had
                else f"{member.mention} の記録はありません。")
        await self.bot._say(ui.embed(body, "To記録", ui.COLOR_ANSWER), ctx.message)


HELP_LINES = [
    ("R!tocount [@ユーザー]", "Toされた回数を表示します"),
    ("R!toreset @ユーザー", "To記録を消します (要権限)"),
]


async def setup(bot):
    await bot.add_cog(TimeoutWatch(bot))
