from __future__ import annotations

import json
import re
import select
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .permissions import ROLE_CAPABILITIES

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # SQLite remains usable when the optional adapter is absent.
    psycopg = None
    dict_row = None

try:
    from psycopg_pool import ConnectionPool
except ImportError:
    ConnectionPool = None


if psycopg is None:
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
else:
    INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)

# How long a request waits for a PostgreSQL connection before it is told the
# database is unreachable, and how soon a silent server is noticed.
POOL_TIMEOUT_SECONDS = 5.0
CONNECTION_OPTIONS = {
    "connect_timeout": 5,
    "keepalives": 1,
    "keepalives_idle": 10,
    "keepalives_interval": 5,
    "keepalives_count": 3,
    # Linux: give up on a query whose packets the server stops acknowledging.
    "tcp_user_timeout": 15000,
}


CHECK_TIMEOUT_SECONDS = 3.0


def check_connection(connection: Any) -> None:
    """The pool's check of an idle connection before handing it out, with a deadline.
    A server that stops answering without closing the connection (paused or frozen)
    would otherwise hold the request, and its worker thread, forever."""
    pgconn = connection.pgconn
    try:
        pgconn.send_query(b"")
        deadline = time.monotonic() + CHECK_TIMEOUT_SECONDS
        while True:
            pgconn.consume_input()
            if not pgconn.is_busy():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([pgconn.socket], [], [], remaining)[0]:
                raise psycopg.OperationalError("The database did not answer in time.")
        while (result := pgconn.get_result()) is not None:
            if result.status == psycopg.pq.ExecStatus.FATAL_ERROR:
                raise psycopg.OperationalError((result.error_message or b"").decode(errors="replace"))
    except Exception:
        # The pool replaces a closed connection instead of handing it out again.
        connection.close()
        raise


def database_unreachable(error: BaseException) -> bool:
    """Whether an error means PostgreSQL could not be reached, rather than that it
    refused a statement: no connection in time, a refused or dropped connection, or
    a server shutting down."""
    if psycopg is None or not isinstance(error, psycopg.OperationalError):
        return False
    sqlstate = getattr(error, "sqlstate", None)
    return sqlstate is None or sqlstate.startswith(("08", "57P"))


NAMED_PARAMETER_PATTERN = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


# How a model is listed, as people corrected it. Detected values stay in
# local_models, so rescans keep refreshing them underneath; the corrections are
# merged in whenever a model is read.
LISTING_FIELDS = (
    "pipeline_tag", "precision", "parameter_count", "library_name", "license", "tags",
    "base_model", "base_model_relation",
)
LISTED_MODEL = (
    "local_models.*, model_listing.overrides_json AS listing_json FROM local_models "
    "LEFT JOIN model_listing ON model_listing.repo_id = local_models.repo_id"
)


def _listing_value(model: dict[str, Any], field: str) -> Any:
    if field == "precision":
        return (model.get("config") or {}).get("precision")
    return model.get(field)


def _apply_listing(model: dict[str, Any], raw: str | None) -> None:
    """Merge listing corrections into a decoded model row, keeping what the files
    said under `detected`."""
    try:
        overrides = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        overrides = {}
    overrides = {
        key: value for key, value in (overrides if isinstance(overrides, dict) else {}).items()
        if key in LISTING_FIELDS
    }
    model["detected"] = {field: _listing_value(model, field) for field in LISTING_FIELDS}
    model["listing_overrides"] = overrides
    for field, value in overrides.items():
        if field == "precision":
            model["config"] = {**(model.get("config") or {}), "precision": value}
        else:
            model[field] = value


def _postgres_query(query: str, parameters: object = ()) -> str:
    if isinstance(parameters, Mapping):
        return NAMED_PARAMETER_PATTERN.sub(r"%(\1)s", query)
    return query.replace("?", "%s")


class _PostgresConnection:
    def __init__(self, lease: Any):
        # The pool's connection() context: entering it borrows a connection.
        self._lease = lease
        self._connection: Any = None

    def __enter__(self) -> _PostgresConnection:
        self._connection = self._lease.__enter__()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> object:
        try:
            return self._lease.__exit__(exc_type, exc_value, traceback)
        finally:
            self._connection = None

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
# Columns holding byte counts or parameter counts, which pass 2**31 for large models.
# SQLite integers are 64-bit already; PostgreSQL INTEGER is 32-bit, so older
# PostgreSQL databases have these widened to BIGINT at start.
BIG_NUMBER_COLUMNS = {
    "downloads": ("total_bytes", "downloaded_bytes"),
    "local_models": ("size_bytes", "parameter_count"),
    "runtime_jobs": ("total_bytes", "processed_bytes"),
    "storage_moves": ("total_bytes", "copied_bytes", "verified_bytes"),
}
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
                    external_subject TEXT,
                    avatar_updated_at TEXT
""".strip("\n")
LEGACY_USER_COLUMNS = (
    "id", "username", "display_name", "password_hash", "role", "created_at", "updated_at"
)
VISIBILITIES = ("private", "organization", "public")
OWNED_REPOSITORY_COLUMNS = """
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                    repo_id TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    visibility TEXT NOT NULL DEFAULT 'private'
                        CHECK (visibility IN ('private', 'organization', 'public')),
                    status TEXT NOT NULL DEFAULT 'uploading'
                        CHECK (status IN ('uploading', 'ready')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    organization_id TEXT REFERENCES organizations(id) ON DELETE RESTRICT
""".strip("\n")
_VISIBILITY_UPGRADE = (
    "CASE WHEN visibility = 'shared' THEN 'public' "
    "WHEN organization_id IS NOT NULL THEN 'organization' ELSE visibility END"
)
OWNED_REPOSITORY_FIELDS = (
    "id", "owner_id", "repo_id", "description", "visibility", "status",
    "created_at", "updated_at", "organization_id",
)
# An organization Admin or Write role acts as one only while the account is enabled
# and its server role may create repositories; a Viewer's organization roles only
# read (see effective_org_role). A WHERE fragment over `users`.
ORG_ROLE_ACTS = "users.disabled = 0 AND users.role IN ({})".format(
    ", ".join(f"'{role}'" for role, capabilities in ROLE_CAPABILITIES.items() if "repos.create" in capabilities)
)
# Who may see an uploaded repository, as a WHERE fragment over `owned_repositories`
# that takes the viewing user's id three times. Models that are not uploads are
# public; organization repositories are seen by every member, and private ones by
# members whose Admin or Write role acts (ORG_ROLE_ACTS); server administrators see
# every repository, as they manage them all.
VISIBLE_TO_USER = """(
                    owned_repositories.id IS NULL
                    OR owned_repositories.visibility = 'public'
                    OR (owned_repositories.organization_id IS NULL
                        AND owned_repositories.owner_id = ?)
                    OR owned_repositories.organization_id IN (
                        SELECT organization_members.organization_id FROM organization_members
                        JOIN users ON users.id = organization_members.user_id
                        WHERE organization_members.user_id = ?
                          AND (owned_repositories.visibility = 'organization'
                               OR (organization_members.role IN ('admin', 'write')
                                   AND """ + ORG_ROLE_ACTS + """))
                    )
                    OR EXISTS (
                        SELECT 1 FROM users
                        WHERE users.id = ? AND users.role = 'admin' AND users.disabled = 0
                    )
                )"""


class Database:
    def __init__(self, target: Path | str, pool_size: int = 10):
        value = str(target).strip()
        self.backend = (
            "postgresql"
            if value.startswith(("postgresql://", "postgres://"))
            else "sqlite"
        )
        self.path = Path(target) if self.backend == "sqlite" else None
        self._database_url = value if self.backend == "postgresql" else None
        self._write_lock = threading.RLock()
        self._pool_size = max(1, pool_size)
        self._pool: Any = None
        self._pool_lock = threading.Lock()
        self._lock_pool: Any = None
        self._named_locks: dict[str, threading.Lock] = {}
        self._named_locks_guard = threading.Lock()

    def _connection_pool(self) -> Any:
        """PostgreSQL connections, opened on first use and reused afterwards."""
        with self._pool_lock:
            if self._pool is None:
                if psycopg is None or dict_row is None or ConnectionPool is None:
                    raise RuntimeError(
                        "PostgreSQL support requires the 'psycopg[binary,pool]' dependency."
                    )
                self._pool = ConnectionPool(
                    self._database_url,
                    min_size=1,
                    max_size=self._pool_size,
                    kwargs={"row_factory": dict_row, **CONNECTION_OPTIONS},
                    timeout=POOL_TIMEOUT_SECONDS,
                    # A connection the server dropped (a restart) is replaced, not handed out.
                    check=check_connection,
                    name="hugginghack",
                    open=True,
                )
            return self._pool

    def _locks_pool(self) -> Any:
        """Connections that hold cluster locks, apart from the query pool, so work done
        under a lock can still take query connections."""
        with self._pool_lock:
            if self._lock_pool is None:
                if ConnectionPool is None:
                    raise RuntimeError(
                        "PostgreSQL support requires the 'psycopg[binary,pool]' dependency."
                    )
                self._lock_pool = ConnectionPool(
                    self._database_url,
                    min_size=0,
                    max_size=self._pool_size,
                    kwargs={"autocommit": True, **CONNECTION_OPTIONS},
                    timeout=POOL_TIMEOUT_SECONDS,
                    check=check_connection,
                    name="hugginghack-locks",
                    open=True,
                )
            return self._lock_pool

    @contextmanager
    def cluster_lock(self, key: str) -> Iterator[None]:
        """One holder at a time for `key`, among the threads of this process and, on
        PostgreSQL, every HuggingHack process sharing the database. Threads of one
        process queue on an in-process lock first, so only one of them waits on the
        database. The advisory lock belongs to its connection's session: if the
        process dies, PostgreSQL releases it."""
        with self._named_locks_guard:
            local = self._named_locks.setdefault(key, threading.Lock())
        with local:
            if self.backend != "postgresql":
                yield
                return
            with self._locks_pool().connection() as connection:
                connection.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
                try:
                    yield
                finally:
                    try:
                        connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
                    except psycopg.Error:
                        # A broken connection has already lost the lock with its session.
                        pass

    @contextmanager
    def _guarded(self, key: str = "accounts") -> Iterator[Any]:
        """One transaction that no other writer guarded by `key` runs alongside: in
        this process through the write lock, and on PostgreSQL through a
        transaction-scoped advisory lock taken on the same connection, so checks such
        as "the last administrator stays" hold across every process. It needs no
        extra connection and is released when the transaction ends."""
        with self._write_lock, self.connect() as connection:
            if self.backend == "postgresql":
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", (f"hugginghack:{key}",)
                )
            yield connection

    def close(self) -> None:
        """Close pooled PostgreSQL connections; the next query opens a new pool."""
        with self._pool_lock:
            pool, self._pool = self._pool, None
            lock_pool, self._lock_pool = self._lock_pool, None
        for each in (pool, lock_pool):
            if each is not None:
                each.close()

    def connect(self) -> sqlite3.Connection | _PostgresConnection:
        if self.backend == "postgresql":
            # Leaving the block commits, or rolls back after an exception, and
            # returns the connection to the pool.
            return _PostgresConnection(self._connection_pool().connection())
        assert self.path is not None
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        # Servers starting together would otherwise both add the same column.
        with self.cluster_lock("schema"):
            self._initialize()

    def _initialize(self) -> None:
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
                    total_bytes BIGINT NOT NULL DEFAULT 0,
                    downloaded_bytes BIGINT NOT NULL DEFAULT 0,
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
                    size_bytes BIGINT NOT NULL DEFAULT 0,
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
                    base_model TEXT,
                    base_model_relation TEXT,
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
                    total_bytes BIGINT NOT NULL DEFAULT 0,
                    processed_bytes BIGINT NOT NULL DEFAULT 0,
                    progress REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                    worker_id TEXT,
                    heartbeat_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
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
                    updated_at TEXT NOT NULL,
                    avatar_updated_at TEXT
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
{OWNED_REPOSITORY_COLUMNS}
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

                CREATE TABLE IF NOT EXISTS model_hardware (
                    repo_id TEXT NOT NULL,
                    hardware TEXT NOT NULL,
                    PRIMARY KEY(repo_id, hardware)
                );

                CREATE TABLE IF NOT EXISTS storage_grants (
                    target_id TEXT NOT NULL,
                    user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
                    organization_id TEXT REFERENCES organizations(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    CHECK ((user_id IS NULL) <> (organization_id IS NULL))
                );

                CREATE TABLE IF NOT EXISTS storage_moves (
                    id TEXT PRIMARY KEY,
                    repo_id TEXT NOT NULL,
                    source_target TEXT NOT NULL,
                    destination_target TEXT NOT NULL,
                    keep_local INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    total_bytes BIGINT NOT NULL DEFAULT 0,
                    copied_bytes BIGINT NOT NULL DEFAULT 0,
                    verified_bytes BIGINT NOT NULL DEFAULT 0,
                    file_count INTEGER NOT NULL DEFAULT 0,
                    active_reads INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    switched_at TEXT,
                    finished_at TEXT,
                    worker_id TEXT,
                    heartbeat_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_storage_moves_repo
                    ON storage_moves(repo_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS login_attempts (
                    attempt_key TEXT NOT NULL,
                    attempted_at BIGINT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_login_attempts_key
                    ON login_attempts(attempt_key, attempted_at);

                CREATE TABLE IF NOT EXISTS cluster_state (
                    name TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS revision_aliases (
                    repo_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    target TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(repo_id, alias)
                );

                CREATE TABLE IF NOT EXISTS model_listing (
                    repo_id TEXT PRIMARY KEY,
                    overrides_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT
                );

                CREATE TABLE IF NOT EXISTS config_revisions (
                    id TEXT PRIMARY KEY,
                    repo_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    parent_id TEXT,
                    message TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    author_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                    author_name TEXT NOT NULL DEFAULT '',
                    files_json TEXT NOT NULL DEFAULT '[]',
                    changes_json TEXT NOT NULL DEFAULT '[]',
                    results_json TEXT NOT NULL DEFAULT '{}',
                    results_updated_at TEXT,
                    results_updated_by TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(repo_id, sequence)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_storage_grants_user
                    ON storage_grants(target_id, user_id) WHERE user_id IS NOT NULL;

                CREATE UNIQUE INDEX IF NOT EXISTS idx_storage_grants_organization
                    ON storage_grants(target_id, organization_id)
                    WHERE organization_id IS NOT NULL;
                """.replace("{USERS_COLUMNS}", USERS_COLUMNS).replace(
                    "{OWNED_REPOSITORY_COLUMNS}", OWNED_REPOSITORY_COLUMNS
                )
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
            for column in ("base_model", "base_model_relation"):
                if column not in local_model_columns:
                    connection.execute(f"ALTER TABLE local_models ADD COLUMN {column} TEXT")
            # When a profile picture was last set; it versions the picture's URL.
            for table in ("users", "organizations"):
                if "avatar_updated_at" not in self._column_names(connection, table):
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN avatar_updated_at TEXT")
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
            # Which server runs a job or move, when it last said so, and whether
            # someone asked another server to stop it (docs/SCALING.md, phase 3).
            for table in ("runtime_jobs", "storage_moves"):
                existing = self._column_names(connection, table)
                for column in ("worker_id", "heartbeat_at"):
                    if column not in existing:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
                if "cancel_requested" not in existing:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
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
            if self.backend == "postgresql":
                self._widen_big_number_columns(connection)
        self._migrate_visibility()

    def _widen_big_number_columns(self, connection: _PostgresConnection) -> None:
        """Make byte and parameter counts BIGINT in PostgreSQL databases created
        when they were INTEGER, which overflows past 2 GB or 2 billion parameters."""
        for table, columns in BIG_NUMBER_COLUMNS.items():
            narrow = {
                row["name"]
                for row in connection.execute(
                    """
                    SELECT column_name AS name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema() AND table_name = ?
                      AND data_type IN ('integer', 'smallint')
                    """,
                    (table,),
                ).fetchall()
            }
            for column in columns:
                if column in narrow:
                    connection.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT")

    def _migrate_visibility(self) -> None:
        """Move uploads from private/shared to private/organization/public.

        Organization members could already see private organization repositories,
        so those become `organization`; `shared` was open to everyone, so `public`.
        """
        if self.backend == "sqlite":
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'owned_repositories'"
                ).fetchone()
            if not row or "'shared'" not in row["sql"]:
                return
            assert self.path is not None
            fields = ", ".join(OWNED_REPOSITORY_FIELDS)
            connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            try:
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        f"CREATE TABLE owned_repositories_new ({OWNED_REPOSITORY_COLUMNS})"
                    )
                    connection.execute(
                        f"INSERT INTO owned_repositories_new ({fields}) "
                        f"SELECT {fields.replace('visibility', _VISIBILITY_UPGRADE)} "
                        "FROM owned_repositories"
                    )
                    connection.execute("DROP TABLE owned_repositories")
                    connection.execute(
                        "ALTER TABLE owned_repositories_new RENAME TO owned_repositories"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_owned_repositories_owner "
                        "ON owned_repositories(owner_id, updated_at DESC)"
                    )
                    problems = connection.execute("PRAGMA foreign_key_check").fetchall()
                    if problems:
                        raise RuntimeError(
                            f"Visibility migration broke references: {problems[:5]}"
                        )
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.close()
            return
        with self._write_lock, self.connect() as connection:
            checks = connection.execute(
                """
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'owned_repositories'::regclass AND contype = 'c'
                  AND pg_get_constraintdef(oid) LIKE ?
                """,
                ("%shared%",),
            ).fetchall()
            if not checks:
                return
            for check in checks:
                connection.execute(
                    f'ALTER TABLE owned_repositories DROP CONSTRAINT "{check["conname"]}"'
                )
            connection.execute(
                f"UPDATE owned_repositories SET visibility = {_VISIBILITY_UPGRADE}"
            )
            connection.execute(
                "ALTER TABLE owned_repositories ADD CONSTRAINT owned_repositories_visibility_check "
                "CHECK (visibility IN ('private', 'organization', 'public'))"
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
        if "listing_json" in result:
            _apply_listing(result, result.pop("listing_json"))
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

    def ping(self) -> dict[str, Any]:
        """Whether a query runs right now, for the health check."""
        try:
            with self.connect() as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception as error:  # noqa: BLE001 - any failure is reported, not raised
            reason = (
                "The database is not reachable."
                if database_unreachable(error)
                else f"The database failed a test query ({error.__class__.__name__})."
            )
            return {"backend": self.backend, "connected": False, "error": reason}
        return {"backend": self.backend, "connected": True, "error": None}

    def count_users(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()
            return int(row["count"])

    def namespace_taken(self, name: str) -> bool:
        """True when a user or organization already uses this name, ignoring case."""
        with self.connect() as connection:
            return self._namespace_taken(connection, name)

    @staticmethod
    def _namespace_taken(connection: Any, name: str) -> bool:
        for table, column in (("users", "username"), ("organizations", "name")):
            row = connection.execute(
                f"SELECT 1 FROM {table} WHERE LOWER({column}) = LOWER(?)", (name,)
            ).fetchone()
            if row:
                return True
        return False

    def create_user(self, record: dict[str, Any], *, first: bool = False) -> dict[str, Any]:
        """Insert an account. With `first`, only while there is none (the owner)."""
        record = {
            "email": None,
            "auth_provider": "local",
            "external_subject": None,
            **record,
        }
        with self._guarded() as connection:
            if first and connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise ValueError("The owner account already exists.")
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
        unknown = set(changes) - self.USER_FIELDS
        if unknown:
            raise ValueError(f"Unknown user fields: {sorted(unknown)}")
        with self._guarded() as connection:
            current = connection.execute(
                "SELECT role, disabled FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not current:
                return None
            role = changes.get("role", current["role"])
            disabled = bool(changes.get("disabled", current["disabled"]))
            if current["role"] == "admin" and not current["disabled"] and (
                role != "admin" or disabled
            ):
                if self._active_admins(connection) <= 1:
                    raise ValueError("At least one active administrator is required.")
            if changes:
                assignments = ", ".join(f"{key} = :{key}" for key in changes)
                connection.execute(
                    f"UPDATE users SET {assignments} WHERE id = :user_id",
                    {**changes, "user_id": user_id},
                )
        return self.get_user(user_id, include_secret=False)

    @staticmethod
    def _active_admins(connection: Any) -> int:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM users WHERE role = 'admin' AND disabled = 0"
        ).fetchone()
        return int(row["count"])

    def count_active_admins(self) -> int:
        with self.connect() as connection:
            return self._active_admins(connection)

    def delete_user(self, user_id: str) -> None:
        with self._guarded() as connection:
            user = connection.execute(
                "SELECT role, disabled FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if user and user["role"] == "admin" and not user["disabled"]:
                if self._active_admins(connection) <= 1:
                    raise ValueError("At least one active administrator is required.")
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

    def delete_user_tokens(self, user_id: str) -> int:
        """Revoke every API token of a user; returns how many there were."""
        with self._write_lock, self.connect() as connection:
            cursor = connection.execute("DELETE FROM api_tokens WHERE user_id = ?", (user_id,))
        return max(0, int(cursor.rowcount or 0))

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
        record = {"worker_id": None, "heartbeat_at": None, **record}
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_jobs (
                    id, target_id, target_name, target_kind, repo_id,
                    runtime_model_name, source_file, status, total_bytes,
                    processed_bytes, progress, message, error, created_at,
                    updated_at, completed_at, user_id, worker_id, heartbeat_at
                ) VALUES (
                    :id, :target_id, :target_name, :target_kind, :repo_id,
                    :runtime_model_name, :source_file, :status, :total_bytes,
                    :processed_bytes, :progress, :message, :error, :created_at,
                    :updated_at, :completed_at, :user_id, :worker_id, :heartbeat_at
                )
                """,
                record,
            )
        return self.get_runtime_job(record["id"])

    def update_runtime_job(
        self, job_id: str, *, unless_cancelled: bool = False, **changes: Any
    ) -> dict[str, Any] | None:
        """Change a job. With `unless_cancelled`, a job someone cancelled (perhaps
        through another server) keeps its cancelled state; the worker's progress
        is dropped instead."""
        safe_changes = {
            key: value for key, value in changes.items() if key in RUNTIME_JOB_FIELDS
        }
        if not safe_changes:
            return self.get_runtime_job(job_id)
        assignments = ", ".join(f"{key} = :{key}" for key in safe_changes)
        safe_changes["id"] = job_id
        guard = " AND status <> 'cancelled'" if unless_cancelled else ""
        with self._write_lock, self.connect() as connection:
            connection.execute(
                f"UPDATE runtime_jobs SET {assignments} WHERE id = :id{guard}",
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

    def find_active_runtime_job_for_repo(self, repo_id: str) -> dict[str, Any] | None:
        """Any runtime job still reading this repository's files."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runtime_jobs
                WHERE repo_id = ? AND status IN ('queued', 'preparing', 'transferring', 'loading')
                ORDER BY created_at DESC LIMIT 1
                """,
                (repo_id,),
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

    def fail_unfinished_runtime_jobs(self, updated_at: str, worker_id: str | None = None) -> None:
        """After a restart: jobs still marked as running cannot be. With `worker_id`,
        only that server's own jobs (other servers may still be running theirs)."""
        mine = " AND worker_id = ?" if worker_id is not None else ""
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
                """ + mine,
                (updated_at, updated_at, *((worker_id,) if worker_id is not None else ())),
            )

    def upsert_local_model(self, record: dict[str, Any]) -> None:
        record = {
            "parameter_count": None,
            "formats_json": "[]",
            "base_model": None,
            "base_model_relation": None,
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
                    storage_target, base_model, base_model_relation
                ) VALUES (
                    :repo_id, :relative_path, :size_bytes, :file_count, :modified_at,
                    :downloaded_at, :revision, :sha, :pipeline_tag, :library_name,
                    :license, :tags_json, :config_json, :source_url, :managed,
                    :storage_backend, :cached, :remote_uri, :parameter_count, :formats_json,
                    :storage_target, :base_model, :base_model_relation
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
                    storage_target = excluded.storage_target,
                    base_model = excluded.base_model,
                    base_model_relation = excluded.base_model_relation
                """,
                record,
            )

    def list_local_models(self, query: str = "") -> list[dict[str, Any]]:
        with self.connect() as connection:
            if query:
                rows = connection.execute(
                    """
                    SELECT local_models.*, model_listing.overrides_json AS listing_json
                    FROM local_models LEFT JOIN model_listing ON model_listing.repo_id = local_models.repo_id
                    WHERE LOWER(local_models.repo_id) LIKE LOWER(?)
                       OR LOWER(local_models.pipeline_tag) LIKE LOWER(?)
                       OR LOWER(local_models.library_name) LIKE LOWER(?)
                    ORDER BY local_models.modified_at DESC
                    """,
                    (f"%{query}%", f"%{query}%", f"%{query}%"),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT {LISTED_MODEL} ORDER BY local_models.modified_at DESC"
                ).fetchall()
        return [self._decode_row(row) for row in rows]

    def get_local_model(self, repo_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT {LISTED_MODEL} WHERE local_models.repo_id = ?", (repo_id,)
            ).fetchone()
        return self._decode_row(row)

    def repository_id_taken(self, repo_id: str, ignore: str | None = None) -> bool:
        """Whether a repository id is in use, ignoring case: on a case-insensitive
        disk the two would share a folder, and to people they read as one name."""
        with self.connect() as connection:
            for table in ("owned_repositories", "local_models"):
                rows = connection.execute(
                    f"SELECT repo_id FROM {table} WHERE LOWER(repo_id) = LOWER(?)", (repo_id,)
                ).fetchall()
                if any(row["repo_id"] != ignore for row in rows):
                    return True
        return False

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
        parameters: list[Any] = [user_id, user_id, user_id]
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
                SELECT local_models.*, model_listing.overrides_json AS listing_json
                FROM local_models
                LEFT JOIN model_listing ON model_listing.repo_id = local_models.repo_id
                LEFT JOIN owned_repositories
                    ON owned_repositories.repo_id = local_models.repo_id
                WHERE """ + VISIBLE_TO_USER + """
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
                SELECT local_models.*, model_listing.overrides_json AS listing_json
                FROM local_models
                LEFT JOIN model_listing ON model_listing.repo_id = local_models.repo_id
                LEFT JOIN owned_repositories
                    ON owned_repositories.repo_id = local_models.repo_id
                WHERE local_models.repo_id = ?
                  AND """ + VISIBLE_TO_USER + """
                """,
                (repo_id, user_id, user_id, user_id),
            ).fetchone()
        return self._decode_row(row)

    def get_public_local_model(self, repo_id: str) -> dict[str, Any] | None:
        """Return a model anyone on the network may pull: never a private upload."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT local_models.*, model_listing.overrides_json AS listing_json
                FROM local_models
                LEFT JOIN model_listing ON model_listing.repo_id = local_models.repo_id
                LEFT JOIN owned_repositories
                    ON owned_repositories.repo_id = local_models.repo_id
                WHERE local_models.repo_id = ?
                  AND (
                    owned_repositories.id IS NULL
                    OR owned_repositories.visibility = 'public'
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
                """,
                (user_id,),
            ).fetchall()
        # Sorted here: SQLite's LOWER() folds only ASCII, so "Ü" and "ü" would
        # order differently than on PostgreSQL.
        collections = [dict(row) for row in rows]
        collections.sort(key=lambda item: (item["name"].casefold(), item["name"], item["id"]))
        return collections

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
        """Uploaded repositories this user may write to: their own and their
        organizations' where they are an admin or writer."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT owned_repositories.*, users.username AS owner_username,
                       users.display_name AS owner_display_name,
                       organizations.name AS organization_name,
                       local_models.size_bytes, local_models.file_count,
                       local_models.modified_at,
                       local_models.repo_id IS NOT NULL AS indexed
                FROM owned_repositories
                JOIN users ON users.id = owned_repositories.owner_id
                LEFT JOIN organizations
                    ON organizations.id = owned_repositories.organization_id
                LEFT JOIN local_models ON local_models.repo_id = owned_repositories.repo_id
                WHERE (owned_repositories.organization_id IS NULL AND owner_id = ?)
                   OR owned_repositories.organization_id IN (
                       SELECT organization_id FROM organization_members
                       WHERE user_id = ? AND role IN ('admin', 'write')
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
            connection.execute("DELETE FROM model_hardware WHERE repo_id = ?", (repo_id,))
            connection.execute("DELETE FROM model_listing WHERE repo_id = ?", (repo_id,))
        return cursor.rowcount > 0

    # Hardware tags are kept apart from local_models so rescans never drop them.

    def model_hardware(self, repo_ids: list[str] | None = None) -> dict[str, list[str]]:
        query = "SELECT repo_id, hardware FROM model_hardware"
        params: tuple[Any, ...] = ()
        if repo_ids is not None:
            if not repo_ids:
                return {}
            query += f" WHERE repo_id IN ({', '.join('?' for _ in repo_ids)})"
            params = tuple(repo_ids)
        tags: dict[str, list[str]] = {}
        with self.connect() as connection:
            for row in connection.execute(query + " ORDER BY hardware", params).fetchall():
                tags.setdefault(row["repo_id"], []).append(row["hardware"])
        return tags

    def set_model_hardware(self, repo_id: str, hardware: list[str]) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute("DELETE FROM model_hardware WHERE repo_id = ?", (repo_id,))
            connection.executemany(
                "INSERT INTO model_hardware (repo_id, hardware) VALUES (?, ?)",
                [(repo_id, item) for item in hardware],
            )

    # Storage moves: a job per move, kept as history like downloads.
    MOVE_FIELDS = (
        "status", "message", "error", "total_bytes", "copied_bytes", "verified_bytes",
        "file_count", "active_reads", "updated_at", "switched_at", "finished_at",
    )

    def create_move(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO storage_moves (
                    id, repo_id, source_target, destination_target, keep_local, status,
                    message, created_by, created_at, updated_at
                ) VALUES (
                    :id, :repo_id, :source_target, :destination_target, :keep_local, :status,
                    :message, :created_by, :created_at, :updated_at
                )
                """,
                record,
            )
        return self.get_move(record["id"])

    def update_move(self, move_id: str, **changes: Any) -> dict[str, Any] | None:
        fields = [key for key in changes if key in self.MOVE_FIELDS]
        if fields:
            with self._write_lock, self.connect() as connection:
                connection.execute(
                    f"UPDATE storage_moves SET {', '.join(f'{key} = ?' for key in fields)} WHERE id = ?",
                    (*(changes[key] for key in fields), move_id),
                )
        return self.get_move(move_id)

    def get_move(self, move_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM storage_moves WHERE id = ?", (move_id,)).fetchone()
        return self._decode_move(row)

    def list_moves(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM storage_moves ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._decode_move(row) for row in rows]

    def unfinished_moves(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM storage_moves WHERE status NOT IN ('done', 'failed', 'cancelled') "
                "ORDER BY created_at"
            ).fetchall()
        return [self._decode_move(row) for row in rows]

    @staticmethod
    def _decode_move(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        move = dict(row)
        move["keep_local"] = bool(move["keep_local"])
        if "cancel_requested" in move:
            move["cancel_requested"] = bool(move["cancel_requested"])
        return move

    # Several servers (docs/SCALING.md, phase 3): each job and move names the server
    # running it, which says so every few seconds; another server's cancel is a flag
    # the running one picks up; work whose server went quiet is taken over.

    def claim_move(self, move_id: str, worker_id: str, now: str, stale_before: str) -> bool:
        """Take a move for this server, unless another server is running it."""
        with self._write_lock, self.connect() as connection:
            cursor = connection.execute(
                "UPDATE storage_moves SET worker_id = ?, heartbeat_at = ? WHERE id = ? "
                "AND (worker_id IS NULL OR worker_id = ? OR heartbeat_at IS NULL OR heartbeat_at < ?)",
                (worker_id, now, move_id, worker_id, stale_before),
            )
            return cursor.rowcount > 0

    def heartbeat(self, worker_id: str, now: str) -> None:
        """This server is still running its unfinished jobs and moves."""
        with self.connect() as connection:
            connection.execute(
                "UPDATE runtime_jobs SET heartbeat_at = ? WHERE worker_id = ? "
                "AND status IN ('queued', 'preparing', 'transferring', 'loading')",
                (now, worker_id),
            )
            connection.execute(
                "UPDATE storage_moves SET heartbeat_at = ? WHERE worker_id = ? "
                "AND status NOT IN ('done', 'failed', 'cancelled')",
                (now, worker_id),
            )

    def request_runtime_cancel(self, job_id: str, updated_at: str) -> dict[str, Any] | None:
        """Mark an active job cancelled, and ask its server to stop sending it."""
        with self._write_lock, self.connect() as connection:
            connection.execute(
                "UPDATE runtime_jobs SET status = 'cancelled', message = 'Cancelled', error = NULL, "
                "updated_at = ?, completed_at = ?, cancel_requested = 1 WHERE id = ? "
                "AND status IN ('queued', 'preparing', 'transferring', 'loading')",
                (updated_at, updated_at, job_id),
            )
        return self.get_runtime_job(job_id)

    def runtime_cancel_requests(self, worker_id: str) -> list[str]:
        """This server's jobs that someone cancelled, perhaps through another server."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM runtime_jobs WHERE worker_id = ? AND cancel_requested = 1",
                (worker_id,),
            ).fetchall()
        return [row["id"] for row in rows]

    def clear_runtime_cancel(self, job_id: str) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE runtime_jobs SET cancel_requested = 0 WHERE id = ?", (job_id,))

    def fail_stale_runtime_jobs(self, stale_before: str, updated_at: str) -> int:
        """Jobs whose server stopped saying it runs them; they cannot finish now."""
        with self._write_lock, self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE runtime_jobs
                SET status = 'failed',
                    error = 'The HuggingHack server running this job stopped before it finished. Load the model again.',
                    message = 'Interrupted',
                    updated_at = ?,
                    completed_at = ?
                WHERE status IN ('queued', 'preparing', 'transferring', 'loading')
                  AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                """,
                (updated_at, updated_at, stale_before),
            )
            return max(0, cursor.rowcount)

    def request_move_cancel(self, move_id: str) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                "UPDATE storage_moves SET cancel_requested = 1 WHERE id = ?", (move_id,)
            )

    def move_cancel_requests(self, worker_id: str) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM storage_moves WHERE worker_id = ? AND cancel_requested = 1 "
                "AND status IN ('copying', 'verifying')",
                (worker_id,),
            ).fetchall()
        return [row["id"] for row in rows]

    # Failed sign-ins, shared by every server (AuthService, in cluster mode).

    def count_login_failures(self, key: str, since_ms: int) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM login_attempts WHERE attempt_key = ? AND attempted_at >= ?",
                (key, since_ms),
            ).fetchone()
        return int(row["count"])

    def add_login_failures(self, keys: Sequence[str], now_ms: int, forget_before_ms: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (forget_before_ms,))
            for key in keys:
                connection.execute(
                    "INSERT INTO login_attempts (attempt_key, attempted_at) VALUES (?, ?)",
                    (key, now_ms),
                )

    def clear_login_failures(self, key: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM login_attempts WHERE attempt_key = ?", (key,))

    # What the last library scan found, for every server to show.

    def set_cluster_state(self, name: str, value: Any, updated_at: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO cluster_state (name, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET value_json = excluded.value_json, "
                "updated_at = excluded.updated_at",
                (name, json.dumps(value), updated_at),
            )

    def get_cluster_state(self, name: str) -> Any:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM cluster_state WHERE name = ?", (name,)
            ).fetchone()
        return json.loads(row["value_json"]) if row else None

    def open_session(self) -> Any:
        """A PostgreSQL connection of its own, outside the pools, for a session-level
        lock held as long as the process lives (the cluster's leader lock)."""
        if psycopg is None:
            raise RuntimeError("PostgreSQL support requires the 'psycopg[binary,pool]' dependency.")
        return psycopg.connect(self._database_url, autocommit=True, **CONNECTION_OPTIONS)

    def set_local_model_location(
        self, repo_id: str, storage_backend: str, storage_target: str, remote_uri: str | None, cached: bool
    ) -> None:
        """Point an indexed model at the storage location that now holds it."""
        with self._write_lock, self.connect() as connection:
            connection.execute(
                "UPDATE local_models SET storage_backend = ?, storage_target = ?, remote_uri = ?, "
                "cached = ? WHERE repo_id = ?",
                (storage_backend, storage_target, remote_uri, int(cached), repo_id),
            )

    def relabel_latest_commit(self, repo_id: str, versions: dict[str, tuple[int, str]]) -> None:
        """Give the latest commit's files the versions they have in a new location.
        Only files of the same size are relabeled; the content was checked equal."""
        latest = self.latest_commit(repo_id)
        if not latest:
            return
        snapshot = latest.get("snapshot") or []
        for item in snapshot:
            known = versions.get(item.get("path"))
            if known and known[0] == item.get("size"):
                item["version"] = known[1]
        with self._write_lock, self.connect() as connection:
            connection.execute(
                "UPDATE repo_commits SET snapshot_json = ? WHERE id = ?",
                (json.dumps(snapshot), latest["id"]),
            )

    def revision_alias_target(self, repo_id: str, alias: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT target FROM revision_aliases WHERE repo_id = ? AND alias = ?", (repo_id, alias)
            ).fetchone()
        return row["target"] if row else None

    def add_revision_alias(self, repo_id: str, alias: str, target: str, created_at: str) -> None:
        """Let `alias` name the content now at `target`; older names of the alias's
        content follow it to the new target."""
        if alias == target:
            return
        with self._write_lock, self.connect() as connection:
            connection.execute(
                "UPDATE revision_aliases SET target = ? WHERE repo_id = ? AND target = ?",
                (target, repo_id, alias),
            )
            connection.execute("DELETE FROM revision_aliases WHERE repo_id = ? AND alias = ?", (repo_id, alias))
            connection.execute(
                "INSERT INTO revision_aliases (repo_id, alias, target, created_at) VALUES (?, ?, ?, ?)",
                (repo_id, alias, target, created_at),
            )

    # Profile pictures: the file lives on disk; the timestamp says there is one.
    def set_avatar(self, kind: str, owner_id: str, updated_at: str | None) -> None:
        table = {"user": "users", "organization": "organizations"}[kind]
        with self._write_lock, self.connect() as connection:
            connection.execute(f"UPDATE {table} SET avatar_updated_at = ? WHERE id = ?", (updated_at, owner_id))

    def avatar_owner(self, namespace: str) -> tuple[str, str, str] | None:
        """(kind, id, version) of the user or organization named `namespace`, if it
        has a picture. Users and organizations share one namespace."""
        with self.connect() as connection:
            for kind, table, column in (("user", "users", "username"), ("organization", "organizations", "name")):
                row = connection.execute(
                    f"SELECT id, avatar_updated_at FROM {table} WHERE LOWER({column}) = LOWER(?)", (namespace,)
                ).fetchone()
                if row:
                    return (kind, row["id"], row["avatar_updated_at"]) if row["avatar_updated_at"] else None
        return None

    def avatar_versions(self) -> dict[str, str]:
        """Every namespace with a picture, lowercased, and its version: one query for
        a whole page of model cards."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT LOWER(username) AS name, avatar_updated_at AS version FROM users "
                "WHERE avatar_updated_at IS NOT NULL "
                "UNION ALL SELECT LOWER(name) AS name, avatar_updated_at AS version FROM organizations "
                "WHERE avatar_updated_at IS NOT NULL"
            ).fetchall()
        return {row["name"]: row["version"] for row in rows}

    def listing_overrides(self, repo_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT overrides_json FROM model_listing WHERE repo_id = ?", (repo_id,)
            ).fetchone()
        try:
            overrides = json.loads(row["overrides_json"]) if row else {}
        except (TypeError, json.JSONDecodeError):
            overrides = {}
        return overrides if isinstance(overrides, dict) else {}

    def set_listing_overrides(
        self, repo_id: str, overrides: dict[str, Any], updated_at: str, updated_by: str | None
    ) -> None:
        """Replace what people changed about how a model is listed; empty clears it."""
        with self._write_lock, self.connect() as connection:
            connection.execute("DELETE FROM model_listing WHERE repo_id = ?", (repo_id,))
            if overrides:
                connection.execute(
                    "INSERT INTO model_listing (repo_id, overrides_json, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?)",
                    (repo_id, json.dumps(overrides, sort_keys=True), updated_at, updated_by),
                )

    # Tables whose rows belong to a repository and follow it when it is renamed.
    # `downloads` and `runtime_jobs` are history and keep the name they ran under.
    RENAMED_WITH_REPOSITORY = (
        "repo_commits", "file_digests", "model_hardware", "config_revisions",
        "model_listing", "revision_aliases",
    )

    def rename_repository(
        self, old: str, new: str, ownership: dict[str, Any] | None = None
    ) -> None:
        """Move every row of a repository to its new name in one transaction.

        `ownership` sets the owner, organization, and visibility of an uploaded
        repository; with an `id` it registers a downloaded model as owned.
        """
        with self._write_lock, self.connect() as connection:
            for table in (*self.RENAMED_WITH_REPOSITORY, "saved_models"):
                # Leftovers of an earlier repository with the new name must not merge in.
                connection.execute(f"DELETE FROM {table} WHERE repo_id = ?", (new,))
            for table in self.RENAMED_WITH_REPOSITORY:
                connection.execute(
                    f"UPDATE {table} SET repo_id = ? WHERE repo_id = ?", (new, old)
                )
            connection.execute(
                "UPDATE local_models SET repo_id = ?, relative_path = ? WHERE repo_id = ?",
                (new, new, old),
            )
            if ownership and "id" in ownership:
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
                    {**ownership, "repo_id": new},
                )
            elif ownership:
                connection.execute(
                    """
                    UPDATE owned_repositories
                    SET repo_id = :new, owner_id = :owner_id, organization_id = :organization_id,
                        visibility = :visibility, updated_at = :updated_at
                    WHERE repo_id = :old
                    """,
                    {**ownership, "new": new, "old": old},
                )
            else:
                connection.execute(
                    "UPDATE owned_repositories SET repo_id = ? WHERE repo_id = ?", (new, old)
                )
            # Saves follow the repository only for those who may still see it under
            # its new owner; the others keep the old name, as after a deletion, so
            # a private new name is never shown to them.
            connection.execute(
                """
                UPDATE saved_models SET repo_id = ?
                WHERE repo_id = ? AND EXISTS (
                    SELECT 1 FROM local_models
                    LEFT JOIN owned_repositories
                        ON owned_repositories.repo_id = local_models.repo_id
                    WHERE local_models.repo_id = ?
                      AND """ + VISIBLE_TO_USER.replace("?", "saved_models.user_id") + """
                )
                """,
                (new, old, new),
            )

    # Deployment config revisions: linear per repository, numbered from 1.

    @staticmethod
    def _decode_config(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["files"] = json.loads(result.pop("files_json") or "[]")
        result["changes"] = json.loads(result.pop("changes_json") or "[]")
        result["results"] = json.loads(result.pop("results_json") or "{}")
        return result

    def create_config_revision(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._write_lock, self.connect() as connection:
            row = connection.execute(
                "SELECT id, sequence FROM config_revisions WHERE repo_id = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (record["repo_id"],),
            ).fetchone()
            if (row["id"] if row else None) != record["parent_id"]:
                raise RuntimeError("Someone added a revision since you started. Reload and try again.")
            connection.execute(
                """
                INSERT INTO config_revisions (
                    id, repo_id, sequence, parent_id, message, description, author_id,
                    author_name, files_json, changes_json, results_json,
                    results_updated_at, results_updated_by, created_at
                ) VALUES (
                    :id, :repo_id, :sequence, :parent_id, :message, :description, :author_id,
                    :author_name, :files_json, :changes_json, :results_json,
                    :results_updated_at, :results_updated_by, :created_at
                )
                """,
                {**record, "sequence": (int(row["sequence"]) if row else 0) + 1},
            )
        return self.get_config_revision(record["repo_id"], record["id"])

    def get_config_revision(self, repo_id: str, revision_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM config_revisions WHERE repo_id = ? AND id = ?",
                (repo_id, revision_id),
            ).fetchone()
        return self._decode_config(row)

    def latest_config_revision(self, repo_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM config_revisions WHERE repo_id = ? ORDER BY sequence DESC LIMIT 1",
                (repo_id,),
            ).fetchone()
        return self._decode_config(row)

    def list_config_revisions(self, repo_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM config_revisions WHERE repo_id = ? ORDER BY sequence DESC",
                (repo_id,),
            ).fetchall()
        return [self._decode_config(row) for row in rows]

    def count_config_revisions(self, repo_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM config_revisions WHERE repo_id = ?", (repo_id,)
            ).fetchone()
        return int(row["count"])

    def update_config_results(
        self, repo_id: str, revision_id: str, results_json: str, updated_at: str, updated_by: str
    ) -> dict[str, Any] | None:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                "UPDATE config_revisions SET results_json = ?, results_updated_at = ?, "
                "results_updated_by = ? WHERE repo_id = ? AND id = ?",
                (results_json, updated_at, updated_by, repo_id, revision_id),
            )
        return self.get_config_revision(repo_id, revision_id)

    def delete_config_revisions(self, repo_id: str) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute("DELETE FROM config_revisions WHERE repo_id = ?", (repo_id,))

    # A storage target with grants only takes repositories of the granted users and
    # organizations; one without grants is open to every uploader.

    def storage_grants(self) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT storage_grants.target_id,
                       CASE WHEN storage_grants.user_id IS NULL
                            THEN 'organization' ELSE 'user' END AS kind,
                       COALESCE(storage_grants.user_id, storage_grants.organization_id) AS id,
                       COALESCE(users.username, organizations.name) AS name,
                       COALESCE(users.display_name, organizations.display_name) AS display_name
                FROM storage_grants
                LEFT JOIN users ON users.id = storage_grants.user_id
                LEFT JOIN organizations ON organizations.id = storage_grants.organization_id
                ORDER BY kind, LOWER(COALESCE(users.username, organizations.name))
                """
            ).fetchall()
        grants: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            grants.setdefault(item.pop("target_id"), []).append(item)
        return grants

    def set_storage_grants(
        self,
        target_id: str,
        user_ids: Iterable[str],
        organization_ids: Iterable[str],
        created_at: str,
    ) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute("DELETE FROM storage_grants WHERE target_id = ?", (target_id,))
            connection.executemany(
                "INSERT INTO storage_grants (target_id, user_id, organization_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                [(target_id, user_id, None, created_at) for user_id in user_ids]
                + [(target_id, None, org_id, created_at) for org_id in organization_ids],
            )

    # Organizations share one namespace with usernames.

    def create_organization(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._guarded() as connection:
            if self._namespace_taken(connection, record["name"]):
                raise ValueError("That name is already used by a user or organization.")
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
        """Every organization, counting only the repositories `user_id` may see."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT organizations.*,
                    (SELECT COUNT(*) FROM organization_members
                        WHERE organization_id = organizations.id) AS member_count,
                    (SELECT COUNT(*) FROM owned_repositories
                        WHERE owned_repositories.organization_id = organizations.id
                          AND """ + VISIBLE_TO_USER + """) AS repository_count,
                    (SELECT role FROM organization_members
                        WHERE organization_id = organizations.id AND user_id = ?) AS my_role
                FROM organizations
                ORDER BY LOWER(organizations.name)
                """,
                (user_id, user_id, user_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    ORGANIZATION_SORTS = {
        "name": "LOWER(name)",
        "newest": "created_at DESC, LOWER(name)",
        "repositories": "repository_count DESC, LOWER(name)",
        "members": "member_count DESC, LOWER(name)",
    }
    ORGANIZATION_FILTERS = {
        "with_repositories": "repository_count > 0",
        "empty": "repository_count = 0",
        "mine": "my_role IS NOT NULL",
    }

    def search_organizations(
        self,
        user_id: str,
        *,
        query: str = "",
        filter: str | None = None,
        sort: str = "name",
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        """One page of organizations for the admin list, the total that matched,
        and counts per filter for the search text alone."""
        base = """
            SELECT organizations.*,
                (SELECT COUNT(*) FROM organization_members
                    WHERE organization_id = organizations.id) AS member_count,
                (SELECT COUNT(*) FROM owned_repositories
                    WHERE organization_id = organizations.id) AS repository_count,
                (SELECT role FROM organization_members
                    WHERE organization_id = organizations.id AND user_id = ?) AS my_role
            FROM organizations
        """
        search_clauses: list[str] = []
        search_params: list[Any] = [user_id]
        text = query.strip().lower()
        if text:
            # '!' escapes LIKE wildcards; the pattern travels as a parameter.
            escaped = text.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            pattern = f"%{escaped}%"
            search_clauses.append(
                "(LOWER(name) LIKE ? ESCAPE '!' OR LOWER(display_name) LIKE ? ESCAPE '!' "
                "OR LOWER(description) LIKE ? ESCAPE '!')"
            )
            search_params += [pattern, pattern, pattern]
        clauses = list(search_clauses)
        if filter in self.ORGANIZATION_FILTERS:
            clauses.append(self.ORGANIZATION_FILTERS[filter])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        search_where = f"WHERE {' AND '.join(search_clauses)}" if search_clauses else ""
        order = self.ORGANIZATION_SORTS.get(sort, self.ORGANIZATION_SORTS["name"])
        with self.connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) AS count FROM ({base}) AS orgs {where}", search_params
                ).fetchone()["count"]
            )
            rows = connection.execute(
                f"SELECT * FROM ({base}) AS orgs {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*search_params, limit, offset],
            ).fetchall()
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS all_count,
                    COALESCE(SUM(CASE WHEN repository_count > 0 THEN 1 ELSE 0 END), 0) AS with_repositories,
                    COALESCE(SUM(CASE WHEN my_role IS NOT NULL THEN 1 ELSE 0 END), 0) AS mine
                FROM ({base}) AS orgs {search_where}
                """,
                search_params,
            ).fetchone()
        counts = {
            "all": int(row["all_count"]),
            "with_repositories": int(row["with_repositories"]),
            "empty": int(row["all_count"]) - int(row["with_repositories"]),
            "mine": int(row["mine"]),
        }
        return [dict(item) for item in rows], total, counts

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
        """Admins who can act as one; see ORG_ROLE_ACTS."""
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM organization_members "
            "JOIN users ON users.id = organization_members.user_id "
            "WHERE organization_members.organization_id = ? AND organization_members.role = 'admin' "
            f"AND {ORG_ROLE_ACTS}",
            (organization_id,),
        ).fetchone()
        return int(row["count"])

    def _membership(self, connection: Any, organization_id: str, user_id: str) -> Any:
        """A member's role, and whether they act as an admin (1) or not (0)."""
        return connection.execute(
            "SELECT organization_members.role, "
            f"CASE WHEN organization_members.role = 'admin' AND {ORG_ROLE_ACTS} "
            "THEN 1 ELSE 0 END AS acting_admin "
            "FROM organization_members JOIN users ON users.id = organization_members.user_id "
            "WHERE organization_members.organization_id = ? AND organization_members.user_id = ?",
            (organization_id, user_id),
        ).fetchone()

    def set_organization_member(
        self, organization_id: str, user_id: str, role: str, created_at: str, *, force: bool = False
    ) -> None:
        """Add or change a member; the last acting organization admin stays unless
        `force`. An admin row held by a disabled account or a Viewer does not count,
        so such an organization can always be given a new admin."""
        with self._guarded() as connection:
            current = self._membership(connection, organization_id, user_id)
            if (
                current
                and current["acting_admin"]
                and role != "admin"
                and not force
                and self._organization_admins(connection, organization_id) <= 1
            ):
                raise ValueError(
                    "An organization needs at least one admin. Make another member an admin first."
                )
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
        with self._guarded() as connection:
            current = self._membership(connection, organization_id, user_id)
            if not current:
                return False
            if (
                current["acting_admin"]
                and not force
                and self._organization_admins(connection, organization_id) <= 1
            ):
                raise ValueError(
                    "An organization needs at least one admin. Make another member an admin first."
                )
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
                       organizations.avatar_updated_at, organization_members.role
                FROM organization_members
                JOIN organizations ON organizations.id = organization_members.organization_id
                WHERE organization_members.user_id = ?
                ORDER BY LOWER(organizations.name)
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def organizations_only_administered_by(self, user_id: str) -> list[str]:
        """Organizations in which this user is the only admin who can act as one."""
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT organizations.name FROM organization_members
                JOIN organizations ON organizations.id = organization_members.organization_id
                JOIN users ON users.id = organization_members.user_id
                WHERE organization_members.user_id = ? AND organization_members.role = 'admin'
                  AND {ORG_ROLE_ACTS}
                  AND (SELECT COUNT(*) FROM organization_members AS others
                       JOIN users ON users.id = others.user_id
                       WHERE others.organization_id = organization_members.organization_id
                         AND others.role = 'admin' AND {ORG_ROLE_ACTS}) = 1
                ORDER BY LOWER(organizations.name)
                """,
                (user_id,),
            ).fetchall()
        return [row["name"] for row in rows]

    def reassign_organization_repositories(self, from_user: str, to_user: str) -> int:
        """Keep an organization's repositories when the account that created them is deleted."""
        with self._write_lock, self.connect() as connection:
            cursor = connection.execute(
                "UPDATE owned_repositories SET owner_id = ? "
                "WHERE owner_id = ? AND organization_id IS NOT NULL",
                (to_user, from_user),
            )
        return cursor.rowcount
