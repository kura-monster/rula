"""Free PC games (Steam, Epic Games, etc.) tracker and notifier.

Commands:
  R!freegames               — List currently free PC games
  R!freegames setchannel    — Set channel for automatic notifications
  R!freegames off           — Disable automatic notifications
"""
import asyncio
import json
import logging
import os
import aiohttp
import discord
from discord.ext import commands, tasks

import ui

log = logging.getLogger("rula.freegames")

GAMERPOWER_API = "https://www.gamerpower.com/api/giveaways?platform=pc&type=game"
EPIC_API = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?locale=ja-JP&country=JP&allowCountries=JP"
DATA_FILE = "freegames_data.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _load_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("設定ファイルの読み込みに失敗しました: %s", e)
    return {"guild_channels": {}, "notified_ids": []}


def _save_data(data: dict):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("設定ファイルの保存に失敗しました: %s", e)


def _make_session() -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    return aiohttp.ClientSession(connector=connector)


async def fetch_free_games(session: aiohttp.ClientSession = None) -> list[dict]:
    """Fetch free games from GamerPower API (covering Steam, Epic, GOG, etc.)."""
    headers = {"User-Agent": USER_AGENT}
    games = []

    own_session = session is None
    sess = _make_session() if own_session else session

    try:
        try:
            async with sess.get(GAMERPOWER_API, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        for item in data:
                            # Only games, not loot or betas
                            if item.get("type", "").lower() != "game":
                                continue
                            games.append({
                                "id": f"gp_{item.get('id')}",
                                "title": item.get("title", "不明なゲーム"),
                                "worth": item.get("worth", "N/A"),
                                "thumbnail": item.get("image", ""),
                                "url": item.get("open_giveaway_url") or item.get("gamerpower_url", ""),
                                "platforms": item.get("platforms", "PC"),
                                "end_date": item.get("end_date", "未定"),
                                "description": item.get("description", "")[:200],
                            })
        except Exception as e:
            log.warning("GamerPower API取得失敗: %s", e)

    # Fetch Epic Games directly as backup/addition
        try:
            async with sess.get(EPIC_API, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
                    for item in elements:
                        promos = item.get("promotions") or {}
                        offers = promos.get("promotionalOffers") or []
                        if not offers:
                            continue
                        # Check if 100% off right now
                        price = item.get("price", {}).get("totalPrice", {})
                        discount_price = price.get("discountPrice", 1)
                        if discount_price == 0:
                            gid = f"epic_{item.get('id')}"
                            # Check duplicate
                            if any(g["title"].lower() == item.get("title", "").lower() for g in games):
                                continue
                            slug = item.get("productSlug") or item.get("urlSlug") or ""
                            url = f"https://store.epicgames.com/ja/p/{slug}" if slug else "https://store.epicgames.com/ja/free-games"
                            image = ""
                            for img in item.get("keyImages", []):
                                if img.get("type") in ("OfferImageWide", "Thumbnail", "DieselStoreFrontWide"):
                                    image = img.get("url")
                                    break
                            games.append({
                                "id": gid,
                                "title": item.get("title", "不明なゲーム"),
                                "worth": f"¥{price.get('originalPrice', 0)}",
                                "thumbnail": image,
                                "url": url,
                                "platforms": "Epic Games Store (PC)",
                                "end_date": "今週中",
                                "description": item.get("description", "")[:200],
                            })
        except Exception as e:
            log.warning("Epic Games API取得失敗: %s", e)
    finally:
        if own_session:
            await sess.close()

    return games


class FreeGames(commands.Cog, name="無料ゲーム通知"):
    """Steam / Epic Games 等の無料配布ゲームを監視・通知する機能。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = _load_data()
        self._check_loop.start()

    def cog_unload(self):
        self._check_loop.cancel()

    @tasks.loop(hours=2)
    async def _check_loop(self):
        """Periodically check for new free games and notify registered channels."""
        await self.bot.wait_until_ready()
        guild_channels = self.data.get("guild_channels", {})
        if not guild_channels:
            return

        async with _make_session() as session:
            games = await fetch_free_games(session)

        notified_ids = set(self.data.get("notified_ids", []))
        new_games = [g for g in games if g["id"] not in notified_ids]

        if not new_games:
            return

        for game in new_games:
            notified_ids.add(game["id"])
            embed = self._create_game_embed(game, is_new=True)

            for guild_id_str, channel_id in list(guild_channels.items()):
                channel = self.bot.get_channel(channel_id)
                if channel:
                    try:
                        await channel.send(
                            content="🎁 **【無料配布】新しい無料PCゲームが配布されています！**",
                            embed=embed
                        )
                    except Exception as e:
                        log.warning("無料ゲーム通知送信失敗 (ch=%s): %s", channel_id, e)

        self.data["notified_ids"] = list(notified_ids)[-200:]  # Keep recent 200 IDs
        _save_data(self.data)

    def _create_game_embed(self, game: dict, is_new: bool = False) -> discord.Embed:
        title = f"🎁 {game['title']}"
        desc = (
            f"**プラットフォーム:** {game['platforms']}\n"
            f"**通常価格:** {game['worth']} → **無料 (100% OFF)**\n"
            f"**終了予定:** {game['end_date']}\n\n"
            f"{game['description']}\n\n"
            f"👉 [**今すぐ入手する**]({game['url']})"
        )
        embed = ui.embed(desc, title, ui.COLOR_ANSWER if not is_new else 0x2ECC71)
        if game.get("thumbnail"):
            embed.set_thumbnail(url=game["thumbnail"])
        return embed

    @commands.group(name="freegames", aliases=["fg", "freegame", "無料ゲーム"], invoke_without_command=True)
    async def freegames(self, ctx: commands.Context):
        """現在無料配布中のPCゲーム一覧を表示します。R!freegames"""
        async with ui.typing(ctx):
            async with _make_session() as session:
                games = await fetch_free_games(session)

        if not games:
            await ctx.reply(
                embed=ui.embed("現在開催中の無料配布ゲームは見つかりませんでした。", "無料配布ゲーム", ui.COLOR_ANSWER),
                mention_author=False
            )
            return

        embeds = []
        for g in games[:5]:  # Show top 5
            embeds.append(self._create_game_embed(g))

        first_embed = embeds[0]
        await ctx.reply(
            content=f"現在 **{len(games)}件** のPCゲームが無料配布中です！",
            embed=first_embed,
            mention_author=False
        )

        for other_embed in embeds[1:4]:
            await ctx.send(embed=other_embed)

    @freegames.command(name="setchannel", aliases=["channel", "set"])
    @commands.has_permissions(manage_guild=True)
    async def setchannel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """新着無料ゲームの通知チャンネルを設定します。R!freegames setchannel [#チャンネル]"""
        target = channel or ctx.channel
        guild_id_str = str(ctx.guild.id)
        self.data.setdefault("guild_channels", {})[guild_id_str] = target.id
        _save_data(self.data)

        await ctx.reply(
            embed=ui.embed(f"無料ゲームの自動通知チャンネルを {target.mention} に設定しました！\n新しい無料配布が始まり次第、自動でお知らせします。",
                           "設定完了", ui.COLOR_ANSWER),
            mention_author=False
        )

    @freegames.command(name="off", aliases=["disable", "remove"])
    @commands.has_permissions(manage_guild=True)
    async def disable_notify(self, ctx: commands.Context):
        """無料ゲームの自動通知を解除します。R!freegames off"""
        guild_id_str = str(ctx.guild.id)
        if guild_id_str in self.data.get("guild_channels", {}):
            del self.data["guild_channels"][guild_id_str]
            _save_data(self.data)
            await ctx.reply(
                embed=ui.embed("無料ゲームの自動通知を解除しました。", "解除完了", ui.COLOR_ANSWER),
                mention_author=False
            )
        else:
            await ctx.reply(
                embed=ui.embed("このサーバーでは通知が設定されていません。", "通知未設定", ui.COLOR_WORKING),
                mention_author=False
            )


HELP_LINES = [
    ("R!freegames", "現在Steam/Epic等で無料配布中のPCゲーム一覧を表示します"),
    ("R!freegames setchannel [#ch]", "無料ゲームの自動通知チャンネルを設定します (要管理者権限)"),
    ("R!freegames off", "無料ゲームの自動通知を解除します"),
]


async def setup(bot: commands.Bot):
    await bot.add_cog(FreeGames(bot))
