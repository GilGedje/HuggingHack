import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.auth import AuthService
from app.catalog import LocalCatalog
from app.config import Settings
from app.database import Database
from app.history import RepoHistory
from app.hub_api import HubError, HubRepositories
from app.indexer import LocalModelIndexer
from app.storage import S3ModelStorage, S3TargetConfig, StorageRegistry
from app.uploads import UploadManager
from test_core import FakeS3Client
from test_storage_targets import publish


@pytest.fixture()
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage_path = (tmp_path / "models").resolve()
    settings = Settings(model_storage=storage_path, data_dir=(tmp_path / "data").resolve())
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    bucket = FakeS3Client()
    remote = S3ModelStorage(
        settings,
        client=bucket,
        transfer_config=object(),
        target=S3TargetConfig(id="lake", name="Lake", bucket="lake"),
    )
    registry = StorageRegistry(settings, [remote])
    indexer = LocalModelIndexer(settings, database)
    repositories = HubRepositories(settings, database, registry)
    history = RepoHistory(database, repositories)
    uploads = UploadManager(settings, database, indexer, registry, history)
    for name, value in {
        "settings": settings,
        "database": database,
        "indexer": indexer,
        "storages": registry,
        "uploads": uploads,
        "catalog": LocalCatalog(settings, registry),
        "hub_repositories": repositories,
        "history": history,
    }.items():
        monkeypatch.setattr(main, name, value)
    auth = AuthService(settings, database)
    root = storage_path / "acme" / "local-model"
    root.mkdir(parents=True)
    (root / "config.json").write_text('{"hidden_size": 64}\n', encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"\0" * 32)
    publish(bucket, "models", "acme/lake-model", {"config.json": b'{"a": 1}\n', "w.bin": b"12345"})
    return {
        "settings": settings,
        "database": database,
        "root": root,
        "bucket": bucket,
        "uploads": uploads,
        "history": history,
        "repositories": repositories,
        "admin": auth.create_user("admin", "Admin", "correct horse battery", "admin"),
        "member": auth.create_user("member", "Member", "another secure phrase", "member"),
        "owner": auth.create_user("owner", "Owner", "yet another passphrase", "member"),
    }


def commits(database: Database, repo_id: str) -> list[dict]:
    return database.list_commits(repo_id, full=True)


def test_scans_record_one_initial_import_then_only_real_changes(library):
    database = library["database"]
    main.refresh_model_index()
    main.refresh_model_index()
    for repo_id in ("acme/local-model", "acme/lake-model"):
        [initial] = commits(database, repo_id)
        assert initial["message"] == "Initial import"
        assert initial["author_name"] == "HuggingHack"
        assert initial["summary"]["added"] == 2

    root = library["root"]
    (root / "config.json").write_text('{"hidden_size": 128}\n', encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"\0" * 64)
    (root / "README.md").write_text("# Card\n", encoding="utf-8")
    main.refresh_model_index()
    latest, _ = commits(database, "acme/local-model")
    assert latest["message"] == "Detected changes in storage"
    changes = {change["path"]: change for change in latest["changes"]}
    assert changes["README.md"]["change"] == "added"
    assert changes["config.json"]["change"] == "modified"
    assert changes["model.safetensors"] == {
        **changes["model.safetensors"],
        "change": "modified",
        "old_size": 32,
        "new_size": 64,
    }
    diff = library["history"].diff(changes["config.json"])
    assert diff["binary"] is False
    assert '-{"hidden_size": 64}' in diff["diff"]
    assert '+{"hidden_size": 128}' in diff["diff"]
    assert library["history"].diff(changes["model.safetensors"])["binary"] is True

    # Same content rewritten (new timestamp) is not a change for text files.
    os.utime(root / "README.md", ns=(1, 1))
    (root / "README.md").write_text("# Card\n", encoding="utf-8")
    (root / "config.json").unlink()
    main.refresh_model_index()
    latest = commits(database, "acme/local-model")[0]
    assert [(c["path"], c["change"]) for c in latest["changes"]] == [("config.json", "deleted")]


def test_restoring_and_evicting_s3_cache_is_not_a_change(library):
    database = library["database"]
    main.refresh_model_index()
    storage = main.storages.get("lake")
    storage.restore_repository("acme/lake-model")
    main.refresh_model_index()
    storage.evict_repository_cache("acme/lake-model")
    main.refresh_model_index()
    assert len(commits(database, "acme/lake-model")) == 1


def test_changes_are_staged_until_committed(library):
    database = library["database"]
    admin = library["admin"]
    uploads = library["uploads"]
    main.refresh_model_index()

    session = uploads.start_change("acme/local-model", admin)
    payload = b'{"hidden_size": 256}\n'
    uploads.change_chunk(session["id"], admin, "config.json", 0, len(payload), payload)
    uploads.change_chunk(session["id"], admin, "extra/notes.txt", 0, 2, b"hi")
    # Pulls keep seeing the committed files while the change is staged.
    snapshot = library["repositories"].snapshot("acme/local-model")
    assert {entry.path for entry in snapshot.entries} == {"config.json", "model.safetensors"}
    assert library["repositories"].read_all(snapshot, snapshot.entry("config.json")) == (
        b'{"hidden_size": 64}\n'
    )

    result = uploads.commit_change(
        session["id"],
        admin,
        "Bump hidden size",
        "Retrained with a wider layer.",
        deletions=["model.safetensors"],
    )
    commit = result["commit"]
    assert commit["message"] == "Bump hidden size"
    assert commit["author_name"] == "Admin"
    assert commit["summary"] == {"added": 1, "modified": 1, "deleted": 1}
    root = library["root"]
    assert (root / "config.json").read_bytes() == payload
    assert (root / "extra" / "notes.txt").read_bytes() == b"hi"
    assert not (root / "model.safetensors").exists()
    assert not any((library["settings"].model_storage / ".hugginghack-staging").glob("*/*"))
    with pytest.raises(FileNotFoundError):
        uploads.change_file_status(session["id"], admin, "config.json")

    # Nothing to commit is rejected; an aborted session leaves no trace.
    empty = uploads.start_change("acme/local-model", admin)
    with pytest.raises(ValueError):
        uploads.commit_change(empty["id"], admin, "Nothing")
    uploads.abort_change(empty["id"], admin)
    assert len(commits(database, "acme/local-model")) == 2


def test_change_to_s3_only_model_updates_the_bucket_directly(library):
    admin = library["admin"]
    uploads = library["uploads"]
    bucket = library["bucket"]
    main.refresh_model_index()
    before = dict(bucket.modified)

    session = uploads.start_change("acme/lake-model", admin)
    uploads.change_chunk(session["id"], admin, "README.md", 0, 7, b"# Lake\n")
    result = uploads.commit_change(session["id"], admin, "Add model card", deletions=["w.bin"])

    assert bucket.objects["models/acme/lake-model/README.md"] == b"# Lake\n"
    assert "models/acme/lake-model/w.bin" not in bucket.objects
    assert bucket.modified.get("models/acme/lake-model/config.json") == before.get(
        "models/acme/lake-model/config.json"
    )
    assert "models/acme/lake-model/config.json" not in bucket.uploads
    manifest = json.loads(bucket.objects["models/acme/lake-model/.hugginghack.json"])
    assert manifest["status"] == "complete"
    assert manifest["storage_target"] == "lake"
    assert not (library["settings"].model_storage / "acme" / "lake-model").exists()
    assert result["commit"]["summary"] == {"added": 1, "modified": 0, "deleted": 1}
    assert result["model"]["file_count"] == 2
    main.refresh_model_index()
    assert len(commits(library["database"], "acme/lake-model")) == 2


def test_history_api_permissions(library):
    uploads = library["uploads"]
    owner = library["owner"]
    repository = uploads.create_repository(owner, "secret", "", "private")
    uploads.upload_chunk(repository["repo_id"], owner["id"], "config.json", 0, 2, b"{}")
    uploads.finalize(repository["repo_id"], owner["id"], "First upload", "Base weights")
    main.refresh_model_index()
    [upload_commit] = commits(library["database"], "owner/secret")
    assert upload_commit["message"] == "First upload"
    assert upload_commit["author_name"] == "Owner"

    current = {"user": library["member"]}
    main.app.dependency_overrides[main.require_user] = lambda: current["user"]
    main.app.dependency_overrides[main.require_write_user] = lambda: current["user"]
    try:
        client = TestClient(main.app)
        assert client.get("/api/library/commits", params={"repo_id": "owner/secret"}).status_code == 404
        assert client.post("/api/repos/changes", json={"repo_id": "owner/secret"}).status_code == 404
        # Members cannot change repositories they do not own.
        assert client.post("/api/repos/changes", json={"repo_id": "acme/local-model"}).status_code == 403
        details = client.get("/api/library/models/acme/local-model").json()
        assert details["can_edit"] is False
        assert details["commit_count"] == 1
        assert details["latest_commit"]["message"] == "Initial import"
        assert all(file["last_commit"]["message"] == "Initial import" for file in details["files"])

        current["user"] = owner
        listing = client.get("/api/library/commits", params={"repo_id": "owner/secret"}).json()
        assert listing["total"] == 1
        commit_id = listing["items"][0]["id"]
        detail = client.get(
            "/api/library/commit", params={"repo_id": "owner/secret", "commit_id": commit_id}
        ).json()
        assert detail["description"] == "Base weights"
        assert detail["changes"][0]["diff"] == ["--- /dev/null", "+++ b/config.json", "@@ -0,0 +1 @@", "+{}"]

        started = client.post("/api/repos/changes", json={"repo_id": "owner/secret"})
        assert started.status_code == 201
        session_id = started.json()["id"]
        chunk = client.put(
            f"/api/repos/changes/{session_id}/files",
            params={"path": "README.md"},
            headers={"Upload-Offset": "0", "Upload-Length": "5"},
            content=b"# Hi\n",
        )
        assert chunk.json()["complete"] is True
        committed = client.post(
            f"/api/repos/changes/{session_id}/commit", json={"message": "Add card"}
        ).json()
        assert committed["commit"]["summary"]["added"] == 1
        download = client.get(
            "/api/library/file", params={"repo_id": "owner/secret", "path": "README.md"}
        )
        assert download.content == b"# Hi\n"
        assert "attachment" in download.headers["content-disposition"]
        started = client.post("/api/repos/changes", json={"repo_id": "owner/secret"}).json()
        name = "résumé notes.txt"
        client.put(
            f"/api/repos/changes/{started['id']}/files",
            params={"path": name},
            headers={"Upload-Offset": "0", "Upload-Length": "2"},
            content=b"ok",
        )
        client.post(f"/api/repos/changes/{started['id']}/commit", json={"message": "Unicode"})
        unicode_download = client.get(
            "/api/library/file", params={"repo_id": "owner/secret", "path": name}
        )
        assert unicode_download.status_code == 200
        assert "filename*=UTF-8''r%C3%A9sum%C3%A9%20notes.txt" in unicode_download.headers["content-disposition"]

        # Another user's session id is not usable.
        current["user"] = library["admin"]
        other = client.post("/api/repos/changes", json={"repo_id": "acme/local-model"}).json()
        current["user"] = owner
        assert client.delete(f"/api/repos/changes/{other['id']}").status_code == 404
    finally:
        main.app.dependency_overrides.clear()
    with pytest.raises(HubError):
        library["repositories"].snapshot("owner/secret")


def test_staging_is_hidden_from_hub_listing(library):
    admin = library["admin"]
    main.refresh_model_index()
    session = library["uploads"].start_change("acme/local-model", admin)
    library["uploads"].change_chunk(session["id"], admin, "new.bin", 0, 3, b"abc")
    staging = library["settings"].model_storage / ".hugginghack-staging"
    assert staging.is_dir()
    main.refresh_model_index()
    assert "acme/local-model" in {m["repo_id"] for m in library["database"].list_local_models()}
    assert not any(".hugginghack-staging" in m["relative_path"] for m in library["database"].list_local_models())
    assert httpx is not None


def test_git_mirror_commit_uses_the_latest_history_message(library):
    import zlib

    from app.git_mirror import GitMirrors

    main.refresh_model_index()
    [latest] = commits(library["database"], "acme/local-model")
    mirror = GitMirrors(library["repositories"]).ensure("acme/local-model")
    raw = zlib.decompress(
        (mirror.root / "objects" / mirror.commit[:2] / mirror.commit[2:]).read_bytes()
    ).decode("utf-8")
    assert "author HuggingHack <hugginghack@localhost>" in raw
    assert raw.rstrip().endswith(f"Initial import\n\nHuggingHack-Commit: {latest['id']}")
