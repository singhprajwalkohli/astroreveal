"""Credit + gating tests. Run with a mock Mongo, no network, no real AI:

    cd backend && python -m pytest -n 0 tests/test_credits.py

Covers: idempotent grants, atomic single-spend, expiry, refund on failure, and that
every AI endpoint refuses unpaid users without calling the AI.
"""
from __future__ import annotations

import asyncio
import base64
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
from services.ai import AIServiceUnavailable  # noqa: E402

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()
PROFILE = dict(name="Test", dob="1999-01-01", birth_time="10:30", birthplace="Delhi, India",
               latitude=28.6139, longitude=77.209, timezone_name="Asia/Kolkata")


@pytest.fixture()
def env(monkeypatch):
    db = AsyncMongoMockClient()["t"]
    monkeypatch.setattr(server, "db", db)
    calls = {"interpret": 0, "chat": 0, "palm": 0, "charts": []}

    async def fake_interpret(self, k):
        calls["interpret"] += 1
        return "FULL INTERPRETATION " * 40

    async def fake_chat(self, q, k):
        calls["chat"] += 1
        calls["charts"].append(k)
        return "answer"

    async def fake_palm(self, img, hand):
        calls["palm"] += 1
        return "PALM READING " * 40

    monkeypatch.setattr(server.AstrologyInterpretationService, "interpret", fake_interpret)
    monkeypatch.setattr(server.KundliChatService, "answer", fake_chat)
    monkeypatch.setattr(server.PalmReadingService, "analyze", fake_palm)

    client = TestClient(server.app)
    r = client.post("/api/auth/register", json={"email": "a@b.com", "password": "secret1"})
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    uid = r.json()["user"]["id"]
    return client, db, headers, uid, calls


def _grant(db, uid, product="bundle", order="order_1"):
    asyncio.get_event_loop().run_until_complete(
        credits.grant_for_order(db, {"user_id": uid, "product": product, "razorpay_order_id": order}))


def test_unpaid_user_cannot_use_any_ai_endpoint(env):
    client, db, h, uid, calls = env
    # chart is free, but with a teaser and NO ai call
    r = client.post("/api/kundli", json=PROFILE, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["locked"] is True and calls["interpret"] == 0
    assert client.post("/api/chat", json={"question": "career?"}, headers=h).status_code == 402
    assert client.post("/api/palm", json={"image_base64": PNG, "hand": "left"}, headers=h).status_code == 402
    assert (calls["interpret"], calls["chat"], calls["palm"]) == (0, 0, 0)


def test_paid_flow_spends_credits_and_runs_out(env):
    client, db, h, uid, calls = env
    _grant(db, uid, "kundli", "o1")  # 1 kundli + 30 chat
    r = client.post("/api/kundli", json=PROFILE, headers=h)
    assert r.json()["locked"] is False and calls["interpret"] == 1
    # same person again: returns the same unlocked chart, no new AI call, no new charge
    again = client.post("/api/kundli", json=PROFILE, headers=h)
    assert again.json()["id"] == r.json()["id"] and calls["interpret"] == 1
    # the single kundli credit is gone: a chart for someone else is free-preview only, no AI call
    other = {**PROFILE, "name": "Someone Else", "relation": "friend", "consent_confirmed": True}
    r2 = client.post("/api/kundli", json=other, headers=h)
    assert r2.json()["locked"] is True and calls["interpret"] == 1
    for _ in range(3):
        assert client.post("/api/chat", json={"question": "career?", "profile_id": r.json()["profile_id"]}, headers=h).status_code == 200
    ent = client.get("/api/entitlements", headers=h).json()
    assert ent["credits"]["chat"] == 27 and ent["credits"]["kundli"] == 0


def test_one_palm_purchase_gives_exactly_one_reading(env):
    client, db, h, uid, calls = env
    _grant(db, uid, "palm", "o2")
    assert client.post("/api/palm", json={"image_base64": PNG, "hand": "right"}, headers=h).status_code == 200
    assert client.post("/api/palm", json={"image_base64": PNG, "hand": "right"}, headers=h).status_code == 402
    assert calls["palm"] == 1


def test_ai_failure_refunds_credit(env, monkeypatch):
    client, db, h, uid, calls = env
    _grant(db, uid, "palm", "o3")

    async def boom(self, img, hand):
        raise AIServiceUnavailable("down")
    monkeypatch.setattr(server.PalmReadingService, "analyze", boom)
    assert client.post("/api/palm", json={"image_base64": PNG, "hand": "left"}, headers=h).status_code == 503
    assert client.get("/api/entitlements", headers=h).json()["credits"]["palm"] == 1


def test_invalid_palm_refunds_credit(env, monkeypatch):
    client, db, h, uid, calls = env
    _grant(db, uid, "palm", "o4")

    async def invalid(self, img, hand):
        return "INVALID_PALM"
    monkeypatch.setattr(server.PalmReadingService, "analyze", invalid)
    assert client.post("/api/palm", json={"image_base64": PNG, "hand": "left"}, headers=h).status_code == 422
    assert client.get("/api/entitlements", headers=h).json()["credits"]["palm"] == 1


def test_unlock_existing_free_chart_after_purchase(env):
    client, db, h, uid, calls = env
    chart = client.post("/api/kundli", json=PROFILE, headers=h).json()
    assert chart["locked"] is True
    assert client.post(f"/api/kundli/{chart['id']}/unlock", headers=h).status_code == 402
    _grant(db, uid, "kundli", "o5")
    r = client.post(f"/api/kundli/{chart['id']}/unlock", headers=h)
    assert r.status_code == 200 and r.json()["locked"] is False
    # unlocking again is free and does not spend another credit
    client.post(f"/api/kundli/{chart['id']}/unlock", headers=h)
    assert calls["interpret"] == 1


def test_grants_are_idempotent_and_expire(env):
    client, db, h, uid, calls = env
    order = {"user_id": uid, "product": "palm", "razorpay_order_id": "o6"}
    loop = asyncio.get_event_loop()
    for _ in range(3):
        loop.run_until_complete(credits.grant_for_order(db, order))
    assert loop.run_until_complete(db.credit_grants.count_documents({})) == 1
    loop.run_until_complete(db.credit_grants.update_one(
        {"razorpay_order_id": "o6"}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}}))
    assert loop.run_until_complete(credits.consume(db, uid, "palm")) is None


def test_concurrent_requests_cannot_double_spend(env):
    client, db, h, uid, calls = env
    _grant(db, uid, "palm", "o7")

    async def race():
        return await asyncio.gather(*[credits.consume(db, uid, "palm") for _ in range(10)])
    got = asyncio.get_event_loop().run_until_complete(race())
    assert sum(1 for g in got if g) == 1


def test_chat_rate_limit(env):
    client, db, h, uid, calls = env
    client.post("/api/kundli", json=PROFILE, headers=h)
    _grant(db, uid, "bundle", "o8")
    codes = [client.post("/api/chat", json={"question": "career?"}, headers=h).status_code for _ in range(10)]
    assert codes.count(200) == server.CHAT_MAX_PER_WINDOW and codes[-1] == 429


def test_llm_never_receives_identity_fields():
    from services.ai import chart_for_llm
    sent = chart_for_llm({"name": "A", "dob": "1999-01-01", "birth_time": "10:30", "birthplace": "Delhi",
                          "location": {}, "coordinates": {}, "user_id": "u", "lagna": "Leo", "planets": [1]})
    assert set(sent) == {"lagna", "planets"}


def _paid_order(db, uid, product, order_id):
    asyncio.get_event_loop().run_until_complete(db.orders.insert_one({
        "id": "x", "user_id": uid, "product": product, "razorpay_order_id": order_id,
        "payment_status": "created", "amount": 9900}))


def test_verify_and_webhook_grant_exactly_once(env, monkeypatch):
    import hashlib, hmac, json
    client, db, h, uid, calls = env
    monkeypatch.setattr(server.payments, "key_secret", "ksecret")
    monkeypatch.setattr(server.payments, "webhook_secret", "wsecret")
    _paid_order(db, uid, "palm", "order_X")
    sig = hmac.new(b"ksecret", b"order_X|pay_X", hashlib.sha256).hexdigest()
    body = {"razorpay_order_id": "order_X", "razorpay_payment_id": "pay_X", "razorpay_signature": sig}
    # a forged signature grants nothing
    bad = client.post("/api/payments/verify", json={**body, "razorpay_signature": "nope"}, headers=h)
    assert bad.status_code == 400 and client.get("/api/entitlements", headers=h).json()["credits"]["palm"] == 0
    assert client.post("/api/payments/verify", json=body, headers=h).status_code == 200
    client.post("/api/payments/verify", json=body, headers=h)  # repeat
    raw = json.dumps({"id": "evt1", "event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_X", "order_id": "order_X"}}}}).encode()
    wsig = hmac.new(b"wsecret", raw, hashlib.sha256).hexdigest()
    client.post("/api/payments/webhook", content=raw, headers={"x-razorpay-signature": wsig})  # webhook after verify
    assert client.get("/api/entitlements", headers=h).json()["credits"]["palm"] == 1  # still one credit, not three
