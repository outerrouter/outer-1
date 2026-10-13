"""One-time login: creates TELEGRAM_STRING_SESSION. Run on YOUR OWN computer, not on a shared machine.

    pip install telethon
    python scripts/telegram_login.py

The printed string is the key to your Telegram account. Put it only in .env (never in git, never in chat).
"""
import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(os.getenv("TELEGRAM_API_ID") or input("api_id (from my.telegram.org): ").strip())
    api_hash = os.getenv("TELEGRAM_API_HASH") or input("api_hash: ").strip()
    async with TelegramClient(StringSession(), api_id, api_hash) as client:   # asks phone + OTP
        print("\nTELEGRAM_STRING_SESSION=" + client.session.save())
        print("\nCopy that whole line into your .env file. Keep it secret.")


asyncio.run(main())
