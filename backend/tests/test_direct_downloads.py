"""Direct downloads: clients fetch bucket files through short-lived signed links
(docs/SERVE_FROM_S3.md, "Direct downloads"; docs/SCALING.md phase 1)."""

import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.auth import AuthService
from app.catalog import LocalCatalog
from app.config import Settings
from app.database import Database
from app.git_mirror import GitMirrors
from app.history import RepoHistory
from app.hub_api import HubRepositories
from app.indexer import LocalModelIndexer
from app.storage import S3ModelStorage, S3TargetConfig, StorageRegistry
from app.uploads import UploadManager
from test_core import FakeS3Client
from test_storage_targets import publish

PASSWORD = "correct horse battery"
WEIGHTS = bytes(range(256)) * (9 * 4096)  # 9 MiB: above the library/file threshold
CONFIG = b'{"model_type": "llama"}'


def bucket(settings: Settings, target_id: str, fake: FakeS3Client, **options) -> S3ModelStorage:
    """A bucket target whose requests go to `fake`, but whose links are signed by a
    real boto3 client, offline, as in production."""
    storage = S3ModelStorage(
        settings,
        target=S3TargetConfig(
            id=target_id,
            name=target_id.title(),
            bucket=target_id,
            prefix="models",
            endpoint_url="http://grid-internal:9000",
            public_endpoint_url="https://s3.grid.example",
            region="us-east-1",
            access_key_id="AKIDEXAMPLE",
            secret_access_key="secret-key-example",
            addressing_style="path",
            **options,
        ),
    )
    storage.client = storage.read_client = storage.health_client = fake
    return storage


@pytest.fixture()
def direct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        accounts_enabled=True,
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    fakes = {"grid": FakeS3Client(), "plain": FakeS3Client()}
    grid = bucket(settings, "grid", fakes["grid"], direct_downloads=True, presign_ttl_seconds=600)
    plain = bucket(settings, "plain", fakes["plain"])
    registry = StorageRegistry(settings, [grid, plain], default_target="grid")
    indexer = LocalModelIndexer(settings, database)
    repositories = HubRepositories(settings, database, registry)
    history = RepoHistory(database, repositories)
    uploads = UploadManager(settings, database, indexer, registry, history)
    auth = AuthService(settings, database)
    for name, value in {
        "settings": settings,
        "database": database,
        "auth": auth,
        "indexer": indexer,
        "storages": registry,
        "uploads": uploads,
        "catalog": LocalCatalog(settings, registry),
        "hub_repositories": repositories,
        "history": history,
        "git_mirrors": GitMirrors(repositories),
    }.items():
        monkeypatch.setattr(main, name, value)
    owner = auth.create_user("owner", "Owner", PASSWORD, "admin")

    # Public, only in the direct bucket.
    publish(fakes["grid"], "models", "acme/open", {"config.json": CONFIG, "model.safetensors": WEIGHTS})
    # Only in a bucket that does not hand out links.
    publish(fakes["plain"], "models", "acme/plain", {"config.json": CONFIG, "model.safetensors": WEIGHTS})
    # On local disk.
    local = settings.model_storage / "acme" / "disk"
    local.mkdir(parents=True)
    (local / "config.json").write_bytes(CONFIG)
    (local / "model.safetensors").write_bytes(WEIGHTS)

    # Uploaded to the direct bucket: one private and evicted, one public and still cached here.
    for slug, visibility, keep_cache in (("secret", "private", False), ("cached", "public", True)):
        repository = uploads.create_repository(owner, slug, "", visibility, storage_target="grid")
        for path, payload in {"config.json": CONFIG, "model.safetensors": WEIGHTS}.items():
            chunk = 4 * 1024 * 1024
            for offset in range(0, len(payload), chunk):
                uploads.upload_chunk(
                    repository["repo_id"], owner["id"], path, offset, len(payload), payload[offset : offset + chunk]
                )
        uploads.finalize(repository["repo_id"], owner["id"])
        if not keep_cache:
            grid.evict_repository_cache(repository["repo_id"])
            database.set_local_model_cached(repository["repo_id"], False)
    main.refresh_model_index()
    token, _ = auth.create_api_token(owner["id"], "puller", "read", None)
    return {"settings": settings, "grid": grid, "fakes": fakes, "token": token, "repositories": repositories}


def anonymous() -> TestClient:
    return TestClient(main.app, follow_redirects=False)


def signed_in() -> TestClient:
    client = TestClient(main.app, follow_redirects=False)
    response = client.post("/api/auth/login", json={"username": "owner", "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def assert_signed_link(location: str, key: str) -> dict[str, list[str]]:
    parts = urlsplit(location)
    # Signed for the address clients reach, never the one this server uses.
    assert parts.netloc == "s3.grid.example"
    assert parts.path == f"/grid/{key}"
    query = parse_qs(parts.query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    return query


def test_a_pull_from_a_direct_bucket_is_redirected_with_hub_headers(direct):
    client = anonymous()
    head = client.head("/acme/open/resolve/main/model.safetensors")
    # HEAD answers itself: a bucket refuses HEAD on a link signed for GET.
    assert head.status_code == 200
    assert "location" not in head.headers
    assert head.headers["content-length"] == str(len(WEIGHTS))
    etag, commit = head.headers["etag"], head.headers["x-repo-commit"]

    response = client.get("/acme/open/resolve/main/model.safetensors", headers={"Range": "bytes=10-19"})
    assert response.status_code == 302
    assert response.content == b""
    query = assert_signed_link(response.headers["location"], "models/acme/open/model.safetensors")
    assert query["X-Amz-Expires"] == ["600"]
    assert "response-content-disposition" not in query
    assert response.headers["x-linked-size"] == str(len(WEIGHTS))
    assert response.headers["x-linked-etag"] == response.headers["etag"] == etag
    assert response.headers["x-repo-commit"] == commit
    assert response.headers["accept-ranges"] == "bytes"

    # Several ranges are refused before any link is signed, as for streamed files.
    refused = client.get("/acme/open/resolve/main/model.safetensors", headers={"Range": "bytes=0-1,5-6"})
    assert refused.status_code == 416 and "location" not in refused.headers


def test_links_are_signed_only_after_the_access_check(direct, monkeypatch: pytest.MonkeyPatch):
    path = "/owner/secret/resolve/main/model.safetensors"
    refused = anonymous().get(path)
    assert refused.status_code == 401
    assert "location" not in refused.headers
    wrong = anonymous().get(path, headers={"Authorization": "Bearer hht_not-a-real-token"})
    assert wrong.status_code == 401 and "location" not in wrong.headers

    allowed = anonymous().get(path, headers={"Authorization": f"Bearer {direct['token']}"})
    assert allowed.status_code == 302
    assert_signed_link(allowed.headers["location"], "models/owner/secret/model.safetensors")

    # With anonymous pulls off, public models need a token too.
    settings = replace(direct["settings"], hub_api_enabled=False)
    monkeypatch.setattr(direct["repositories"], "settings", settings)
    assert anonymous().get("/acme/open/resolve/main/config.json").status_code == 401
    tokened = anonymous().get(
        "/acme/open/resolve/main/config.json", headers={"Authorization": f"Bearer {direct['token']}"}
    )
    assert tokened.status_code == 302


def test_files_this_server_holds_or_that_have_no_links_are_streamed(direct):
    client = anonymous()
    for repo_id in ("acme/disk", "owner/cached", "acme/plain"):
        response = client.get(f"/{repo_id}/resolve/main/model.safetensors", headers={"Range": "bytes=0-9"})
        assert response.status_code == 206, repo_id
        assert response.content == WEIGHTS[:10], repo_id
        assert "location" not in response.headers


def test_git_lfs_gets_signed_links_for_bucket_weights_only(direct):
    client = anonymous()

    def batch(repo_id: str) -> dict:
        mirror = main.git_mirrors.ensure(repo_id)
        [oid] = list(mirror.lfs)
        response = client.post(
            f"/{repo_id}.git/info/lfs/objects/batch",
            json={"operation": "download", "objects": [{"oid": oid, "size": len(WEIGHTS)}]},
            headers={"Accept": "application/vnd.git-lfs+json"},
        )
        assert response.status_code == 200, response.text
        return response.json()["objects"][0]

    remote = batch("acme/open")
    assert remote["authenticated"] is False
    assert "header" not in remote["actions"]["download"]
    assert remote["actions"]["download"]["expires_in"] == 600
    assert_signed_link(remote["actions"]["download"]["href"], "models/acme/open/model.safetensors")

    local = batch("acme/disk")
    assert local["authenticated"] is True
    assert urlsplit(local["actions"]["download"]["href"]).netloc == "testserver"


def test_the_browser_download_gets_a_link_only_for_large_files(direct):
    client = signed_in()
    big = client.get("/api/library/file", params={"repo_id": "owner/secret", "path": "model.safetensors"})
    assert big.status_code == 302
    query = assert_signed_link(big.headers["location"], "models/owner/secret/model.safetensors")
    # Another origin ignores the page's download attribute, so the link names the file.
    assert query["response-content-disposition"] == [
        "attachment; filename=\"model.safetensors\"; filename*=UTF-8''model.safetensors"
    ]
    assert "content-disposition" not in big.headers

    small = client.get("/api/library/file", params={"repo_id": "owner/secret", "path": "config.json"})
    assert small.status_code == 200
    assert small.content == CONFIG
    assert small.headers["content-disposition"].startswith('attachment; filename="config.json"')

    assert anonymous().get(
        "/api/library/file", params={"repo_id": "owner/secret", "path": "model.safetensors"}
    ).status_code == 401


def test_signed_links_never_reach_error_texts(direct):
    link = direct["grid"].presigned_get("acme/open", "model.safetensors")
    signature = parse_qs(urlsplit(link).query)["X-Amz-Signature"][0]
    assert signature not in direct["grid"].redact(json.dumps({"detail": f"GET {link} failed"}))
