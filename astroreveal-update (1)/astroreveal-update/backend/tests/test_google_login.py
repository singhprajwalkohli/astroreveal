"""Google sign-in tests. Google itself is mocked (no network).

    cd backend && python -m pytest -n 0 tests/test_google_login.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

os.environ.update(MONGO_URL="mongodb://mock", DB_NAME="t", JWT_SECRET="test-secret", GEMINI_API_KEY="x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mongomock_motor import AsyncMongoMockClient  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

CRED = {"credential": "x" * 40}


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def env(monkeypatch):
    db = AsyncMongoMockClient()["t"]
    monkeypatch.setattr(server, "db", db)
    monkeypatch.setattr(server, "GOOGLE_CLIENT_ID", "client-id.apps.googleusercontent.com")
    claims = {"sub": "g-123", "email": "Person@Example.com", "email_verified": True}
    monkeypatch.setattr(server, "_verify_google_credential", lambda cred: dict(claims))
    return TestClient(server.app), db, claims


def test_not_configured_returns_503(env, monkeypatch):
    client, db, claims = env
    monkeypatch.setattr(server, "GOOGLE_CLIENT_ID", "")
    assert client.post("/api/auth/google", json=CRED).status_code == 503


def test_new_google_user_is_created_and_token_works(env):
    client, db, claims = env
    r = client.post("/api/auth/google", json=CRED)
    assert r.status_code == 200 and r.json()["user"]["email"] == "person@example.com"
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['token']}"})
    assert me.status_code == 200 and "google_sub" not in me.json() and "password_hash" not in me.json()


def test_same_google_account_is_the_same_user(env):
    client, db, claims = env
    a = client.post("/api/auth/google", json=CRED).json()["user"]["id"]
    b = client.post("/api/auth/google", json=CRED).json()["user"]["id"]
    assert a == b and run(db.users.count_documents({})) == 1


def test_unverified_google_email_is_refused(env):
    client, db, claims = env
    claims["email_verified"] = False
    assert client.post("/api/auth/google", json=CRED).status_code == 401
    assert run(db.users.count_documents({})) == 0


def test_invalid_or_wrong_audience_token_is_refused(env, monkeypatch):
    client, db, claims = env
    def bad(cred): raise ValueError("Token has wrong audience")
    monkeypatch.setattr(server, "_verify_google_credential", bad)
    assert client.post("/api/auth/google", json=CRED).status_code == 401
    assert run(db.users.count_documents({})) == 0


def test_linking_keeps_data_and_kills_the_squatters_password(env):
    client, db, claims = env
    # someone registers with an email they don't own, using their own password
    reg = client.post("/api/auth/register", json={"email": "person@example.com", "password": "squatter1"})
    old_id = reg.json()["user"]["id"]
    # the real owner arrives through Google
    g = client.post("/api/auth/google", json=CRED)
    assert g.json()["user"]["id"] == old_id                     # linked, not duplicated
    login = client.post("/api/auth/login", json={"email": "person@example.com", "password": "squatter1"})
    assert login.status_code == 401 and "Google" in login.json()["detail"]   # old password no longer works
    assert run(db.users.count_documents({})) == 1


def test_google_only_account_cannot_password_login_and_email_is_taken(env):
    client, db, claims = env
    client.post("/api/auth/google", json=CRED)
    r = client.post("/api/auth/login", json={"email": "person@example.com", "password": "anything1"})
    assert r.status_code == 401
    assert client.post("/api/auth/register", json={"email": "person@example.com", "password": "anything1"}).status_code == 409


def test_email_linked_to_another_google_account_is_refused(env):
    client, db, claims = env
    client.post("/api/auth/google", json=CRED)
    claims["sub"] = "someone-else"
    assert client.post("/api/auth/google", json=CRED).status_code == 409
