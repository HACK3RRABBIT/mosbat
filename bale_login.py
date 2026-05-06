"""Run once to create bale_downloader session."""
from telethon.sync import TelegramClient
from config import API_ID, API_HASH

with TelegramClient("bale_downloader", API_ID, API_HASH) as tg:
    tg.start()
    me = tg.get_me()
    print(f"Logged in as: {me.first_name} (@{me.username})")
    print("Session saved! You can now run bale_bot.py")
