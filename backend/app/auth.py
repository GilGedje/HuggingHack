from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .database import Database


USERNAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{1,30}[a-z0-9])?$")
PASSWORD_MIN_LENGTH = 12
API_TOKEN_PREFIX = "hht_"
TOKEN_SCOPES = ("read", "write")
# Avoid a database write on every request (every upload chunk, for example).
TOUCH_INTERVAL_SECONDS = 300
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def normalize_username(value: str) -> str:
    username = value.strip().lower()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError(
            "Username must be 3-32 lowercase letters, numbers, underscores, or hyphens."
        )
    return username


def validate_password(value: str) -> str:
    if len(value) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(value) > 256:
        raise ValueError("Password must be 256 characters or fewer.")
    return value


def hash_password(password: str) -> str:
    validated = validate_password(password)
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        validated.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


class AuthService:
    cookie_name = "hugginghack_session"

    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._attempt_lock = threading.Lock()
        self._setup_lock = threading.Lock()
        self._touched: dict[str, float] = {}

    def ensure_local_user(self) -> None:
        if self.settings.accounts_enabled:
            return
        if self.database.get_user("local") is None:
            self.database.create_user(
                {
                    "id": "local",
                    "username": "local",
                    "display_name": "Local user",
                    "password_hash": "disabled",
                    "role": "admin",
                    "created_at": utc_iso(),
                    "updated_at": utc_iso(),
                }
            )

    def setup_required(self) -> bool:
        return self.settings.accounts_enabled and self.database.count_users() == 0

    def create_user(
        self,
        username: str,
        display_name: str,
        password: str,
        role: str = "member",
    ) -> dict[str, Any]:
        normalized = normalize_username(username)
        name = display_name.strip() or normalized
        if len(name) > 80:
            raise ValueError("Display name must be 80 characters or fewer.")
        if role not in {"admin", "member", "viewer"}:
            raise ValueError("Role must be admin, member, or viewer.")
        timestamp = utc_iso()
        return self.database.create_user(
            {
                "id": uuid.uuid4().hex,
                "username": normalized,
                "display_name": name,
                "password_hash": hash_password(password),
                "role": role,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )

    def create_owner(
        self, username: str, display_name: str, password: str
    ) -> dict[str, Any]:
        with self._setup_lock:
            if self.database.count_users() != 0:
                raise ValueError("The owner account already exists.")
            return self.create_user(username, display_name, password, role="admin")

    def authenticate(self, username: str, password: str, client_key: str) -> dict[str, Any] | None:
        self._check_rate_limit(client_key)
        try:
            normalized = normalize_username(username)
        except ValueError:
            self._record_failure(client_key)
            return None
        user = self.database.get_user_by_username(normalized)
        if not user or not verify_password(password, user["password_hash"]):
            self._record_failure(client_key)
            return None
        with self._attempt_lock:
            self._attempts.pop(client_key, None)
        if user.get("disabled"):
            raise PermissionError("This account is disabled. Ask an administrator to enable it.")
        return user

    def _due(self, key: str) -> bool:
        now = utc_now().timestamp()
        if now - self._touched.get(key, 0) < TOUCH_INTERVAL_SECONDS:
            return False
        self._touched[key] = now
        return True

    def create_session(
        self, user_id: str, user_agent: str | None = None, ip: str | None = None
    ) -> tuple[str, str]:
        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        csrf_token = secrets.token_urlsafe(32)
        created = utc_now()
        self.database.create_session(
            {
                "token_hash": token_hash,
                "user_id": user_id,
                "csrf_token": csrf_token,
                "created_at": utc_iso(created),
                "expires_at": utc_iso(
                    created + timedelta(hours=self.settings.session_ttl_hours)
                ),
                "user_agent": (user_agent or "")[:300] or None,
                "ip": (ip or "")[:64] or None,
            }
        )
        self.database.update_user(user_id, last_login_at=utc_iso(created))
        return raw_token, csrf_token

    def session(self, raw_token: str | None) -> dict[str, Any] | None:
        if not self.settings.accounts_enabled:
            user = self.database.get_user("local", include_secret=False)
            return {"user": user, "csrf_token": "accounts-disabled"} if user else None
        if not raw_token:
            return None
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        session = self.database.get_session(token_hash)
        if not session:
            return None
        try:
            expires = datetime.fromisoformat(session["expires_at"])
        except (TypeError, ValueError):
            self.database.delete_session(token_hash)
            return None
        if expires <= utc_now():
            self.database.delete_session(token_hash)
            return None
        if session["user"].get("disabled"):
            return None
        session["token_hash"] = token_hash
        if self._due(f"session:{token_hash}"):
            self.database.touch_session(token_hash, utc_iso())
        return session

    def create_api_token(
        self, user_id: str, name: str, scope: str, expires_in_days: int | None
    ) -> tuple[str, dict[str, Any]]:
        label = name.strip()
        if not label or len(label) > 80:
            raise ValueError("Token name must be 1-80 characters.")
        if scope not in TOKEN_SCOPES:
            raise ValueError("Token scope must be read or write.")
        if expires_in_days is not None and not 1 <= expires_in_days <= 3650:
            raise ValueError("Tokens expire after 1-3650 days, or never.")
        raw_token = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
        created = utc_now()
        record = self.database.create_api_token(
            {
                "id": uuid.uuid4().hex,
                "user_id": user_id,
                "name": label,
                "token_hash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
                "prefix": raw_token[: len(API_TOKEN_PREFIX) + 6],
                "scope": scope,
                "created_at": utc_iso(created),
                "expires_at": (
                    utc_iso(created + timedelta(days=expires_in_days))
                    if expires_in_days
                    else None
                ),
            }
        )
        return raw_token, record

    def token_principal(self, raw_token: str) -> dict[str, Any] | None:
        """The user and scope behind a personal API token, or None if it is invalid."""
        if not self.settings.accounts_enabled or not raw_token.startswith(API_TOKEN_PREFIX):
            return None
        token = self.database.get_api_token_by_hash(
            hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        )
        if not token:
            return None
        if token.get("expires_at"):
            try:
                if datetime.fromisoformat(token["expires_at"]) <= utc_now():
                    return None
            except (TypeError, ValueError):
                return None
        user = self.database.get_user(token["user_id"], include_secret=False)
        if not user or user.get("disabled"):
            return None
        if self._due(f"token:{token['id']}"):
            self.database.touch_api_token(token["id"], utc_iso())
        return {"user": user, "token": token}

    def revoke(self, raw_token: str | None) -> None:
        if raw_token:
            self.database.delete_session(
                hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            )

    def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
        raw_session_token: str,
    ) -> None:
        user = self.database.get_user(user_id)
        if not user or not verify_password(current_password, user["password_hash"]):
            raise ValueError("Current password is incorrect.")
        replacement = hash_password(new_password)
        self.database.update_user_password(user_id, replacement, utc_iso())
        keep_hash = hashlib.sha256(raw_session_token.encode("utf-8")).hexdigest()
        self.database.delete_other_sessions(user_id, keep_hash)

    def verify_csrf(self, session: dict[str, Any], token: str | None) -> bool:
        if not self.settings.accounts_enabled:
            return True
        return bool(token) and hmac.compare_digest(
            str(session.get("csrf_token") or ""), token
        )

    def _check_rate_limit(self, key: str) -> None:
        cutoff = utc_now().timestamp() - 300
        with self._attempt_lock:
            attempts = self._attempts[key]
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            if len(attempts) >= 8:
                raise ValueError("Too many sign-in attempts. Try again in a few minutes.")

    def _record_failure(self, key: str) -> None:
        with self._attempt_lock:
            self._attempts[key].append(utc_now().timestamp())
