import json
import uuid

from fastapi.testclient import TestClient

import app.main as main
from test_access import login, server  # noqa: F401  (fixture)


def sso_user(server) -> tuple[dict, TestClient]:  # noqa: F811
    """An account from the identity provider, signed in the way a finished SSO flow leaves it."""
    user = server["database"].create_user(
        {
            "id": uuid.uuid4().hex,
            "username": "jane",
            "display_name": "Jane Doe",
            "password_hash": "!oidc",
            "role": "member",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "email": "jane@corp.example",
            "auth_provider": "oidc",
            "external_subject": "u-1",
        }
    )
    raw, csrf = server["auth"].create_session(user["id"], "pytest", "127.0.0.1")
    client = TestClient(main.app)
    client.cookies.set(server["auth"].cookie_name, raw)
    client.headers["X-CSRF-Token"] = csrf
    return user, client


def test_admin_sees_an_account_with_token_prefixes_only(server):  # noqa: F811
    member, _ = login("member")
    raw = member.post("/api/account/tokens", json={"name": "ci", "scope": "write"}).json()["token"]
    member.post("/api/account/tokens", json={"name": "laptop", "scope": "read"})
    member_id = server["users"]["member"]["id"]

    admin, _ = login("admin")
    response = admin.get(f"/api/admin/users/{member_id}")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["user"]["username"] == "member"
    assert detail["external"] is False and detail["local_password"] is True
    assert detail["repositories"] == ["member/secret"]
    assert len(detail["sessions"]) == 1 and detail["sessions"][0]["current"] is False
    tokens = {token["name"]: token for token in detail["tokens"]}
    assert set(tokens) == {"ci", "laptop"}
    assert raw.startswith(tokens["ci"]["prefix"]) and len(tokens["ci"]["prefix"]) < len(raw)

    # Nothing secret leaves the server: no hashes, no password, no full token.
    text = json.dumps(detail)
    for secret in ("token_hash", "password_hash", "csrf_token", raw, raw[: len(tokens["ci"]["prefix"]) + 4]):
        assert secret not in text

    assert admin.get(f"/api/admin/users/{uuid.uuid4().hex}").status_code == 404
    assert member.get(f"/api/admin/users/{member_id}").status_code == 403


def test_admin_revokes_one_token_of_that_account_only(server):  # noqa: F811
    member, _ = login("member")
    kept = member.post("/api/account/tokens", json={"name": "kept", "scope": "read"}).json()
    gone = member.post("/api/account/tokens", json={"name": "gone", "scope": "read"}).json()
    viewer, _ = login("viewer")
    other = viewer.post("/api/account/tokens", json={"name": "other", "scope": "read"}).json()
    member_id = server["users"]["member"]["id"]

    admin, _ = login("admin")
    base = f"/api/admin/users/{member_id}/tokens"
    # A token id that belongs to someone else is not found under this account.
    assert admin.delete(f"{base}/{other['id']}").status_code == 404
    assert admin.delete(f"{base}/{gone['id']}").status_code == 200
    assert admin.delete(f"{base}/{gone['id']}").status_code == 404

    names = [token["name"] for token in admin.get(f"/api/admin/users/{member_id}").json()["tokens"]]
    assert names == ["kept"]
    revoked = TestClient(main.app)
    revoked.headers["Authorization"] = f"Bearer {gone['token']}"
    assert revoked.get("/api/library/models").status_code == 401
    still = TestClient(main.app)
    still.headers["Authorization"] = f"Bearer {kept['token']}"
    assert still.get("/api/library/models").status_code == 200
    assert viewer.get("/api/account/tokens").json()["items"][0]["name"] == "other"
    assert member.delete(f"{base}/{kept['id']}").status_code == 403


def test_single_sign_on_accounts_are_managed_by_the_identity_provider(server):  # noqa: F811
    user, client = sso_user(server)
    account = client.get("/api/account").json()
    assert account["local_password"] is False

    # Their own name and email are read-only, and there is no password to change.
    assert client.patch("/api/account/profile", json={"display_name": "Someone", "email": None}).status_code == 409
    assert client.get("/api/auth/status").json()["user"]["display_name"] == "Jane Doe"

    admin, _ = login("admin")
    detail = admin.get(f"/api/admin/users/{user['id']}").json()
    assert detail["external"] is True and detail["local_password"] is False
    base = f"/api/admin/users/{user['id']}"
    assert admin.patch(base, json={"display_name": "Renamed"}).status_code == 409
    assert admin.patch(base, json={"email": "x@example.com"}).status_code == 409
    assert admin.post(f"{base}/password", json={"new_password": "a brand new passphrase"}).status_code == 409
    # Role and access are HuggingHack's own decisions and stay editable.
    assert admin.patch(base, json={"role": "viewer"}).json()["role"] == "viewer"
    assert admin.patch(base, json={"disabled": True}).json()["disabled"] is True
    assert client.get("/api/account").status_code == 401


def test_local_accounts_keep_their_profile_and_password_controls(server):  # noqa: F811
    member, _ = login("member")
    saved = member.patch("/api/account/profile", json={"display_name": "Member Two", "email": "m@example.com"})
    assert saved.status_code == 200 and saved.json()["display_name"] == "Member Two"

    admin, status = login("admin")
    member_id = server["users"]["member"]["id"]
    assert admin.patch(f"/api/admin/users/{member_id}", json={"display_name": "M"}).json()["display_name"] == "M"
    assert admin.post(
        f"/api/admin/users/{member_id}/password", json={"new_password": "a brand new passphrase"}
    ).status_code == 200
    assert member.get("/api/account").status_code == 401
    # An administrator changes their own password from their account, not through a reset.
    own = admin.post(f"/api/admin/users/{status['user']['id']}/password", json={"new_password": "another new passphrase"})
    assert own.status_code == 409
    assert admin.get("/api/account").status_code == 200
