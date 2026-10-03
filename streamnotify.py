"""YouTube and Twitch live stream notifier.

Periodically checks registered YouTube channels and Twitch streams,
and sends an alert to Discord when a stream goes live.

Commands:
  R!stream add <youtube|twitch> <URL|ID> [#channel] — Register a stream to monitor
  R!stream list                                     — List registered streams
  R!stream remove <number>                          — Remove a stream from monitoring
  R!stream check                                    — Check all registered streams now
"""
import asyncio
import json
import logging
import os
import re
import aiohttp
import discord
from discord.ext import commands, tasks

import ui

log = logging.getLogger("rula.streamnotify")

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

DATA_FILE = "stream_notify.json"


def _load_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("配信通知設定の読み込みに失敗しました: %s", e)
    return {}


def _save_data(data: dict):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("配信通知設定の保存に失敗しました: %s", e)


def _check_stream_sync(target_url: str) -> dict | None:
    """Check if a YouTube or Twitch channel is currently live using yt-dlp."""
    if yt_dlp is None:
        return None

    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "playlist_items": "1",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=False)
            if not info:
                return None

            is_live = bool(info.get("is_live") or info.get("live_status") == "is_live")
            video_id = info.get("id") or ""
            title = info.get("title") or "ライブ配信"
            uploader = info.get("uploader") or info.get("channel") or "配信者"
            url = info.get("webpage_url") or target_url
            thumbnail = info.get("thumbnail") or ""

            return {
                "is_live": is_live,
                "video_id": video_id,
                "title": title,
                "uploader": uploader,
                "url": url,
                "thumbnail": thumbnail,
            }
    except Exception as e:
        # Most "not live" streams raise an error or return non-live info
        log.debug("Stream check for %s returned: %s", target_url, e)
        return None


class StreamNotify(commands.Cog, name="配信通知"):
    """YouTubeやTwitchの配信開始を自動通知する機能。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = _load_data()
        self._check_loop.start()

    def cog_unload(self):
        self._check_loop.cancel()

    @tasks.loop(minutes=3)
    async def _check_loop(self):
        """Check all registered streams every 3 minutes."""
        await self.bot.wait_until_ready()
        changed = False

        for guild_id_str, g_data in list(self.data.items()):
            streams = g_data.get("streams", [])
            for item in streams:
                check_url = item["url"]
                if item["platform"] == "youtube" and not check_url.endswith("/live"):
                    check_url = check_url.rstrip("/") + "/live"

                result = await asyncio.to_thread(_check_stream_sync, check_url)
                if not result:
                    item["is_live"] = False
                    continue

                is_live_now = result["is_live"]
                vid = result["video_id"]
                last_vid = item.get("last_video_id")

                if is_live_now:
                    # New stream detected
                    if not item.get("is_live") or (vid and vid != last_vid):
                        item["is_live"] = True
                        item["last_video_id"] = vid
                        changed = True

                        # Send notification
                        channel = self.bot.get_channel(item["notify_channel_id"])
                        if channel:
                            embed = ui.embed(
                                f"**[{result['title']}]({result['url']})**\n\n"
                                f"チャンネル: **{result['uploader']}**\n"
                                f"プラットフォーム: **{item['platform'].upper()}**\n\n"
                                f"👉 [**配信を見に行く**]({result['url']})",
                                f"🔴 【配信開始】{result['uploader']}",
                                0xE74C3C  # Red
                            )
                            if result.get("thumbnail"):
                                embed.set_image(url=result["thumbnail"])
                            try:
                                await channel.send(
                                    content=f"📢 **{result['uploader']}** が配信を開始しました！",
                                    embed=embed
                                )
                            except Exception as e:
                                log.warning("配信通知送信失敗 (ch=%s): %s", item["notify_channel_id"], e)
                else:
                    if item.get("is_live"):
                        item["is_live"] = False
                        changed = True

        if changed:
            _save_data(self.data)

    @commands.group(name="stream", aliases=["streamnotify", "配信通知"], invoke_without_command=True)
    async def stream(self, ctx: commands.Context):
        """配信通知コマンドの使い方を表示します。R!stream"""
        lines = [f"`{cmd}` — {desc}" for cmd, desc in HELP_LINES]
        await ctx.reply(
            embed=ui.embed("\n".join(lines), "配信通知機能の使い方", ui.COLOR_ANSWER),
            mention_author=False
        )

    @stream.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def add_stream(self, ctx: commands.Context, platform: str, target: str, channel: discord.TextChannel = None):
        """配信者を監視リストに追加します。R!stream add <youtube|twitch> <URLまたは名前> [#通知チャンネル]"""
        plat = platform.lower()
        if plat not in ("youtube", "yt", "twitch"):
            raise commands.UserInputError("プラットフォームは `youtube` または `twitch` を指定してください。")

        plat_canonical = "youtube" if plat in ("youtube", "yt") else "twitch"
        notify_channel = channel or ctx.channel

        if plat_canonical == "youtube":
            if not target.startswith("http"):
                if target.startswith("@"):
                    url = f"https://www.youtube.com/{target}"
                elif target.startswith("UC"):
                    url = f"https://www.youtube.com/channel/{target}"
                else:
                    url = f"https://www.youtube.com/@{target}"
            else:
                url = target
        else:  # twitch
            username = target.rstrip("/").split("/")[-1]
            url = f"https://www.twitch.tv/{username}"

        guild_id_str = str(ctx.guild.id)
        g_data = self.data.setdefault(guild_id_str, {"streams": []})

        # Check duplicate
        for s in g_data["streams"]:
            if s["url"].lower() == url.lower():
                await ctx.reply(
                    embed=ui.embed(f"この配信者（`{url}`）は既に登録されています。", "登録済み", ui.COLOR_WORKING),
                    mention_author=False
                )
                return

        status_msg = await ctx.reply(
            embed=ui.embed(f"`{url}` の情報を確認しています…", "確認中", ui.COLOR_WORKING),
            mention_author=False
        )

        # Check channel validity
        info = await asyncio.to_thread(_check_stream_sync, url)
        uploader_name = info.get("uploader") if info else target

        stream_entry = {
            "platform": plat_canonical,
            "url": url,
            "channel_name": uploader_name or target,
            "notify_channel_id": notify_channel.id,
            "last_video_id": info.get("video_id", "") if info else "",
            "is_live": info.get("is_live", False) if info else False,
        }

        g_data["streams"].append(stream_entry)
        _save_data(self.data)

        await status_msg.edit(
            embed=ui.embed(
                f"**配信者:** {stream_entry['channel_name']}\n"
                f"**プラットフォーム:** {plat_canonical.upper()}\n"
                f"**URL:** {url}\n"
                f"**通知先:** {notify_channel.mention}\n\n"
                f"配信が開始され次第、自動で通知します！",
                "配信通知 登録完了",
                ui.COLOR_ANSWER
            )
        )

    @stream.command(name="list", aliases=["show"])
    async def list_streams(self, ctx: commands.Context):
        """登録されている配信通知の一覧を表示します。R!stream list"""
        guild_id_str = str(ctx.guild.id)
        streams = self.data.get(guild_id_str, {}).get("streams", [])

        if not streams:
            await ctx.reply(
                embed=ui.embed("現在このサーバーには登録されている配信通知はありません。\n`R!stream add youtube <URL>` で登録できます。",
                               "登録リスト", ui.COLOR_ANSWER),
                mention_author=False
            )
            return

        lines = []
        for i, s in enumerate(streams, 1):
            ch = self.bot.get_channel(s["notify_channel_id"])
            ch_str = ch.mention if ch else f"#{s['notify_channel_id']}"
            live_status = "🔴 配信中" if s.get("is_live") else "⚪ オフライン"
            lines.append(
                f"**{i}. {s['channel_name']}** ({s['platform'].upper()}) - {live_status}\n"
                f"　URL: {s['url']}\n"
                f"　通知先: {ch_str}"
            )

        await ctx.reply(
            embed=ui.embed("\n\n".join(lines), f"登録中の配信通知 ({len(streams)}件)", ui.COLOR_ANSWER),
            mention_author=False
        )

    @stream.command(name="remove", aliases=["del", "delete"])
    @commands.has_permissions(manage_guild=True)
    async def remove_stream(self, ctx: commands.Context, index: int):
        """登録した配信者を番号で削除します。R!stream remove <番号>"""
        guild_id_str = str(ctx.guild.id)
        streams = self.data.get(guild_id_str, {}).get("streams", [])

        if not streams or index < 1 or index > len(streams):
            raise commands.UserInputError(f"1 から {len(streams)} の番号を指定してください (`R!stream list` で確認可能)。")

        removed = streams.pop(index - 1)
        _save_data(self.data)

        await ctx.reply(
            embed=ui.embed(f"**{removed['channel_name']}** ({removed['platform'].upper()}) の通知登録を削除しました。",
                           "削除完了", ui.COLOR_ANSWER),
            mention_author=False
        )

    @stream.command(name="check")
    @commands.has_permissions(manage_guild=True)
    async def check_now(self, ctx: commands.Context):
        """今すぐ登録中の配信状態をチェックします。R!stream check"""
        status_msg = await ctx.reply(
            embed=ui.embed("登録されている配信者の状態をチェックしています…", "チェック中", ui.COLOR_WORKING),
            mention_author=False
        )
        await self._check_loop()
        await status_msg.edit(
            embed=ui.embed("配信チェックが完了しました！新規のライブ配信があれば通知チャンネルへ送信されています。",
                           "チェック完了", ui.COLOR_ANSWER)
        )


HELP_LINES = [
    ("R!stream add <yt|twitch> <URL> [#ch]", "配信者を監視リストに追加します (要管理者権限)"),
    ("R!stream list", "登録中の配信監視一覧を表示します"),
    ("R!stream remove <番号>", "登録を解除します (要管理者権限)"),
    ("R!stream check", "今すぐ配信状況を確認します"),
]


async def setup(bot: commands.Bot):
    await bot.add_cog(StreamNotify(bot))
