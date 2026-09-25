import base64
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import app.main as main
import app.oidc as oidc_module
from app.auth import AuthService
from app.config import Settings
from app.database import Database
from app.oidc import OidcClient

ISSUER = "https://id.example.internal/application/o/hugginghack/"
CLIENT_ID = "hugginghack"
CLIENT_SECRET = "oidc-client-secret-value-9876"


def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk(key, kid: str) -> dict:
    entry = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**entry, "kid": kid, "use": "sig", "alg": "RS256"}


class FakeProvider:
    """Just enough of an OpenID provider (shaped like Authentik) for the flow."""

    def __init__(self):
        self.keys = {"k1": rsa_key()}
        self.algorithms = ["RS256"]
        self.codes: dict[str, dict] = {}
        self.userinfo: dict = {}
        self.jwks_requests = 0

    def discovery(self) -> dict:
        return {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}authorize/",
            "token_endpoint": f"{ISSUER}token/",
            "userinfo_endpoint": f"{ISSUER}userinfo/",
            "jwks_uri": f"{ISSUER}jwks/",
            "id_token_signing_alg_values_supported": self.algorithms,
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        }

    def issue(self, code: str, challenge: str, token: str) -> None:
        self.codes[code] = {"challenge": challenge, "token": token}

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{ISSUER}.well-known/openid-configuration" or url == f"{ISSUER.rstrip('/')}/.well-known/openid-configuration":
            return httpx.Response(200, json=self.discovery())
        if url == f"{ISSUER}jwks/":
            self.jwks_requests += 1
            return httpx.Response(200, json={"keys": [public_jwk(key, kid) for kid, key in self.keys.items()]})
        if url == f"{ISSUER}userinfo/":
            return httpx.Response(200, json=self.userinfo)
        if url == f"{ISSUER}token/":
            expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if request.headers.get("authorization") != expected:
                return httpx.Response(401, json={"error": "invalid_client"})
            form = parse_qs(request.content.decode())
            grant = self.codes.pop(form["code"][0], None)
            if grant is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            verifier = form["code_verifier"][0]
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if digest != grant["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE mismatch"})
            return httpx.Response(200, json={"id_token": grant["token"], "access_token": "at", "token_type": "Bearer"})
        return httpx.Response(404)


def id_token(claims: dict, *, key=None, kid: str | None = "k1", algorithm: str = "RS256", provider=None) -> str:
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300, **claims}
    headers = {"kid": kid} if kid else {}
    if algorithm == "none":
        return jwt.encode(payload, None, algorithm="none", headers=headers)
    if algorithm.startswith("HS"):
        return jwt.encode(payload, key or CLIENT_SECRET, algorithm=algorithm, headers=headers)
    return jwt.encode(payload, key or provider.keys[kid or "k1"], algorithm=algorithm, headers=headers)


@pytest.fixture()
def sso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        accounts_enabled=True,
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT_ID,
        oidc_client_secret=CLIENT_SECRET,
        oidc_provider_name="Authentik",
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    auth = AuthService(settings, database)
    provider = FakeProvider()
    for name, value in {"settings": settings, "database": database, "auth": auth}.items():
        monkeypatch.setattr(main, name, value)

    def use(settings_value: Settings) -> None:
        monkeypatch.setattr(main, "settings", settings_value)
        monkeypatch.setattr(main, "oidc", OidcClient(settings_value, transport=httpx.MockTransport(provider.handle)))

    use(settings)
    return {"settings": settings, "database": database, "auth": auth, "provider": provider, "use": use}


def start(client: TestClient, next_path: str = "/models/acme/tiny") -> dict:
    response = client.get("/api/auth/oidc/login", params={"next": next_path}, follow_redirects=False)
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith(f"{ISSUER}authorize/?")
    query = {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}
    assert query["code_challenge_method"] == "S256" and query["client_id"] == CLIENT_ID
    return query


def finish(client: TestClient, provider: FakeProvider, query: dict, token: str, code: str = "code-1"):
    provider.issue(code, query["code_challenge"], token)
    return client.get(
        "/api/auth/oidc/callback", params={"code": code, "state": query["state"]}, follow_redirects=False
    )


def sso_error(response) -> str:
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/#/?sso_error="), location
    return unquote(location.split("sso_error=", 1)[1])


def owner(sso) -> dict:
    return sso["auth"].create_user("owner", "Owner", "correct horse battery", "admin")


def test_sso_creates_an_account_and_signs_in(sso):
    owner(sso)
    client = TestClient(main.app)
    status = client.get("/api/auth/status").json()
    assert status["oidc"] == {"enabled": True, "name": "Authentik"}
    query = start(client)
    token = id_token(
        {"sub": "u-123", "nonce": query["nonce"], "preferred_username": "Jane.Doe", "name": "Jane Doe", "email": "jane@corp.example"},
        provider=sso["provider"],
    )
    response = finish(client, sso["provider"], query, token)
    assert response.status_code == 303
    assert response.headers["location"] == "/#/models/acme/tiny"
    me = client.get("/api/auth/status").json()["user"]
    assert me["username"] == "jane-doe"
    assert me["display_name"] == "Jane Doe" and me["email"] == "jane@corp.example"
    assert me["role"] == "viewer" and me["auth_provider"] == "oidc"
    assert client.get("/api/account").json()["local_password"] is False
    # The browser-binding cookie is single use.
    assert "hugginghack_oidc" not in client.cookies


def test_sso_accounts_never_merge_with_local_ones_and_keep_admin_changes(sso):
    owner(sso)
    sso["auth"].create_user("jane", "Local Jane", "local password here", "member")
    provider = sso["provider"]
    client = TestClient(main.app)
    query = start(client)
    first = finish(client, provider, query, id_token({"sub": "u-9", "nonce": query["nonce"], "preferred_username": "jane", "name": "Jane"}, provider=provider))
    assert first.status_code == 303 and "sso_error" not in first.headers["location"]
    external = client.get("/api/auth/status").json()["user"]
    assert external["username"] == "jane-2"
    assert sso["database"].get_user_by_username("jane")["auth_provider"] == "local"

    sso["database"].update_user(external["id"], role="member")
    again = TestClient(main.app)
    query = start(again)
    finish(again, provider, query, id_token({"sub": "u-9", "nonce": query["nonce"], "preferred_username": "renamed", "name": "Jane Q"}, provider=provider), code="code-2")
    user = again.get("/api/auth/status").json()["user"]
    assert user["id"] == external["id"] and user["username"] == "jane-2"
    assert user["display_name"] == "Jane Q" and user["role"] == "member"


@pytest.mark.parametrize(
    "case",
    ["bad-signature", "wrong-audience", "wrong-issuer", "expired", "wrong-nonce", "alg-none", "hs256-not-listed", "no-sub"],
)
def test_sso_rejects_invalid_id_tokens(sso, case):
    owner(sso)
    provider = sso["provider"]
    client = TestClient(main.app)
    query = start(client)
    claims = {"sub": "u-1", "nonce": query["nonce"], "preferred_username": "mallory"}
    token = {
        "bad-signature": lambda: id_token(claims, key=rsa_key()),
        "wrong-audience": lambda: id_token({**claims, "aud": "other-app"}, provider=provider),
        "wrong-issuer": lambda: id_token({**claims, "iss": ISSUER.rstrip("/")}, provider=provider),
        "expired": lambda: id_token({**claims, "exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, provider=provider),
        "wrong-nonce": lambda: id_token({**claims, "nonce": "someone-else"}, provider=provider),
        "alg-none": lambda: id_token(claims, algorithm="none"),
        "hs256-not-listed": lambda: id_token(claims, algorithm="HS256"),
        "no-sub": lambda: id_token({key: value for key, value in claims.items() if key != "sub"}, provider=provider),
    }[case]()
    assert sso_error(finish(client, provider, query, token))
    assert sso["database"].get_user_by_username("mallory") is None
    assert client.get("/api/auth/status").json()["user"] is None


def test_sso_state_is_single_use_and_bound_to_the_browser(sso):
    owner(sso)
    provider = sso["provider"]
    victim = TestClient(main.app)
    attacker = TestClient(main.app)
    query = start(attacker)
    token = id_token({"sub": "attacker", "nonce": query["nonce"], "preferred_username": "attacker"}, provider=provider)
    provider.issue("stolen", query["code_challenge"], token)
    # The attacker's callback link opened in another browser does not sign it in.
    lured = victim.get("/api/auth/oidc/callback", params={"code": "stolen", "state": query["state"]}, follow_redirects=False)
    assert "same browser" in sso_error(lured)
    assert victim.get("/api/auth/status").json()["user"] is None
    # The state was consumed, so it cannot be replayed either.
    assert "expired or was already used" in sso_error(finish(attacker, provider, query, token, code="again"))
    unknown = attacker.get("/api/auth/oidc/callback", params={"code": "x", "state": "made-up"}, follow_redirects=False)
    assert sso_error(unknown)


def test_sso_errors_show_no_link_text_until_the_state_checks_out(sso):
    owner(sso)
    client = TestClient(main.app)
    lure = {"error": "access_denied", "error_description": "Call +1-555-0100 to unlock your account"}
    # A crafted link shows nothing it carries.
    forged = client.get("/api/auth/oidc/callback", params={**lure, "state": "made-up"}, follow_redirects=False)
    assert "555" not in sso_error(forged) and "expired" in sso_error(forged)
    # A real refusal names only a standard error code, never the description.
    query = start(client)
    refused = client.get("/api/auth/oidc/callback", params={**lure, "state": query["state"]}, follow_redirects=False)
    assert sso_error(refused) == "The identity provider refused the sign-in (access_denied)."
    query = start(client)
    odd = client.get(
        "/api/auth/oidc/callback", params={"error": "visit evil.example", "state": query["state"]}, follow_redirects=False
    )
    assert sso_error(odd) == "The identity provider refused the sign-in."


def test_sso_never_redirects_off_site(sso):
    owner(sso)
    provider = sso["provider"]
    for target in ("//evil.example/x", "https://evil.example", "/\\evil.example"):
        client = TestClient(main.app)
        query = start(client, target)
        response = finish(client, provider, query, id_token({"sub": "u-5", "nonce": query["nonce"]}, provider=provider), code=target)
        assert response.headers["location"] == "/#/models"


def test_sso_respects_disabled_accounts_groups_setup_and_account_mode(sso):
    provider = sso["provider"]
    client = TestClient(main.app)
    # Before the owner exists, single sign-on must not create the first account.
    response = client.get("/api/auth/oidc/login", follow_redirects=False)
    assert "owner account" in sso_error(response)

    owner(sso)
    query = start(client)
    finish(client, provider, query, id_token({"sub": "u-7", "nonce": query["nonce"], "preferred_username": "dora"}, provider=provider))
    dora = sso["database"].get_user_by_username("dora")
    sso["database"].update_user(dora["id"], disabled=1)
    blocked = TestClient(main.app)
    query = start(blocked)
    assert "disabled" in sso_error(finish(blocked, provider, query, id_token({"sub": "u-7", "nonce": query["nonce"]}, provider=provider), code="c2"))

    sso["use"](replace(sso["settings"], oidc_allowed_groups="ml-team, admins"))
    outsider = TestClient(main.app)
    query = start(outsider)
    token = id_token({"sub": "u-8", "nonce": query["nonce"], "groups": ["sales"]}, provider=provider)
    assert "not in a group" in sso_error(finish(outsider, provider, query, token, code="c3"))
    missing = TestClient(main.app)
    query = start(missing)
    assert "claim" in sso_error(finish(missing, provider, query, id_token({"sub": "u-8", "nonce": query["nonce"]}, provider=provider), code="c4"))
    # Groups can also come from the userinfo endpoint.
    provider.userinfo = {"sub": "u-8", "groups": ["ml-team"]}
    member = TestClient(main.app)
    query = start(member)
    finish(member, provider, query, id_token({"sub": "u-8", "nonce": query["nonce"], "preferred_username": "mo"}, provider=provider), code="c5")
    assert member.get("/api/auth/status").json()["user"]["username"] == "user-mo"  # too short alone

    sso["use"](replace(sso["settings"], accounts_enabled=False))
    assert TestClient(main.app).get("/api/auth/oidc/login", follow_redirects=False).status_code == 404


def test_sso_follows_key_rotation_and_hmac_when_advertised(sso, monkeypatch: pytest.MonkeyPatch):
    owner(sso)
    provider = sso["provider"]
    monkeypatch.setattr(oidc_module, "JWKS_REFRESH_SECONDS", 0)
    client = TestClient(main.app)
    query = start(client)
    finish(client, provider, query, id_token({"sub": "r-1", "nonce": query["nonce"]}, provider=provider))
    provider.keys["k2"] = rsa_key()
    rotated = TestClient(main.app)
    query = start(rotated)
    response = finish(rotated, provider, query, id_token({"sub": "r-1", "nonce": query["nonce"]}, kid="k2", provider=provider), code="c2")
    assert "sso_error" not in response.headers["location"]

    provider.algorithms = ["RS256", "HS256"]
    main.oidc._discovery = None
    hmac_client = TestClient(main.app)
    query = start(hmac_client)
    good = id_token({"sub": "h-1", "nonce": query["nonce"], "preferred_username": "hmac"}, algorithm="HS256", kid=None)
    assert "sso_error" not in finish(hmac_client, provider, query, good, code="c3").headers["location"]
    forged_client = TestClient(main.app)
    query = start(forged_client)
    forged = id_token({"sub": "h-2", "nonce": query["nonce"]}, algorithm="HS256", key="guessed-secret-value-123", kid=None)
    assert sso_error(finish(forged_client, provider, query, forged, code="c4"))


def test_sso_settings_show_on_the_server_tab_without_the_secret(sso):
    owner(sso)
    client = TestClient(main.app)
    client.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"})
    response = client.get("/api/admin/server")
    assert response.status_code == 200
    assert CLIENT_SECRET not in response.text
    assert response.json()["sso"]["client_secret_configured"] is True
    assert response.json()["sso"]["issuer"] == ISSUER


def test_unreachable_provider_does_not_block_password_sign_in(sso):
    owner(sso)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    main.oidc = OidcClient(sso["settings"], transport=httpx.MockTransport(offline))
    client = TestClient(main.app)
    assert "could not be reached" in sso_error(client.get("/api/auth/oidc/login", follow_redirects=False))
    assert client.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"}).status_code == 200
