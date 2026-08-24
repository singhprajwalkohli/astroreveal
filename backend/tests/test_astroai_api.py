import base64
import os

import pytest
import requests


BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
EMAIL = "astroai.test@example.com"
PASSWORD = "AstroAI!2025"


@pytest.fixture(scope="module")
def client():
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


@pytest.fixture(scope="module")
def auth_client(client):
    response = client.post(f"{BASE_URL}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    if response.status_code != 200:
        pytest.skip(f"Provided test account unavailable: {response.status_code} {response.text[:200]}")
    token = response.json().get("token")
    assert token
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def test_api_root(client):
    response = client.get(f"{BASE_URL}/api/", timeout=30)
    assert response.status_code == 200
    assert response.json().get("message") == "AstroAI API ready"


def test_login_and_me(client):
    login = client.post(f"{BASE_URL}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    assert login.status_code == 200
    payload = login.json()
    assert payload["user"]["email"] == EMAIL
    assert isinstance(payload["token"], str) and payload["token"]
    client.headers.update({"Authorization": f"Bearer {payload['token']}"})
    me = client.get(f"{BASE_URL}/api/auth/me", timeout=30)
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL


def test_register_route_rejects_existing_account(client):
    response = client.post(f"{BASE_URL}/api/auth/register", json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_auth_protected_routes_reject_anonymous(client):
    headers = client.headers.pop("Authorization", None)
    try:
        for route in ("/api/dashboard", "/api/kundli/latest"):
            response = client.get(f"{BASE_URL}{route}", timeout=30)
            assert response.status_code == 401
            assert "detail" in response.json()
    finally:
        if headers:
            client.headers["Authorization"] = headers


def test_kundli_create_persists_and_returns_chart(auth_client):
    payload = {"name": "TEST_Astro User", "dob": "1990-05-17", "birth_time": "14:30", "birthplace": "Pune"}
    response = auth_client.post(f"{BASE_URL}/api/kundli", json=payload, timeout=120)
    assert response.status_code == 200
    chart = response.json()
    assert chart["name"] == payload["name"]
    assert chart["engine_status"] == "MOCK DEVELOPMENT CALCULATIONS"
    assert chart["planets"] and len(chart["planets"]) >= 9
    assert chart["interpretation"]
    latest = auth_client.get(f"{BASE_URL}/api/kundli/latest", timeout=30)
    assert latest.status_code == 200
    assert latest.json()["name"] == payload["name"]


def test_chat_uses_chart_context(auth_client):
    response = auth_client.post(f"{BASE_URL}/api/chat", json={"question": "What does my career suggest?"}, timeout=120)
    assert response.status_code == 200
    assert response.json().get("answer")


def test_palm_rejects_invalid_image(auth_client):
    response = auth_client.post(f"{BASE_URL}/api/palm", json={"image_base64": "not-an-image", "hand": "right"}, timeout=30)
    assert response.status_code == 400
    assert "image" in response.json()["detail"].lower()


def test_palm_accepts_featured_jpeg(auth_client):
    # Small non-uniform JPEG-like payload used only to exercise MIME validation and service flow.
    image = base64.b64encode(bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300" + "10" * 67 + "ffc00011080001000103011100021101031101ffda000c03010002110311003f00" + "00" * 8 + "ffd9")).decode()
    response = auth_client.post(f"{BASE_URL}/api/palm", json={"image_base64": f"data:image/jpeg;base64,{image}", "hand": "left"}, timeout=120)
    assert response.status_code == 200
    assert response.json()["hand"] == "left"
    assert response.json().get("reading")


def test_dashboard_shape(auth_client):
    response = auth_client.get(f"{BASE_URL}/api/dashboard", timeout=30)
    assert response.status_code == 200
    payload = response.json()
    assert payload["user"]["email"] == EMAIL
    assert isinstance(payload["palm_readings"], int)