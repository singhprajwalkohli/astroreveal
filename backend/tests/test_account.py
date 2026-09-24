"""Account deletion + retention tests (mock Mongo, no network).

    cd backend && python -m pytest -n 0 tests/test_account.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.update(MONGO_URL="mongodb://mock", DB_NAME="t", JWT_SECRET="test-secret", GEMINI_API_KEY="x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mongomock_motor import AsyncMongoMockClient  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from services import credits  # noqa: E402

ME = dict(name="Me Self", dob="1999-01-01", birth_time="10:30", birthplace="Delhi, India",
          latitude=28.6139, longitude=77.209, timezone_name="Asia/Kolkata")


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture()
def env(monkeypatch):
    db = AsyncMongoMockClient()["t"]
    monkeypatch.setattr(server, "db", db)

    async def fake_interpret(self, k): return "FULL " * 50
    async def fake_chat(self, q, k): return "answer"
    monkeypatch.setattr(server.AstrologyInterpretationService, "interpret", fake_interpret)
    monkeypatch.setattr(server.KundliChatService, "answer", fake_chat)
    client = TestClient(server.app)

    def user(email):
        r = client.post("/api/auth/register", json={"email": email, "password": "secret12"})
        return {"Authorization": f"Bearer {r.json()['token']}"}, r.json()["user"]["id"]
    return client, db, user


def fill(client, db, h, uid, order):
    """Give a user a bit of everything: credits, a person with a chart, a chat, an order and a webhook copy."""
    run(credits.grant_for_order(db, {"user_id": uid, "product": "bundle", "razorpay_order_id": order}))
    chart = client.post("/api/kundli", json=ME, headers=h).json()
    client.post("/api/chat", json={"question": "career?", "profile_id": chart["profile_id"]}, headers=h)
    run(db.palm_readings.insert_one({"id": "p" + uid, "user_id": uid, "reading": "x", "unlocked": True, "created_at": ago(0)}))
    run(db.orders.insert_one({"id": "ord" + uid, "user_id": uid, "product": "bundle", "amount": 15900, "razorpay_order_id": order, "payment_status": "paid"}))
    run(db.payment_events.insert_one({"event_id": "e" + uid, "raw": {"payload": {"payment": {"entity": {"order_id": order, "email": "x@y.com", "contact": "+911234567890"}}}}}))


def counts(db, uid):
    return {c: run(getattr(db, c).count_documents({"user_id": uid})) for c in
            ("kundlis", "profiles", "palm_readings", "ai_conversations", "credit_grants", "orders")}


def test_delete_account_removes_the_user_and_only_the_user(env):
    client, db, user = env
    ha, ua = user("a@b.com")
    hb, ub = user("b@b.com")
    fill(client, db, ha, ua, "order_a")
    fill(client, db, hb, ub, "order_b")
    before_b = counts(db, ub)

    r = client.post("/api/account/delete", json={"confirm": "DELETE"}, headers=ha)
    assert r.status_code == 200 and r.json()["deleted"] is True

    after = counts(db, ua)
    assert {k: v for k, v in after.items() if k != "orders"} == {"kundlis": 0, "profiles": 0, "palm_readings": 0, "ai_conversations": 0, "credit_grants": 0}
    assert run(db.users.count_documents({"id": ua})) == 0
    # webhook copies (customer contact details) are gone for A, but the accounting order row stays, with no email in it
    assert run(db.payment_events.count_documents({"event_id": "e" + ua})) == 0
    order = run(db.orders.find_one({"user_id": ua}))
    assert order["amount"] == 15900 and "email" not in order
    # B is untouched
    assert counts(db, ub) == before_b and run(db.payment_events.count_documents({"event_id": "e" + ub})) == 1
    # A's token no longer works, and the email can be registered again
    assert client.get("/api/auth/me", headers=ha).status_code == 401
    assert client.post("/api/auth/register", json={"email": "a@b.com", "password": "secret12"}).status_code == 200


def test_delete_needs_the_exact_confirmation(env):
    client, db, user = env
    h, uid = user("a@b.com")
    fill(client, db, h, uid, "order_a")
    for bad in ("delete", "", "yes"):
        assert client.post("/api/account/delete", json={"confirm": bad}, headers=h).status_code == 400
    assert counts(db, uid)["kundlis"] == 1 and run(db.users.count_documents({"id": uid})) == 1
    assert client.post("/api/account/delete", json={"confirm": "DELETE"}).status_code in (401, 403)   # not signed in


def test_activity_is_recorded_at_most_once_a_day(env):
    client, db, user = env
    h, uid = user("a@b.com")
    assert run(db.users.find_one({"id": uid}))["last_active_at"]
    run(db.users.update_one({"id": uid}, {"$set": {"last_active_at": ago(3)}}))
    client.post("/api/auth/login", json={"email": "a@b.com", "password": "secret12"})
    refreshed = run(db.users.find_one({"id": uid}))["last_active_at"]
    assert refreshed > ago(1)
    client.get("/api/dashboard", headers=h)          # same day: no rewrite
    assert run(db.users.find_one({"id": uid}))["last_active_at"] == refreshed


def test_retention_dry_run_deletes_nothing_and_real_run_deletes_only_the_inactive(env, monkeypatch):
    client, db, user = env
    old_h, old = user("old@b.com")
    new_h, new = user("new@b.com")
    legacy_h, legacy = user("legacy@b.com")
    fill(client, db, old_h, old, "order_old")
    fill(client, db, new_h, new, "order_new")
    run(db.users.update_one({"id": old}, {"$set": {"last_active_at": ago(server.RETENTION_DAYS + 5)}}))
    # an old account from before activity tracking existed: judged by created_at
    run(db.users.update_one({"id": legacy}, {"$set": {"created_at": ago(server.RETENTION_DAYS + 5)}, "$unset": {"last_active_at": ""}}))

    assert sorted(run(server.purge_inactive_accounts(dry_run=True))) == sorted([old, legacy])
    assert run(db.users.count_documents({})) == 3 and counts(db, old)["kundlis"] == 1      # dry run: nothing deleted

    assert sorted(run(server.purge_inactive_accounts(dry_run=False))) == sorted([old, legacy])
    assert run(db.users.count_documents({"id": new})) == 1 and counts(db, new)["kundlis"] == 1
    assert run(db.users.count_documents({"id": old})) == 0 and counts(db, old)["kundlis"] == 0


def test_retention_is_off_by_default_and_never_shorter_than_90_days():
    assert server.RETENTION_ENABLED is False
    assert server.RETENTION_DAYS >= 90


def test_app_starts_and_stops_cleanly_with_the_retention_task(monkeypatch):
    monkeypatch.setattr(server, "db", AsyncMongoMockClient()["t"])
    with TestClient(server.app) as client:
        assert client.get("/api/").status_code == 200
        assert server.app.state.retention_task.done() is False
