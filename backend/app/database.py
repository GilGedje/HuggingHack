from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # SQLite remains usable when the optional adapter is absent.
    psycopg = None
    dict_row = None


if psycopg is None:
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
else:
    INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)


NAMED_PARAMETER_PATTERN = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


def _postgres_query(query: str, parameters: object = ()) -> str:
    if isinstance(parameters, Mapping):
        return NAMED_PARAMETER_PATTERN.sub(r"%(\1)s", query)
    return query.replace("?", "%s")


class _PostgresConnection:
    def __init__(self, connection: Any):
        self._connection = connection

    def __enter__(self) -> _PostgresConnection:
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> object:
        return self._connection.__exit__(exc_type, exc_value, traceback)

    def execute(
        self,
        query: str,
        parameters: Mapping[str, Any] | Sequence[Any] = (),
    ) -> Any:
        return self._connection.execute(_postgres_query(query, parameters), parameters)

    def executemany(
        self,
        query: str,
        parameters: Iterable[Mapping[str, Any] | Sequence[Any]],
    ) -> Any:
        parameter_rows = list(parameters)
        cursor = self._connection.cursor()
        cursor.executemany(
            _postgres_query(query, parameter_rows[0] if parameter_rows else ()),
            parameter_rows,
        )
        return cursor

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self._connection.execute(statement)


DOWNLOAD_FIELDS = {
    "status",
    "total_bytes",
    "downloaded_bytes",
    "progress",
    "speed_bps",
    "error",
    "target_path",
    "metadata_json",
    "updated_at",
    "completed_at",
}

RUNTIME_JOB_FIELDS = {
    "status",
    "total_bytes",
    "processed_bytes",
    "progress",
    "message",
    "error",
    "updated_at",
    "completed_at",
}


ROLES = ("admin", "member", "viewer")
USERS_COLUMNS = """
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    email TEXT,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    last_login_at TEXT,
                    preferences_json TEXT NOT NULL DEFAULT '{}',
                    auth_provider TEXT NOT NULL DEFAULT 'local',
                    external_subject TEXT
""".strip("\n")
LEGACY_USER_COLUMNS = (
    "id", "username", "display_name", "password_hash", "role", "created_at", "updated_at"
)


class Database:
    def __init__(self, target: Path | str):
        value = str(target).strip()
        self.backend = (
            "postgresql"
            if value.startswith(("postgresql://", "postgres://"))
            else "sqlite"
        )
        self.path = Path(target) if self.backend == "sqlite" else None
        self._database_url = value if self.backend == "postgresql" else None
        self._write_lock = threading.RLock()

    def connect(self) -> sqlite3.Connection | _PostgresConnection:
        if self.backend == "postgresql":
            if psycopg is None or dict_row is None:
                raise RuntimeError(
                    "PostgreSQL support requires the 'psycopg[binary]' dependency."
                )
            connection = psycopg.connect(self._database_url, row_factory=dict_row)
            return _PostgresConnection(connection)
        assert self.path is not None
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_users()
        with self._write_lock, self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
{USERS_COLUMNS}
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_nocase
                    ON users(LOWER(username));

                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_expiry
                    ON sessions(expires_at);

                CREATE TABLE IF NOT EXISTS oidc_states (
                    state_hash TEXT PRIMARY KEY,
                    browser_hash TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    code_verifier TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    next_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS api_tokens (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    prefix TEXT NOT NULL,
                    scope TEXT NOT NULL CHECK (scope IN ('read', 'write')),
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    expires_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_api_tokens_user
                    ON api_tokens(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS downloads (
                    id TEXT PRIMARY KEY,
                    repo_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    progress REAL NOT NULL DEFAULT 0,
                    speed_bps REAL NOT NULL DEFAULT 0,
                    error TEXT,
                    target_path TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    user_id TEXT REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_downloads_status
                    ON downloads(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS local_models (
                    repo_id TEXT PRIMARY KEY,
                    relative_path TEXT NOT NULL UNIQUE,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    file_count INTEGER NOT NULL DEFAULT 0,
                    modified_at TEXT NOT NULL,
                    downloaded_at TEXT,
                    revision TEXT,
                    sha TEXT,
                    pipeline_tag TEXT,
                    library_name TEXT,
                    license TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    config_json TEXT NOT NULL DEFAULT '{}',
                    source_url TEXT,
                    managed INTEGER NOT NULL DEFAULT 0,
                    storage_backend TEXT NOT NULL DEFAULT 'filesystem',
                    cached INTEGER NOT NULL DEFAULT 1,
                    remote_uri TEXT,
                    parameter_count BIGINT,
                    formats_json TEXT NOT NULL DEFAULT '[]',
                    storage_target TEXT NOT NULL DEFAULT 'local'
                );

                CREATE INDEX IF NOT EXISTS idx_local_models_modified
                    ON local_models(modified_at DESC);

                CREATE TABLE IF NOT EXISTS runtime_jobs (
                    id TEXT PRIMARY KEY,
                    target_id TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    target_kind TEXT NOT NULL CHECK (target_kind IN ('ollama', 'vllm')),
                    repo_id TEXT NOT NULL,
                    runtime_model_name TEXT NOT NULL,
                    source_file TEXT,
                    status TEXT NOT NULL,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    processed_bytes INTEGER NOT NULL DEFAULT 0,
                    progress REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    user_id TEXT REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_runtime_jobs_created
                    ON runtime_jobs(created_at DESC);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_jobs_active_model
                    ON runtime_jobs(target_id, repo_id)
                    WHERE status IN ('queued', 'preparing', 'transferring', 'loading');

                CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_jobs_active_vllm
                    ON runtime_jobs(target_id)
                    WHERE target_kind = 'vllm'
                      AND status IN ('queued', 'preparing', 'transferring', 'loading');

                CREATE TABLE IF NOT EXISTS collections (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, name)
                );

                CREATE TABLE IF NOT EXISTS saved_models (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    repo_id TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, repo_id)
                );

                CREATE INDEX IF NOT EXISTS idx_saved_models_user_updated
                    ON saved_models(user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS collection_items (
                    collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                    saved_model_id TEXT NOT NULL REFERENCES saved_models(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(collection_id, saved_model_id)
                );

                CREATE TABLE IF NOT EXISTS organizations (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_organizations_name_nocase
                    ON organizations(LOWER(name));

                CREATE TABLE IF NOT EXISTS organization_members (
                    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'write', 'read')),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (organization_id, user_id)
                );

                CREATE INDEX IF NOT EXISTS idx_organization_members_user
                    ON organization_members(user_id);

                CREATE TABLE IF NOT EXISTS owned_repositories (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                    repo_id TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    visibility TEXT NOT NULL DEFAULT 'private'
                        CHECK (visibility IN ('private', 'shared')),
                    status TEXT NOT NULL DEFAULT 'uploading'
                        CHECK (status IN ('uploading', 'ready')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    organization_id TEXT REFERENCES organizations(id) ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_owned_repositories_owner
                    ON owned_repositories(owner_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS repo_commits (
                    id TEXT PRIMARY KEY,
                    repo_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    parent_id TEXT,
                    author_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                    author_name TEXT NOT NULL,
                    message TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    changes_json TEXT NOT NULL,
                    UNIQUE(repo_id, sequence)
                );

                CREATE INDEX IF NOT EXISTS idx_repo_commits_repo
                    ON repo_commits(repo_id, sequence DESC);

                CREATE TABLE IF NOT EXISTS text_blobs (
                    sha256 TEXT PRIMARY KEY,
                    content TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS file_digests (
                    repo_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    version TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    PRIMARY KEY(repo_id, path)
                );
                """.replace("{USERS_COLUMNS}", USERS_COLUMNS)
            )
            columns = self._column_names(connection, "downloads")
            if "user_id" not in columns:
                connection.execute(
                    "ALTER TABLE downloads ADD COLUMN user_id TEXT "
                    "REFERENCES users(id) ON DELETE SET NULL"
                )
            local_model_columns = self._column_names(connection, "local_models")
            if "storage_backend" not in local_model_columns:
                connection.execute(
                    "ALTER TABLE local_models ADD COLUMN storage_backend "
                    "TEXT NOT NULL DEFAULT 'filesystem'"
                )
            if "cached" not in local_model_columns:
                connection.execute(
                    "ALTER TABLE local_models ADD COLUMN cached INTEGER NOT NULL DEFAULT 1"
                )
            if "remote_uri" not in local_model_columns:
                connection.execute("ALTER TABLE local_models ADD COLUMN remote_uri TEXT")
            if "parameter_count" not in local_model_columns:
                connection.execute("ALTER TABLE local_models ADD COLUMN parameter_count BIGINT")
            if "formats_json" not in local_model_columns:
                connection.execute(
                    "ALTER TABLE local_models ADD COLUMN formats_json "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
            if "storage_target" not in local_model_columns:
                connection.execute(
                    "ALTER TABLE local_models ADD COLUMN storage_target "
                    "TEXT NOT NULL DEFAULT 'local'"
                )
                # Rows from before storage targets belong to the single S3 bucket.
                connection.execute(
                    "UPDATE local_models SET storage_target = 's3' WHERE storage_backend = 's3'"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_user_created "
                "ON downloads(user_id, created_at DESC)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_external_subject "
                "ON users(auth_provider, external_subject) WHERE external_subject IS NOT NULL"
            )
            if "organization_id" not in self._column_names(connection, "owned_repositories"):
                connection.execute(
                    "ALTER TABLE owned_repositories ADD COLUMN organization_id TEXT "
                    "REFERENCES organizations(id) ON DELETE RESTRICT"
                )
            session_columns = self._column_names(connection, "sessions")
            for column in ("id", "user_agent", "ip", "last_seen_at"):
                if column not in session_columns:
                    connection.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT")
            for row in connection.execute(
                "SELECT token_hash FROM sessions WHERE id IS NULL"
            ).fetchall():
                connection.execute(
                    "UPDATE sessions SET id = ? WHERE token_hash = ?",
                    (uuid.uuid4().hex, row["token_hash"]),
                )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_id ON sessions(id)"
            )
            connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ?",
                (datetime.now(timezone.utc).isoformat(),),
            )

    def _migrate_users(self) -> None:
        """Bring a users table from before roles and external sign-in up to date.

        SQLite cannot change a CHECK constraint, so the table is rebuilt with
        foreign keys switched off, following SQLite's documented procedure.
        """
        with self.connect() as connection:
            columns = self._column_names(connection, "users")
        if not columns or "disabled" in columns:
            return
        if self.backend == "sqlite":
            assert self.path is not None
            connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            try:
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    legacy = ", ".join(LEGACY_USER_COLUMNS)
                    connection.execute(f"CREATE TABLE users_new ({USERS_COLUMNS})")
                    connection.execute(
                        f"INSERT INTO users_new ({legacy}) SELECT {legacy} FROM users"
                    )
                    connection.execute("DROP TABLE users")
                    connection.execute("ALTER TABLE users_new RENAME TO users")
                    connection.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_nocase "
                        "ON users(LOWER(username))"
                    )
                    problems = connection.execute("PRAGMA foreign_key_check").fetchall()
                    if problems:
                        raise RuntimeError(f"User migration broke references: {problems[:5]}")
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.close()
            return
        with self._write_lock, self.connect() as connection:
            for column in (
                "email TEXT",
                "disabled INTEGER NOT NULL DEFAULT 0",
                "last_login_at TEXT",
                "preferences_json TEXT NOT NULL DEFAULT '{}'",
                "auth_provider TEXT NOT NULL DEFAULT 'local'",
                "external_subject TEXT",
            ):
                connection.execute(f"ALTER TABLE users ADD COLUMN {column}")
            checks = connection.execute(
                """
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'users'::regclass AND contype = 'c'
                  AND pg_get_constraintdef(oid) LIKE ?
                """,
                ("%role%",),
            ).fetchall()
            for check in checks:
                connection.execute(f'ALTER TABLE users DROP CONSTRAINT "{check["conname"]}"')
            connection.execute(
                "ALTER TABLE users ADD CONSTRAINT users_role_check "
                "CHECK (role IN ('admin', 'member', 'viewer'))"
            )

    def _column_names(
        self, connection: sqlite3.Connection | _PostgresConnection, table: str
    ) -> set[str]:
        if self.backend == "sqlite":
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        else:
            rows = connection.execute(
                """
                SELECT column_name AS name
                FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = ?
                """,
                (table,),
            ).fetchall()
        return {row["name"] for row in rows}

    @staticmethod
    def _decode_row(
        row: sqlite3.Row | dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        list_keys = {"tags_json", "formats_json"}
        for key in ("payload_json", "metadata_json", "tags_json", "config_json", "formats_json"):
            if key in result:
                raw = result.pop(key)
                output_key = key.removesuffix("_json")
                try:
                    result[output_key] = json.loads(raw or ("[]" if key in list_keys else "{}"))
                except (TypeError, json.JSONDecodeError):
                    result[output_key] = [] if key in list_keys else {}
        if "managed" in result:
            result["managed"] = bool(result["managed"])
        if "cached" in result:
            result["cached"] = bool(result["cached"])
        return result

    @staticmethod
    def _user(row: sqlite3.Row | dict[str, Any] | None, include_secret: bool) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        if not include_secret:
            result.pop("password_hash", None)
        try:
            result["preferences"] = json.loads(result.pop("preferences_json", None) or "{}")
        except (TypeError, json.JSONDecodeError):
            result["preferences"] = {}
        result["disabled"] = bool(result.get("disabled"))
        return result

    @classmethod
    def _public_user(cls, row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
        return cls._user(row, include_secret=False)

    def count_users(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()
            return int(row["count"])

    def namespace_taken(self, name: str) -> bool:
        """True when a user or organization already uses this name, ignoring case."""
        with self.connect() as connection:
            for table, column in (("users", "username"), ("organizations", "name")):
                row = connection.execute(
                    f"SELECT 1 FROM {table} WHERE LOWER({column}) = LOWER(?)", (name,)
                ).fetchone()
                if row:
                    return True
        return False

    def create_user(self, record: dict[str, Any]) -> dict[str, Any]:
        record = {
            "email": None,
            "auth_provider": "local",
            "external_subject": None,
            **record,
        }
        with self._write_lock, self.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM organizations WHERE LOWER(name) = LOWER(?)", (record["username"],)
            ).fetchone():
                raise ValueError("That name belongs to an organization.")
            connection.execute(
                """
                INSERT INTO users (
                    id, username, display_name, password_hash, role, created_at, updated_at,
                    email, auth_provider, external_subject
                ) VALUES (
                    :id, :username, :display_name, :password_hash, :role, :created_at,
                    :updated_at, :email, :auth_provider, :external_subject
                )
                """,
                record,
            )
        return self.get_user(record["id"], include_secret=False)

    def get_user(self, user_id: str, include_secret: bool = True) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._user(row, include_secret)

    def get_user_by_external(self, provider: str, subject: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE auth_provider = ? AND external_subject = ?",
                (provider, subject),
            ).fetchone()
        return self._user(row, include_secret=False)

    def create_oidc_state(self, record: dict[str, Any]) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                "DELETE FROM oidc_states WHERE created_at < ?", (record["expires_before"],)
            )
            connection.execute(
                """
                INSERT INTO oidc_states (
                    state_hash, browser_hash, nonce, code_verifier, redirect_uri,
                    next_path, created_at
                ) VALUES (
                    :state_hash, :browser_hash, :nonce, :code_verifier, :redirect_uri,
                    :next_path, :created_at
                )
                """,
                {key: value for key, value in record.items() if key != "expires_before"},
            )

    def take_oidc_state(self, state_hash: str) -> dict[str, Any] | None:
        """Return and delete a pending sign-in, so each state works exactly once."""
        with self._write_lock, self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM oidc_states WHERE state_hash = ?", (state_hash,)
            ).fetchone()
            connection.execute("DELETE FROM oidc_states WHERE state_hash = ?", (state_hash,))
        return dict(row) if row else None

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)
            ).fetchone()
        return self._user(row, include_secret=True)

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY role, username"
            ).fetchall()
        return [self._public_user(row) for row in rows]

    USER_SORTS = {
        "role": "CASE role WHEN 'admin' THEN 0 WHEN 'member' THEN 1 ELSE 2 END, LOWER(username)",
        "name": "LOWER(username)",
        "last_login": "CASE WHEN last_login_at IS NULL THEN 1 ELSE 0 END, last_login_at DESC, LOWER(username)",
        "newest": "created_at DESC, LOWER(username)",
    }

    def search_users(
        self,
        *,
        query: str = "",
        role: str | None = None,
        status: str | None = None,
        sort: str = "role",
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        """One page of accounts for the admin list, the total that matched, and
        per-role and per-status counts for the search text alone."""
        search_clauses: list[str] = []
        search_params: list[Any] = []
        text = query.strip().lower()
        if text:
            # '!' escapes LIKE wildcards; the pattern travels as a parameter so the
            # SQL itself never contains a literal percent sign.
            escaped = text.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            pattern = f"%{escaped}%"
            search_clauses.append(
                "(LOWER(username) LIKE ? ESCAPE '!' OR LOWER(display_name) LIKE ? ESCAPE '!' "
                "OR LOWER(COALESCE(email, '')) LIKE ? ESCAPE '!')"
            )
            search_params += [pattern, pattern, pattern]
        clauses, params = list(search_clauses), list(search_params)
        if role:
            clauses.append("role = ?")
            params.append(role)
        if status in {"active", "disabled"}:
            clauses.append("disabled = ?")
            params.append(1 if status == "disabled" else 0)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        search_where = f"WHERE {' AND '.join(search_clauses)}" if search_clauses else ""
        order = self.USER_SORTS.get(sort, self.USER_SORTS["role"])
        with self.connect() as connection:
            total = int(
                connection.execute(f"SELECT COUNT(*) AS count FROM users {where}", params).fetchone()["count"]
            )
            rows = connection.execute(
                f"SELECT * FROM users {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            counts = {"all": 0, "admin": 0, "member": 0, "viewer": 0, "active": 0, "disabled": 0}
            for row in connection.execute(
                f"SELECT role, disabled, COUNT(*) AS count FROM users {search_where} GROUP BY role, disabled",
                search_params,
            ).fetchall():
                count = int(row["count"])
                counts["all"] += count
                counts[row["role"]] = counts.get(row["role"], 0) + count
                counts["disabled" if row["disabled"] else "active"] += count
        return [self._public_user(row) for row in rows], total, counts

    USER_FIELDS = {
        "display_name", "email", "role", "disabled", "preferences_json",
        "last_login_at", "updated_at", "password_hash",
    }

    def update_user(self, user_id: str, **changes: Any) -> dict[str, Any] | None:
        if not changes:
            return self.get_user(user_id, include_secret=False)
        unknown = set(changes) - self.USER_FIELDS
        if unknown:
            raise ValueError(f"Unknown user fields: {sorted(unknown)}")
        assignments = ", ".join(f"{key} = :{key}" for key in changes)
        with self._write_lock, self.connect() as connection:
            connection.execute(
                f"UPDATE users SET {assignments} WHERE id = :user_id",
                {**changes, "user_id": user_id},
            )
        return self.get_user(user_id, include_secret=False)

    def update_user_guarded(
        self, user_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update a user unless it would leave no active administrator."""
        with self._write_lock:
            current = self.get_user(user_id, include_secret=False)
            if not current:
                return None
            role = changes.get("role", current["role"])
            disabled = bool(changes.get("disabled", current["disabled"]))
            if current["role"] == "admin" and not current["disabled"] and (
                role != "admin" or disabled
            ):
                if self.count_active_admins() <= 1:
                    raise ValueError("At least one active administrator is required.")
            return self.update_user(user_id, **changes)

    def count_active_admins(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM users WHERE role = 'admin' AND disabled = 0"
            ).fetchone()
        return int(row["count"])

    def delete_user(self, user_id: str) -> None:
        with self._write_lock:
            user = self.get_user(user_id, include_secret=False)
            if user and user["role"] == "admin" and not user["disabled"]:
                if self.count_active_admins() <= 1:
                    raise ValueError("At least one active administrator is required.")
            with self.connect() as connection:
                connection.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def update_user_password(
        self, user_id: str, password_hash: str, updated_at: str
    ) -> None:
        self.update_user(user_id, password_hash=password_hash, updated_at=updated_at)

    def create_session(self, record: dict[str, Any]) -> None:
        record = {
            "id": uuid.uuid4().hex,
            "user_agent": None,
            "ip": None,
            "last_seen_at": record.get("created_at"),
            **record,
        }
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    token_hash, user_id, csrf_token, created_at, expires_at,
                    id, user_agent, ip, last_seen_at
                ) VALUES (
                    :token_hash, :user_id, :csrf_token, :created_at, :expires_at,
                    :id, :user_agent, :ip, :last_seen_at
                )
                """,
                record,
            )

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["user"] = self.get_user(result["user_id"], include_secret=False)
        return result if result["user"] else None

    def list_sessions(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def touch_session(self, token_hash: str, seen_at: str) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                (seen_at, token_hash),
            )

    def delete_session(self, token_hash: str) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_session_by_id(self, user_id: str, session_id: str) -> bool:
        with self._write_lock, self.connect() as connection:
            result = connection.execute(
                "DELETE FROM sessions WHERE user_id = ? AND id = ?", (user_id, session_id)
            )
            return bool(getattr(result, "rowcount", 1))

    def delete_other_sessions(self, user_id: str, keep_token_hash: str) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                (user_id, keep_token_hash),
            )

    def delete_user_sessions(self, user_id: str) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def create_api_token(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO api_tokens (
                    id, user_id, name, token_hash, prefix, scope, created_at, expires_at
                ) VALUES (
                    :id, :user_id, :name, :token_hash, :prefix, :scope, :created_at, :expires_at
                )
                """,
                record,
            )
        return next(token for token in self.list_api_tokens(record["user_id"]) if token["id"] == record["id"])

    def list_api_tokens(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, user_id, name, prefix, scope, created_at, last_used_at, expires_at "
                "FROM api_tokens WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_api_token_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM api_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return dict(row) if row else None

    def touch_api_token(self, token_id: str, used_at: str) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (used_at, token_id)
            )

    def delete_api_token(self, user_id: str, token_id: str) -> bool:
        with self._write_lock, self.connect() as connection:
            result = connection.execute(
                "DELETE FROM api_tokens WHERE user_id = ? AND id = ?", (user_id, token_id)
            )
            return bool(getattr(result, "rowcount", 1))

    def delete_user_tokens(self, user_id: str) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute("DELETE FROM api_tokens WHERE user_id = ?", (user_id,))

    def user_activity(self, user_ids: list[str] | None = None) -> dict[str, dict[str, int]]:
        """Counts shown next to each account on the admin page, optionally only
        for the accounts on the current page."""
        activity: dict[str, dict[str, int]] = {}
        if user_ids is not None and not user_ids:
            return activity
        only = f" WHERE {{column}} IN ({', '.join('?' for _ in user_ids)})" if user_ids else ""
        with self.connect() as connection:
            for key, table, column in (
                ("sessions", "sessions", "user_id"),
                ("tokens", "api_tokens", "user_id"),
                ("repositories", "owned_repositories", "owner_id"),
            ):
                query = (
                    f"SELECT {column} AS user_id, COUNT(*) AS count FROM {table}"
                    f"{only.format(column=column)} GROUP BY {column}"
                )
                for row in connection.execute(query, list(user_ids or [])).fetchall():
                    activity.setdefault(row["user_id"], {})[key] = int(row["count"])
        return activity

    def owned_repository_ids(self, owner_id: str) -> list[str]:
        """Personal repositories created by a user (organization ones are not theirs)."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT repo_id FROM owned_repositories "
                "WHERE owner_id = ? AND organization_id IS NULL ORDER BY repo_id",
                (owner_id,),
            ).fetchall()
        return [row["repo_id"] for row in rows]

    def create_download(self, record: dict[str, Any]) -> dict[str, Any]:
        params = {**record, "user_id": record.get("user_id")}
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO downloads (
                    id, repo_id, revision, status, total_bytes, downloaded_bytes,
                    progress, speed_bps, error, target_path, payload_json,
                    metadata_json, created_at, updated_at, completed_at, user_id
                ) VALUES (
                    :id, :repo_id, :revision, :status, :total_bytes, :downloaded_bytes,
                    :progress, :speed_bps, :error, :target_path, :payload_json,
                    :metadata_json, :created_at, :updated_at, :completed_at, :user_id
                )
                """,
                params,
            )
        return self.get_download(record["id"])

    def update_download(self, download_id: str, **changes: Any) -> dict[str, Any] | None:
        safe_changes = {key: value for key, value in changes.items() if key in DOWNLOAD_FIELDS}
        if not safe_changes:
            return self.get_download(download_id)
        assignments = ", ".join(f"{key} = :{key}" for key in safe_changes)
        safe_changes["id"] = download_id
        with self._write_lock, self.connect() as connection:
            connection.execute(f"UPDATE downloads SET {assignments} WHERE id = :id", safe_changes)
        return self.get_download(download_id)

    def get_download(self, download_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
        return self._decode_row(row)

    def list_downloads(
        self, limit: int = 100, user_id: str | None = None, include_unowned: bool = False
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if user_id is None:
                rows = connection.execute(
                    "SELECT * FROM downloads ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            elif include_unowned:
                rows = connection.execute(
                    """
                    SELECT * FROM downloads
                    WHERE user_id = ? OR user_id IS NULL
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM downloads
                    WHERE user_id = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
        return [self._decode_row(row) for row in rows]

    def find_active_download(self, repo_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM downloads
                WHERE repo_id = ? AND status IN ('queued', 'preparing', 'downloading')
                ORDER BY created_at DESC LIMIT 1
                """,
                (repo_id,),
            ).fetchone()
        return self._decode_row(row)

    def unfinished_downloads(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM downloads WHERE status IN ('queued', 'preparing', 'downloading')"
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def create_runtime_job(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_jobs (
                    id, target_id, target_name, target_kind, repo_id,
                    runtime_model_name, source_file, status, total_bytes,
                    processed_bytes, progress, message, error, created_at,
                    updated_at, completed_at, user_id
                ) VALUES (
                    :id, :target_id, :target_name, :target_kind, :repo_id,
                    :runtime_model_name, :source_file, :status, :total_bytes,
                    :processed_bytes, :progress, :message, :error, :created_at,
                    :updated_at, :completed_at, :user_id
                )
                """,
                record,
            )
        return self.get_runtime_job(record["id"])

    def update_runtime_job(
        self, job_id: str, **changes: Any
    ) -> dict[str, Any] | None:
        safe_changes = {
            key: value for key, value in changes.items() if key in RUNTIME_JOB_FIELDS
        }
        if not safe_changes:
            return self.get_runtime_job(job_id)
        assignments = ", ".join(f"{key} = :{key}" for key in safe_changes)
        safe_changes["id"] = job_id
        with self._write_lock, self.connect() as connection:
            connection.execute(
                f"UPDATE runtime_jobs SET {assignments} WHERE id = :id",
                safe_changes,
            )
        return self.get_runtime_job(job_id)

    def get_runtime_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._decode_row(row)

    def list_runtime_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def find_active_runtime_job(
        self, target_id: str, repo_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runtime_jobs
                WHERE target_id = ? AND repo_id = ?
                  AND status IN ('queued', 'preparing', 'transferring', 'loading')
                ORDER BY created_at DESC LIMIT 1
                """,
                (target_id, repo_id),
            ).fetchone()
        return self._decode_row(row)

    def find_active_runtime_target(
        self, target_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runtime_jobs
                WHERE target_id = ?
                  AND status IN ('queued', 'preparing', 'transferring', 'loading')
                ORDER BY created_at DESC LIMIT 1
                """,
                (target_id,),
            ).fetchone()
        return self._decode_row(row)

    def fail_unfinished_runtime_jobs(self, updated_at: str) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE runtime_jobs
                SET status = 'failed',
                    error = 'HuggingHack restarted before this runtime job completed.',
                    message = 'Interrupted',
                    updated_at = ?,
                    completed_at = ?
                WHERE status IN ('queued', 'preparing', 'transferring', 'loading')
                """,
                (updated_at, updated_at),
            )

    def upsert_local_model(self, record: dict[str, Any]) -> None:
        record = {
            "parameter_count": None,
            "formats_json": "[]",
            "storage_target": "s3" if record.get("storage_backend") == "s3" else "local",
            **record,
        }
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO local_models (
                    repo_id, relative_path, size_bytes, file_count, modified_at,
                    downloaded_at, revision, sha, pipeline_tag, library_name,
                    license, tags_json, config_json, source_url, managed,
                    storage_backend, cached, remote_uri, parameter_count, formats_json,
                    storage_target
                ) VALUES (
                    :repo_id, :relative_path, :size_bytes, :file_count, :modified_at,
                    :downloaded_at, :revision, :sha, :pipeline_tag, :library_name,
                    :license, :tags_json, :config_json, :source_url, :managed,
                    :storage_backend, :cached, :remote_uri, :parameter_count, :formats_json,
                    :storage_target
                )
                ON CONFLICT(repo_id) DO UPDATE SET
                    relative_path = excluded.relative_path,
                    size_bytes = excluded.size_bytes,
                    file_count = excluded.file_count,
                    modified_at = excluded.modified_at,
                    downloaded_at = excluded.downloaded_at,
                    revision = excluded.revision,
                    sha = excluded.sha,
                    pipeline_tag = excluded.pipeline_tag,
                    library_name = excluded.library_name,
                    license = excluded.license,
                    tags_json = excluded.tags_json,
                    config_json = excluded.config_json,
                    source_url = excluded.source_url,
                    managed = excluded.managed,
                    storage_backend = excluded.storage_backend,
                    cached = excluded.cached,
                    remote_uri = excluded.remote_uri,
                    parameter_count = excluded.parameter_count,
                    formats_json = excluded.formats_json,
                    storage_target = excluded.storage_target
                """,
                record,
            )

    def list_local_models(self, query: str = "") -> list[dict[str, Any]]:
        with self.connect() as connection:
            if query:
                rows = connection.execute(
                    """
                    SELECT * FROM local_models
                    WHERE LOWER(repo_id) LIKE LOWER(?)
                       OR LOWER(pipeline_tag) LIKE LOWER(?)
                       OR LOWER(library_name) LIKE LOWER(?)
                    ORDER BY modified_at DESC
                    """,
                    (f"%{query}%", f"%{query}%", f"%{query}%"),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM local_models ORDER BY modified_at DESC"
                ).fetchall()
        return [self._decode_row(row) for row in rows]

    def get_local_model(self, repo_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM local_models WHERE repo_id = ?", (repo_id,)
            ).fetchone()
        return self._decode_row(row)

    def prune_local_models(self, relative_paths: set[str]) -> None:
        with self._write_lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT relative_path FROM local_models WHERE storage_backend = 'filesystem'"
            ).fetchall()
            stale = [row["relative_path"] for row in rows if row["relative_path"] not in relative_paths]
            connection.executemany(
                "DELETE FROM local_models WHERE relative_path = ?",
                ((path,) for path in stale),
            )

    def prune_remote_models(self, storage_target: str, repo_ids: set[str]) -> None:
        """Drop uncached rows of a target whose repositories are gone from its bucket."""
        with self._write_lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT repo_id FROM local_models WHERE storage_target = ? AND cached = 0",
                (storage_target,),
            ).fetchall()
            connection.executemany(
                "DELETE FROM local_models WHERE repo_id = ?",
                ((row["repo_id"],) for row in rows if row["repo_id"] not in repo_ids),
            )

    def prune_unknown_targets(self, storage_targets: set[str]) -> None:
        """Drop uncached rows whose storage target is no longer configured."""
        with self._write_lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT repo_id, storage_target FROM local_models WHERE cached = 0"
            ).fetchall()
            connection.executemany(
                "DELETE FROM local_models WHERE repo_id = ?",
                (
                    (row["repo_id"],)
                    for row in rows
                    if row["storage_target"] not in storage_targets
                ),
            )

    def set_local_model_cached(self, repo_id: str, cached: bool) -> dict[str, Any] | None:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                "UPDATE local_models SET cached = ? WHERE repo_id = ?",
                (int(cached), repo_id),
            )
        return self.get_local_model(repo_id)

    def list_visible_local_models(
        self, user_id: str, query: str = ""
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = [user_id, user_id]
        query_clause = ""
        if query:
            query_clause = (
                " AND (LOWER(local_models.repo_id) LIKE LOWER(?) "
                "OR LOWER(local_models.pipeline_tag) LIKE LOWER(?) "
                "OR LOWER(local_models.library_name) LIKE LOWER(?))"
            )
            parameters.extend((f"%{query}%", f"%{query}%", f"%{query}%"))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT local_models.*
                FROM local_models
                LEFT JOIN owned_repositories
                    ON owned_repositories.repo_id = local_models.repo_id
                WHERE (
                    owned_repositories.id IS NULL
                    OR (owned_repositories.organization_id IS NULL
                        AND owned_repositories.owner_id = ?)
                    OR owned_repositories.visibility = 'shared'
                    OR owned_repositories.organization_id IN (
                        SELECT organization_id FROM organization_members WHERE user_id = ?
                    )
                )
                """
                + query_clause
                + " ORDER BY local_models.modified_at DESC",
                parameters,
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def get_visible_local_model(
        self, user_id: str, repo_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT local_models.*
                FROM local_models
                LEFT JOIN owned_repositories
                    ON owned_repositories.repo_id = local_models.repo_id
                WHERE local_models.repo_id = ?
                  AND (
                    owned_repositories.id IS NULL
                    OR (owned_repositories.organization_id IS NULL
                        AND owned_repositories.owner_id = ?)
                    OR owned_repositories.visibility = 'shared'
                    OR owned_repositories.organization_id IN (
                        SELECT organization_id FROM organization_members WHERE user_id = ?
                    )
                  )
                """,
                (repo_id, user_id, user_id),
            ).fetchone()
        return self._decode_row(row)

    def get_public_local_model(self, repo_id: str) -> dict[str, Any] | None:
        """Return a model anyone on the network may pull: never a private upload."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT local_models.*
                FROM local_models
                LEFT JOIN owned_repositories
                    ON owned_repositories.repo_id = local_models.repo_id
                WHERE local_models.repo_id = ?
                  AND (
                    owned_repositories.id IS NULL
                    OR owned_repositories.visibility = 'shared'
                  )
                """,
                (repo_id,),
            ).fetchone()
        return self._decode_row(row)

    @staticmethod
    def _decode_commit(row: Any, full: bool) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        snapshot = result.pop("snapshot_json", None)
        changes = json.loads(result.pop("changes_json", None) or "[]")
        result["summary"] = {
            kind: sum(1 for change in changes if change["change"] == kind)
            for kind in ("added", "modified", "deleted")
        }
        if full:
            result["changes"] = changes
            result["snapshot"] = json.loads(snapshot or "[]")
        return result

    def latest_commit(self, repo_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM repo_commits WHERE repo_id = ? ORDER BY sequence DESC LIMIT 1",
                (repo_id,),
            ).fetchone()
        return self._decode_commit(row, full=True)

    def create_commit(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO repo_commits (
                    id, repo_id, sequence, parent_id, author_id, author_name, message,
                    description, created_at, snapshot_json, changes_json
                ) VALUES (
                    :id, :repo_id, :sequence, :parent_id, :author_id, :author_name, :message,
                    :description, :created_at, :snapshot_json, :changes_json
                )
                """,
                record,
            )
        return self.get_commit(record["repo_id"], record["id"])

    def get_commit(self, repo_id: str, commit_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM repo_commits WHERE repo_id = ? AND id = ?",
                (repo_id, commit_id),
            ).fetchone()
        return self._decode_commit(row, full=True)

    def list_commits(
        self, repo_id: str, limit: int = 50, offset: int = 0, full: bool = False
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM repo_commits WHERE repo_id = ? "
                "ORDER BY sequence DESC LIMIT ? OFFSET ?",
                (repo_id, limit, offset),
            ).fetchall()
        return [self._decode_commit(row, full=full) for row in rows]

    def count_commits(self, repo_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM repo_commits WHERE repo_id = ?", (repo_id,)
            ).fetchone()
        return int(row["count"])

    def delete_commits(self, repo_id: str) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute("DELETE FROM repo_commits WHERE repo_id = ?", (repo_id,))

    def put_text_blob(self, sha256: str, content: str) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                "INSERT INTO text_blobs (sha256, content) VALUES (?, ?) "
                "ON CONFLICT(sha256) DO NOTHING",
                (sha256, content),
            )

    def get_text_blob(self, sha256: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT content FROM text_blobs WHERE sha256 = ?", (sha256,)
            ).fetchone()
        return row["content"] if row else None

    def delete_file_digests(self, repo_id: str) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute("DELETE FROM file_digests WHERE repo_id = ?", (repo_id,))

    def get_file_digest(self, repo_id: str, path: str, version: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT sha256 FROM file_digests WHERE repo_id = ? AND path = ? AND version = ?",
                (repo_id, path, version),
            ).fetchone()
        return row["sha256"] if row else None

    def set_file_digest(self, repo_id: str, path: str, version: str, sha256: str) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO file_digests (repo_id, path, version, sha256)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(repo_id, path) DO UPDATE SET
                    version = excluded.version,
                    sha256 = excluded.sha256
                """,
                (repo_id, path, version, sha256),
            )

    def create_collection(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO collections (
                    id, user_id, name, description, created_at, updated_at
                ) VALUES (
                    :id, :user_id, :name, :description, :created_at, :updated_at
                )
                """,
                record,
            )
        return self.get_collection(record["id"], record["user_id"])

    def get_collection(self, collection_id: str, user_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM collections WHERE id = ? AND user_id = ?",
                (collection_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def list_collections(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT collections.*, COUNT(collection_items.saved_model_id) AS model_count
                FROM collections
                LEFT JOIN collection_items
                    ON collection_items.collection_id = collections.id
                WHERE collections.user_id = ?
                GROUP BY collections.id
                ORDER BY LOWER(collections.name)
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_collection(self, collection_id: str, user_id: str) -> bool:
        with self._write_lock, self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM collections WHERE id = ? AND user_id = ?",
                (collection_id, user_id),
            )
        return cursor.rowcount > 0

    def save_model(self, record: dict[str, Any], collection_ids: list[str]) -> dict[str, Any]:
        collection_ids = list(dict.fromkeys(collection_ids))
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO saved_models (
                    id, user_id, repo_id, note, metadata_json, created_at, updated_at
                ) VALUES (
                    :id, :user_id, :repo_id, :note, :metadata_json, :created_at, :updated_at
                )
                ON CONFLICT(user_id, repo_id) DO UPDATE SET
                    note = excluded.note,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                record,
            )
            row = connection.execute(
                "SELECT id FROM saved_models WHERE user_id = ? AND repo_id = ?",
                (record["user_id"], record["repo_id"]),
            ).fetchone()
            saved_id = row["id"]
            connection.execute(
                "DELETE FROM collection_items WHERE saved_model_id = ?", (saved_id,)
            )
            if collection_ids:
                valid = connection.execute(
                    """
                    SELECT id FROM collections
                    WHERE user_id = ? AND id IN ({})
                    """.format(",".join("?" for _ in collection_ids)),
                    (record["user_id"], *collection_ids),
                ).fetchall()
                connection.executemany(
                    """
                    INSERT INTO collection_items (
                        collection_id, saved_model_id, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        (item["id"], saved_id, record["updated_at"])
                        for item in valid
                    ),
                )
        return self.get_saved_model(record["user_id"], record["repo_id"])

    def _saved_collections(
        self,
        connection: sqlite3.Connection | _PostgresConnection,
        saved_id: str,
    ) -> list[str]:
        rows = connection.execute(
            "SELECT collection_id FROM collection_items WHERE saved_model_id = ?",
            (saved_id,),
        ).fetchall()
        return [row["collection_id"] for row in rows]

    def get_saved_model(self, user_id: str, repo_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM saved_models WHERE user_id = ? AND repo_id = ?",
                (user_id, repo_id),
            ).fetchone()
            if not row:
                return None
            result = self._decode_row(row)
            result["collections"] = self._saved_collections(connection, result["id"])
        return result

    def list_saved_models(
        self, user_id: str, query: str = "", collection_id: str = ""
    ) -> list[dict[str, Any]]:
        joins = ""
        clauses = ["saved_models.user_id = ?"]
        parameters: list[Any] = [user_id]
        if collection_id:
            joins = (
                " JOIN collection_items ON collection_items.saved_model_id = saved_models.id"
            )
            clauses.append("collection_items.collection_id = ?")
            parameters.append(collection_id)
        if query:
            clauses.append(
                "(LOWER(saved_models.repo_id) LIKE LOWER(?) "
                "OR LOWER(saved_models.note) LIKE LOWER(?))"
            )
            parameters.extend((f"%{query}%", f"%{query}%"))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT saved_models.* FROM saved_models"
                + joins
                + " WHERE "
                + " AND ".join(clauses)
                + " ORDER BY saved_models.updated_at DESC",
                parameters,
            ).fetchall()
            results = []
            for row in rows:
                item = self._decode_row(row)
                item["collections"] = self._saved_collections(connection, item["id"])
                results.append(item)
        return results

    def saved_repo_ids(self, user_id: str) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT repo_id FROM saved_models WHERE user_id = ?", (user_id,)
            ).fetchall()
        return {row["repo_id"] for row in rows}

    def delete_saved_model(self, user_id: str, repo_id: str) -> bool:
        with self._write_lock, self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM saved_models WHERE user_id = ? AND repo_id = ?",
                (user_id, repo_id),
            )
        return cursor.rowcount > 0

    def create_owned_repository(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO owned_repositories (
                    id, owner_id, repo_id, description, visibility, status,
                    created_at, updated_at, organization_id
                ) VALUES (
                    :id, :owner_id, :repo_id, :description, :visibility, :status,
                    :created_at, :updated_at, :organization_id
                )
                """,
                {"organization_id": None, **record},
            )
        return self.get_owned_repository(record["repo_id"], record["owner_id"])

    def get_owned_repository(
        self, repo_id: str, owner_id: str | None = None
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            if owner_id:
                row = connection.execute(
                    """
                    SELECT owned_repositories.*, users.username AS owner_username,
                           users.display_name AS owner_display_name,
                           organizations.name AS organization_name
                    FROM owned_repositories
                    JOIN users ON users.id = owned_repositories.owner_id
                    LEFT JOIN organizations
                        ON organizations.id = owned_repositories.organization_id
                    WHERE repo_id = ? AND owner_id = ?
                    """,
                    (repo_id, owner_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT owned_repositories.*, users.username AS owner_username,
                           users.display_name AS owner_display_name,
                           organizations.name AS organization_name
                    FROM owned_repositories
                    JOIN users ON users.id = owned_repositories.owner_id
                    LEFT JOIN organizations
                        ON organizations.id = owned_repositories.organization_id
                    WHERE repo_id = ?
                    """,
                    (repo_id,),
                ).fetchone()
        return dict(row) if row else None

    def list_owned_repositories(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT owned_repositories.*, users.username AS owner_username,
                       users.display_name AS owner_display_name,
                       organizations.name AS organization_name,
                       local_models.size_bytes, local_models.file_count,
                       local_models.modified_at
                FROM owned_repositories
                JOIN users ON users.id = owned_repositories.owner_id
                LEFT JOIN organizations
                    ON organizations.id = owned_repositories.organization_id
                LEFT JOIN local_models ON local_models.repo_id = owned_repositories.repo_id
                WHERE (owned_repositories.organization_id IS NULL AND owner_id = ?)
                   OR visibility = 'shared'
                   OR owned_repositories.organization_id IN (
                       SELECT organization_id FROM organization_members WHERE user_id = ?
                   )
                ORDER BY owned_repositories.updated_at DESC
                """,
                (user_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_owned_repository(self, repo_id: str, **changes: Any) -> dict[str, Any] | None:
        """Update an uploaded repository. Callers check who may change it first."""
        allowed = {
            key: value
            for key, value in changes.items()
            if key in {"description", "visibility", "status", "updated_at", "owner_id"}
        }
        if allowed:
            assignments = ", ".join(f"{key} = :{key}" for key in allowed)
            with self._write_lock, self.connect() as connection:
                connection.execute(
                    f"UPDATE owned_repositories SET {assignments} WHERE repo_id = :repo_id",
                    {**allowed, "repo_id": repo_id},
                )
        return self.get_owned_repository(repo_id)

    def delete_owned_repository(self, repo_id: str) -> bool:
        with self._write_lock, self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM owned_repositories WHERE repo_id = ?", (repo_id,)
            )
            connection.execute("DELETE FROM local_models WHERE repo_id = ?", (repo_id,))
        return cursor.rowcount > 0

    # Organizations share one namespace with usernames.

    def create_organization(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._write_lock:
            if self.namespace_taken(record["name"]):
                raise ValueError("That name is already used by a user or organization.")
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO organizations (
                        id, name, display_name, description, created_at, updated_at
                    ) VALUES (
                        :id, :name, :display_name, :description, :created_at, :updated_at
                    )
                    """,
                    record,
                )
        return self.get_organization(record["name"])

    def get_organization(self, name: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM organizations WHERE LOWER(name) = LOWER(?)", (name,)
            ).fetchone()
        return dict(row) if row else None

    def list_organizations(self, user_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT organizations.*,
                    (SELECT COUNT(*) FROM organization_members
                        WHERE organization_id = organizations.id) AS member_count,
                    (SELECT COUNT(*) FROM owned_repositories
                        WHERE organization_id = organizations.id) AS repository_count,
                    (SELECT role FROM organization_members
                        WHERE organization_id = organizations.id AND user_id = ?) AS my_role
                FROM organizations
                ORDER BY LOWER(organizations.name)
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_organization(self, organization_id: str, **changes: Any) -> None:
        allowed = {
            key: value
            for key, value in changes.items()
            if key in {"display_name", "description", "updated_at"}
        }
        if not allowed:
            return
        assignments = ", ".join(f"{key} = :{key}" for key in allowed)
        with self._write_lock, self.connect() as connection:
            connection.execute(
                f"UPDATE organizations SET {assignments} WHERE id = :organization_id",
                {**allowed, "organization_id": organization_id},
            )

    def delete_organization(self, organization_id: str) -> None:
        with self._write_lock, self.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM owned_repositories WHERE organization_id = ?", (organization_id,)
            ).fetchone():
                raise ValueError("Delete or move this organization's repositories first.")
            connection.execute("DELETE FROM organizations WHERE id = ?", (organization_id,))

    def organization_members(self, organization_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT organization_members.role, organization_members.created_at AS joined_at,
                       users.id, users.username, users.display_name, users.role AS server_role,
                       users.disabled
                FROM organization_members
                JOIN users ON users.id = organization_members.user_id
                WHERE organization_members.organization_id = ?
                ORDER BY CASE organization_members.role
                    WHEN 'admin' THEN 0 WHEN 'write' THEN 1 ELSE 2 END, users.username
                """,
                (organization_id,),
            ).fetchall()
        return [{**dict(row), "disabled": bool(dict(row)["disabled"])} for row in rows]

    def organization_role(self, organization_id: str, user_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT role FROM organization_members WHERE organization_id = ? AND user_id = ?",
                (organization_id, user_id),
            ).fetchone()
        return row["role"] if row else None

    def _organization_admins(self, connection: Any, organization_id: str) -> int:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM organization_members "
            "WHERE organization_id = ? AND role = 'admin'",
            (organization_id,),
        ).fetchone()
        return int(row["count"])

    def set_organization_member(
        self, organization_id: str, user_id: str, role: str, created_at: str, *, force: bool = False
    ) -> None:
        """Add or change a member; the last organization admin stays unless `force`."""
        with self._write_lock, self.connect() as connection:
            current = connection.execute(
                "SELECT role FROM organization_members WHERE organization_id = ? AND user_id = ?",
                (organization_id, user_id),
            ).fetchone()
            if (
                current
                and current["role"] == "admin"
                and role != "admin"
                and not force
                and self._organization_admins(connection, organization_id) <= 1
            ):
                raise ValueError("An organization needs at least one admin.")
            if current:
                connection.execute(
                    "UPDATE organization_members SET role = ? "
                    "WHERE organization_id = ? AND user_id = ?",
                    (role, organization_id, user_id),
                )
            else:
                connection.execute(
                    "INSERT INTO organization_members (organization_id, user_id, role, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (organization_id, user_id, role, created_at),
                )

    def remove_organization_member(
        self, organization_id: str, user_id: str, *, force: bool = False
    ) -> bool:
        with self._write_lock, self.connect() as connection:
            current = connection.execute(
                "SELECT role FROM organization_members WHERE organization_id = ? AND user_id = ?",
                (organization_id, user_id),
            ).fetchone()
            if not current:
                return False
            if (
                current["role"] == "admin"
                and not force
                and self._organization_admins(connection, organization_id) <= 1
            ):
                raise ValueError("An organization needs at least one admin.")
            connection.execute(
                "DELETE FROM organization_members WHERE organization_id = ? AND user_id = ?",
                (organization_id, user_id),
            )
        return True

    def user_organizations(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT organizations.id, organizations.name, organizations.display_name,
                       organization_members.role
                FROM organization_members
                JOIN organizations ON organizations.id = organization_members.organization_id
                WHERE organization_members.user_id = ?
                ORDER BY LOWER(organizations.name)
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def reassign_organization_repositories(self, from_user: str, to_user: str) -> int:
        """Keep an organization's repositories when the account that created them is deleted."""
        with self._write_lock, self.connect() as connection:
            cursor = connection.execute(
                "UPDATE owned_repositories SET owner_id = ? "
                "WHERE owner_id = ? AND organization_id IS NOT NULL",
                (to_user, from_user),
            )
        return cursor.rowcount
