"""OpenID Connect sign-in (Authorization Code flow with PKCE).

Works with any standards-compliant identity provider: Authentik, Keycloak,
Microsoft Entra ID, Okta, Dex, and others. Everything provider-specific comes from
the issuer's discovery document, which is fetched on the first sign-in so an
unreachable identity provider never blocks the server or password sign-in.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt.algorithms import get_default_algorithms

from .config import Settings


ASYMMETRIC_ALGORITHMS = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
HMAC_ALGORITHMS = {"HS256", "HS384", "HS512"}
DISCOVERY_TTL_SECONDS = 3600
JWKS_REFRESH_SECONDS = 60
CLOCK_LEEWAY_SECONDS = 60


class OidcError(Exception):
    """A sign-in problem that is safe to show to the person signing in."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class OidcClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        verify: bool | str = settings.oidc_verify_ssl
        if settings.oidc_ca_bundle:
            verify = settings.oidc_ca_bundle
        self.http = httpx.Client(
            timeout=httpx.Timeout(10.0, connect=5.0),
            verify=verify,
            transport=transport,
            headers={"User-Agent": f"HuggingHack/{settings.app_version}"},
        )
        self._lock = threading.Lock()
        self._discovery: dict[str, Any] | None = None
        self._discovered_at = 0.0
        self._keys: dict[str, Any] = {}
        self._keys_fetched_at = 0.0

    def close(self) -> None:
        self.http.close()

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = self.http.get(url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise OidcError(f"The identity provider could not be reached ({error.__class__.__name__}).") from error
        if not isinstance(payload, dict):
            raise OidcError("The identity provider returned an unexpected response.")
        return payload

    def discovery(self) -> dict[str, Any]:
        with self._lock:
            if self._discovery and time.monotonic() - self._discovered_at < DISCOVERY_TTL_SECONDS:
                return self._discovery
        issuer = (self.settings.oidc_issuer or "").rstrip("/")
        document = self._get_json(f"{issuer}/.well-known/openid-configuration")
        for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(document.get(key), str):
                raise OidcError(f"The identity provider's configuration is missing {key}.")
        with self._lock:
            self._discovery = document
            self._discovered_at = time.monotonic()
        return document

    def allowed_algorithms(self) -> list[str]:
        advertised = self.discovery().get("id_token_signing_alg_values_supported") or ["RS256"]
        supported = set(get_default_algorithms())
        algorithms = [
            algorithm
            for algorithm in advertised
            if algorithm in supported and (algorithm in ASYMMETRIC_ALGORITHMS or algorithm in HMAC_ALGORITHMS)
        ]
        if not algorithms:
            raise OidcError("The identity provider uses no signing algorithm HuggingHack accepts.")
        return algorithms

    def authorization_url(self, state: str, nonce: str, challenge: str, redirect_uri: str) -> str:
        endpoint = self.discovery()["authorization_endpoint"]
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.oidc_client_id,
                "redirect_uri": redirect_uri,
                "scope": self.settings.oidc_scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    def exchange(self, code: str, verifier: str, redirect_uri: str) -> dict[str, Any]:
        document = self.discovery()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "client_id": self.settings.oidc_client_id,
        }
        auth: tuple[str, str] | None = None
        secret = self.settings.oidc_client_secret
        if secret:
            methods = document.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
            if "client_secret_basic" in methods:
                auth = (self.settings.oidc_client_id or "", secret)
            else:
                data["client_secret"] = secret
        try:
            response = self.http.post(document["token_endpoint"], data=data, auth=auth)
        except httpx.HTTPError as error:
            raise OidcError("The identity provider could not be reached to finish sign-in.") from error
        if response.status_code != 200:
            try:
                detail = response.json().get("error_description") or response.json().get("error")
            except ValueError:
                detail = None
            raise OidcError(f"The identity provider rejected the sign-in{f': {detail}' if detail else ''}.")
        tokens = response.json()
        if not isinstance(tokens, dict) or not tokens.get("id_token"):
            raise OidcError("The identity provider did not return an ID token.")
        return tokens

    def _signing_key(self, kid: str | None) -> Any:
        def lookup() -> Any:
            if kid is None and len(self._keys) == 1:
                return next(iter(self._keys.values()))
            return self._keys.get(kid or "")

        key = lookup()
        if key is not None:
            return key
        # Unknown key id: the provider may have rotated keys. Refresh, but not too often.
        if time.monotonic() - self._keys_fetched_at >= JWKS_REFRESH_SECONDS or not self._keys:
            document = self._get_json(self.discovery()["jwks_uri"])
            keys = {}
            for entry in document.get("keys") or []:
                try:
                    keys[entry.get("kid") or f"_{len(keys)}"] = jwt.PyJWK(entry)
                except (jwt.PyJWKError, jwt.InvalidKeyError):
                    continue
            self._keys = keys
            self._keys_fetched_at = time.monotonic()
            key = lookup()
        if key is None:
            raise OidcError("The ID token was signed with an unknown key.")
        return key

    def validate_id_token(self, token: str, nonce: str) -> dict[str, Any]:
        document = self.discovery()
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise OidcError("The identity provider returned a malformed ID token.") from error
        algorithm = header.get("alg")
        allowed = self.allowed_algorithms()
        if algorithm not in allowed:
            raise OidcError(f"The ID token uses a signing algorithm that is not allowed ({algorithm}).")
        if algorithm in HMAC_ALGORITHMS:
            # Symmetric tokens are signed with the client secret, never a published key.
            if not self.settings.oidc_client_secret:
                raise OidcError("An HMAC-signed ID token needs OIDC_CLIENT_SECRET.")
            key: Any = self.settings.oidc_client_secret.encode("utf-8")
        else:
            key = self._signing_key(header.get("kid")).key
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=self.settings.oidc_client_id,
                issuer=document["issuer"],
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as error:
            raise OidcError("The sign-in expired. Try again.") from error
        except jwt.PyJWTError as error:
            raise OidcError(f"The ID token is not valid ({error.__class__.__name__}).") from error
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        if len(audiences) > 1 and claims.get("azp") != self.settings.oidc_client_id:
            raise OidcError("The ID token was issued to a different application.")
        if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise OidcError("The sign-in response does not match this request. Try again.")
        return claims

    def userinfo(self, access_token: str | None) -> dict[str, Any]:
        endpoint = self.discovery().get("userinfo_endpoint")
        if not endpoint or not access_token:
            return {}
        try:
            response = self.http.get(endpoint, headers={"Authorization": f"Bearer {access_token}"})
            if response.status_code != 200:
                return {}
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}


def claim_groups(claims: dict[str, Any], claim: str) -> list[str] | None:
    value = claims.get(claim)
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return None

