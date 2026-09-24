"""Profile editing tests (mock Mongo, no network, no real AI).

    cd backend && python -m pytest -n 0 tests/test_editing.py
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
from services.ai import AIServiceUnavailable  # noqa: E402

ME = dict(name="Me Self", dob="1999-01-01", birth_time="10:30", birthplace="Delhi, India",
          latitude=28.6139, longitude=77.209, timezone_name="Asia/Kolkata")
MOM = dict(name="Mom", dob="1970-05-05", birth_time="06:15", birthplace="Jaipur, India",
           latitude=26.9124, longitude=75.7873, timezone_name="Asia/Kolkata")


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def env(monkeypatch):
    db = AsyncMongoMockClient()["t"]
    monkeypatch.setattr(server, "db", db)
    seen = {"interpret": 0, "fail": False}

    async def fake_interpret(self, k):
        if seen["fail"]:
            raise AIServiceUnavailable("down")
        seen["interpret"] += 1
        return f"FULL INTERPRETATION for {k['dob']} " * 30

    monkeypatch.setattr(server.AstrologyInterpretationService, "interpret", fake_interpret)
    client = TestClient(server.app)
    r = client.post("/api/auth/register", json={"email": "a@b.com", "password": "secret12"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    return client, db, seen, h, r.json()["user"]["id"]


def grant(db, uid, order="o1", product="kundli"):
    run(credits.grant_for_order(db, {"user_id": uid, "product": product, "razorpay_order_id": order}))


def make(client, h, person=ME, **extra):
    body = {**person, **extra}
    r = client.post("/api/kundli", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def pid(client, h, name):
    return next(p["id"] for p in client.get("/api/profiles", headers=h).json()["profiles"] if p["name"] == name)


def credits_left(client, h):
    return client.get("/api/entitlements", headers=h).json()["credits"]["kundli"]


def test_name_change_is_always_free_and_leaves_the_chart_alone(env):
    client, db, seen, h, uid = env
    grant(db, uid)
    chart = make(client, h)
    r = client.put(f"/api/profiles/{pid(client, h, 'Me Self')}", json={**ME, "name": "New Name"}, headers=h)
    assert r.status_code == 200 and r.json()["charged"] is False and r.json()["kundli"]["name"] == "New Name"
    assert seen["interpret"] == 1 and r.json()["kundli"]["lagna"] == chart["lagna"]


def test_correcting_a_free_preview_recalculates_without_ai_or_cost(env):
    client, db, seen, h, uid = env
    make(client, h)
    r = client.put(f"/api/profiles/{pid(client, h, 'Me Self')}", json={**ME, "dob": "1995-07-20"}, headers=h)
    assert r.status_code == 200 and r.json()["kundli"]["dob"] == "1995-07-20" and r.json()["kundli"]["locked"] is True
    assert seen["interpret"] == 0 and run(db.kundlis.count_documents({})) == 1


def test_two_free_corrections_then_the_third_needs_a_credit(env):
    client, db, seen, h, uid = env
    grant(db, uid)                                   # 1 kundli credit, spent on the first unlock
    make(client, h)
    p = pid(client, h, "Me Self")
    assert client.put(f"/api/profiles/{p}", json={**ME, "birth_time": "10:31"}, headers=h).json()["charged"] is False
    assert client.put(f"/api/profiles/{p}", json={**ME, "birth_time": "10:32"}, headers=h).json()["charged"] is False
    assert seen["interpret"] == 3 and credits_left(client, h) == 0
    third = {**ME, "birth_time": "10:33"}
    # not confirmed: refuses, and nothing changes
    r = client.put(f"/api/profiles/{p}", json=third, headers=h)
    assert r.status_code == 409 and "1 Kundli credit" in r.json()["detail"]
    # confirmed but no credit: 402, and the chart is untouched
    r = client.put(f"/api/profiles/{p}", json={**third, "confirm_spend": True}, headers=h)
    assert r.status_code == 402
    assert client.get("/api/kundli/latest", headers=h).json()["birth_time"] == "10:32"
    # confirmed with a credit: works, spends exactly one, and opens a fresh free window
    grant(db, uid, "o2")
    r = client.put(f"/api/profiles/{p}", json={**third, "confirm_spend": True}, headers=h)
    assert r.status_code == 200 and r.json()["charged"] is True and credits_left(client, h) == 0
    assert client.put(f"/api/profiles/{p}", json={**ME, "birth_time": "10:34"}, headers=h).json()["charged"] is False


def test_after_the_free_window_a_correction_costs_a_credit(env):
    client, db, seen, h, uid = env
    grant(db, uid)
    make(client, h)
    old = (datetime.now(timezone.utc) - timedelta(hours=server.EDIT_WINDOW_HOURS + 1)).isoformat()
    run(db.kundlis.update_many({}, {"$set": {"unlocked_at": old}}))
    p = pid(client, h, "Me Self")
    assert client.get(f"/api/profiles/{p}", headers=h).json()["edit_free"] is False
    assert client.put(f"/api/profiles/{p}", json={**ME, "dob": "1999-01-02"}, headers=h).status_code == 409
    grant(db, uid, "o2")
    r = client.put(f"/api/profiles/{p}", json={**ME, "dob": "1999-01-02", "confirm_spend": True}, headers=h)
    assert r.status_code == 200 and r.json()["charged"] is True and credits_left(client, h) == 0


def test_failed_ai_or_bad_data_changes_nothing_and_charges_nothing(env):
    client, db, seen, h, uid = env
    grant(db, uid)
    make(client, h)
    p = pid(client, h, "Me Self")
    before = client.get("/api/kundli/latest", headers=h).json()
    seen["fail"] = True
    assert client.put(f"/api/profiles/{p}", json={**ME, "dob": "1990-02-02"}, headers=h).status_code == 503
    seen["fail"] = False
    assert client.put(f"/api/profiles/{p}", json={**ME, "dob": "not-a-date"}, headers=h).status_code == 422
    after = client.get("/api/kundli/latest", headers=h).json()
    assert after["dob"] == before["dob"] == "1999-01-01" and after["interpretation"] == before["interpretation"]
    detail = client.get(f"/api/profiles/{p}", headers=h).json()
    assert detail["dob"] == "1999-01-01" and detail["edit_free"] is True   # failed attempts used no free correction


def test_paid_correction_that_fails_refunds_the_credit(env):
    client, db, seen, h, uid = env
    grant(db, uid)
    make(client, h)
    p = pid(client, h, "Me Self")
    run(db.kundlis.update_many({}, {"$set": {"free_edits_used": server.MAX_FREE_EDITS}}))
    grant(db, uid, "o2")
    seen["fail"] = True
    r = client.put(f"/api/profiles/{p}", json={**ME, "dob": "1990-02-02", "confirm_spend": True}, headers=h)
    assert r.status_code == 503 and credits_left(client, h) == 1


def test_cannot_edit_or_read_someone_elses_profile(env):
    client, db, seen, h, uid = env
    make(client, h)
    p = pid(client, h, "Me Self")
    r2 = client.post("/api/auth/register", json={"email": "c@d.com", "password": "secret12"})
    h2 = {"Authorization": f"Bearer {r2.json()['token']}"}
    assert client.get(f"/api/profiles/{p}", headers=h2).status_code == 404
    assert client.put(f"/api/profiles/{p}", json={**ME, "name": "Hacked"}, headers=h2).status_code == 404
    assert client.get(f"/api/profiles/{p}", headers=h).json()["name"] == "Me Self"


def test_relation_and_duplicate_rules(env):
    client, db, seen, h, uid = env
    make(client, h)
    make(client, h, MOM, relation="parent", consent_confirmed=True)
    me, mom = pid(client, h, "Me Self"), pid(client, h, "Mom")
    # cannot turn Mom into a second "self"
    assert client.put(f"/api/profiles/{mom}", json={**MOM, "relation": "self"}, headers=h).status_code == 409
    # cannot make Mom identical to an existing person
    assert client.put(f"/api/profiles/{mom}", json={**ME}, headers=h).status_code == 409
    # turning "me" into a non-self relation needs the consent box
    assert client.put(f"/api/profiles/{me}", json={**ME, "relation": "friend"}, headers=h).status_code == 400
    assert client.put(f"/api/profiles/{me}", json={**ME, "relation": "friend", "consent_confirmed": True}, headers=h).status_code == 200


def test_legacy_chart_without_unlocked_at_uses_created_at(env):
    client, db, seen, h, uid = env
    grant(db, uid)
    chart = make(client, h)
    run(db.kundlis.update_many({}, {"$unset": {"unlocked_at": ""}}))
    p = pid(client, h, "Me Self")
    assert client.get(f"/api/profiles/{p}", headers=h).json()["edit_free"] is True     # created just now
    run(db.kundlis.update_many({}, {"$set": {"created_at": "2020-01-01T00:00:00+00:00"}}))
    assert client.get(f"/api/profiles/{p}", headers=h).json()["edit_free"] is False
