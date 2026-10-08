import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import record, tg_message

from bot.db import DB
from bot.llm import LLM, ModelUnavailable, validate_url
from bot.models import Verdict


def test_history_1000_chronological_and_group_isolated(db):
    for i in range(1, 1101):
        db.save_message(record(message=i, text=f"message {i}"))
    db.save_message(record(message=9999, chat_id=-1002, text="Other group's secret"))
    rows = db.history(-1001, 1000, before=1100)
    assert len(rows) == 1000
    assert rows[0]["message_id"] == 100
    assert rows[-1]["message_id"] == 1099
    assert all(r["chat_id"] == -1001 for r in rows)


def test_memory_survives_restart_and_is_scoped(tmp_path):
    path = str(tmp_path / "durable.sqlite3")
    db = DB(path)
    db.set("owner_id", 1)
    rid = db.new_request("promo", -1001, 2, rule=1, text="Discussing a job offer")
    db.feedback(db.request(rid), "Discussion, not recruiting")
    db.save_message(record())
    db.enqueue(record())
    assert db.claim_job()["status"] == "pending"
    db.close()
    reopened = DB(path)
    assert reopened.get("owner_id") == 1
    assert reopened.exact_exception(-1001, 1, "Discussing a job offer")
    assert not reopened.exact_exception(-1002, 1, "Discussing a job offer")
    assert len(reopened.history(-1001, 10)) == 1
    assert len(reopened.recalled(-1001, 1, "job offer")) == 1
    reopened.execute("UPDATE jobs SET status='pending' WHERE status='running'")
    assert reopened.claim_job() is not None
    reopened.close()


def test_request_claim_prevents_duplicate_decisions(db):
    rid = db.new_request("promo", -1001, 2)
    assert db.claim(rid, 10)
    assert not db.claim(rid, 11)


async def test_owner_username_bound_once_and_cannot_be_taken_over(service, db):
    db.execute("DELETE FROM settings WHERE key='owner_id'")
    await service.command(tg_message(user=11, chat=11, username="yucant"), "/start")
    assert db.get("owner_id") == 11
    await service.command(tg_message(user=12, chat=12, username="yucant"), "/start")
    assert not db.allowed(12)


async def test_owner_only_grants_and_private_api_updates(service, db):
    await service.command(tg_message(user=2, chat=2, text="/allow 3"), "/allow 3")
    assert not db.allowed(3)
    await service.command(tg_message(user=1, chat=-1001, text="/allow 3"), "/allow 3")
    assert not db.allowed(3)
    await service.command(tg_message(user=1, chat=1, text="/allow 3"), "/allow 3")
    assert db.allowed(3)


async def test_approved_regular_member_cannot_add_rule(service, db):
    db.execute("INSERT INTO grants VALUES (?,?)", (2, 0))
    await service.command(tg_message(user=2, text="/promo delete"), "/promo delete")
    assert db.rules(-1001) == []


async def test_review_checks_original_group_not_log_admin(service, db):
    rid = db.new_request("promo", -1001, 2)
    query = SimpleNamespace(
        data=f"r:{rid}:ban",
        from_user=tg_message(user=3).from_user,
        message=SimpleNamespace(chat_id=-1009),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    service.bot.get_chat_member.side_effect = lambda chat, user: SimpleNamespace(
        status="administrator" if chat == -1009 else "member"
    )
    await service.on_callback(SimpleNamespace(callback_query=query), None)
    assert db.request(rid)["status"] == "pending"
    service.bot.ban_chat_member.assert_not_awaited()


async def test_access_approval_owner_only(service, db):
    rid = db.new_request("access", -1001, 10)
    with pytest.raises(ValueError):
        await service.resolve(db.request(rid), "allow", 10)
    assert not db.allowed(10)
    await service.resolve(db.request(rid), "allow", 1)
    assert db.allowed(10)


async def test_summary_uses_snapshot_excludes_command_and_other_group(service, db):
    for i in range(1, 6):
        db.save_message(record(message=i))
    db.save_message(record(message=1, chat_id=-1002))
    captured = []

    def schedule(coro, **kwargs):
        captured.append(coro)

    service.app.create_task = schedule
    service.llm.summarize.return_value = "A conclusion"
    message = tg_message(text="/summary 500")
    object.__setattr__(message, "message_id", 6)
    await service.schedule_summary(message, 500, "/summary 500")
    await captured[0]
    passed = service.llm.summarize.call_args.args[0]
    assert [m["message_id"] for m in passed] == [1, 2, 3, 4, 5]
    assert all(m["chat_id"] == -1001 for m in passed)
    assert -1001 not in service.summary_busy


async def test_1000_message_summary_includes_every_chunk(config, db):
    llm = LLM(config, db)
    seen = []

    async def request(system, data, **kwargs):
        if "messages" in data:
            seen.extend(m["id"] for m in data["messages"])
            return "Evidence note"
        assert data["statistics"]["actual"] == 1000
        return "Summary"

    llm.request = request
    result = await llm.summarize([record(message=i) for i in range(1000)], 1000, "Rate the discussion")
    assert result == "Summary"
    assert seen == list(range(1000))
    await llm.close()


async def test_http_schema_request_and_validation(config, db, monkeypatch):
    monkeypatch.setenv("TEST_GROQ_KEY", "fake_test_key")
    llm = LLM(config, db)
    await llm.client.aclose()

    def transport(request):
        body = json.loads(request.content)
        assert body["model"] == "openai/gpt-oss-120b"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert request.headers["authorization"] == "Bearer fake_test_key"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"promo": False, "confidence": 0.9, "reason": "benign", "evidence": ""}
                            )
                        },
                    }
                ]
            },
        )

    llm.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    response = await llm.request("JSON classification", {"text": "Hello"}, Verdict)
    assert response.promo is False
    await llm.close()


async def test_provider_error_does_not_expose_secret(config, db, monkeypatch):
    monkeypatch.setenv("TEST_GROQ_KEY", "secret_key_do_not_echo")
    llm = LLM(config, db)
    await llm.client.aclose()
    llm.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "secret_key_do_not_echo"})
        )
    )
    with pytest.raises(ModelUnavailable) as exc:
        await llm.request("test", {})
    assert "secret_key_do_not_echo" not in str(exc.value)
    await llm.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "https://user:pass@example.com/v1",
        "https://example.com:444/v1",
        "https://example.com/v1?key=secret",
    ],
)
async def test_invalid_api_urls_rejected(url):
    with pytest.raises(ValueError):
        await validate_url(url)


async def test_private_api_host_rejected(monkeypatch):
    monkeypatch.setattr("bot.llm.socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError):
        await validate_url("https://example.com/v1")


async def test_callback_false_flag_closes_and_learns(service, db):
    rid = db.new_request("promo", -1001, 2, rule=1, text="Discussing jobs")
    service.llm.request.side_effect = ModelUnavailable("offline")
    query = SimpleNamespace(
        data=f"r:{rid}:false",
        from_user=tg_message(user=10).from_user,
        message=SimpleNamespace(chat_id=-1009),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    await service.on_callback(SimpleNamespace(callback_query=query), None)
    assert db.request(rid)["status"] == "false_flag"
    assert db.exact_exception(-1001, 1, "Discussing jobs")
    await service.on_callback(SimpleNamespace(callback_query=query), None)
    assert db.one("SELECT count(*) n FROM feedback")["n"] == 1
