import os
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from app.database import Database
from app.migrate_sqlite import migrate


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
LEGACY_SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_users_username_nocase ON users(LOWER(username));
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE owned_repositories (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    repo_id TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'private',
    status TEXT NOT NULL DEFAULT 'uploading',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO users VALUES ('u1', 'owner', 'Owner', 'hash', 'admin', 'now', 'now');
INSERT INTO users VALUES ('u2', 'member', 'Member', 'hash', 'member', 'now', 'now');
INSERT INTO sessions VALUES ('h1', 'u1', 'csrf', 'now', '2999-01-01T00:00:00+00:00');
INSERT INTO sessions VALUES ('h2', 'u2', 'csrf', 'now', '2999-01-01T00:00:00+00:00');
INSERT INTO owned_repositories VALUES ('r1', 'u1', 'owner/model', '', 'private', 'ready', 'now', 'now');
"""


def test_sqlite_users_migration_keeps_references_and_allows_viewers(tmp_path: Path):
    path = tmp_path / "hugginghack.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(LEGACY_SCHEMA)

    database = Database(path)
    database.initialize()
    database.initialize()

    owner = database.get_user("u1", include_secret=False)
    assert owner["role"] == "admin"
    assert owner["disabled"] is False
    assert owner["preferences"] == {}
    assert owner["auth_provider"] == "local"
    sessions = database.list_sessions("u1")
    assert len(sessions) == 1 and len(sessions[0]["id"]) == 32
    database.update_user("u2", role="viewer")
    assert database.get_user("u2")["role"] == "viewer"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE users SET role = 'root' WHERE id = 'u2'")

    # Foreign keys still work after the rebuild.
    database.delete_user("u2")
    assert database.list_sessions("u2") == []
    with pytest.raises(ValueError):
        database.delete_user("u1")  # the only administrator
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id = 'u1'")  # owns a repository


def test_last_active_admin_is_protected(tmp_path: Path):
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    for user_id, role in (("a1", "admin"), ("a2", "admin"), ("m1", "member")):
        database.create_user(
            {
                "id": user_id,
                "username": user_id,
                "display_name": user_id,
                "password_hash": "x",
                "role": role,
                "created_at": "now",
                "updated_at": "now",
            }
        )
    database.update_user_guarded("a1", {"role": "member"})
    with pytest.raises(ValueError):
        database.update_user_guarded("a2", {"disabled": 1})
    with pytest.raises(ValueError):
        database.update_user_guarded("a2", {"role": "viewer"})
    with pytest.raises(ValueError):
        database.delete_user("a2")
    database.update_user_guarded("m1", {"role": "admin"})
    database.update_user_guarded("a2", {"disabled": 1})
    assert database.count_active_admins() == 1


@pytest.fixture()
def fresh_postgres():
    if not POSTGRES_URL:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    import psycopg

    name = f"hh_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    parts = urlsplit(POSTGRES_URL)
    try:
        yield urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))
    finally:
        with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_postgresql_users_migration_replaces_the_role_check(fresh_postgres: str):
    import psycopg

    with psycopg.connect(fresh_postgres, autocommit=True) as connection:
        for statement in LEGACY_SCHEMA.strip().split(";"):
            if statement.strip():
                connection.execute(statement)
    database = Database(fresh_postgres)
    database.initialize()
    database.initialize()
    database.update_user("u2", role="viewer")
    assert database.get_user("u2", include_secret=False)["role"] == "viewer"
    assert len(database.list_sessions("u1")[0]["id"]) == 32
    with psycopg.connect(fresh_postgres, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute("UPDATE users SET role = 'root' WHERE id = 'u2'")
    database.delete_user("u2")
    assert database.list_sessions("u2") == []


def test_sqlite_installation_copies_into_postgresql(tmp_path: Path, fresh_postgres: str):
    source_path = tmp_path / "hugginghack.sqlite3"
    with sqlite3.connect(source_path) as connection:
        connection.executescript(LEGACY_SCHEMA)
    source = Database(source_path)
    source.initialize()
    source.create_commit(
        {
            "id": "c" * 40,
            "repo_id": "owner/model",
            "sequence": 1,
            "parent_id": None,
            "author_id": "u1",
            "author_name": "Owner",
            "message": "Initial import",
            "description": "",
            "created_at": "now",
            "snapshot_json": "[]",
            "changes_json": "[]",
        }
    )
    source.put_text_blob("a" * 64, "hello")
    organization = source.create_organization(
        {"id": "org1", "name": "Nvidia", "display_name": "NVIDIA", "description": "", "created_at": "now", "updated_at": "now"}
    )
    source.set_organization_member(organization["id"], "u1", "admin", "now")
    source.create_owned_repository(
        {"id": "r2", "owner_id": "u1", "repo_id": "Nvidia/GLM", "description": "", "visibility": "private",
         "status": "ready", "created_at": "now", "updated_at": "now", "organization_id": "org1"}
    )

    copied = migrate(source_path, fresh_postgres)

    assert copied["users"] == 2 and copied["sessions"] == 2 and copied["repo_commits"] == 1
    target = Database(fresh_postgres)
    assert target.get_user_by_username("owner")["password_hash"] == "hash"
    assert target.get_owned_repository("owner/model")["owner_id"] == "u1"
    assert target.latest_commit("owner/model")["message"] == "Initial import"
    assert target.get_text_blob("a" * 64) == "hello"
    assert copied["organizations"] == 1 and copied["organization_members"] == 1
    assert target.get_organization("nvidia")["display_name"] == "NVIDIA"
    assert target.get_owned_repository("Nvidia/GLM")["organization_name"] == "Nvidia"
    with pytest.raises(ValueError):
        migrate(source_path, fresh_postgres)


def test_postgresql_sso_state_and_external_accounts(fresh_postgres: str):
    database = Database(fresh_postgres)
    database.initialize()
    database.create_oidc_state(
        {
            "state_hash": "s" * 64,
            "browser_hash": "b" * 64,
            "nonce": "n",
            "code_verifier": "v",
            "redirect_uri": "http://x/api/auth/oidc/callback",
            "next_path": "/models",
            "created_at": "2026-09-24T00:00:00+00:00",
            "expires_before": "2026-09-23T00:00:00+00:00",
        }
    )
    assert database.take_oidc_state("s" * 64)["nonce"] == "n"
    assert database.take_oidc_state("s" * 64) is None
    database.create_user(
        {
            "id": "ext1",
            "username": "sso-user",
            "display_name": "SSO",
            "password_hash": "!oidc",
            "role": "viewer",
            "created_at": "now",
            "updated_at": "now",
            "auth_provider": "oidc",
            "external_subject": "subject-1",
        }
    )
    assert database.get_user_by_external("oidc", "subject-1")["id"] == "ext1"
    import psycopg

    with pytest.raises(psycopg.errors.UniqueViolation):
        database.create_user(
            {
                "id": "ext2",
                "username": "sso-user-2",
                "display_name": "Dup",
                "password_hash": "!oidc",
                "role": "viewer",
                "created_at": "now",
                "updated_at": "now",
                "auth_provider": "oidc",
                "external_subject": "subject-1",
            }
        )


def test_deleting_a_collection_keeps_its_saved_models(tmp_path: Path):
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    for user_id in ("u1", "u2"):
        database.create_user(
            {
                "id": user_id,
                "username": user_id,
                "display_name": user_id,
                "password_hash": "x",
                "role": "member",
                "created_at": "now",
                "updated_at": "now",
            }
        )
    database.create_collection(
        {"id": "c1", "user_id": "u1", "name": "Rig", "description": "", "created_at": "now", "updated_at": "now"}
    )
    database.save_model(
        {
            "id": "s1",
            "user_id": "u1",
            "repo_id": "owner/model",
            "note": "",
            "metadata_json": "{}",
            "created_at": "now",
            "updated_at": "now",
        },
        ["c1"],
    )

    assert not database.delete_collection("c1", "u2")
    assert database.delete_collection("c1", "u1")
    assert not database.delete_collection("c1", "u1")
    assert database.list_collections("u1") == []
    [saved] = database.list_saved_models("u1")
    assert saved["repo_id"] == "owner/model"
    assert saved["collections"] == []
