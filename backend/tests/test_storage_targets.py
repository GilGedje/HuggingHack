import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.auth import AuthService
from app.catalog import LocalCatalog
from app.config import Settings
from app.database import Database
from app.history import RepoHistory
from app.hub_api import HubRepositories
from app.indexer import LocalModelIndexer
from app.storage import (
    S3ModelStorage,
    S3TargetConfig,
    StorageRegistry,
    create_storage_registry,
    parse_storage_targets,
)
from app.uploads import UploadManager
from test_core import FakeS3Client


def publish(client: FakeS3Client, prefix: str, repo_id: str, files: dict[str, bytes]) -> None:
    for path, payload in files.items():
        client.objects[f"{prefix}/{repo_id}/{path}"] = payload
    client.objects[f"{prefix}/{repo_id}/.hugginghack.json"] = json.dumps(
        {
            "status": "complete",
            "repo_id": repo_id,
            "total_bytes": sum(len(value) for value in files.values()),
            "file_count": len(files),
        }
    ).encode()


def test_storage_targets_are_validated_and_read_secrets_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MINIO_A_KEY", "AKIA-EXAMPLE")
    monkeypatch.setenv("MINIO_A_SECRET", "super-secret-value")
    [target] = parse_storage_targets(
        json.dumps(
            [
                {
                    "id": "minio-a",
                    "name": "MinIO A",
                    "bucket": "models-a",
                    "endpoint_url": "http://minio:9000",
                    "access_key_env": "MINIO_A_KEY",
                    "secret_key_env": "MINIO_A_SECRET",
                    "addressing_style": "path",
                }
            ]
        )
    )
    assert target.access_key_id == "AKIA-EXAMPLE"
    assert "super-secret-value" not in repr(target)
    for bad in (
        [{"id": "local", "bucket": "x"}],
        [{"id": "Bad Id", "bucket": "x"}],
        [{"id": "a", "bucket": "x"}, {"id": "a", "bucket": "y"}],
        [{"id": "a"}],
        [{"id": "a", "bucket": "x", "access_key_env": "$(whoami)"}],
        [{"id": "a", "bucket": "x", "kind": "gcs"}],
        {"id": "a"},
    ):
        with pytest.raises(ValueError):
            parse_storage_targets(json.dumps(bad))

    class FailingClient(FakeS3Client):
        def list_objects_v2(self, **_):
            raise RuntimeError("denied for AKIA-EXAMPLE with super-secret-value")

    storage = S3ModelStorage(
        Settings(model_storage=Path("/tmp/unused"), data_dir=Path("/tmp/unused")),
        client=FailingClient(),
        transfer_config=object(),
        target=target,
    )
    health = storage.health()
    assert health["connected"] is False
    assert "AKIA-EXAMPLE" not in health["error"]
    assert "super-secret-value" not in health["error"]


def test_registry_keeps_legacy_bucket_first_and_honors_default(tmp_path: Path):
    settings = Settings(
        model_storage=tmp_path / "models",
        data_dir=tmp_path / "data",
        model_storage_backend="s3",
        s3_bucket="legacy",
        storage_targets_json=json.dumps([{"id": "archive", "bucket": "archive"}]),
    )
    registry = create_storage_registry(settings)
    assert registry.ids() == ["local", "s3", "archive"]
    assert registry.default_id == "s3"
    assert registry.for_model({"storage_backend": "s3"}).id == "s3"
    assert registry.for_model({"storage_target": "archive"}).id == "archive"
    with pytest.raises(ValueError):
        registry.get("missing")
    with pytest.raises(ValueError):
        create_storage_registry(
            Settings(
                model_storage=tmp_path / "models",
                data_dir=tmp_path / "data",
                default_storage_target="missing",
            )
        )


def test_database_assigns_legacy_s3_rows_to_the_s3_target(tmp_path: Path):
    database_path = tmp_path / "hugginghack.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE local_models (
                repo_id TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL UNIQUE,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                file_count INTEGER NOT NULL DEFAULT 0,
                modified_at TEXT NOT NULL,
                downloaded_at TEXT, revision TEXT, sha TEXT, pipeline_tag TEXT,
                library_name TEXT, license TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]',
                config_json TEXT NOT NULL DEFAULT '{}',
                source_url TEXT,
                managed INTEGER NOT NULL DEFAULT 0,
                storage_backend TEXT NOT NULL DEFAULT 'filesystem',
                cached INTEGER NOT NULL DEFAULT 1,
                remote_uri TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO local_models (repo_id, relative_path, modified_at, storage_backend) "
            "VALUES ('acme/remote', 'acme/remote', 'now', 's3'), "
            "('acme/disk', 'acme/disk', 'now', 'filesystem')"
        )
    database = Database(database_path)
    database.initialize()
    assert database.get_local_model("acme/remote")["storage_target"] == "s3"
    assert database.get_local_model("acme/disk")["storage_target"] == "local"


@pytest.fixture()
def multi_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage_path = (tmp_path / "models").resolve()
    settings = Settings(model_storage=storage_path, data_dir=(tmp_path / "data").resolve())
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    clients = {"bucket-a": FakeS3Client(), "bucket-b": FakeS3Client()}
    remotes = [
        S3ModelStorage(
            settings,
            client=client,
            transfer_config=object(),
            target=S3TargetConfig(id=target_id, name=target_id.upper(), bucket=target_id),
        )
        for target_id, client in clients.items()
    ]
    registry = StorageRegistry(settings, remotes, default_target="bucket-b")
    indexer = LocalModelIndexer(settings, database)
    repositories = HubRepositories(settings, database, registry)
    history = RepoHistory(database, repositories)
    for name, value in {
        "settings": settings,
        "database": database,
        "indexer": indexer,
        "storages": registry,
        "uploads": UploadManager(settings, database, indexer, registry, history),
        "catalog": LocalCatalog(settings, registry),
        "hub_repositories": repositories,
        "history": history,
    }.items():
        monkeypatch.setattr(main, name, value)
    auth = AuthService(settings, database)
    admin = auth.create_user("admin", "Admin", "correct horse battery", "admin")
    member = auth.create_user("member", "Member", "another secure phrase", "member")
    local = storage_path / "acme" / "on-disk"
    local.mkdir(parents=True)
    (local / "config.json").write_text("{}", encoding="utf-8")
    return {
        "settings": settings,
        "database": database,
        "history": history,
        "clients": clients,
        "registry": registry,
        "admin": admin,
        "member": member,
    }


def test_scan_indexes_every_target_and_reports_conflicts(multi_target):
    clients = multi_target["clients"]
    database = multi_target["database"]
    publish(clients["bucket-a"], "models", "acme/alpha", {"config.json": b"{}", "w.gguf": b"GGUF"})
    publish(clients["bucket-a"], "models", "acme/shared", {"config.json": b"{}"})
    publish(clients["bucket-b"], "models", "acme/shared", {"config.json": b"{}", "extra.bin": b"x"})
    publish(clients["bucket-b"], "models", "acme/beta", {"config.json": b"{}"})

    result = main.refresh_model_index()

    targets = {model["repo_id"]: model["storage_target"] for model in database.list_local_models()}
    assert targets == {
        "acme/on-disk": "local",
        "acme/alpha": "bucket-a",
        "acme/shared": "bucket-a",
        "acme/beta": "bucket-b",
    }
    assert result["conflicts"] == [
        {"repo_id": "acme/shared", "kept_target": "bucket-a", "skipped_target": "bucket-b"}
    ]
    snapshot = main.hub_repositories.snapshot("acme/alpha")
    assert [entry.path for entry in snapshot.entries] == ["config.json", "w.gguf"]

    # An unreachable bucket keeps its models; a deleted repository disappears.
    def broken(**_):
        raise RuntimeError("connection refused")

    clients["bucket-b"].get_paginator = lambda _: type("P", (), {"paginate": staticmethod(broken)})()
    for key in [key for key in clients["bucket-a"].objects if "/acme/alpha/" in key]:
        del clients["bucket-a"].objects[key]
    result = main.refresh_model_index()
    remaining = {model["repo_id"] for model in database.list_local_models()}
    assert "acme/beta" in remaining
    assert "acme/alpha" not in remaining
    assert "bucket-b" in result["remote_error"]


def test_storage_page_is_admin_only_and_groups_models_by_target(multi_target):
    publish(multi_target["clients"]["bucket-a"], "models", "acme/alpha", {"config.json": b"{}"})
    main.refresh_model_index()
    current = {"user": multi_target["member"]}
    main.app.dependency_overrides[main.require_user] = lambda: current["user"]
    main.app.dependency_overrides[main.require_write_user] = lambda: current["user"]
    try:
        client = TestClient(main.app)
        assert client.get("/api/storage/targets").status_code == 403
        options = client.get("/api/storage/options").json()
        assert options["default"] == "bucket-b"
        assert [item["id"] for item in options["items"]] == ["local", "bucket-a", "bucket-b"]
        assert "bucket" not in options["items"][1]

        current["user"] = multi_target["admin"]
        payload = client.get("/api/storage/targets").json()
        by_id = {target["id"]: target for target in payload["targets"]}
        assert by_id["local"]["capacity"]["total_bytes"] > 0
        assert [model["repo_id"] for model in by_id["local"]["models"]] == ["acme/on-disk"]
        assert [model["repo_id"] for model in by_id["bucket-a"]["models"]] == ["acme/alpha"]
        assert by_id["bucket-a"]["bucket"] == "bucket-a"
        assert by_id["bucket-a"]["connected"] is True
        assert by_id["bucket-b"]["default"] is True
        assert by_id["bucket-b"]["model_count"] == 0

        # Evicting a model that only exists on local disk must never delete it.
        assert client.delete("/api/local-models/acme/on-disk/cache").status_code in {403, 409}
    finally:
        main.app.dependency_overrides.clear()
    assert (multi_target["settings"].model_storage / "acme" / "on-disk" / "config.json").exists()


def test_uploads_sync_to_the_chosen_target(multi_target):
    uploads = main.uploads
    admin = multi_target["admin"]
    repository = uploads.create_repository(admin, "tiny", "", "public", "bucket-a")
    uploads.upload_chunk(repository["repo_id"], admin["id"], "config.json", 0, 2, b"{}")
    uploads.finalize(repository["repo_id"], admin["id"])
    assert "models/admin/tiny/config.json" in multi_target["clients"]["bucket-a"].objects
    assert not multi_target["clients"]["bucket-b"].objects
    indexed = multi_target["database"].get_local_model("admin/tiny")
    assert indexed["storage_target"] == "bucket-a"
    with pytest.raises(ValueError):
        uploads.create_repository(admin, "other", "", "public", "nowhere")

    mirror = multi_target["settings"].data_dir / "git-mirrors" / "admin" / "tiny"
    mirror.mkdir(parents=True)
    multi_target["database"].set_file_digest("admin/tiny", "w", "v", "a" * 64)
    uploads.delete_repository("admin/tiny", admin, "admin/tiny")
    assert not any("admin/tiny" in key for key in multi_target["clients"]["bucket-a"].objects)
    assert not mirror.exists()
    assert multi_target["database"].get_file_digest("admin/tiny", "w", "v") is None
    assert multi_target["database"].count_commits("admin/tiny") == 0
