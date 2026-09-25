"""What the server does while its database or a bucket is down, and the plain
answers it gives instead of a slow bare 500."""

from __future__ import annotations

import errno
import time

import psycopg
import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

import app.main as main
from app import database as database_module
from app.auth import AuthService
from app.config import Settings, validate_namespace
from app.database import Database, database_unreachable
from app.hub_api import HubRepositories
from app.storage import S3ModelStorage, S3TargetConfig, StorageRegistry, StorageUnavailableError
from test_access import server  # noqa: F401  (fixture)
from test_organizations import login  # noqa: F401
from test_storage_targets import multi_target, publish  # noqa: F401  (fixture)


def unreachable(*_args, **_kwargs):
    raise PoolTimeout("couldn't get a connection after 5.00 sec")


def test_only_connection_failures_count_as_an_unreachable_database():
    assert database_unreachable(PoolTimeout("timeout"))
    assert database_unreachable(psycopg.OperationalError("connection refused"))
    assert not database_unreachable(psycopg.errors.DeadlockDetected("deadlock"))
    assert not database_unreachable(ValueError("no"))


def test_the_connection_pool_gives_up_in_seconds(monkeypatch):
    seen: dict = {}

    class Pool:
        check_connection = staticmethod(lambda connection: None)

        def __init__(self, url, **options):
            seen.update(options)

    monkeypatch.setattr(database_module, "ConnectionPool", Pool)
    Database("postgresql://nobody@127.0.0.1:1/none")._connection_pool()
    assert seen["timeout"] <= 5
    assert seen["kwargs"]["connect_timeout"] <= 5 and seen["kwargs"]["keepalives"] == 1


def test_anonymous_health_answers_without_the_database(server, monkeypatch):  # noqa: F811
    assert main.auth.setup_required() is False  # an account exists, and that is remembered
    monkeypatch.setattr(main.database, "count_users", unreachable)
    monkeypatch.setattr(main.database, "get_session", unreachable)
    started = time.monotonic()
    response = TestClient(main.app).get("/api/health")
    assert response.status_code == 200 and response.json()["status"] == "ok"
    assert time.monotonic() - started < 2

    # A signed-in browser still learns the server is up, but not healthy.
    signed_in = TestClient(main.app, cookies={main.auth.cookie_name: "some-session"})
    assert signed_in.get("/api/health").json()["status"] == "degraded"


def test_requests_get_a_sentence_and_503_while_the_database_is_down(server, monkeypatch):  # noqa: F811
    admin, _ = login("admin")
    monkeypatch.setattr(main.database, "get_session", unreachable)
    response = admin.get("/api/library/models")
    assert response.status_code == 503
    assert response.json()["detail"].startswith("The database is not reachable.")


def test_server_health_reports_the_database(server):  # noqa: F811
    admin, _ = login("admin")
    payload = admin.get("/api/health").json()
    assert payload["database"] == {"backend": "sqlite", "connected": True, "error": None}


def test_a_bucket_that_is_down_answers_503_quickly_without_its_address(multi_target, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "auth", AuthService(multi_target["settings"], multi_target["database"]))
    client = multi_target["clients"]["bucket-a"]
    publish(client, "models", "acme/alpha", {"config.json": b"{}", "w.gguf": b"GGUF"})
    main.refresh_model_index()

    def down(*_args, **_kwargs):
        raise EndpointConnectionError(endpoint_url="http://10.0.0.9:9000/bucket-a?list-type=2")

    monkeypatch.setattr(client, "paginate", down)
    monkeypatch.setattr(client, "head_object", down, raising=False)
    admin, _ = login("admin")
    for path in ("/api/models/acme/alpha", "/api/local-models/acme/alpha"):
        response = admin.get(path)
        assert response.status_code == 503, path
        assert "10.0.0.9" not in response.text and "list-type" not in response.text
    anonymous = TestClient(main.app).get("/acme/alpha/resolve/main/config.json")
    assert anonymous.status_code == 503


def test_an_unreachable_bucket_is_described_in_one_sentence(tmp_path):
    settings = Settings(model_storage=tmp_path / "models", data_dir=tmp_path / "data")
    storage = S3ModelStorage(
        settings,
        target=S3TargetConfig(
            id="nope", name="Nope", bucket="nope", endpoint_url="http://127.0.0.1:1",
            access_key_id="AKIA", secret_access_key="secret",
        ),
    )
    # Reads someone waits on retry once; transfers keep retrying.
    assert storage.read_client.meta.config.retries["total_max_attempts"] == 2
    assert storage.client.meta.config.retries["total_max_attempts"] > 2
    health = storage.health()
    assert health["connected"] is False
    assert health["error"] == (
        "Cannot reach s3://nope at http://127.0.0.1:1. Check the endpoint and that the storage server is running."
    )
    with pytest.raises(StorageUnavailableError):
        HubRepositories(settings, None, StorageRegistry(settings, [storage])).remote_entries(
            {"repo_id": "acme/alpha", "storage_target": "nope", "storage_backend": "s3"}
        )


def test_a_scan_error_is_forgotten_once_the_bucket_answers(multi_target, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "auth", AuthService(multi_target["settings"], multi_target["database"]))
    main.storage_errors["bucket-a"] = "Cannot reach s3://bucket-a."
    admin, _ = login("admin")
    targets = {item["id"]: item for item in admin.get("/api/storage/targets").json()["targets"]}
    assert targets["bucket-a"]["connected"] and targets["bucket-a"]["error"] is None
    assert "bucket-a" not in main.storage_errors


def test_file_system_errors_never_show_server_paths(server):  # noqa: F811
    error = OSError(errno.ENAMETOOLONG, "File name too long", "/models/local/secret/aaaa")
    assert main.error_text(error) == "A file or folder name in this path is too long."
    assert main.error_text(FileExistsError(errno.EEXIST, "File exists", "/models/x/f.bin")).startswith("Another file")
    # HuggingHack's own errors are already sentences.
    assert main.error_text(FileExistsError("That name is taken.")) == "That name is taken."

    member, _ = login("member")
    created = member.post("/api/uploads/repositories", json={"slug": "paths", "visibility": "private"})
    repo_id = created.json()["repo_id"]
    put = lambda path: member.put(  # noqa: E731
        "/api/uploads/repositories/files",
        params={"repo_id": repo_id, "path": path},
        headers={"Upload-Offset": "0", "Upload-Length": "1"},
        content=b"x",
    )
    assert put("f.bin").status_code == 200
    response = put("f.bin/x")
    assert response.status_code in {400, 409}
    assert str(main.settings.model_storage) not in response.text and "Errno" not in response.text
    response = put("long/" + "a" * 300)
    assert response.status_code == 400
    assert str(main.settings.model_storage) not in response.text and "Errno" not in response.text


def test_finished_uploads_whose_files_vanished_are_flagged(server):  # noqa: F811
    member, _ = login("member")
    items = {item["repo_id"]: item for item in member.get("/api/uploads/repositories").json()["items"]}
    assert items["member/secret"]["missing"] is False
    with main.database.connect() as connection:
        connection.execute("DELETE FROM local_models WHERE repo_id = ?", ("member/secret",))
    items = {item["repo_id"]: item for item in member.get("/api/uploads/repositories").json()["items"]}
    assert items["member/secret"]["missing"] is True
    assert "indexed" not in items["member/secret"]


def test_collections_sort_the_same_on_both_databases(server):  # noqa: F811
    user_id = server["users"]["member"]["id"]
    for name in ("ünïcödé COLL", "Ünïcödé coll", "apple", "Banana"):
        main.database.create_collection(
            {"id": name.encode().hex(), "user_id": user_id, "name": name, "description": "",
             "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"}
        )
    names = [item["name"] for item in main.database.list_collections(user_id)]
    assert names == ["apple", "Banana", "Ünïcödé coll", "ünïcödé COLL"]


def test_only_roles_that_upload_get_write_tokens(server):  # noqa: F811
    viewer, _ = login("viewer")
    refused = viewer.post("/api/account/tokens", json={"name": "ci", "scope": "write"})
    assert refused.status_code == 403
    assert "read tokens" in refused.json()["detail"]
    assert viewer.post("/api/account/tokens", json={"name": "pull", "scope": "read"}).status_code == 201
    member, _ = login("member")
    assert member.post("/api/account/tokens", json={"name": "ci", "scope": "write"}).status_code == 201


def test_organizations_cannot_take_the_names_of_app_pages(server):  # noqa: F811
    for name in ("admin", "Orgs", "models", "account", "api"):
        with pytest.raises(ValueError, match="reserved"):
            validate_namespace(name)
    admin, _ = login("admin")
    response = admin.post("/api/organizations", json={"name": "models", "display_name": "Models"})
    assert response.status_code == 409 and "reserved" in response.json()["detail"]
    assert admin.post("/api/organizations", json={"name": "modelers", "display_name": "M"}).status_code == 201
    # Accounts may still be called admin: owners often are.
    with pytest.raises(ValueError, match="reserved"):
        main.auth.create_user("orgs", "Orgs", "correct horse battery", "member")
