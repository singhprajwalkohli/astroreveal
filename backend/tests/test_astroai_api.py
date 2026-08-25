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
    assert chart["engine_status"] == "SWISS EPHEMERIS · LAHIRI SIDEREAL"
    assert chart["coordinates"]["latitude"]
    assert chart["coordinates"]["longitude"]
    assert chart["coordinates"]["timezone"]
    assert chart["houses"] and len(chart["houses"]) == 12
    assert chart["dashas"] and chart["planets"][0]["longitude"] is not None
    assert chart["planets"] and len(chart["planets"]) >= 9
    assert isinstance(chart["nakshatra"], dict)
    assert {"name", "pada", "lord"} <= chart["nakshatra"].keys()
    assert all(isinstance(planet["nakshatra"], dict) and planet["house"] >= 1 for planet in chart["planets"])
    assert chart["interpretation"]
    latest = auth_client.get(f"{BASE_URL}/api/kundli/latest", timeout=30)
    assert latest.status_code == 200
    latest_chart = latest.json()
    assert latest_chart["name"] == payload["name"]
    assert "_id" not in latest_chart
    assert latest_chart.get("interpretation"), "latest chart must persist the AI interpretation"


def test_chat_uses_chart_context(auth_client):
    response = auth_client.post(f"{BASE_URL}/api/chat", json={"question": "What does my career suggest?"}, timeout=120)
    assert response.status_code == 200
    assert response.json().get("answer")


def test_palm_rejects_invalid_image(auth_client):
    response = auth_client.post(f"{BASE_URL}/api/palm", json={"image_base64": "not-an-image", "hand": "right"}, timeout=30)
    assert response.status_code == 400
    assert "image" in response.json()["detail"].lower()


def test_palm_rejects_mismatched_image_bytes(auth_client):
    image = base64.b64encode(b"not-a-jpeg").decode()
    response = auth_client.post(f"{BASE_URL}/api/palm", json={"image_base64": f"data:image/jpeg;base64,{image}", "hand": "left"}, timeout=30)
    assert response.status_code == 400
    assert "contents" in response.json()["detail"]


def test_palm_rejects_unsupported_and_invalid_payloads(auth_client):
    cases = [
        ("data:image/svg+xml;base64,PHN2Zy8+", 400),
        ("data:image/png;base64,%%%", 400),
        ("data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff" + b"x" * (10 * 1024 * 1024)).decode(), 413),
    ]
    for image_base64, expected_status in cases:
        response = auth_client.post(f"{BASE_URL}/api/palm", json={"image_base64": image_base64, "hand": "right"}, timeout=30)
        assert response.status_code == expected_status
        assert response.json().get("detail")


def test_geocode_search_too_short(client):
    response = client.get(f"{BASE_URL}/api/geocode/search", params={"q": "Mu"}, timeout=30)
    assert response.status_code == 200
    assert response.json() == {"results": []}


def test_geocode_search_returns_matches(client):
    response = client.get(f"{BASE_URL}/api/geocode/search", params={"q": "Mumbai"}, timeout=30)
    assert response.status_code == 200
    results = response.json().get("results", [])
    assert len(results) >= 1
    first = results[0]
    for field in ("display_name", "latitude", "longitude", "timezone"):
        assert field in first
    assert isinstance(first["latitude"], float)
    assert isinstance(first["longitude"], float)


def test_kundli_uses_user_provided_coordinates(auth_client):
    payload = {
        "name": "TEST_Autocomplete User",
        "dob": "1990-05-15",
        "birth_time": "14:30",
        "birthplace": "Mumbai, Maharashtra, India",
        "latitude": 19.0759837,
        "longitude": 72.8776559,
        "timezone_name": "Asia/Kolkata",
    }
    response = auth_client.post(f"{BASE_URL}/api/kundli", json=payload, timeout=120)
    assert response.status_code == 200, response.text
    chart = response.json()
    assert chart["coordinates"]["timezone"] == "Asia/Kolkata"
    assert chart["lagna"]
    assert chart["rashi"]
    assert isinstance(chart["nakshatra"], dict) and chart["nakshatra"].get("name")
    assert chart["interpretation"] and len(chart["interpretation"]) > 200


def test_dashboard_shape(auth_client):
    response = auth_client.get(f"{BASE_URL}/api/dashboard", timeout=30)
    assert response.status_code == 200
    payload = response.json()
    assert payload["user"]["email"] == EMAIL
    assert isinstance(payload["palm_readings"], int)