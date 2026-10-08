"""Optional owner-authorized Telegram history reader. Never sends or joins as the owner."""

import asyncio
import os
import secrets
import time
from contextlib import suppress
from pathlib import Path

from dotenv import load_dotenv, set_key
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError


class HistoryUnavailable(ValueError):
    pass


class HistoryReader:
    def __init__(self, service):
        self.service = service
        self.client = None
        self.login = None
        self.lock = asyncio.Lock()
        self.expiry_task = None

    def configured(self):
        return bool(os.getenv("TG_API_ID") and os.getenv("TG_API_HASH"))

    @staticmethod
    def session_path():
        path = Path(os.getenv("TG_USER_SESSION_PATH", "data/history-owner.session")).expanduser()
        return path if str(path).endswith(".session") else Path(str(path) + ".session")

    def new_client(self):
        if not self.configured():
            raise HistoryUnavailable("Set TG_API_ID and TG_API_HASH in .env first (from my.telegram.org).")
        try:
            from telethon import TelegramClient
        except ImportError as exc:
            raise HistoryUnavailable("Install the updated requirements to enable history login.") from exc
        path = self.session_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Session contains an account authorization key. It is never sent to any chat or log.
        client = TelegramClient(
            str(path),
            int(os.environ["TG_API_ID"]),
            os.environ["TG_API_HASH"],
            receive_updates=False,
            flood_sleep_threshold=0,
        )
        if path.exists():
            path.chmod(0o600)
        return client

    async def connected(self):
        if self.login:
            raise HistoryUnavailable("Owner login is in progress; finish /login first.")
        if not self.client:
            self.client = self.new_client()
        if not self.client.is_connected():
            await self.client.connect()
        if not await self.client.is_user_authorized():
            raise HistoryUnavailable(
                "The owner must /login in the bot's private chat to read older messages."
            )
        me = await self.client.get_me()
        if me.id != self.service.db.get("owner_id"):
            raise HistoryUnavailable("History session does not belong to the permanently bound owner.")
        return self.client

    async def entity(self, chat):
        if not self.service.db.enabled(chat) or chat == self.service.config.log_group:
            raise HistoryUnavailable("History reading is limited to enabled source groups.")
        client = await self.connected()
        try:
            return await client.get_input_entity(chat)
        except ValueError:
            # Populate access hashes only from chats the owner already belongs to. Never join a group.
            async for dialog in client.iter_dialogs():
                if dialog.id == chat:
                    return dialog.input_entity
            raise HistoryUnavailable("The owner's account must already be a member of this group.") from None

    @staticmethod
    async def record(message, chat):
        sender = await message.get_sender()
        uid = getattr(sender, "id", None)
        is_person = bool(sender and hasattr(sender, "first_name"))
        media = "text"
        for kind in ("sticker", "gif", "video", "voice", "audio", "photo", "document"):
            if getattr(message, kind, None):
                media = "animation" if kind == "gif" else kind
                break
        name = (
            " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
            if is_person
            else getattr(sender, "title", "unknown")
        )
        return {
            "chat_id": chat,
            "message_id": message.id,
            "user_id": uid if is_person else None,
            "username": getattr(sender, "username", None),
            "name": name,
            "text": message.message or "",
            "media": media,
            "via_bot": None,
            "sender_chat": None if is_person else uid,
            "date": message.date.timestamp(),
            "edited": message.edit_date.timestamp() if message.edit_date else None,
        }

    async def import_history(self, chat, count, before):
        async with self.lock:
            entity = await self.entity(chat)
            total = 0
            async for message in self.client.iter_messages(entity, limit=min(count, 1000), max_id=before):
                if not hasattr(message, "message") or getattr(message, "action", None):
                    continue
                item = await self.record(message, chat)
                # Backfill deliberately does NOT enqueue moderation; future rules remain prospective.
                self.service.db.save_message(item)
                total += 1
            return total

    async def read(self, chat, mid):
        async with self.lock:
            entity = await self.entity(chat)
            message = await self.client.get_messages(entity, ids=mid)
            if not message or not hasattr(message, "message"):
                raise HistoryUnavailable("This message is deleted or inaccessible to the owner session.")
            item = await self.record(message, chat)
            self.service.db.save_message(item)
            image = None
            if message.photo and (getattr(message.file, "size", 0) or 0) <= 10_000_000:
                image = await self.client.download_media(message, file=bytes)
                if image and len(image) > 10_000_000:
                    raise HistoryUnavailable("This photo exceeds the 10 MB reading limit.")
            return item, image

    def keyboard(self):
        nonce = self.login["nonce"]
        rows = [
            [InlineKeyboardButton(str(n), callback_data=f"login:{nonce}:{n}") for n in range(i, i + 3)]
            for i in (1, 4, 7)
        ]
        rows += [
            [
                InlineKeyboardButton("⌫", callback_data=f"login:{nonce}:back"),
                InlineKeyboardButton("0", callback_data=f"login:{nonce}:0"),
                InlineKeyboardButton(
                    f"Submit ({len(self.login['digits'])} digits)", callback_data=f"login:{nonce}:submit"
                ),
            ],
            [InlineKeyboardButton("Cancel login", callback_data=f"login:{nonce}:cancel")],
        ]
        return InlineKeyboardMarkup(rows)

    async def command(self, message, cmd, args):
        # Defense in depth; granted operators and other group admins cannot authenticate an account.
        if message.chat.type != "private" or not self.service.owner(message.from_user):
            return
        async with self.lock:
            try:
                if cmd == "/login":
                    await self.cancel()
                    if not args or not args.startswith("+") or not args[1:].isdigit():
                        raise HistoryUnavailable("Use /login +COUNTRYCODEPHONENUMBER in this private chat.")
                    self.client = self.new_client()
                    await self.client.connect()
                    if await self.client.is_user_authorized():
                        await self.connected()
                        await self.service.send(
                            message.chat_id, "✅ Owner session already connected. Old history is available."
                        )
                        return
                    sent = await self.client.send_code_request(args)
                    self.login = {
                        "phone": args,
                        "hash": sent.phone_code_hash,
                        "digits": "",
                        "nonce": secrets.token_hex(6),
                        "expires": time.time() + 300,
                        "chat": message.chat_id,
                        "stage": "otp",
                        "attempts": 0,
                    }
                    self.expiry_task = asyncio.create_task(self.expire(self.login["nonce"]))
                    posted = await self.service.send(
                        message.chat_id,
                        "🔐 Owner authentication\n\nEnter the login code using the keypad below, then Submit. "
                        "Do not send or forward the code as a message: Telegram can invalidate it. "
                        "The keypad expires in five minutes. Only the bound owner can use it.",
                        reply_markup=self.keyboard(),
                    )
                    self.login["message_id"] = posted.message_id
                elif cmd == "/login2fa":
                    if (
                        not self.login
                        or self.login["stage"] != "password"
                        or time.time() > self.login["expires"]
                    ):
                        raise HistoryUnavailable("No active two-step password request. Start /login again.")
                    # Private password message is deleted immediately and never stored in message memory.
                    with suppress(TelegramError):
                        await self.service.bot.delete_message(message.chat_id, message.message_id)
                    if not args:
                        raise HistoryUnavailable("Use /login2fa YOUR_TWO_STEP_PASSWORD in this private chat.")
                    await self.client.sign_in(password=args)
                    await self.finish(message.chat_id)
                elif cmd == "/logout":
                    await self.cancel()
                    if not self.client:
                        self.client = self.new_client()
                    if self.client:
                        await self.client.connect()
                        if await self.client.is_user_authorized():
                            await self.client.log_out()
                        await self.client.disconnect()
                        self.client = None
                    await self.service.send(
                        message.chat_id, "Owner history session disconnected and revoked."
                    )
                elif cmd == "/history_status":
                    await self.connected()
                    await self.service.send(
                        message.chat_id, "✅ History reader authenticated as the bound owner."
                    )
            except HistoryUnavailable as exc:
                await self.service.send(message.chat_id, str(exc))
            except Exception as exc:
                # No RPC error strings, phone numbers, codes or passwords in logs/messages.
                await self.service.send(
                    message.chat_id,
                    f"Login operation failed ({type(exc).__name__}). "
                    "Check the code/password or /login again. No secrets were logged.",
                )

    async def callback(self, query):
        if (
            not query.message
            or query.message.chat.type != "private"
            or not self.service.owner(query.from_user)
        ):
            return
        async with self.lock:
            fields = (query.data or "").split(":")
            if (
                len(fields) != 3
                or not self.login
                or fields[1] != self.login["nonce"]
                or query.message.chat_id != self.login["chat"]
                or query.message.message_id != self.login.get("message_id")
                or time.time() > self.login["expires"]
            ):
                await self.service.send(query.message.chat_id, "Login keypad expired. Start /login again.")
                return
            action = fields[2]
            if action == "cancel":
                await self.cancel()
                with suppress(TelegramError):
                    await query.edit_message_reply_markup(reply_markup=None)
                await self.service.send(query.message.chat_id, "Login cancelled.")
                return
            if self.login["stage"] != "otp":
                return
            if action.isdigit() and len(action) == 1:
                self.login["digits"] = (self.login["digits"] + action)[:7]
            elif action == "back":
                self.login["digits"] = self.login["digits"][:-1]
            elif action == "submit":
                if not 5 <= len(self.login["digits"]) <= 7:
                    await self.service.send(query.message.chat_id, "Enter all 5–7 code digits on the keypad.")
                    return
                from telethon.errors import SessionPasswordNeededError

                try:
                    await self.client.sign_in(
                        phone=self.login["phone"],
                        code=self.login["digits"],
                        phone_code_hash=self.login["hash"],
                    )
                    await self.finish(query.message.chat_id)
                    with suppress(TelegramError):
                        await query.edit_message_reply_markup(reply_markup=None)
                except SessionPasswordNeededError:
                    self.login["digits"] = ""
                    self.login["stage"] = "password"
                    with suppress(TelegramError):
                        await query.edit_message_reply_markup(reply_markup=None)
                    await self.service.send(
                        query.message.chat_id,
                        "Two-step verification is enabled. Use /login2fa YOUR_PASSWORD here; "
                        "the message is deleted immediately. Alternatively authenticate from the server terminal.",
                    )
                except Exception as exc:
                    if self.login:
                        self.login["digits"] = ""
                        self.login["attempts"] += 1
                        if self.login["attempts"] >= 3:
                            await self.cancel()
                    await self.service.send(
                        query.message.chat_id,
                        f"Code was not accepted ({type(exc).__name__}). Retry the keypad or /login again.",
                    )
                return
            # Never display digits; only count. Editing a regular message also works on older clients.
            with suppress(TelegramError):
                await query.edit_message_reply_markup(reply_markup=self.keyboard())

    async def finish(self, chat):
        me = await self.client.get_me()
        if me.id != self.service.db.get("owner_id"):
            await self.client.log_out()
            await self.cancel()
            raise HistoryUnavailable(
                "Account rejected: it must match the permanently bound owner's numeric ID."
            )
        path = self.session_path()
        if path.exists():
            path.chmod(0o600)
        env_path = Path(os.getenv("ENV_FILE", ".env"))
        # Store the session PATH in env, rather than the reusable account credential itself.
        env_saved = False
        if env_path.exists():
            try:
                set_key(str(env_path), "TG_USER_SESSION_PATH", str(path))
                env_path.chmod(0o600)
                env_saved = True
            except OSError:
                pass  # Docker env_file is read at startup; the persisted session still works.
        self.login = None
        if self.expiry_task:
            self.expiry_task.cancel()
            self.expiry_task = None
        await self.service.send(
            chat,
            "✅ Owner authenticated\n\nSession saved on the server. "
            + (
                "Its path is saved in .env. "
                if env_saved
                else "Keep TG_USER_SESSION_PATH set to this session path in your deployment environment. "
            )
            + "Summaries can now load up to 1000 older messages in enabled groups you already belong to. "
            "Use /logout to revoke this session.",
        )

    async def expire(self, nonce):
        await asyncio.sleep(300)
        async with self.lock:
            if self.login and self.login["nonce"] == nonce:
                chat = self.login["chat"]
                await self.cancel()
                with suppress(TelegramError):
                    await self.service.send(chat, "Login expired. Start /login again.")

    async def cancel(self):
        self.login = None
        if self.expiry_task and self.expiry_task != asyncio.current_task():
            self.expiry_task.cancel()
        self.expiry_task = None
        if self.client:
            await self.client.disconnect()

    async def close(self):
        await self.cancel()


async def terminal_login():
    import getpass

    from telethon import TelegramClient

    load_dotenv()
    from .config import Config
    from .db import DB

    config = Config.load()
    db = DB(config.database)
    owner = db.get("owner_id") or config.owner_id
    if not owner:
        raise SystemExit("First bind the owner with /start in the bot's private chat.")
    path = HistoryReader.session_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = TelegramClient(
        str(path), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"], receive_updates=False
    )
    try:
        await client.start(
            phone=lambda: input("Owner phone (+countrycode): "),
            code_callback=lambda: getpass.getpass("Telegram login code: "),
            password=lambda: getpass.getpass("Two-step password: "),
        )
        if (await client.get_me()).id != owner:
            await client.log_out()
            raise SystemExit("Account rejected: it is not the bound owner.")
        path.chmod(0o600)
        print("Owner history session saved. Restart the bot.")
    finally:
        await client.disconnect()
        db.close()


if __name__ == "__main__":
    asyncio.run(terminal_login())
