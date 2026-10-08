import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Chat, ChatPermissions, Message, User

from bot.config import Config
from bot.db import DB
from bot.service import Service


@pytest.fixture
def db(tmp_path):
    database = DB(str(tmp_path / "memory.sqlite3"))
    database.set("owner_id", 1)
    database.enable(-1001, 1, "Test")
    yield database
    database.close()


@pytest.fixture
def config(tmp_path):
    return Config(
        "123:placeholder",
        -1009,
        1,
        "yucant",
        str(tmp_path / "memory.sqlite3"),
        "https://api.groq.com/openai/v1",
        "openai/gpt-oss-120b",
        "TEST_GROQ_KEY",
        0.92,
        60,
        2,
        "schema",
    )


@pytest.fixture
def service(config, db):
    llm = SimpleNamespace(request=AsyncMock(), summarize=AsyncMock())
    instance = Service(config, db, llm)
    instance.username = "testbot"
    member = SimpleNamespace(status="member")
    admin = SimpleNamespace(status="administrator", can_restrict_members=True, can_delete_messages=True)
    instance.bot = SimpleNamespace(
        id=99,
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=500)),
        delete_message=AsyncMock(),
        ban_chat_member=AsyncMock(),
        restrict_chat_member=AsyncMock(),
        get_chat_member=AsyncMock(side_effect=lambda chat, user: admin if user in {1, 10, 99} else member),
        get_chat=AsyncMock(
            return_value=SimpleNamespace(
                id=-1001, title="Test", type="supergroup", permissions=ChatPermissions.all_permissions()
            )
        ),
    )
    instance.app = SimpleNamespace(create_task=lambda coro, **kw: coro.close())
    return instance


def record(message=2, user=2, text="Join @jobs for work", **kwargs):
    return {
        "chat_id": -1001,
        "message_id": message,
        "user_id": user,
        "username": "member",
        "name": "Member",
        "text": text,
        "media": "text",
        "via_bot": None,
        "sender_chat": None,
        "date": time.time() + 10,
        "edited": None,
        **kwargs,
    }


def tg_message(user=1, text="/start", chat=-1001, reply=None, username=None):
    return Message(
        1,
        datetime.now(timezone.utc),
        Chat(chat, "private" if chat > 0 else "supergroup", title="Test"),
        from_user=User(user, "Test", False, username=username),
        text=text,
        reply_to_message=reply,
    )
