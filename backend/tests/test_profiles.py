"""Profile + locked-chat tests (mock Mongo, no network, no real AI).

    cd backend && python -m pytest -n 0 tests/test_profiles.py
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
from services import credits  # noqa: E402

BASE = dict(dob="1999-01-01", birth_time="10:30", birthplace="Delhi, India",
            latitude=28.6139, longitude=77.209, timezone_name="Asia/Kolkata")
ME = dict(name="Me Self", **BASE)
MOM = dict(name="Mom", dob="1970-05-05", birth_time="06:15", birthplace="Jaipur, India",
           latitude=26.9124, longitude=75.7873, timezone_name="Asia/Kolkata")
DAD = dict(name="Dad", dob="1968-03-03", birth_time="07:45", birthplace="Jaipur, India",
           latitude=26.9124, longitude=75.7873, timezone_name="Asia/Kolkata")


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def env(monkeypatch):
    db = AsyncMongoMockClient()["t"]
    monkeypatch.setattr(server, "db", db)
    seen = {"charts": [], "interpret": 0}

    async def fake_interpret(self, k):
        seen["interpret"] += 1
        return "FULL INTERPRETATION " * 40

    async def fake_chat(self, q, k):
        seen["charts"].append(k)
        return "answer"

    monkeypatch.setattr(server.AstrologyInterpretationService, "interpret", fake_interpret)
    monkeypatch.setattr(server.KundliChatService, "answer", fake_chat)
    client = TestClient(server.app)

    def user(email):
        r = client.post("/api/auth/register", json={"email": email, "password": "secret1"})
        return {"Authorization": f"Bearer {r.json()['token']}"}, r.json()["user"]["id"]

    return client, db, seen, user


def grant(db, uid, product="bundle", order="o1"):
    run(credits.grant_for_order(db, {"user_id": uid, "product": product, "razorpay_order_id": order}))


def test_family_member_needs_consent_and_self_is_unique(env):
    client, db, seen, user = env
    h, uid = user("a@b.com")
    assert client.post("/api/kundli", json={**ME, "relation": "self"}, headers=h).status_code == 200
    r = client.post("/api/kundli", json={**MOM, "relation": "parent"}, headers=h)
    assert r.status_code == 400 and "permission" in r.json()["detail"]
    assert client.post("/api/kundli", json={**MOM, "relation": "parent", "consent_confirmed": True}, headers=h).status_code == 200
    # a second, different "self" is refused
    r = client.post("/api/kundli", json={**DAD, "relation": "self"}, headers=h)
    assert r.status_code == 409
    profiles = client.get("/api/profiles", headers=h).json()["profiles"]
    assert sorted(p["relation"] for p in profiles) == ["parent", "self"]


def test_profile_cap(env, monkeypatch):
    client, db, seen, user = env
    monkeypatch.setattr(server, "MAX_PROFILES", 2)
    h, uid = user("a@b.com")
    client.post("/api/kundli", json=ME, headers=h)
    client.post("/api/kundli", json={**MOM, "relation": "parent", "consent_confirmed": True}, headers=h)
    r = client.post("/api/kundli", json={**DAD, "relation": "parent", "consent_confirmed": True}, headers=h)
    assert r.status_code == 400 and "up to 2" in r.json()["detail"]
    # a failed chart calculation must not use up a slot
    monkeypatch.setattr(server, "MAX_PROFILES", 3)
    bad = client.post("/api/kundli", json={**DAD, "dob": "not-a-date", "relation": "parent", "consent_confirmed": True}, headers=h)
    assert bad.status_code == 422
    assert len(client.get("/api/profiles", headers=h).json()["profiles"]) == 2


def test_chat_only_sees_the_chosen_persons_chart(env):
    client, db, seen, user = env
    h, uid = user("a@b.com")
    grant(db, uid)
    client.post("/api/kundli", json=ME, headers=h)
    client.post("/api/kundli", json={**MOM, "relation": "parent", "consent_confirmed": True}, headers=h)
    profiles = {p["name"]: p["id"] for p in client.get("/api/profiles", headers=h).json()["profiles"]}
    r = client.post("/api/chat", json={"question": "career?", "profile_id": profiles["Mom"]}, headers=h)
    assert r.status_code == 200
    sent = seen["charts"][-1]
    assert sent["profile_id"] == profiles["Mom"] and sent["birthplace"] == "Jaipur, India"
    # nothing of the other person is in what the AI was given
    assert "Me Self" not in str(sent) and "Delhi" not in str(sent)
    # with 2 profiles a profile must be chosen
    assert client.post("/api/chat", json={"question": "career?"}, headers=h).status_code == 400


def test_cannot_chat_about_another_users_profile(env):
    client, db, seen, user = env
    ha, ua = user("a@b.com")
    hb, ub = user("b@b.com")
    client.post("/api/kundli", json=ME, headers=ha)
    a_profile = client.get("/api/profiles", headers=ha).json()["profiles"][0]["id"]
    grant(db, ub, "bundle", "ob")
    assert client.post("/api/chat", json={"question": "hi there", "profile_id": a_profile}, headers=hb).status_code == 404
    assert client.post("/api/chat", json={"question": "hi there", "profile_id": "made-up"}, headers=hb).status_code == 404
    assert client.delete(f"/api/profiles/{a_profile}", headers=hb).status_code == 404
    assert seen["charts"] == []


def test_single_profile_needs_no_choice(env):
    client, db, seen, user = env
    h, uid = user("a@b.com")
    grant(db, uid)
    client.post("/api/kundli", json=ME, headers=h)
    assert client.post("/api/chat", json={"question": "career?"}, headers=h).status_code == 200


def test_same_person_again_never_duplicates_or_double_charges(env):
    client, db, seen, user = env
    h, uid = user("a@b.com")
    first = client.post("/api/kundli", json=ME, headers=h).json()
    assert first["locked"] is True
    grant(db, uid, "kundli", "o2")            # user buys, then re-submits the same details
    second = client.post("/api/kundli", json=ME, headers=h).json()
    assert second["id"] == first["id"] and second["locked"] is False and seen["interpret"] == 1
    third = client.post("/api/kundli", json=ME, headers=h).json()   # and again: free, no new spend
    assert third["id"] == first["id"] and seen["interpret"] == 1
    assert run(db.kundlis.count_documents({})) == 1 and run(db.profiles.count_documents({})) == 1
    assert client.get("/api/entitlements", headers=h).json()["credits"]["kundli"] == 0


def test_delete_profile_removes_everything_about_that_person(env):
    client, db, seen, user = env
    h, uid = user("a@b.com")
    grant(db, uid)
    client.post("/api/kundli", json=ME, headers=h)
    client.post("/api/kundli", json={**MOM, "relation": "parent", "consent_confirmed": True}, headers=h)
    mom = next(p["id"] for p in client.get("/api/profiles", headers=h).json()["profiles"] if p["name"] == "Mom")
    client.post("/api/chat", json={"question": "career?", "profile_id": mom}, headers=h)
    r = client.delete(f"/api/profiles/{mom}", headers=h).json()
    assert r == {"deleted": True, "kundlis": 1, "conversations": 1}
    assert run(db.kundlis.count_documents({"profile_id": mom})) == 0
    assert run(db.ai_conversations.count_documents({"profile_id": mom})) == 0
    assert run(db.profiles.count_documents({"id": mom})) == 0
    # the other person is untouched
    assert run(db.kundlis.count_documents({})) == 1


def test_legacy_kundlis_get_profiles_automatically(env):
    client, db, seen, user = env
    h, uid = user("a@b.com")
    grant(db, uid)
    for i, (name, when) in enumerate([("Old One", "2026-01-01"), ("Old Two", "2026-02-01")]):
        run(db.kundlis.insert_one({"id": f"k{i}", "user_id": uid, "name": name, "dob": "1990-01-01", "birth_time": "10:00",
                                   "birthplace": "Delhi", "coordinates": {"latitude": 1.0, "longitude": 2.0, "timezone": "Asia/Kolkata"},
                                   "lagna": "Leo", "planets": [], "created_at": when + "T00:00:00+00:00"}))
    profiles = client.get("/api/profiles", headers=h).json()["profiles"]
    assert [(p["name"], p["relation"], p["has_kundli"]) for p in profiles] == [("Old One", "self", True), ("Old Two", "other", True)]
    two = profiles[1]["id"]
    assert client.post("/api/chat", json={"question": "career?", "profile_id": two}, headers=h).status_code == 200
    assert seen["charts"][-1]["name"] == "Old Two"
    # running it again does not create duplicates
    assert len(client.get("/api/profiles", headers=h).json()["profiles"]) == 2


def test_chat_for_profile_without_a_kundli(env):
    client, db, seen, user = env
    h, uid = user("a@b.com")
    grant(db, uid)
    run(db.profiles.insert_one({"id": "p1", "user_id": uid, "name": "Empty", "relation": "self"}))
    r = client.post("/api/chat", json={"question": "career?", "profile_id": "p1"}, headers=h)
    assert r.status_code == 400 and "Empty" in r.json()["detail"]
