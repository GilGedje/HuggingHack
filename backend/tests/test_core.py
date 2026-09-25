import json
import sqlite3
import time
from io import BytesIO
from pathlib import Path

import httpx
import pytest

from app.auth import AuthService, verify_password
from app.catalog import (
    LocalCatalog,
    parse_gguf_range,
    parse_parameter_range,
    search_catalog,
    validate_gguf_filename,
)
from app.config import Settings, repository_path, validate_repo_id
from app.database import Database, _postgres_query
from app.downloads import DownloadManager
from app.history import RepoHistory
from app.hub_api import HubRepositories
from app.indexer import (
    LocalModelIndexer,
    gguf_parameter_count,
    repository_facts,
    safetensors_parameter_count,
)
from app.runtimes import (
    RuntimeManager,
    ollama_files,
    parse_runtime_targets,
    remote_model_path,
)
from app.storage import FilesystemModelStorage, S3ModelStorage, StorageRegistry
from app.uploads import UploadManager, validate_upload_path
from app.vllm_agent import AgentSettings, VllmProcessManager


class FakeS3Client:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []
        self.modified: dict[str, str] = {}
        self._clock = 0

    def _touch(self, key: str) -> None:
        self._clock += 1
        self.modified[key] = f"2026-07-24T12:00:{self._clock % 60:02d}.{self._clock:06d}+00:00"

    def get_paginator(self, operation: str):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, *, Bucket: str, Prefix: str):
        return [
            {
                "Contents": [
                    {
                        "Key": key,
                        "Size": len(value),
                        "LastModified": self.modified.get(key, "2026-07-24T12:00:00+00:00"),
                    }
                    for key, value in sorted(self.objects.items())
                    if key.startswith(Prefix)
                ]
            }
        ]

    def list_objects_v2(self, *, Bucket: str, Prefix: str, MaxKeys: int):
        contents = [
            {"Key": key}
            for key in sorted(self.objects)
            if key.startswith(Prefix)
        ][:MaxKeys]
        return {"Contents": contents}

    def upload_file(self, filename: str, bucket: str, key: str, **kwargs):
        self.objects[key] = Path(filename).read_bytes()
        self.uploads.append(key)
        self._touch(key)

    def download_file(self, bucket: str, key: str, filename: str, **kwargs):
        Path(filename).write_bytes(self.objects[key])

    def delete_objects(self, *, Bucket: str, Delete: dict):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)
        return {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes):
        self.objects[Key] = bytes(Body)
        self.uploads.append(Key)
        self._touch(Key)
        return {}

    def head_object(self, *, Bucket: str, Key: str):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, *, Bucket: str, Key: str, Range: str | None = None):
        if Key not in self.objects:
            raise KeyError(Key)
        payload = self.objects[Key]
        if Range:
            start, end = (int(value) for value in Range.removeprefix("bytes=").split("-"))
            payload = payload[start : end + 1]
        return {"Body": BytesIO(payload)}


def test_repo_id_validation_rejects_path_traversal():
    assert validate_repo_id("google/gemma-2b") == "google/gemma-2b"
    for value in ("../etc", "owner/../../secret", "single-name", "/absolute/model", "owner/model/extra"):
        with pytest.raises(ValueError):
            validate_repo_id(value)


def test_gguf_range_validation_is_bounded_and_path_safe():
    assert validate_gguf_filename("Q4/model-00001-of-00002.gguf") == (
        "Q4/model-00001-of-00002.gguf"
    )
    assert parse_gguf_range("bytes=0-1999999") == (0, 1_999_999)

    for filename in ("../model.gguf", "/model.gguf", "folder\\..\\model.gguf", "model.bin"):
        with pytest.raises(ValueError):
            validate_gguf_filename(filename)
    for range_header in (
        None,
        "bytes=0-1,3-4",
        "bytes=10-9",
        "bytes=0-2100000",
        "bytes=49000000-50000000",
    ):
        with pytest.raises(ValueError):
            parse_gguf_range(range_header)


def test_database_target_defaults_to_sqlite_and_accepts_postgresql(tmp_path: Path):
    sqlite_settings = Settings(data_dir=(tmp_path / "data").resolve())
    assert sqlite_settings.database_target == sqlite_settings.database_path
    assert Database(sqlite_settings.database_target).backend == "sqlite"

    postgres_url = "postgresql://hugginghack:secret@postgres:5432/hugginghack"
    postgres_settings = Settings(
        data_dir=(tmp_path / "data").resolve(),
        database_url=postgres_url,
    )
    assert postgres_settings.database_target == postgres_url
    assert Database(postgres_settings.database_target).backend == "postgresql"


def test_postgresql_query_adapter_handles_positional_and_named_parameters():
    assert _postgres_query("SELECT * FROM users WHERE id = ?", ("owner",)) == (
        "SELECT * FROM users WHERE id = %s"
    )
    assert _postgres_query(
        "UPDATE users SET display_name = :display_name WHERE id = :id",
        {"display_name": "Owner", "id": "owner"},
    ) == (
        "UPDATE users SET display_name = %(display_name)s WHERE id = %(id)s"
    )


def test_indexer_discovers_manually_copied_model(tmp_path: Path):
    storage = tmp_path / "models"
    data = tmp_path / "data"
    model = storage / "acme" / "tiny-model"
    model.mkdir(parents=True)
    (model / "config.json").write_text(
        json.dumps({"model_type": "llama", "architectures": ["LlamaForCausalLM"]}),
        encoding="utf-8",
    )
    (model / "model.safetensors").write_bytes(b"safe-weights")

    settings = Settings(model_storage=storage.resolve(), data_dir=data.resolve())
    database = Database(settings.database_path)
    database.initialize()
    indexer = LocalModelIndexer(settings, database)

    result = indexer.scan()

    assert result["count"] == 1
    indexed = database.get_local_model("acme/tiny-model")
    assert indexed is not None
    assert indexed["relative_path"] == "acme/tiny-model"
    assert indexed["config"]["model_type"] == "llama"
    assert indexed["managed"] is False


def test_indexer_marks_pickle_compatible_files(tmp_path: Path):
    storage = tmp_path / "models"
    data = tmp_path / "data"
    model = storage / "unsafe" / "legacy"
    model.mkdir(parents=True)
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / "pytorch_model.bin").write_bytes(b"not-executed")

    settings = Settings(model_storage=storage.resolve(), data_dir=data.resolve())
    database = Database(settings.database_path)
    database.initialize()
    indexer = LocalModelIndexer(settings, database)
    indexer.scan()

    details = indexer.files_for_model("unsafe/legacy")
    assert details is not None
    assert details["unsafe_file_count"] == 1
    assert details["files"][0]["unsafe_serialization"] is True


def test_indexer_fails_closed_for_upload_without_ownership_metadata(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    model = storage / "former-owner" / "private-model"
    model.mkdir(parents=True)
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / ".hugginghack.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "repo_id": "former-owner/private-model",
                "source": "user-upload",
                "owner_id": "missing-user",
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        model_storage=storage,
        data_dir=(tmp_path / "data").resolve(),
    )
    database = Database(settings.database_path)
    database.initialize()
    indexer = LocalModelIndexer(settings, database)

    result = indexer.scan()

    assert result["count"] == 0
    assert database.get_local_model("former-owner/private-model") is None


def test_cancelled_download_keeps_partial_files_and_resume_metadata(tmp_path: Path):
    storage = tmp_path / "models"
    data = tmp_path / "data"
    target = storage / "acme" / "large-model"
    target.mkdir(parents=True)
    (target / "weights.safetensors.incomplete").write_bytes(b"partial-data")

    settings = Settings(model_storage=storage.resolve(), data_dir=data.resolve())
    database = Database(settings.database_path)
    database.initialize()
    created = "2026-07-23T12:00:00+00:00"
    database.create_download(
        {
            "id": "cancel-me",
            "repo_id": "acme/large-model",
            "revision": "main",
            "status": "downloading",
            "total_bytes": 100,
            "downloaded_bytes": 0,
            "progress": 0,
            "speed_bps": 0,
            "error": None,
            "target_path": str(target),
            "payload_json": json.dumps({"mode": "full"}),
            "metadata_json": "{}",
            "created_at": created,
            "updated_at": created,
            "completed_at": None,
        }
    )
    manager = DownloadManager(settings, database, object(), object())

    class RunningProcess:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    process = RunningProcess()
    manager._processes["cancel-me"] = process  # type: ignore[assignment]

    cancelled = manager.cancel("cancel-me")
    manager.shutdown()

    assert cancelled is not None
    assert process.terminated is True
    assert cancelled["status"] == "cancelled"
    assert cancelled["downloaded_bytes"] >= len(b"partial-data")
    assert (target / "weights.safetensors.incomplete").is_file()
    manifest = json.loads((target / ".hugginghack.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled"


def test_completed_download_syncs_durable_storage_before_indexing(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    settings = Settings(
        model_storage=storage,
        data_dir=(tmp_path / "data").resolve(),
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    indexer = LocalModelIndexer(settings, database)

    class Hub:
        @staticmethod
        def model_details(repo_id: str, revision: str):
            return {
                "total_bytes": 7,
                "files": [{"path": "model.safetensors", "size": 7}],
                "source_url": "https://huggingface.co/acme/tiny",
                "sha": "abc123",
                "pipeline_tag": "text-generation",
                "library_name": "transformers",
                "license": "mit",
                "tags": ["transformers"],
                "gated": False,
            }

    class DurableStorage(FilesystemModelStorage):
        def __init__(self, configured_settings: Settings):
            super().__init__(configured_settings)
            self.synced: list[str] = []

        def sync_repository(self, repo_id: str, root: Path):
            manifest = json.loads(
                (root / ".hugginghack.json").read_text(encoding="utf-8")
            )
            assert manifest["status"] == "complete"
            self.synced.append(repo_id)
            return "s3://model-bucket/models/acme/tiny"

    durable = DurableStorage(settings)
    manager = DownloadManager(settings, database, Hub(), indexer, durable)
    created = "2026-07-24T12:00:00+00:00"
    database.create_download(
        {
            "id": "complete-me",
            "repo_id": "acme/tiny",
            "revision": "main",
            "status": "queued",
            "total_bytes": 0,
            "downloaded_bytes": 0,
            "progress": 0,
            "speed_bps": 0,
            "error": None,
            "target_path": str(storage / "acme" / "tiny"),
            "payload_json": "{}",
            "metadata_json": "{}",
            "created_at": created,
            "updated_at": created,
            "completed_at": None,
        }
    )

    def write_snapshot(download_id, download, target, cancel_event):
        (target / "model.safetensors").write_bytes(b"weights")

    manager._download_snapshot = write_snapshot  # type: ignore[method-assign]
    manager._run("complete-me")
    manager.shutdown()

    assert durable.synced == ["acme/tiny"]
    assert database.get_download("complete-me")["status"] == "complete"
    assert database.get_local_model("acme/tiny") is not None


def test_accounts_use_scrypt_and_separate_saved_libraries(tmp_path: Path):
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        accounts_enabled=True,
    )
    database = Database(settings.database_path)
    database.initialize()
    service = AuthService(settings, database)

    owner = service.create_user("owner", "NAS Owner", "correct horse battery", "admin")
    member = service.create_user("member", "Model Curator", "another secure phrase", "member")

    secret = database.get_user_by_username("owner")
    assert secret is not None
    assert secret["password_hash"].startswith("scrypt$")
    assert verify_password("correct horse battery", secret["password_hash"]) is True
    assert verify_password("incorrect", secret["password_hash"]) is False
    raw_session, _ = service.create_session(owner["id"])
    service.change_password(
        owner["id"],
        "correct horse battery",
        "replacement secure phrase",
        raw_session,
    )
    changed = database.get_user_by_username("owner")
    assert changed is not None
    assert verify_password("correct horse battery", changed["password_hash"]) is False
    assert verify_password("replacement secure phrase", changed["password_hash"]) is True

    saved_at = "2026-07-24T12:00:00+00:00"
    database.save_model(
        {
            "id": "saved-owner",
            "user_id": owner["id"],
            "repo_id": "acme/private-model",
            "note": "Keep for the vision rig",
            "metadata_json": json.dumps({"pipeline_tag": "image-text-to-text"}),
            "created_at": saved_at,
            "updated_at": saved_at,
        },
        [],
    )
    assert database.saved_repo_ids(owner["id"]) == {"acme/private-model"}
    assert database.saved_repo_ids(member["id"]) == set()


def test_accounts_disabled_keeps_single_user_compatibility(tmp_path: Path):
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        accounts_enabled=False,
    )
    database = Database(settings.database_path)
    database.initialize()
    service = AuthService(settings, database)
    service.ensure_local_user()

    session = service.session(None)
    assert session is not None
    assert session["user"]["id"] == "local"
    assert session["user"]["role"] == "admin"
    assert "password_hash" not in session["user"]
    assert service.verify_csrf(session, None) is True


def test_database_migrates_existing_download_history(tmp_path: Path):
    database_path = tmp_path / "data" / "hugginghack.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE downloads (
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
                completed_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO downloads (
                id, repo_id, revision, status, created_at, updated_at
            ) VALUES ('legacy', 'acme/model', 'main', 'complete', 'now', 'now')
            """
        )

    database = Database(database_path)
    database.initialize()

    with database.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(downloads)").fetchall()
        }
    assert "user_id" in columns
    legacy = database.get_download("legacy")
    assert legacy is not None
    assert legacy["repo_id"] == "acme/model"
    assert legacy["user_id"] is None


def test_database_migrates_local_models_for_s3_cache_state(tmp_path: Path):
    database_path = tmp_path / "data" / "hugginghack.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE local_models (
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
                managed INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO local_models (
                repo_id, relative_path, modified_at
            ) VALUES ('acme/model', 'acme/model', 'now')
            """
        )

    database = Database(database_path)
    database.initialize()

    legacy = database.get_local_model("acme/model")
    assert legacy is not None
    assert legacy["storage_backend"] == "filesystem"
    assert legacy["cached"] is True
    assert legacy["remote_uri"] is None


def test_chunked_upload_is_confined_owned_and_indexed(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    data = (tmp_path / "data").resolve()
    settings = Settings(
        model_storage=storage,
        data_dir=data,
        model_storage_backend="s3",
        s3_bucket="model-bucket",
        accounts_enabled=True,
        upload_chunk_mb=1,
        max_upload_size_gb=1,
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    auth = AuthService(settings, database)
    owner = auth.create_user("owner", "Owner", "correct horse battery", "admin")
    member = auth.create_user("member", "Member", "another secure phrase", "member")
    indexer = LocalModelIndexer(settings, database)
    fake_s3 = FakeS3Client()
    model_storage = S3ModelStorage(settings, client=fake_s3)
    manager = UploadManager(settings, database, indexer, model_storage)

    repository = manager.create_repository(
        owner, "tiny-upload", "Private test repository", "private"
    )
    first = manager.upload_chunk(
        repository["repo_id"], owner["id"], "config.json", 0, 22, b'{"model_type":"tiny"'
    )
    assert first == {"offset": 20, "complete": False, "path": "config.json"}
    with pytest.raises(RuntimeError):
        manager.upload_chunk(
            repository["repo_id"], owner["id"], "config.json", 0, 22, b"{}"
        )
    complete = manager.upload_chunk(
        repository["repo_id"], owner["id"], "config.json", 20, 22, b"}\n"
    )
    assert complete["complete"] is True
    empty = manager.upload_chunk(
        repository["repo_id"], owner["id"], "empty.marker", 0, 0, b""
    )
    assert empty["complete"] is True
    finalized = manager.finalize(repository["repo_id"], owner["id"])
    assert finalized["status"] == "ready"
    manifest = json.loads(
        (storage / repository["repo_id"] / ".hugginghack.json").read_text(encoding="utf-8")
    )
    assert manifest["file_count"] == 3
    indexed_model = database.get_local_model(repository["repo_id"])
    assert indexed_model is not None
    assert indexed_model["storage_backend"] == "s3"
    assert indexed_model["cached"] is True
    assert (
        f"models/{repository['repo_id']}/.hugginghack.json"
        in fake_s3.objects
    )
    assert database.get_visible_local_model(owner["id"], repository["repo_id"]) is not None
    assert database.get_visible_local_model(member["id"], repository["repo_id"]) is None

    manager.update_repository(
        repository["repo_id"], owner, "Shared test repository", "public"
    )
    assert database.get_visible_local_model(member["id"], repository["repo_id"]) is not None
    with pytest.raises(ValueError):
        manager.delete_repository(repository["repo_id"], owner, "wrong/name")
    manager.delete_repository(
        repository["repo_id"], owner, repository["repo_id"]
    )
    assert not (storage / repository["repo_id"]).exists()
    assert not any(
        key.startswith(f"models/{repository['repo_id']}/")
        for key in fake_s3.objects
    )
    assert database.get_owned_repository(repository["repo_id"]) is None

    for value in ("../secret", "/absolute/file", ".git/config", ".hugginghack.json"):
        with pytest.raises(ValueError):
            validate_upload_path(value)


def test_s3_storage_sync_discover_evict_and_restore(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    settings = Settings(
        model_storage=storage,
        data_dir=(tmp_path / "data").resolve(),
        model_storage_backend="s3",
        s3_bucket="model-bucket",
        s3_prefix="library",
    )
    root = storage / "acme" / "tiny"
    root.mkdir(parents=True)
    (root / "config.json").write_text(
        json.dumps({"model_type": "llama", "architectures": ["TinyLM"]}),
        encoding="utf-8",
    )
    (root / "model.safetensors").write_bytes(b"weights")
    (root / ".hugginghack.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "repo_id": "acme/tiny",
                "downloaded_at": "2026-07-24T12:00:00+00:00",
                "total_bytes": 7,
                "file_count": 3,
            }
        ),
        encoding="utf-8",
    )
    fake = FakeS3Client()
    fake.objects["library/acme/tiny/obsolete.bin"] = b"old"
    model_storage = S3ModelStorage(
        settings,
        client=fake,
        transfer_config=object(),
    )

    uri = model_storage.sync_repository("acme/tiny", root)

    assert uri == "s3://model-bucket/library/acme/tiny"
    assert "library/acme/tiny/obsolete.bin" not in fake.objects
    assert fake.uploads[-1] == "library/acme/tiny/.hugginghack.json"
    remote_manifest = json.loads(fake.objects[fake.uploads[-1]])
    assert remote_manifest["storage_backend"] == "s3"
    assert remote_manifest["config"]["model_type"] == "llama"
    discovered = model_storage.discover_repositories()
    assert len(discovered) == 1
    assert discovered[0]["repo_id"] == "acme/tiny"
    assert discovered[0]["cached"] is True
    assert model_storage.health()["connected"] is True

    model_storage.evict_repository_cache("acme/tiny")
    assert not root.exists()
    assert model_storage.discover_repositories()[0]["cached"] is False

    restored = model_storage.restore_repository("acme/tiny")
    assert restored == root
    assert (root / "model.safetensors").read_bytes() == b"weights"
    assert json.loads((root / ".hugginghack.json").read_text(encoding="utf-8"))[
        "status"
    ] == "complete"


def test_s3_restore_rejects_traversal_keys(tmp_path: Path):
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        model_storage_backend="s3",
        s3_bucket="model-bucket",
    )
    fake = FakeS3Client()
    manifest = json.dumps({"status": "complete", "repo_id": "acme/tiny"}).encode()
    fake.objects["models/acme/tiny/.hugginghack.json"] = manifest
    fake.objects["models/acme/tiny/../escape.bin"] = b"nope"
    model_storage = S3ModelStorage(settings, client=fake)

    with pytest.raises(ValueError, match="escapes"):
        model_storage.restore_repository("acme/tiny")

    assert not (tmp_path / "models" / "acme" / "escape.bin").exists()


def test_runtime_target_configuration_and_remote_path_mapping():
    targets = parse_runtime_targets(
        json.dumps(
            [
                {
                    "id": "ollama-rig",
                    "name": "Ollama rig",
                    "kind": "ollama",
                    "base_url": "http://192.168.0.36:11434",
                    "keep_alive": "20m",
                },
                {
                    "id": "vllm-rig",
                    "name": "vLLM rig",
                    "kind": "vllm",
                    "base_url": "http://192.168.0.35:8090",
                    "remote_model_root": "/mnt/nas/models",
                    "token_env": "VLLM_AGENT_TOKEN",
                },
            ]
        )
    )

    assert [target.kind for target in targets] == ["ollama", "vllm"]
    assert targets[0].public()["transfer_mode"] == "blob-upload"
    assert (
        remote_model_path("/mnt/nas/models", "acme/tiny")
        == "/mnt/nas/models/acme/tiny"
    )
    assert (
        remote_model_path(r"Z:\models", "acme/tiny")
        == r"Z:\models\acme\tiny"
    )

    with pytest.raises(ValueError, match="requires remote_model_root"):
        parse_runtime_targets(
            '[{"id":"bad","kind":"vllm","base_url":"http://rig:8090"}]'
        )
    with pytest.raises(ValueError, match="without credentials"):
        parse_runtime_targets(
            '[{"id":"bad","kind":"ollama","base_url":"http://user:pass@rig:11434"}]'
        )


def test_ollama_import_requires_explicit_choice_for_multiple_ggufs(tmp_path: Path):
    root = tmp_path / "models" / "acme" / "quantized"
    root.mkdir(parents=True)
    (root / "model-q4.gguf").write_bytes(b"q4")
    (root / "model-q8.gguf").write_bytes(b"q8")

    with pytest.raises(ValueError, match="multiple GGUF"):
        ollama_files(root)
    assert ollama_files(root, "model-q4.gguf") == [
        (root / "model-q4.gguf").resolve()
    ]
    with pytest.raises(ValueError, match="invalid"):
        ollama_files(root, "../secret.bin")


def _wait_for_runtime_job(
    database: Database, job_id: str, timeout: float = 3
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = database.get_runtime_job(job_id)
        if job and job["status"] in {"ready", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("Runtime job did not finish in time")


def test_runtime_manager_transfers_and_preloads_ollama_model(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    root = storage / "acme" / "tiny-gguf"
    root.mkdir(parents=True)
    (root / "tiny.gguf").write_bytes(b"tiny-gguf-weights")
    settings = Settings(
        model_storage=storage,
        data_dir=(tmp_path / "data").resolve(),
        runtime_targets_json=json.dumps(
            [
                {
                    "id": "ollama-rig",
                    "name": "Ollama rig",
                    "kind": "ollama",
                    "base_url": "http://ollama.test:11434",
                    "keep_alive": "15m",
                }
            ]
        ),
    )
    database = Database(settings.database_path)
    database.initialize()
    user = AuthService(settings, database).create_user(
        "owner", "Owner", "correct horse battery", "admin"
    )
    indexer = LocalModelIndexer(settings, database)
    indexer.scan()
    requests: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request):
        content = request.read()
        requests.append((request.method, request.url.path, content))
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.url.path.startswith("/api/blobs/"):
            return httpx.Response(201)
        if request.url.path == "/api/create":
            return httpx.Response(200, json={"status": "success"})
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={"done": True})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    manager = RuntimeManager(
        settings,
        database,
        client_factory=lambda _: httpx.Client(transport=transport),
    )
    model = database.get_local_model("acme/tiny-gguf")
    assert model is not None
    job = manager.queue(
        "ollama-rig", model, "acme-tiny", None, user_id=user["id"]
    )
    finished = _wait_for_runtime_job(database, job["id"])
    manager.shutdown()

    assert finished["status"] == "ready"
    assert finished["progress"] == 100
    assert any(method == "POST" and path.startswith("/api/blobs/") for method, path, _ in requests)
    create_payload = json.loads(
        next(content for method, path, content in requests if path == "/api/create")
    )
    assert create_payload["model"] == "acme-tiny"
    assert set(create_payload["files"]) == {"tiny.gguf"}
    preload_payload = json.loads(
        next(content for method, path, content in requests if path == "/api/generate")
    )
    assert preload_payload["keep_alive"] == "15m"


def test_runtime_manager_maps_shared_model_path_for_vllm_agent(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    root = storage / "acme" / "tiny"
    root.mkdir(parents=True)
    (root / "config.json").write_text("{}", encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"weights")
    settings = Settings(
        model_storage=storage,
        data_dir=(tmp_path / "data").resolve(),
        runtime_targets_json=json.dumps(
            [
                {
                    "id": "vllm-rig",
                    "name": "vLLM rig",
                    "kind": "vllm",
                    "base_url": "http://vllm-agent.test:8090",
                    "remote_model_root": "/srv/nas/models",
                }
            ]
        ),
    )
    database = Database(settings.database_path)
    database.initialize()
    user = AuthService(settings, database).create_user(
        "owner", "Owner", "correct horse battery", "admin"
    )
    LocalModelIndexer(settings, database).scan()
    payloads: list[dict] = []

    def handler(request: httpx.Request):
        payloads.append(json.loads(request.read()))
        return httpx.Response(200, json={"status": "ready"})

    manager = RuntimeManager(
        settings,
        database,
        client_factory=lambda _: httpx.Client(
            transport=httpx.MockTransport(handler)
        ),
    )
    model = database.get_local_model("acme/tiny")
    assert model is not None
    job = manager.queue(
        "vllm-rig", model, "tiny-chat", None, user_id=user["id"]
    )
    finished = _wait_for_runtime_job(database, job["id"])
    manager.shutdown()

    assert finished["status"] == "ready"
    assert payloads == [
        {
            "repo_id": "acme/tiny",
            "model_path": "/srv/nas/models/acme/tiny",
            "served_model_name": "tiny-chat",
        }
    ]


def test_vllm_agent_confines_requested_models_to_shared_root(tmp_path: Path):
    root = (tmp_path / "models").resolve()
    allowed = root / "acme" / "tiny"
    allowed.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    manager = VllmProcessManager(
        AgentSettings(
            token="test-token",
            model_root=root,
            log_path=(tmp_path / "agent.log").resolve(),
        )
    )

    assert manager._validated_model_path(str(allowed)) == allowed
    with pytest.raises(ValueError, match="must stay inside"):
        manager._validated_model_path(str(outside))


def safetensors_bytes(shapes: dict[str, list[int]]) -> bytes:
    header: dict[str, object] = {"__metadata__": {"format": "pt"}}
    offset = 0
    for name, shape in shapes.items():
        size = 4
        for dimension in shape:
            size *= dimension
        header[name] = {"dtype": "F32", "shape": shape, "data_offsets": [offset, offset + size]}
        offset += size
    encoded = json.dumps(header).encode("utf-8")
    return len(encoded).to_bytes(8, "little") + encoded + b"\0" * offset


def gguf_bytes(tensors: dict[str, list[int]]) -> bytes:
    def string(value: str) -> bytes:
        encoded = value.encode("utf-8")
        return len(encoded).to_bytes(8, "little") + encoded

    output = bytearray(b"GGUF")
    output += (3).to_bytes(4, "little")
    output += len(tensors).to_bytes(8, "little")
    output += (3).to_bytes(8, "little")
    output += string("general.architecture") + (8).to_bytes(4, "little") + string("llama")
    output += string("llama.context_length") + (4).to_bytes(4, "little") + (4096).to_bytes(4, "little")
    output += string("tokenizer.ggml.tokens") + (9).to_bytes(4, "little")
    output += (8).to_bytes(4, "little") + (2).to_bytes(8, "little") + string("<s>") + string("</s>")
    for name, shape in tensors.items():
        output += string(name) + len(shape).to_bytes(4, "little")
        for dimension in shape:
            output += dimension.to_bytes(8, "little")
        output += (0).to_bytes(4, "little") + (0).to_bytes(8, "little")
    return bytes(output)


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


def test_indexer_counts_parameters_from_weight_headers(tmp_path: Path):
    safetensors = tmp_path / "model.safetensors"
    safetensors.write_bytes(safetensors_bytes({"embed": [4, 8], "head": [8, 2]}))
    assert safetensors_parameter_count(safetensors) == 48

    gguf = tmp_path / "model-Q4_K_M.gguf"
    gguf.write_bytes(gguf_bytes({"token_embd.weight": [4, 5], "output.weight": [7]}))
    assert gguf_parameter_count(gguf) == 27
    (tmp_path / "broken.gguf").write_bytes(b"GGUF\x03\x00")
    assert gguf_parameter_count(tmp_path / "broken.gguf") is None

    # Root Transformers shards win over a duplicate consolidated checkpoint.
    (tmp_path / "consolidated.safetensors").write_bytes(safetensors_bytes({"all": [48]}))
    (tmp_path / "pytorch_model.bin").write_bytes(b"pickle")
    facts = repository_facts(tmp_path)
    assert {key: facts[key] for key in ("parameter_count", "formats", "precision")} == {
        "parameter_count": 48,
        "formats": ["gguf", "pytorch", "safetensors"],
        "precision": "fp32",  # no config.json, so the header's dtype decides
    }

    gguf_only = tmp_path / "gguf-only"
    gguf_only.mkdir()
    for shard in (1, 2):
        (gguf_only / f"big-Q8_0-0000{shard}-of-00002.gguf").write_bytes(
            gguf_bytes({f"blk.{shard}.weight": [10, 10]})
        )
    (gguf_only / "big-Q4_0.gguf").write_bytes(gguf_bytes({"blk.weight": [10, 20]}))
    (gguf_only / "mmproj-f16.gguf").write_bytes(gguf_bytes({"vision": [3]}))
    assert repository_facts(gguf_only)["parameter_count"] == 200


def test_database_migrates_local_models_for_catalog_facts(tmp_path: Path):
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
                remote_uri TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO local_models (repo_id, relative_path, modified_at) "
            "VALUES ('acme/model', 'acme/model', 'now')"
        )

    database = Database(database_path)
    database.initialize()

    legacy = database.get_local_model("acme/model")
    assert legacy is not None
    assert legacy["parameter_count"] is None
    assert legacy["formats"] == []


def test_catalog_search_filters_sorts_and_builds_facets():
    models = [
        {
            "repo_id": "acme/chat-7b",
            "relative_path": "acme/chat-7b",
            "pipeline_tag": "text-generation",
            "library_name": "transformers",
            "tags": ["chat"],
            "config": {"model_type": "llama", "architectures": ["LlamaForCausalLM"], "precision": "bf16"},
            "parameter_count": 7_000_000_000,
            "formats": ["safetensors"],
            "size_bytes": 14,
            "file_count": 3,
            "modified_at": "2026-07-01T00:00:00+00:00",
            "storage_backend": "filesystem",
            "cached": True,
        },
        {
            "repo_id": "bartowski/tiny-GGUF",
            "relative_path": "bartowski/tiny-GGUF",
            # The indexer falls back to config.model_type, which is not a task.
            "pipeline_tag": "qwen2",
            "config": {"model_type": "qwen2"},
            "parameter_count": 500_000_000,
            "formats": ["gguf"],
            "size_bytes": 20,
            "file_count": 2,
            "modified_at": "2026-08-01T00:00:00+00:00",
            "storage_backend": "s3",
            "cached": False,
        },
    ]

    everything = search_catalog(models, {"acme/chat-7b"}, hardware_tags={"acme/chat-7b": ["b300", "l40"]})
    assert [item["id"] for item in everything["items"]] == ["bartowski/tiny-GGUF", "acme/chat-7b"]
    assert everything["items"][0]["pipeline_tag"] is None
    assert everything["items"][0]["apps"] == ["llama.cpp", "ollama", "lm-studio"]
    assert everything["items"][1]["apps"] == ["vllm"]
    assert everything["items"][1]["saved"] is True
    assert everything["items"][1]["precision"] == "bf16"
    assert everything["items"][1]["hardware"] == ["l40", "b300"]  # in HARDWARE order
    assert everything["total_bytes"] == 34
    assert everything["facets"]["tasks"] == {"text-generation": 1}
    assert everything["facets"]["precision"] == {"bf16": 1}
    assert everything["facets"]["hardware"] == [
        ["l40", "L40", 1], ["a100", "A100", 0], ["rtx-pro-6000", "RTX PRO 6000", 0], ["b300", "B300", 1],
    ]

    def ids(**filters):
        return [item["id"] for item in search_catalog(models, set(), **filters)["items"]]

    assert ids(sort="size") == ["bartowski/tiny-GGUF", "acme/chat-7b"]
    assert ids(search="chat") == ["acme/chat-7b"]
    assert ids(task="text-generation,feature-extraction") == ["acme/chat-7b"]
    assert ids(precision="bf16") == ["acme/chat-7b"]
    assert ids(precision="fp8,nvfp4") == []
    assert ids(parameters="min:1B,max:7B") == ["acme/chat-7b"]  # both ends inclusive
    assert ids(parameters="min:8B") == []
    assert parse_parameter_range("max:1B") == (None, 1_000_000_000)
    for bad in ({"parameters": "lots"}, {"precision": "fp4"}, {"hardware": "h100"}, {"task": "Text Gen"}):
        with pytest.raises(ValueError):
            search_catalog(models, set(), **bad)


def test_parameter_filter_puts_models_in_the_bucket_their_name_claims():
    """Counted parameters run over the name; the slider must follow the name."""
    real = {
        "meta-llama/Llama-3.2-1B-Instruct": 1_235_814_400,
        "Qwen/Qwen2.5-7B-Instruct": 7_615_616_512,
        "meta-llama/Llama-3.1-8B-Instruct": 8_030_261_248,
        "Qwen/Qwen3-8B-FP8": 8_190_735_360,
        "Qwen/Qwen2.5-14B-Instruct": 14_770_033_664,
        "Qwen/Qwen2.5-32B-Instruct": 32_763_876_352,
        "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-FP8": 31_577_937_600,
        "meta-llama/Llama-3.3-70B-Instruct": 70_553_706_496,
        "mistralai/Mixtral-8x7B-Instruct-v0.1": 46_702_792_704,
        "sentence-transformers/all-MiniLM-L6-v2": 22_713_728,
    }
    models = [
        {"repo_id": repo, "relative_path": repo, "parameter_count": count, "formats": [], "size_bytes": 0}
        for repo, count in real.items()
    ]

    def within(parameters):
        return {item["id"].split("/")[1] for item in search_catalog(models, set(), parameters=parameters)["items"]}

    assert within("max:1B") == {"Llama-3.2-1B-Instruct", "all-MiniLM-L6-v2"}
    assert within("min:3B,max:8B") == {"Qwen2.5-7B-Instruct", "Llama-3.1-8B-Instruct", "Qwen3-8B-FP8"}
    assert within("min:8B,max:14B") == {"Llama-3.1-8B-Instruct", "Qwen3-8B-FP8", "Qwen2.5-14B-Instruct"}
    assert within("min:14B,max:32B") == {
        "Qwen2.5-14B-Instruct", "Qwen2.5-32B-Instruct", "NVIDIA-Nemotron-3-Nano-30B-A3B-FP8",
    }
    # 8x7B is a mixture of experts; the name's 7B is per expert, so the count decides.
    assert within("min:32B,max:70B") == {
        "Qwen2.5-32B-Instruct", "Mixtral-8x7B-Instruct-v0.1", "Llama-3.3-70B-Instruct",
    }
    assert within("min:128B") == set()


def test_library_api_serves_local_data_and_hides_private_uploads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fastapi.testclient import TestClient

    import app.main as main

    storage = (tmp_path / "models").resolve()
    settings = Settings(model_storage=storage, data_dir=(tmp_path / "data").resolve())
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    auth = AuthService(settings, database)
    owner = auth.create_user("owner", "Owner", "correct horse battery", "admin")
    member = auth.create_user("member", "Member", "another secure phrase", "member")
    indexer = LocalModelIndexer(settings, database)
    model_storage = FilesystemModelStorage(settings)
    uploads = UploadManager(settings, database, indexer, model_storage)

    public = storage / "acme" / "tiny-GGUF"
    public.mkdir(parents=True)
    gguf_payload = gguf_bytes({"token_embd.weight": [4, 5], "output.weight": [7]})
    (public / "tiny-Q4_K_M.gguf").write_bytes(gguf_payload)
    (public / "README.md").write_text("# Tiny\n\n![chart](assets/chart.png)\n", encoding="utf-8")
    (public / "assets").mkdir()
    (public / "assets" / "chart.png").write_bytes(PNG_BYTES)
    (public / "assets" / "evil.svg").write_text("<svg onload=alert(1)>", encoding="utf-8")

    private = uploads.create_repository(owner, "secret-model", "", "private")
    for path, payload in {
        "README.md": b"# Secret",
        "secret.gguf": gguf_payload,
        "chart.png": PNG_BYTES,
    }.items():
        uploads.upload_chunk(private["repo_id"], owner["id"], path, 0, len(payload), payload)
    uploads.finalize(private["repo_id"], owner["id"])
    indexer.scan()

    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "indexer", indexer)
    monkeypatch.setattr(main, "storages", StorageRegistry.wrap(model_storage, settings))
    monkeypatch.setattr(main, "catalog", LocalCatalog(settings, model_storage))
    hub_repositories = HubRepositories(settings, database, model_storage)
    monkeypatch.setattr(main, "hub_repositories", hub_repositories)
    monkeypatch.setattr(main, "history", RepoHistory(database, hub_repositories))
    monkeypatch.setattr(main, "uploads", uploads)
    current = {"user": member}
    main.app.dependency_overrides[main.require_user] = lambda: current["user"]
    try:
        client = TestClient(main.app)
        secret = private["repo_id"]

        listing = client.get("/api/library/models").json()
        assert [item["id"] for item in listing["items"]] == ["acme/tiny-GGUF"]
        assert listing["items"][0]["parameter_count"] == 27
        assert listing["items"][0]["formats"] == ["gguf"]
        assert client.get(f"/api/library/models/{secret}").status_code == 404
        assert client.get(
            "/api/library/gguf-range",
            params={"repo_id": secret, "filename": "secret.gguf"},
            headers={"Range": "bytes=0-3"},
        ).status_code == 404
        assert client.get(
            "/api/library/asset", params={"repo_id": secret, "path": "chart.png"}
        ).status_code == 404

        details = client.get("/api/library/models/acme/tiny-GGUF").json()
        assert details["model_card"].startswith("# Tiny")
        assert [file["path"] for file in details["files"]] == [
            "README.md",
            "assets/chart.png",
            "assets/evil.svg",
            "tiny-Q4_K_M.gguf",
        ]
        assert details["local_path"].endswith("/acme/tiny-GGUF")

        header = client.get(
            "/api/library/gguf-range",
            params={"repo_id": "acme/tiny-GGUF", "filename": "tiny-Q4_K_M.gguf"},
            headers={"Range": "bytes=0-3"},
        )
        assert header.status_code == 206
        assert header.content == b"GGUF"
        assert header.headers["content-range"] == f"bytes 0-3/{len(gguf_payload)}"
        tail = client.get(
            "/api/library/gguf-range",
            params={"repo_id": "acme/tiny-GGUF", "filename": "tiny-Q4_K_M.gguf"},
            headers={"Range": "bytes=0-2000000"},
        )
        assert tail.content == gguf_payload

        image = client.get(
            "/api/library/asset",
            params={"repo_id": "acme/tiny-GGUF", "path": "assets/chart.png"},
        )
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        assert image.content == PNG_BYTES
        assert client.get(
            "/api/library/asset",
            params={"repo_id": "acme/tiny-GGUF", "path": "assets/evil.svg"},
        ).status_code == 415
        for path in ("../tiny-GGUF/assets/chart.png", ".hugginghack.json", "/etc/passwd.png"):
            assert client.get(
                "/api/library/asset", params={"repo_id": "acme/tiny-GGUF", "path": path}
            ).status_code in {400, 415}

        current["user"] = owner
        owned = client.get("/api/library/models", params={"sort": "name"}).json()
        assert [item["id"] for item in owned["items"]] == ["acme/tiny-GGUF", secret]
        assert client.get(f"/api/library/models/{secret}").json()["model_card"] == "# Secret"
    finally:
        main.app.dependency_overrides.clear()


def test_library_reads_s3_only_models_through_bounded_ranges(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    settings = Settings(
        model_storage=storage,
        data_dir=(tmp_path / "data").resolve(),
        model_storage_backend="s3",
        s3_bucket="model-bucket",
    )
    root = storage / "acme" / "tiny"
    root.mkdir(parents=True)
    gguf_payload = gguf_bytes({"weight": [3, 3]})
    (root / "tiny.gguf").write_bytes(gguf_payload)
    (root / "README.md").write_text("# Remote card", encoding="utf-8")
    (root / ".hugginghack.json").write_text(
        json.dumps({"status": "complete", "repo_id": "acme/tiny"}), encoding="utf-8"
    )
    fake = FakeS3Client()
    model_storage = S3ModelStorage(settings, client=fake, transfer_config=object())
    model_storage.sync_repository("acme/tiny", root)
    remote_manifest = json.loads(fake.objects["models/acme/tiny/.hugginghack.json"])
    assert remote_manifest["parameter_count"] == 9
    assert remote_manifest["formats"] == ["gguf"]

    model_storage.evict_repository_cache("acme/tiny")
    discovered = model_storage.discover_repositories()[0]
    assert discovered["cached"] is False
    assert discovered["parameter_count"] == 9
    assert discovered["formats"] == ["gguf"]

    catalog = LocalCatalog(settings, model_storage)
    model = {
        "repo_id": "acme/tiny",
        "relative_path": "acme/tiny",
        "storage_backend": "s3",
        "cached": False,
    }
    listing = model_storage.list_repository_files("acme/tiny")
    details = catalog.details(model, listing, set())
    assert details["model_card"] == "# Remote card"
    assert [file["path"] for file in details["files"]] == ["README.md", "tiny.gguf"]
    header = catalog.gguf_range(model, "tiny.gguf", "bytes=0-3")
    assert header["content"] == b"GGUF"
    assert header["headers"]["Content-Range"] == f"bytes 0-3/{len(gguf_payload)}"
    with pytest.raises(ValueError):
        catalog.read_bytes(model, ".hugginghack.json")
    with pytest.raises(FileNotFoundError):
        catalog.read_bytes(model, "missing.gguf")


def test_indexer_reads_model_card_metadata_for_copied_folders(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    settings = Settings(model_storage=storage, data_dir=(tmp_path / "data").resolve())
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    root = storage / "acme" / "copied-embedder"
    root.mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"model_type": "bert"}), encoding="utf-8")
    (root / "model.safetensors").write_bytes(safetensors_bytes({"embed": [3, 4]}))
    (root / "README.md").write_text(
        "---\n"
        "pipeline_tag: sentence-similarity\n"
        "library_name: sentence-transformers\n"
        "license: apache-2.0\n"
        "tags:\n  - embeddings\n  - 42\n"
        "---\n# Embedder\n",
        encoding="utf-8",
    )

    model = LocalModelIndexer(settings, database).index_path(root)

    assert model["pipeline_tag"] == "sentence-similarity"
    assert model["library_name"] == "sentence-transformers"
    assert model["license"] == "apache-2.0"
    assert model["tags"] == ["embeddings"]
    assert model["parameter_count"] == 12
    assert model["formats"] == ["safetensors"]
    assert model["managed"] is False
