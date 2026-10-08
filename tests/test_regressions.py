import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import record, tg_message
from telegram import ChatPermissions
from telegram.error import BadRequest, Forbidden

from bot.history import HistoryUnavailable
from bot.llm import LLM, ModelUnavailable
from bot.models import Verdict
from bot.rich import RichSender, message_text
from bot.service import Service


def query(data, user=10, private=False):
    return SimpleNamespace(
        data=data,
        from_user=tg_message(user=user).from_user,
        message=tg_message(chat=user if private else -1009),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


async def test_expired_callback_ack_cannot_strand_review(service, db):
    rid = db.new_request("promo", -1001, 2)
    q = query(f"r:{rid}:cancel")
    q.answer.side_effect = BadRequest("Query is too old")
    await service.on_callback(SimpleNamespace(callback_query=q), None)
    assert db.request(rid)["status"] == "cancelled"


async def test_keyboard_cleanup_failure_keeps_committed_false_flag(service, db):
    rid = db.new_request("promo", -1001, 2, rule=1, text="Benign discussion")
    q = query(f"r:{rid}:false")
    q.edit_message_reply_markup.side_effect = BadRequest("Message is not modified")
    await service.on_callback(SimpleNamespace(callback_query=q), None)
    assert db.request(rid)["status"] == "false_flag"
    assert db.exact_exception(-1001, 1, "Benign discussion")


async def test_callback_ack_before_slow_membership_check(service, db):
    rid = db.new_request("promo", -1001, 2)
    q = query(f"r:{rid}:cancel")

    async def member(chat, uid):
        assert q.answer.await_count >= 1
        return SimpleNamespace(status="administrator")

    service.bot.get_chat_member.side_effect = member
    await service.on_callback(SimpleNamespace(callback_query=q), None)
    assert db.request(rid)["status"] == "cancelled"


async def test_callback_permission_failure_is_retryable(service, db):
    rid = db.new_request("promo", -1001, 2)
    q = query(f"r:{rid}:cancel")
    service.bot.get_chat_member.side_effect = Forbidden("Unavailable")
    await service.on_callback(SimpleNamespace(callback_query=q), None)
    assert db.request(rid)["status"] == "pending"


async def test_mute_notice_contains_reason_and_waiting_status(service, db):
    db.add_rule(-1001, 1, 1, {"kind": "promo_rule", "action": "mute_review", "policy": "Spam"})
    item = record()
    db.save_message(item)
    await service.enforce_promo(
        item,
        db.rules(-1001)[0],
        Verdict(promo=True, confidence=1, evidence="Join @jobs", reason="External recruitment"),
    )
    notices = [c.args[1] for c in service.bot.send_message.call_args_list if c.args[0] == -1001]
    assert any(
        "Muted" in n and "External recruitment" in n and "Waiting for admin inspection" in n for n in notices
    )
    assert db.request(1)["state"]["notice_sent"]


async def test_restore_unknown_permission_and_missing_until(service, db):
    until = int(time.time()) + 3600
    rid = db.new_request(
        "promo",
        -1001,
        2,
        state={
            "owns_mute": True,
            "until": until,
            "original_permissions": {"can_send_messages": True, "can_future_permission": False},
        },
    )
    member = SimpleNamespace(
        status="restricted",
        until_date=datetime.fromtimestamp(until, timezone.utc),
        to_dict=lambda: {"can_send_messages": False},
    )
    service.bot.get_chat_member.side_effect = None
    service.bot.get_chat_member.return_value = member
    await service.restore_mute(db.request(rid))
    permissions = service.bot.restrict_chat_member.call_args.args[2]
    assert isinstance(permissions, ChatPermissions)
    assert permissions.can_send_messages
    member.until_date = None
    assert "changed" in await service.restore_mute(
        {**db.request(rid), "state": {"owns_mute": True, "until": until}}
    )


@pytest.mark.parametrize(
    "text,intent",
    [
        ("what's written here", "read_message"),
        ("censor this to delete", "delete_message"),
        ("copy and send me this message", "copy_message"),
        ("delete his future stickers", None),
    ],
)
def test_reply_routing_does_not_create_accidental_summary(text, intent):
    assert Service.direct_action(text, tg_message(user=2, text="hello")) == intent


async def test_read_reply_returns_selected_text_without_group_summary(service):
    reply = tg_message(user=2, text="A job discussion, not an advert")
    message = tg_message(text="@testbot what's written here", reply=reply)
    await service.command(message, message.text)
    service.llm.request.assert_not_awaited()
    service.llm.summarize.assert_not_awaited()
    assert "A job discussion" in service.bot.send_message.call_args.args[1]


async def test_delete_replied_admin_message_now(service, db):
    reply = tg_message(user=10, text="An admin message")
    message = tg_message(text="@testbot censor this to delete", reply=reply)
    await service.command(message, message.text)
    service.bot.delete_message.assert_awaited_once_with(-1001, reply.message_id)
    assert db.rules(-1001) == []


async def test_approved_nonadmin_cannot_delete_reply(service, db):
    db.execute("INSERT INTO grants VALUES (?,?)", (2, 0))
    message = tg_message(user=2, text="@testbot delete this", reply=tg_message(user=3, text="hello"))
    await service.command(message, message.text)
    service.bot.delete_message.assert_not_awaited()


async def test_copy_private_message_link_with_bot_api(service, db):
    chat = -1002397157264
    db.enable(chat, 1, "Test")
    service.bot.copy_message = AsyncMock()
    message = tg_message(
        chat=chat, text="@testbot https://t.me/c/2397157264/231866 copy and send me this message"
    )
    await service.command(message, message.text)
    service.bot.copy_message.assert_awaited_once_with(chat, chat, 231866)
    service.llm.request.assert_not_awaited()


async def test_copy_cannot_exfiltrate_disabled_source(service):
    service.bot.copy_message = AsyncMock()
    message = tg_message(text="@testbot copy https://t.me/c/555555/231866")
    await service.command(message, message.text)
    service.bot.copy_message.assert_not_awaited()


def test_screenshot_groupwide_instruction_has_no_target_id():
    instruction = (
        "delete any upcoming messages of user who send stickers or gif even reply them to not do "
        "if they don't follow this three time kick them out of group but don't ban them target everyone no id"
    )
    body = Service.direct_media_rule(instruction, None)
    assert body["kind"] == "media_rule"
    assert body["media_types"] == ["sticker", "animation"]
    assert body["kick_after"] == 3 and body["warn"]
    assert "target_id" not in body


async def test_groupwide_media_counts_once_and_kicks_without_permanent_ban(service, db):
    db.add_rule(
        -1001,
        1,
        1,
        {"kind": "media_rule", "media_types": ["sticker", "animation"], "warn": True, "kick_after": 3},
    )
    service.bot.unban_chat_member = AsyncMock()
    for mid in (2, 2, 3, 4, 4):
        await service.moderate(record(message=mid, media="sticker"))
    assert service.bot.delete_message.await_count == 3
    assert service.bot.ban_chat_member.await_count == 1
    assert time.time() + 25 < service.bot.ban_chat_member.call_args.kwargs["until_date"] < time.time() + 65
    service.bot.unban_chat_member.assert_awaited_once_with(-1001, 2, only_if_banned=True)


async def test_global_media_protects_admin_and_text(service, db):
    db.add_rule(
        -1001, 1, 1, {"kind": "media_rule", "media_types": ["sticker"], "warn": True, "kick_after": 3}
    )
    await service.moderate(record(media="sticker", user=10))
    await service.moderate(record(media="text", user=2))
    service.bot.delete_message.assert_not_awaited()
    service.bot.ban_chat_member.assert_not_awaited()


async def test_summary_imports_history_before_snapshot(service, db):
    service.history.configured = lambda: True

    async def backfill(chat, count, before):
        for mid in range(1, 501):
            db.save_message(record(message=mid))
        assert count == 500 and before == 501
        assert not db.one("SELECT 1 FROM jobs")

    service.history.import_history = backfill
    captured = []
    service.app.create_task = lambda coro: captured.append(coro)
    service.llm.summarize.return_value = "Summary"
    message = tg_message(text="/summary 500")
    object.__setattr__(message, "message_id", 501)
    await service.schedule_summary(message, 500, message.text)
    await captured[0]
    assert len(service.llm.summarize.call_args.args[0]) == 500


async def test_truncated_reasoning_response_retries_with_larger_budget(config, db, monkeypatch):
    monkeypatch.setenv("TEST_GROQ_KEY", "test")
    llm = LLM(config, db)
    await llm.client.aclose()
    budgets = []

    def transport(request):
        body = json.loads(request.content)
        budgets.append(body["max_completion_tokens"])
        assert body["reasoning_effort"] == "low" and body["include_reasoning"] is False
        assert "reasoning_format" not in body
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length" if len(budgets) < 3 else "stop",
                        "message": {"content": "Final summary"},
                    }
                ]
            },
        )

    llm.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    assert await llm.request("Summarize", {}) == "Final summary"
    assert budgets == [4000, 8000, 16000]
    await llm.close()


async def test_persistently_truncated_json_never_returns_partial_verdict(config, db, monkeypatch):
    monkeypatch.setenv("TEST_GROQ_KEY", "test")
    llm = LLM(config, db)
    await llm.client.aclose()
    llm.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"finish_reason": "length", "message": {"content": '{"promo":true'}}]}
            )
        )
    )
    with pytest.raises(ModelUnavailable):
        await llm.request("JSON", {}, Verdict)
    await llm.close()


async def test_native_rich_markup_safe_and_falls_back(service):
    service.bot.do_api_request = AsyncMock(side_effect=BadRequest("Method not found"))
    sender = RichSender()
    await sender.send(service.bot, -1001, "🔇 Muted\n\n**Reason:** <unsafe & text>")
    assert "<h3>" in service.bot.do_api_request.call_args.kwargs["api_kwargs"]["rich_message"]["html"]
    text = service.bot.send_message.call_args.args[1]
    assert "<b>Reason:</b>" in text and "&lt;unsafe &amp; text&gt;" in text
    assert sender.mode == "html"


def test_read_native_rich_reply_preserves_visible_text():
    message = SimpleNamespace(
        text=None,
        caption=None,
        api_kwargs={
            "rich_message": {
                "html": "<h3>Muted</h3><p>Reason: &lt;literal text&gt;<br>Waiting for admin inspection</p>"
            }
        },
    )
    assert message_text(message) == "Muted\nReason: <literal text>\nWaiting for admin inspection"


async def test_history_session_separate_account_accepted_without_owner_change(service, db):
    reader = service.history
    reader.client = SimpleNamespace(
        is_connected=lambda: True,
        is_user_authorized=AsyncMock(return_value=True),
        get_me=AsyncMock(return_value=SimpleNamespace(id=123)),
    )
    assert await reader.connected() is reader.client
    assert db.get("history_account")["id"] == 123
    assert db.get("owner_id") == 1
    assert not db.allowed(123)


async def test_disabled_group_history_denied_before_connection(service):
    service.history.connected = AsyncMock()
    with pytest.raises(HistoryUnavailable):
        await service.history.entity(-100555)
    service.history.connected.assert_not_awaited()


@pytest.mark.parametrize("user,chat", [(2, 2), (1, -1001)])
async def test_login_is_exclusive_to_bound_owner_private_chat(service, db, user, chat):
    db.execute("INSERT OR IGNORE INTO grants VALUES (?,?)", (2, 0))
    service.history.command = AsyncMock()
    message = tg_message(user=user, chat=chat, text="/login +1234567890")
    await service.command(message, message.text)
    service.history.command.assert_not_awaited()


async def test_otp_keypad_owner_and_nonce_guards(service):
    reader = service.history
    reader.login = {
        "nonce": "abc",
        "chat": 1,
        "message_id": 1,
        "expires": time.time() + 300,
        "digits": "",
        "stage": "otp",
    }
    for q in (query("login:abc:5", user=2, private=True), query("login:wrong:5", user=1, private=True)):
        await reader.callback(q)
    assert reader.login["digits"] == ""
    q = query("login:abc:5", user=1, private=True)
    await reader.callback(q)
    assert reader.login["digits"] == "5"
    assert (
        "5"
        not in json.dumps(q.edit_message_reply_markup.call_args.kwargs["reply_markup"].to_dict())
        .split("Submit")[1]
        .split("callback_data")[0]
    )


async def test_authenticated_session_saves_path_not_secret_to_env(service, tmp_path, monkeypatch):
    env = tmp_path / "env-file"
    env.write_text("GROQ_API_KEY=placeholder\n")
    path = tmp_path / "owner.session"
    path.write_text("secret-session-key")
    monkeypatch.setenv("ENV_FILE", str(env))
    monkeypatch.setenv("TG_USER_SESSION_PATH", str(path))
    reader = service.history
    reader.client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=1)))
    reader.login = {"digits": "12345"}
    await reader.finish(1)
    saved = env.read_text()
    assert "TG_USER_SESSION_PATH" in saved and "secret-session-key" not in saved and "12345" not in saved
    assert path.stat().st_mode & 0o777 == 0o600
    assert reader.login is None


async def test_authentication_accepts_separate_account_without_revoking_session(
    service, db, tmp_path, monkeypatch
):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "absent-env"))
    monkeypatch.setenv("TG_USER_SESSION_PATH", str(tmp_path / "linked.session"))
    reader = service.history
    reader.client = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=2)), log_out=AsyncMock(), disconnect=AsyncMock()
    )
    reader.login = {"digits": "12345"}
    await reader.finish(1)
    reader.client.log_out.assert_not_awaited()
    assert reader.login is None
    assert db.get("history_account")["id"] == 2
    assert db.get("owner_id") == 1
    assert not db.allowed(2)


async def test_history_import_does_not_enqueue_past_moderation(service, db):
    reader = service.history
    sender = SimpleNamespace(id=2, first_name="Member", last_name=None, username="member")
    message = SimpleNamespace(
        id=100,
        message="Past promo",
        date=datetime.now(timezone.utc),
        edit_date=None,
        get_sender=AsyncMock(return_value=sender),
        action=None,
    )

    async def messages(*args, **kwargs):
        assert kwargs == {"limit": 500, "max_id": 501}
        yield message

    reader.entity = AsyncMock(return_value="entity")
    reader.client = SimpleNamespace(iter_messages=messages)
    assert await reader.import_history(-1001, 500, 501) == 1
    assert db.history(-1001, 500)[0]["text"] == "Past promo"
    assert db.one("SELECT count(*) n FROM jobs")["n"] == 0


async def test_complete_private_keypad_login_links_separate_account(service, db, tmp_path, monkeypatch):
    reader = service.history
    path = tmp_path / "owner.session"
    path.write_text("session")
    monkeypatch.setenv("TG_USER_SESSION_PATH", str(path))
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "nonexistent-env"))
    client = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=False),
        send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="hash")),
        sign_in=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(id=2)),
    )
    reader.new_client = lambda: client
    await service.command(tg_message(chat=1, text="/login +1234567890"), "/login +1234567890")
    nonce = reader.login["nonce"]
    for action in ("1", "2", "3", "4", "5", "submit"):
        q = query(f"login:{nonce}:{action}", user=1, private=True)
        object.__setattr__(q.message, "message_id", 500)
        await service.on_callback(SimpleNamespace(callback_query=q), None)
    client.sign_in.assert_awaited_once_with(phone="+1234567890", code="12345", phone_code_hash="hash")
    assert reader.login is None
    assert path.stat().st_mode & 0o777 == 0o600
    assert db.get("history_account")["id"] == 2
    assert db.get("owner_id") == 1


async def test_two_step_password_deleted_and_not_archived(service, db):
    reader = service.history
    reader.login = {"stage": "password", "expires": time.time() + 100}
    reader.client = SimpleNamespace(sign_in=AsyncMock())
    reader.finish = AsyncMock()
    message = tg_message(chat=1, text="/login2fa a password with spaces")
    await service.on_message(SimpleNamespace(effective_message=message), None)
    reader.client.sign_in.assert_awaited_once_with(password="a password with spaces")
    service.bot.delete_message.assert_awaited_once_with(1, 1)
    assert not db.one("SELECT 1 FROM messages WHERE text LIKE '%password%'")


async def test_login_secret_in_group_never_archived(service, db):
    message = tg_message(text="/login2fa accidental-secret")
    await service.on_message(SimpleNamespace(effective_message=message), None)
    assert not db.history(-1001, 100)
    assert not db.one("SELECT 1 FROM jobs")
    service.bot.delete_message.assert_awaited_once_with(-1001, 1)


async def test_nonowner_cannot_evade_moderation_with_login_prefix(service, db):
    message = tg_message(user=2, text="/login join @spam")
    await service.on_message(SimpleNamespace(effective_message=message), None)
    assert db.history(-1001, 10)[0]["text"] == message.text
    assert db.one("SELECT count(*) n FROM jobs")["n"] == 1


async def test_vision_request_uses_separate_model(config, db, monkeypatch):
    monkeypatch.setenv("TEST_GROQ_KEY", "test")
    monkeypatch.setenv("VISION_MODEL", "qwen/qwen3.8-27b")
    llm = LLM(config, db)
    await llm.client.aclose()

    def transport(request):
        body = json.loads(request.content)
        assert body["model"] == "qwen/qwen3.8-27b"
        assert body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "Visible text"}}]}
        )

    llm.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    assert await llm.request("Read image", {}, image="data:image/jpeg;base64,dGVzdA==") == "Visible text"
    await llm.close()
