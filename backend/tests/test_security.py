"""Sign-in protection, request limits and error-format tests (mock Mongo, no network).

    cd backend && python -m pytest -n 0 tests/test_security.py
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

GOOD, BAD = "correct-horse-1", "wrong-password-1"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def env(monkeypatch):
    db = AsyncMongoMockClient()["t"]
    monkeypatch.setattr(server, "db", db)
    client = TestClient(server.app)
    client.post("/api/auth/register", json={"email": "a@b.com", "password": GOOD})
    return client, db


def login(client, email="a@b.com", pw=BAD, ip="1.1.1.1"):
    return client.post("/api/auth/login", json={"email": email, "password": pw}, headers={"x-forwarded-for": ip})


def test_five_wrong_passwords_lock_that_account_even_for_the_right_password(env):
    client, db = env
    assert [login(client).status_code for _ in range(server.LOGIN_FAILS_PER_EMAIL)] == [401] * server.LOGIN_FAILS_PER_EMAIL
    locked = login(client, pw=GOOD)
    assert locked.status_code == 429 and locked.headers["retry-after"] == "900"
    # changing IP does not help an attacker: the limit is per account
    assert login(client, pw=GOOD, ip="9.9.9.9").status_code == 429
    # ...and it clears by itself once the window has passed
    run(db.auth_attempts.update_many({}, {"$set": {"at": datetime.now(timezone.utc) - timedelta(minutes=16)}}))
    assert login(client, pw=GOOD).status_code == 200


def test_unknown_emails_are_limited_too_so_accounts_cannot_be_discovered(env):
    client, db = env
    codes = [login(client, email="ghost@b.com").status_code for _ in range(server.LOGIN_FAILS_PER_EMAIL + 1)]
    assert codes == [401] * server.LOGIN_FAILS_PER_EMAIL + [429]
    first = login(client, email="other-ghost@b.com")
    real = login(client, email="a@b.com")
    assert first.json()["detail"] == real.json()["detail"] == "Email or password is incorrect"


def test_a_successful_login_resets_the_failure_count(env):
    client, db = env
    for _ in range(server.LOGIN_FAILS_PER_EMAIL - 1):
        login(client)
    assert login(client, pw=GOOD).status_code == 200
    assert [login(client).status_code for _ in range(server.LOGIN_FAILS_PER_EMAIL - 1)] == [401] * (server.LOGIN_FAILS_PER_EMAIL - 1)


def test_one_ip_guessing_many_accounts_is_stopped(env, monkeypatch):
    client, db = env
    monkeypatch.setattr(server, "LOGIN_FAILS_PER_IP", 3)
    codes = [login(client, email=f"u{i}@b.com", ip="5.5.5.5").status_code for i in range(4)]
    assert codes == [401, 401, 401, 429]
    assert login(client, email="fresh@b.com", ip="6.6.6.6").status_code == 401     # another visitor is unaffected


def test_signup_flood_from_one_ip_is_stopped(env, monkeypatch):
    client, db = env
    monkeypatch.setattr(server, "REGISTER_PER_IP_PER_HOUR", 2)
    def reg(i): return client.post("/api/auth/register", json={"email": f"n{i}@b.com", "password": GOOD}, headers={"x-forwarded-for": "7.7.7.7"})
    assert [reg(i).status_code for i in range(3)] == [200, 200, 429]


def test_google_failures_are_limited_per_ip(env, monkeypatch):
    client, db = env
    monkeypatch.setattr(server, "GOOGLE_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setattr(server, "GOOGLE_FAILS_PER_IP", 2)
    def bad(cred): raise ValueError("bad token")
    monkeypatch.setattr(server, "_verify_google_credential", bad)
    post = lambda: client.post("/api/auth/google", json={"credential": "x" * 40}, headers={"x-forwarded-for": "8.8.8.8"})
    assert [post().status_code for _ in range(3)] == [401, 401, 429]


def test_new_passwords_need_8_characters_but_old_short_ones_can_still_log_in(env):
    client, db = env
    short = client.post("/api/auth/register", json={"email": "s@b.com", "password": "abc1234"})
    assert short.status_code == 422 and isinstance(short.json()["detail"], str) and "8 characters" in short.json()["detail"]
    run(db.users.insert_one({"id": "old", "email": "old@b.com", "password_hash": server.pwd.hash("abc123"), "created_at": "2025-01-01"}))
    assert login(client, email="old@b.com", pw="abc123").status_code == 200


def test_validation_errors_are_always_plain_text(env):
    client, db = env
    tok = client.post("/api/auth/login", json={"email": "a@b.com", "password": GOOD}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    for path, body in (("/api/kundli", {"name": ""}), ("/api/chat", {"question": "x"}), ("/api/auth/register", {"email": "not-an-email", "password": GOOD})):
        r = client.post(path, json=body, headers=h)
        assert r.status_code == 422 and isinstance(r.json()["detail"], str), (path, r.json())


def test_oversized_requests_are_refused_before_processing(env):
    client, db = env
    r = client.post("/api/auth/login", content=b"x" * (server.MAX_BODY_BYTES + 1000), headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["detail"] == "Request is too large"


def test_palm_upload_from_an_unpaid_user_is_refused_before_the_image_is_examined(env):
    client, db = env
    tok = client.post("/api/auth/login", json={"email": "a@b.com", "password": GOOD}).json()
    h = {"Authorization": f"Bearer {tok['token']}"}
    junk = {"image_base64": "this is not an image", "hand": "left"}
    assert client.post("/api/palm", json=junk, headers=h).status_code == 402          # no credit: stops immediately
    run(credits.grant_for_order(db, {"user_id": tok["user"]["id"], "product": "palm", "razorpay_order_id": "o1"}))
    assert client.post("/api/palm", json=junk, headers=h).status_code == 400          # with credit: normal image checks apply


def test_payment_order_creation_is_capped(env):
    client, db = env
    tok = client.post("/api/auth/login", json={"email": "a@b.com", "password": GOOD}).json()
    h = {"Authorization": f"Bearer {tok['token']}"}
    now = datetime.now(timezone.utc).isoformat()
    for i in range(20):
        run(db.orders.insert_one({"id": str(i), "user_id": tok["user"]["id"], "created_at": now}))
    r = client.post("/api/payments/order", json={"product": "kundli"}, headers=h)
    assert r.status_code == 429


def test_signin_attempt_log_expires_by_itself_and_startup_survives_index_errors(monkeypatch):
    db = AsyncMongoMockClient()["t"]
    monkeypatch.setattr(server, "db", db)
    with TestClient(server.app) as client:
        assert client.get("/api/").status_code == 200
    info = run(db.auth_attempts.index_information())
    assert any(v.get("expireAfterSeconds") == 86400 for v in info.values())
