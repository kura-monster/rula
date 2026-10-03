"""Entry point, for hosting panels that go looking for one.

Services that run a bot for you generally auto-detect the file to start and only
recognise a handful of names — main.py, bot.py, index.js. Ours is
discord_bot.py, which is not one of them, so this exists purely to carry the
name they expect.

Nothing lives here. The bot is still discord_bot.py, and running that directly
works exactly as it did before.
"""
from discord_bot import main

if __name__ == "__main__":
    raise SystemExit(main())
