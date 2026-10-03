"""外部Bot（ユーザーインストールアプリ / 外部アプリ）の検知・晒し上げモジュール

Discordのサーバー設定で「外部アプリの使用」が許可されていない場合でも、
ユーザーが個人インストールした外部Botやサーバー未参加のBotが使用された際に
確実に検知し、該当チャンネル（および環境変数 BOT_DETECT_CHANNEL でカンマ区切り指定された通知先）
で晒し上げEmbedと煽りメンション（10秒後に自動削除）を実行します。
荒らし・連投によるスパム化を防ぐクールダウンおよび安全対策を完備。
"""
import asyncio
import logging
import os
import time

import discord
from discord.ext import commands

import ui

log = logging.getLogger("rula.botdetect")

# 有効化フラグ（環境変数 BOT_DETECT=off で無効化可能）
ENABLED = os.environ.get("BOT_DETECT", "1").strip().lower() not in ("0", "false", "no")

# 通知先チャンネルの環境変数名（カンマ区切りで複数指定可能: 例 12345,67890）
CHANNEL_ENV = "BOT_DETECT_CHANNEL"

# 荒らし・連投対策用クールダウン（秒）
USER_COOLDOWN = 30.0       # 同一ユーザーからの連投制限
CHANNEL_COOLDOWN = 10.0    # 同一チャンネルでの連投制限

# 煽りメンションの設定
MENTION_COUNT = 5          # メンション送信回数
MENTION_INTERVAL = 1.2     # 送信間隔（秒・Discordレートリミット対策）
MENTION_DELETE_AFTER = 10.0 # 投稿後削除されるまでの秒数

# 煽りメッセージ集
TAUNT_TEMPLATES = [
    "{mention} 外部Bot使ったのバレてて草ｗ",
    "{mention} 外部Bot持ち込んでイキろうとした？ｗｗ",
    "{mention} 外部Bot検知システムなめないでねｗｗｗ",
    "{mention} こっそり使えばバレないと思った？ｗｗｗｗ",
    "{mention} 外部Bot使いましたよ〜〜〜ｗｗｗｗｗ",
]


class BotDetect(commands.Cog):
    """外部Bot・ユーザーインストールアプリの不正利用を検知・晒し上げるCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 荒らし対策: (guild_id, user_id) / (guild_id, channel_id) キーで管理
        # サーバーが違えばクールダウンは別々に扱われる
        self._user_last_triggered: dict[tuple[int, int], float] = {}
        self._channel_last_triggered: dict[tuple[int, int], float] = {}
        # 現在メンション処理中の (guild_id, user_id)（重複発火ロック）
        self._active_users: set[tuple[int, int]] = set()

    def _resolve_target_channels(self, message: discord.Message) -> list[discord.TextChannel | discord.Thread]:
        """通知先チャンネル一覧を取得する（使われたチャンネル ＋ 環境変数指定チャンネル）。
        別サーバーのチャンネルIDも bot.get_channel() で取得するため、クロスサーバー通知に対応。
        """
        targets: list[discord.TextChannel | discord.Thread] = [message.channel]
        seen_ids: set[int] = {message.channel.id}

        # 環境変数 BOT_DETECT_CHANNEL からカンマ区切りで取得
        # bot.get_channel() を使うことで、別サーバーのチャンネルにも通知できる
        raw = os.environ.get(CHANNEL_ENV, "").strip()
        if raw:
            for item in raw.split(","):
                part = item.strip()
                if part.isdigit():
                    cid = int(part)
                    if cid not in seen_ids:
                        ch = self.bot.get_channel(cid)
                        if ch and isinstance(ch, (discord.TextChannel, discord.Thread)):
                            targets.append(ch)
                            seen_ids.add(cid)
                        else:
                            log.warning("BOT_DETECT_CHANNEL: チャンネル %s が見つかりません（Botが未参加か無効なIDです）", cid)

        return targets

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """外部Botによるメッセージ投下を検知する"""
        if not ENABLED:
            return

        # サーバー外（DM）または自Botのメッセージは除外
        if message.guild is None or message.author.id == self.bot.user.id:
            return

        # WebhookはBotとは別枠（外部Bot検知の対象外）
        if message.webhook_id is not None:
            return

        # 外部Botおよび実行ユーザーの判定
        is_external, target_user, reason = await self._identify_external_bot(message)
        if not is_external:
            return

        # 実行ユーザーが特定できない場合はBot自身を対象にはできないためログのみ
        if target_user is None:
            log.info("外部Botを検知しましたが実行ユーザーを特定できませんでした: bot=%s (%s)",
                     message.author, message.author.id)
            return

        # 自Bot自身がターゲットになる誤爆を防ぐ
        if target_user.id == self.bot.user.id or target_user.bot:
            return

        # 荒らし対策: クールダウン判定（サーバー別に独立）
        now = time.monotonic()
        guild_id = message.guild.id
        user_key = (guild_id, target_user.id)
        chan_key = (guild_id, message.channel.id)

        if user_key in self._active_users:
            log.debug("ユーザー %s (guild %s) は現在晒し上げ処理中のためスキップ", target_user.id, guild_id)
            return

        last_user_time = self._user_last_triggered.get(user_key, 0.0)
        if now - last_user_time < USER_COOLDOWN:
            log.info("ユーザー %s (guild %s) のクールダウン中のため晒し上げを抑制", target_user.id, guild_id)
            return

        last_chan_time = self._channel_last_triggered.get(chan_key, 0.0)
        if now - last_chan_time < CHANNEL_COOLDOWN:
            log.info("チャンネル %s (guild %s) のクールダウン中のため晒し上げを抑制", message.channel.id, guild_id)
            return

        # 発火時刻と処理中フラグを記録
        self._user_last_triggered[user_key] = now
        self._channel_last_triggered[chan_key] = now
        self._active_users.add(user_key)

        try:
            await self._expose_external_bot(message, target_user, reason)
        except Exception as e:
            log.exception("外部Botの晒し上げ処理中にエラーが発生しました: %s", e)
        finally:
            self._active_users.discard(user_key)

    async def _identify_external_bot(
        self, message: discord.Message
    ) -> tuple[bool, discord.User | discord.Member | None, str]:
        """メッセージが外部Bot（ユーザーアプリ等）によるものかを判定する。"""
        # 判定1: MessageInteractionMetadata による判定（最も確実なUser App判定）
        if message.interaction_metadata:
            meta = message.interaction_metadata
            user = meta.user
            if hasattr(meta, "is_user_integration") and meta.is_user_integration():
                return True, user, "ユーザーインストールアプリ (User App)"

        # 判定2: 送信者がBotであり、かつサーバーに参加していない外部Botの場合
        if message.author.bot:
            guild = message.guild
            member = guild.get_member(message.author.id)
            if member is None:
                try:
                    member = await guild.fetch_member(message.author.id)
                except (discord.NotFound, discord.HTTPException):
                    member = None

            if member is None:
                user = None
                if message.interaction_metadata:
                    user = message.interaction_metadata.user
                elif message.reference and message.reference.resolved:
                    ref_msg = message.reference.resolved
                    if isinstance(ref_msg, discord.Message) and not ref_msg.author.bot:
                        user = ref_msg.author

                return True, user, "サーバー未参加の外部Bot"

        return False, None, ""

    async def _expose_external_bot(
        self, message: discord.Message, target_user: discord.User | discord.Member, reason: str
    ):
        """晒し上げEmbedと煽りメンション（10秒後に自動削除）を指定された全チャンネルに実行する"""
        guild = message.guild
        bot_author = message.author
        target_channels = self._resolve_target_channels(message)

        # サーバー設定で外部Botが許可されているかどうかのチェック
        external_apps_allowed = True
        try:
            perms = message.channel.permissions_for(guild.default_role)
            if hasattr(perms, "use_external_apps"):
                external_apps_allowed = bool(perms.use_external_apps)
        except Exception:
            pass

        perm_status = (
            "許可中 (全体設定)"
            if external_apps_allowed
            else "**禁止設定 (外部Bot利用不可の設定です)**"
        )

        safe_user_name = discord.utils.escape_markdown(discord.utils.escape_mentions(target_user.name))
        safe_bot_name = discord.utils.escape_markdown(discord.utils.escape_mentions(bot_author.name))

        description_lines = [
            f"**{safe_user_name}** が、外部Botを使いましたよｗ",
            "",
            f"・**実行者**: {target_user.mention} (`{safe_user_name}` / ID: `{target_user.id}`)",
            f"・**使用Bot**: {bot_author.mention} (`{safe_bot_name}` / ID: `{bot_author.id}`)",
            f"・**実行チャンネル**: {message.channel.mention}",
            f"・**サーバー側の外部Bot設定**: {perm_status}",
            f"・**検知種別**: {reason}",
        ]

        embed = ui.embed(
            description="\n".join(description_lines),
            title="外部Bot使用を検知",
            color=ui.COLOR_ERROR,
        )
        if bot_author.display_avatar:
            embed.set_thumbnail(url=bot_author.display_avatar.url)

        # メンション設定: 対象ユーザーのみに通知し、@everyone等は完全ブロック
        safe_mentions = discord.AllowedMentions(
            users=[target_user],
            everyone=False,
            roles=False,
            replied_user=False,
        )

        # 各対象チャンネルに対して晒し上げEmbedを送信
        for ch in target_channels:
            try:
                await ch.send(**ui.payload(ch, embed))
            except discord.HTTPException as e:
                log.warning("チャンネル %s への晒し上げEmbed送信に失敗: %s", ch.id, e)

        # 各対象チャンネルで合計5回の煽りメンションを送信（各投稿10秒後に自動削除）
        for i in range(MENTION_COUNT):
            template = TAUNT_TEMPLATES[i % len(TAUNT_TEMPLATES)]
            taunt_text = template.format(mention=target_user.mention)

            for ch in target_channels:
                try:
                    await ch.send(
                        content=taunt_text,
                        allowed_mentions=safe_mentions,
                        delete_after=MENTION_DELETE_AFTER,
                    )
                except discord.HTTPException as e:
                    log.warning("煽りメンション送信に失敗 (ch=%s, 回数 %d): %s", ch.id, i + 1, e)

            # レートリミット対策の間隔待機
            await asyncio.sleep(MENTION_INTERVAL)

    @commands.command(name="botdetect", aliases=["extbot"])
    @commands.has_permissions(manage_guild=True)
    async def botdetect_status(self, ctx: commands.Context):
        """外部Bot検知の現在の状態を確認します。"""
        status = "有効 (稼働中)" if ENABLED else "無効"
        channel_perms = ctx.channel.permissions_for(ctx.guild.default_role)
        perm_str = "許可" if getattr(channel_perms, "use_external_apps", True) else "禁止"
        env_raw = os.environ.get(CHANNEL_ENV, "").strip() or "(未設定 - 使われたチャンネルのみ)"

        lines = [
            f"外部Bot検知機能: **{status}**",
            f"このチャンネルの外部アプリ権限設定: **{perm_str}**",
            f"追加通知先環境変数 (`{CHANNEL_ENV}`): `{env_raw}`",
            f"煽りメンション回数: **{MENTION_COUNT}回** (10秒後自動削除)",
            f"スパム対策クールダウン: ユーザーごと **{int(USER_COOLDOWN)}秒**",
        ]
        embed = ui.embed("\n".join(lines), title="外部Bot検知設定")
        await ctx.reply(**ui.payload(ctx.channel, embed), mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(BotDetect(bot))
