"""Auth module tests: registration, login, token lifecycle, and the
security properties (no enumeration, token-type confusion rejected)."""
from app.core.security import create_refresh_token


def _register(client, u):
    return client.post("/auth/register", json={
        "email": u["email"], "username": u["username"], "password": u["password"],
    })


def test_register_login_me_roundtrip(client, unique):
    r = _register(client, unique)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == unique["email"]
    assert "hashed_password" not in body  # never leak the hash

    r = client.post("/auth/login", json={"email": unique["email"],
                                         "password": unique["password"]})
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert tokens["token_type"] == "bearer"

    r = client.get("/auth/me",
                   headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert r.status_code == 200
    assert r.json()["username"] == unique["username"]


def test_duplicate_email_conflict(client, unique):
    assert _register(client, unique).status_code == 201
    unique2 = {**unique, "username": unique["username"] + "x"}
    r = _register(client, unique2)
    assert r.status_code == 409


def test_wrong_password_and_unknown_email_same_error(client, unique):
    _register(client, unique)
    r1 = client.post("/auth/login", json={"email": unique["email"],
                                          "password": "wrong-password"})
    r2 = client.post("/auth/login", json={"email": "nobody@example.com",
                                          "password": "wrong-password"})
    # identical status + detail => no account enumeration
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["detail"] == r2.json()["detail"]


def test_refresh_rotates_tokens(client, unique):
    _register(client, unique)
    tokens = client.post("/auth/login", json={
        "email": unique["email"], "password": unique["password"]}).json()

    r = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    new_tokens = r.json()
    assert new_tokens["access_token"]
    assert new_tokens["refresh_token"]

    r = client.get("/auth/me",
                   headers={"Authorization": f"Bearer {new_tokens['access_token']}"})
    assert r.status_code == 200


def test_refresh_token_rejected_as_access_token(client, unique):
    r = _register(client, unique)
    user_id = r.json()["id"]
    refresh = create_refresh_token(user_id)
    # a refresh token must NOT work on authenticated endpoints
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert r.status_code == 401


def test_garbage_and_missing_token_rejected(client):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me",
                      headers={"Authorization": "Bearer not.a.jwt"}).status_code == 401


def test_short_password_rejected(client, unique):
    r = client.post("/auth/register", json={
        "email": unique["email"], "username": unique["username"], "password": "short"})
    assert r.status_code == 422
