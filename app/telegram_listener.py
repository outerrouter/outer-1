"""Reads your Telegram groups/channels with YOUR account (read-only). Needs TELEGRAM_* values in .env.

It only listens to new posts (push, no polling), so listening to many channels costs nothing extra.
It never sends, joins or leaves anything. Join channels yourself in the Telegram app, a few at a time.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path

log = logging.getLogger("telegram")


class Listener:
    def __init__(self, cfg, pipeline, bus):
        self.cfg, self.pipeline, self.bus = cfg, pipeline, bus
        s = cfg.secrets
        self.api_id, self.api_hash, self.session = s["tg_api_id"], s["tg_api_hash"], s["tg_string_session"]
        self.media_dir = Path(cfg.data_dir) / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.connected = False

    @property
    def configured(self) -> bool:
        return bool(self.api_id and self.api_hash and self.session)

    def _allowed(self, title: str, chat_id, username: str | None) -> bool:
        t = self.cfg.telegram
        names = {str(title).lower(), str(chat_id), str(username or "").lower().lstrip("@")}
        if names & {str(x).lower().lstrip("@") for x in t.deny}:
            return False
        if t.mode == "whitelist":
            return bool(names & {str(x).lower().lstrip("@") for x in t.allow})
        return True

    async def run(self) -> None:
        if not self.configured:
            log.info("Telegram user session not configured: channel reading is off (see README)")
            return
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
        from telethon.tl.types import MessageEntityTextUrl

        while True:
            client = TelegramClient(StringSession(self.session), self.api_id, self.api_hash)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    log.error("Telegram session is not logged in. Run scripts/telegram_login.py again.")
                    return

                async def handle(event, edited: bool):
                    if event.is_private or not (event.is_channel or event.is_group):
                        return                                   # never read private chats
                    chat = await event.get_chat()
                    title = getattr(chat, "title", None) or str(event.chat_id)
                    if not self._allowed(title, event.chat_id, getattr(chat, "username", None)):
                        return
                    msg = event.message
                    extra = []
                    for ent, _txt in (msg.get_entities_text() or []):      # hidden links behind words
                        if isinstance(ent, MessageEntityTextUrl) and ent.url:
                            extra.append(ent.url)
                    for row in (msg.buttons or []):                         # link buttons under the post
                        for button in row:
                            if getattr(button, "url", None):
                                extra.append(button.url)
                    post = {"channel": title, "channel_id": str(event.chat_id), "msg_id": msg.id,
                            "ts": msg.date.timestamp() if msg.date else time.time(),
                            "text": event.raw_text or "", "extra_urls": extra, "reply_to": msg.reply_to_msg_id}
                    if msg.photo and self.cfg.telegram.save_images:
                        post["download_image"] = lambda m=msg, cid=event.chat_id: self._save_image(m, cid)
                    await self.pipeline.ingest(post, edited=edited)

                @client.on(events.NewMessage(incoming=True))
                async def on_new(event):
                    try:
                        await handle(event, False)
                    except Exception:
                        log.exception("failed to handle a new post")

                @client.on(events.MessageEdited(incoming=True))
                async def on_edit(event):
                    try:
                        await handle(event, True)
                    except Exception:
                        log.exception("failed to handle an edited post")

                self.connected = True
                self.bus.publish("telegram", {"connected": True})
                log.info("Telegram listener running")
                await client.run_until_disconnected()
            except asyncio.CancelledError:
                await client.disconnect()
                raise
            except Exception as exc:
                log.warning("Telegram listener error: %s", type(exc).__name__)
            finally:
                self.connected = False
                self.bus.publish("telegram", {"connected": False})
            await asyncio.sleep(15)

    async def _save_image(self, msg, chat_id) -> str | None:
        name = hashlib.sha1(f"{chat_id}:{msg.id}".encode()).hexdigest()[:16] + ".jpg"
        path = self.media_dir / name
        if not path.exists():
            await msg.download_media(file=str(path), thumb=-1)
        return name if path.exists() else None
