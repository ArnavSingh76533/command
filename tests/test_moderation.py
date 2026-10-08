import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from conftest import record, tg_message
from telegram.error import Forbidden

from bot.llm import ModelUnavailable
from bot.models import Verdict
from bot.service import target_matches


def rule(db, kind="promo_rule", **kwargs):
    db.add_rule(
        -1001,
        1,
        1,
        {"kind": kind, "policy": "Unsolicited job channel promotion", "action": "delete", **kwargs},
    )
    return db.rules(-1001)[-1]


def verdict(promo=True, confidence=0.99, evidence="Join @jobs"):
    return Verdict(promo=promo, confidence=confidence, evidence=evidence, reason="Unsolicited channel ad")


async def test_promo_two_reviews_and_manual_ban(service, db):
    rule(db)
    item = record()
    db.save_message(item)
    service.llm.request.side_effect = [verdict(), verdict()]
    await service.moderate(item)
    assert service.llm.request.await_count == 2
    service.bot.delete_message.assert_awaited_once_with(-1001, 2)
    service.bot.ban_chat_member.assert_not_awaited()
    request = db.request(1)
    assert request["status"] == "pending"
    assert request["state"]["stage"] == "deleted"
    await service.resolve(request, "ban", 10)
    service.bot.ban_chat_member.assert_awaited_once_with(-1001, 2, revoke_messages=False)


@pytest.mark.parametrize("user,sender", [(10, None), (None, -111)])
async def test_admin_and_sender_chat_never_flagged(service, db, user, sender):
    rule(db)
    await service.moderate(record(user=user, sender_chat=sender))
    service.llm.request.assert_not_awaited()
    service.bot.delete_message.assert_not_awaited()
    assert db.all("SELECT * FROM requests") == []


async def test_explicit_target_can_delete_admin(service, db):
    rule(db, "target_rule", target_id=10, media="all")
    await service.moderate(record(user=10))
    service.bot.delete_message.assert_awaited_once()
    assert db.all("SELECT * FROM requests") == []


@pytest.mark.parametrize(
    "media,text,match",
    [
        ("sticker", "@jobs", True),
        ("video", "@jobs", False),
        ("sticker", "@jobss", False),
        ("sticker", "email@jobs", False),
        ("sticker", "Hello @JOBS!", True),
        ("sticker", "hello", False),
    ],
)
def test_target_media_and_mention(media, text, match):
    assert (
        target_matches(
            {"target_id": 2, "media": "sticker", "mention": "jobs"}, record(media=media, text=text)
        )
        is match
    )


async def test_target_does_not_follow_username_change(service, db):
    rule(db, "target_rule", target_id=2, media="all")
    await service.moderate(record(user=3, username="member"))
    service.bot.delete_message.assert_not_awaited()


async def test_inline_uses_metadata_not_text_and_protects_admin(service, db):
    rule(db, "inline_rule", inline_username="gif")
    await service.moderate(record(text="Sent via @gif"))
    await service.moderate(record(message=3, via_bot="gif", user=10))
    service.bot.delete_message.assert_not_awaited()
    await service.moderate(record(message=4, via_bot="gif"))
    service.bot.delete_message.assert_awaited_once_with(-1001, 4)


@pytest.mark.parametrize(
    "first,second",
    [
        (verdict(False), verdict()),
        (verdict(), verdict(False)),
        (verdict(confidence=0.6), verdict()),
        (verdict(evidence="not present"), verdict()),
    ],
)
async def test_uncertainty_disagreement_invalid_evidence_no_action(service, db, first, second):
    rule(db)
    service.llm.request.side_effect = [first, second]
    await service.moderate(record())
    service.bot.delete_message.assert_not_awaited()
    assert db.all("SELECT * FROM requests") == []


async def test_model_failure_never_deletes(service, db):
    rule(db)
    service.llm.request.side_effect = ModelUnavailable("unavailable")
    await service.moderate(record())
    service.bot.delete_message.assert_not_awaited()


async def test_log_delivery_failure_prevents_action(service, db):
    rule(db)
    service.llm.request.side_effect = [verdict(), verdict()]
    service.bot.send_message.side_effect = Forbidden("log unavailable")
    await service.moderate(record())
    service.bot.delete_message.assert_not_awaited()
    service.bot.restrict_chat_member.assert_not_awaited()
    assert db.request(1)["state"]["stage"] == "prepared"


async def test_false_flag_persists_without_provider_and_exact_replay_ignored(service, db):
    r = rule(db)
    rid = db.new_request("promo", -1001, 2, 2, r["id"], "Join @jobs for work", "flag", {})
    service.llm.request.side_effect = ModelUnavailable("unavailable")
    await service.resolve(db.request(rid), "false", 10)
    assert db.exact_exception(-1001, r["id"], "JOIN   @jobs for work")
    await service.moderate(record())
    assert service.llm.request.await_count == 0  # background feedback scheduled; classifier skipped
    await service.learn_false_flag(db.request(rid))
    assert service.llm.request.await_count == 1
    service.bot.delete_message.assert_not_awaited()


async def test_no_retroactive_moderation_or_disabled_rule(service, db):
    r = rule(db)
    await service.moderate(record(message=1))
    await service.moderate(record(message=2, date=time.time() - 1000))
    db.execute("UPDATE rules SET active=0 WHERE id=?", (r["id"],))
    await service.moderate(record(message=3))
    service.llm.request.assert_not_awaited()


async def test_disabled_group_and_revoked_creator_no_actions(service, db):
    rule(db, "target_rule", target_id=2)
    db.enable(-1001, 1, "Test", False)
    await service.moderate(record())
    db.enable(-1001, 1, "Test")
    service.bot.get_chat_member.side_effect = lambda chat, user: SimpleNamespace(status="member")
    await service.moderate(record(message=3))
    service.bot.delete_message.assert_not_awaited()


async def test_mute_and_cancel_restore_original_permissions(service, db):
    r = rule(db, action="mute_review")
    item = record()
    db.save_message(item)
    await service.enforce_promo(item, r, verdict())
    request = db.request(1)
    assert request["state"]["owns_mute"]
    assert request["state"]["stage"] == "muted"
    until = request["state"]["until"]
    restricted = SimpleNamespace(
        status="restricted",
        until_date=datetime.fromtimestamp(until, timezone.utc),
        to_dict=lambda: {"can_send_messages": False, "can_send_photos": False},
    )
    service.bot.get_chat_member.side_effect = lambda chat, user: (
        SimpleNamespace(status="administrator") if user == 10 else restricted
    )
    await service.resolve(request, "cancel", 10)
    assert service.bot.restrict_chat_member.await_count == 2
    assert db.request(1)["state"]["owns_mute"] is False


async def test_preexisting_mute_not_overwritten(service, db):
    r = rule(db, action="mute_review")
    service.bot.get_chat_member.side_effect = lambda chat, user: SimpleNamespace(
        status="administrator" if user == 1 else "restricted"
    )
    await service.enforce_promo(record(), r, verdict())
    service.bot.restrict_chat_member.assert_not_awaited()
    assert not db.request(1)["state"]["owns_mute"]


async def test_other_moderator_mute_not_restored(service, db):
    rid = db.new_request(
        "promo",
        -1001,
        2,
        state={"owns_mute": True, "until": 12345, "original_permissions": {"can_send_messages": True}},
    )
    service.bot.get_chat_member.side_effect = None
    service.bot.get_chat_member.return_value = SimpleNamespace(
        status="restricted",
        until_date=datetime.fromtimestamp(23456, timezone.utc),
        to_dict=lambda: {"can_send_messages": False},
    )
    assert "changed" in await service.restore_mute(db.request(rid))
    service.bot.restrict_chat_member.assert_not_awaited()


async def test_admin_cannot_be_banned_even_on_old_review(service, db):
    rid = db.new_request("promo", -1001, 10)
    with pytest.raises(ValueError):
        await service.resolve(db.request(rid), "ban", 1)
    service.bot.ban_chat_member.assert_not_awaited()


async def test_regular_user_cannot_review(service, db):
    rid = db.new_request("promo", -1001, 2)
    with pytest.raises(ValueError):
        await service.resolve(db.request(rid), "ban", 3)
    service.bot.ban_chat_member.assert_not_awaited()


async def test_permission_changed_during_llm_review(service, db):
    rule(db)

    async def response(*args, **kwargs):
        db.enable(-1001, 1, "Test", False)
        return verdict()

    service.llm.request.side_effect = response
    await service.moderate(record())
    service.bot.delete_message.assert_not_awaited()


async def test_target_dedup(service, db):
    rule(db, "target_rule", target_id=2)
    await service.moderate(record(date=9999999999))
    await service.moderate(record(date=9999999999))
    service.bot.delete_message.assert_awaited_once()


async def test_reply_target_binds_numeric_id(service, db):
    message = tg_message(text="/target sticker", reply=tg_message(user=10))
    await service.install_rule(message, {"kind": "target_rule", "media": "sticker"})
    assert db.rules(-1001)[0]["body"]["target_id"] == 10


async def test_closed_request_no_late_punishment(service, db):
    r = rule(db)
    rid = db.new_request("promo", -1001, 2, state={"action": "delete", "stage": "prepared"})
    db.update_request(rid, status="false_flag")
    await service.apply_request(rid, record(), r)
    service.bot.delete_message.assert_not_awaited()


async def test_untrusted_slash_not_exempt_from_moderation(service, db):
    message = tg_message(user=2, text="/spam Join @jobs for work")
    await service.on_message(SimpleNamespace(effective_message=message), None)
    assert db.claim_job()["item"]["operator_control"] is False


async def test_bot_cannot_issue_owner_commands(service, db):
    message = tg_message(user=1, text="/allow 2", chat=1)
    object.__setattr__(message.from_user, "is_bot", True)
    await service.command(message, message.text)
    assert not db.allowed(2)


async def test_rule_requires_bot_permissions(service, db):
    service.bot.get_chat_member.side_effect = lambda chat, user: SimpleNamespace(
        status="administrator", can_delete_messages=user != 99, can_restrict_members=False
    )
    await service.install_rule(tg_message(reply=tg_message(user=2)), {"kind": "target_rule", "media": "all"})
    assert not db.rules(-1001)


async def test_same_second_future_message_is_not_skipped(service, db):
    created = int(time.time())
    db.add_rule(-1001, 1, 1, {"kind": "target_rule", "target_id": 2}, created=created)
    await service.moderate(record(message=2, date=created))
    service.bot.delete_message.assert_awaited_once()


async def test_owner_without_group_admin_status_cannot_approve_ban(service, db):
    rid = db.new_request("promo", -1001, 2)
    service.bot.get_chat_member.side_effect = lambda chat, user: SimpleNamespace(status="member")
    with pytest.raises(ValueError):
        await service.resolve(db.request(rid), "ban", 1)
    service.bot.ban_chat_member.assert_not_awaited()
