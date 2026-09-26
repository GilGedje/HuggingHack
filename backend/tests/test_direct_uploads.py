"""Direct uploads: the browser sends each file's parts straight to the bucket through
signed links, and HuggingHack only starts, checks and publishes them
(docs/SERVE_FROM_S3.md, "Direct uploads"; docs/SCALING.md phase 2)."""

import json
import os
import struct
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import app.main as main
from app.auth import AuthService
from app.catalog import LocalCatalog
from app.config import Settings
from app.database import Database
from app.git_mirror import GitMirrors
from app.history import RepoHistory
from app.hub_api import HubRepositories
from app.indexer import LocalModelIndexer, hidden_path
from app.storage import (
    CHANGES_DIRECTORY,
    PENDING_NAME,
    FilesystemModelStorage,
    S3ModelStorage,
    S3TargetConfig,
    StorageRegistry,
    part_size_for,
)
from app.uploads import STALE_CHANGE_SECONDS, UploadManager, iso_seconds_ago
from test_core import FakeS3Client
from test_storage_targets import publish

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
PASSWORDS = {"owner": "owner password 1", "member": "member password 1", "viewer": "viewer password 1"}
MB = 1024**2


class MultipartS3Client(FakeS3Client):
    """The fake bucket, plus multipart uploads and server-side copies. `put_part`
    stands in for the browser's PUT to a signed part link."""

    def __init__(self):
        super().__init__()
        self.multipart: dict[str, dict] = {}
        self.copies: list[tuple[str, str]] = []

    def create_multipart_upload(self, *, Bucket: str, Key: str, **kwargs):
        upload_id = uuid4().hex
        self.multipart[upload_id] = {"key": Key, "parts": {}}
        return {"UploadId": upload_id}

    def put_part(self, upload_id: str, number: int, data: bytes) -> None:
        self.multipart[upload_id]["parts"][number] = data

    def list_parts(self, *, Bucket: str, Key: str, UploadId: str, PartNumberMarker: int = 0, MaxParts: int = 1000):
        upload = self.multipart.get(UploadId)
        if upload is None or upload["key"] != Key:
            raise ClientError({"Error": {"Code": "NoSuchUpload", "Message": "gone"}}, "ListParts")
        numbers = sorted(number for number in upload["parts"] if number > PartNumberMarker)
        page = numbers[:MaxParts]
        return {
            "Parts": [
                {"PartNumber": number, "Size": len(upload["parts"][number]), "ETag": f'"etag-{number}"'}
                for number in page
            ],
            "IsTruncated": len(numbers) > MaxParts,
            "NextPartNumberMarker": page[-1] if page else 0,
        }

    def complete_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str, MultipartUpload: dict):
        upload = self.multipart.pop(UploadId)
        numbers = [part["PartNumber"] for part in MultipartUpload["Parts"]]
        assert numbers == sorted(numbers) and upload["key"] == Key
        self.objects[Key] = b"".join(upload["parts"][number] for number in numbers)
        self._touch(Key)
        return {}

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str):
        if self.multipart.pop(UploadId, None) is None:
            raise ClientError({"Error": {"Code": "NoSuchUpload", "Message": "gone"}}, "AbortMultipartUpload")
        return {}

    def copy(self, CopySource: dict, Bucket: str, Key: str, **kwargs):
        self.objects[Key] = self.objects[CopySource["Key"]]
        self.copies.append((CopySource["Key"], Key))
        self._touch(Key)


def bucket(settings: Settings, target_id: str, fake: FakeS3Client, **options) -> S3ModelStorage:
    """A bucket whose requests go to `fake` but whose links a real boto3 client signs, offline."""
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


def safetensors(parameters: int, size: int) -> bytes:
    """A SafeTensors file of `size` bytes whose header lists `parameters` weights."""
    header = json.dumps({"w": {"dtype": "F32", "shape": [parameters], "data_offsets": [0, parameters * 4]}}).encode()
    body = struct.pack("<Q", len(header)) + header
    return body + bytes(size - len(body))


WEIGHTS = safetensors(1234, 11 * MB)  # three parts of 5 MB
CARD = b"---\nlicense: mit\npipeline_tag: text-generation\n---\n# Direct\n"
CONFIG = b'{"model_type": "llama"}'


@pytest.fixture(params=["sqlite", "postgresql"])
def database_url(request, tmp_path: Path):
    """Every test here runs on both databases: SQLite, and a PostgreSQL database of
    its own that is dropped afterwards."""
    if request.param == "sqlite":
        yield str(tmp_path / "data" / "hugginghack.sqlite3")
        return
    if not POSTGRES_URL:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    import psycopg

    name = f"hh_direct_{uuid4().hex[:12]}"
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    parts = urlsplit(POSTGRES_URL)
    try:
        yield urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))
    finally:
        with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture()
def direct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_url: str):
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        accounts_enabled=True,
        max_upload_size_gb=1,
        database_url="" if database_url.endswith(".sqlite3") else database_url,
    )
    settings.ensure_directories()
    database = Database(database_url)
    database.initialize()
    monkeypatch.setattr(main, "database", database)
    fakes = {"grid": MultipartS3Client(), "plain": MultipartS3Client()}
    grid = bucket(settings, "grid", fakes["grid"], direct_uploads=True, direct_downloads=True, part_size_mb=5)
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
    for username, role in (("owner", "admin"), ("member", "member"), ("viewer", "viewer")):
        auth.create_user(username, username.title(), PASSWORDS[username], role)
    yield {"settings": settings, "database": database, "uploads": uploads, "grid": grid, "fakes": fakes}
    database.close()


def login(username: str) -> TestClient:
    client = TestClient(main.app, follow_redirects=False)
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORDS[username]})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


def send(fake: MultipartS3Client, links: list[dict], data: bytes, part_size: int, only: set[int] | None = None) -> None:
    """What the browser does with signed part links: PUT each slice to its URL."""
    for link in links:
        number = link["number"]
        if only is not None and number not in only:
            continue
        parts = urlsplit(link["url"])
        assert parts.netloc == "s3.grid.example"
        query = parse_qs(parts.query)
        assert query["partNumber"] == [str(number)] and "X-Amz-Signature" in query
        chunk = data[(number - 1) * part_size : number * part_size]
        assert len(chunk) == link["size"]
        fake.put_part(query["uploadId"][0], number, chunk)


def upload(client: TestClient, base: str, params: dict, fake: MultipartS3Client, path: str, data: bytes, only=None) -> dict:
    begun = client.post(f"{base}/begin", params=params, json={"path": path, "size": len(data)})
    assert begun.status_code == 200, begun.text
    state = begun.json()
    assert state["direct"] is True
    missing = [number for number in range(1, state["part_count"] + 1) if number not in state["done"]]
    if missing:
        links = client.post(f"{base}/parts", params=params, json={"path": path, "parts": missing})
        assert links.status_code == 200, links.text
        send(fake, links.json()["parts"], data, state["part_size"], only)
    return client.post(f"{base}/complete", params=params, json={"path": path})


def new_repository(client: TestClient, slug: str, target: str = "grid") -> str:
    response = client.post(
        "/api/uploads/repositories", json={"slug": slug, "visibility": "public", "storage_target": target}
    )
    assert response.status_code == 201, response.text
    return response.json()["repo_id"]


def publish_repository(direct, client: TestClient, slug: str, files: dict[str, bytes]) -> str:
    repo_id = new_repository(client, slug)
    for path, data in files.items():
        assert upload(client, "/api/uploads/repositories/files", {"repo_id": repo_id}, direct["fakes"]["grid"], path, data).status_code == 200
    finalized = client.post("/api/uploads/repositories/finalize", params={"repo_id": repo_id}, json={})
    assert finalized.status_code == 200, finalized.text
    return repo_id


def test_a_new_repository_uploads_straight_to_the_bucket_and_appears_only_once_finalized(direct):
    fake = direct["fakes"]["grid"]
    owner = login("owner")
    repo_id = new_repository(owner, "direct")
    # Nothing is kept on this server's disk for it.
    assert not (direct["settings"].model_storage / "owner" / "direct").exists()
    base, params = "/api/uploads/repositories/files", {"repo_id": repo_id}

    begun = owner.post(f"{base}/begin", params=params, json={"path": "model.safetensors", "size": len(WEIGHTS)}).json()
    assert begun == {
        "direct": True, "path": "model.safetensors", "size": len(WEIGHTS), "part_size": 5 * MB,
        "part_count": 3, "complete": False, "done": [],
    }
    links = owner.post(f"{base}/parts", params=params, json={"path": "model.safetensors", "parts": [1, 2, 3]})
    assert links.status_code == 200
    assert [link["size"] for link in links.json()["parts"]] == [5 * MB, 5 * MB, MB]
    send(fake, links.json()["parts"], WEIGHTS, 5 * MB, only={1, 2})

    # A reload asks again: what reached the bucket comes from the bucket.
    again = owner.post(f"{base}/begin", params=params, json={"path": "model.safetensors", "size": len(WEIGHTS)})
    assert again.json()["done"] == [1, 2]
    missing = owner.post(f"{base}/complete", params=params, json={"path": "model.safetensors"})
    assert missing.status_code == 409
    assert missing.json()["detail"] == (
        "Part 3 of model.safetensors has not reached the storage yet. Retry the upload to send it again."
    )
    # A file keeps the size it started with.
    resized = owner.post(f"{base}/begin", params=params, json={"path": "model.safetensors", "size": len(WEIGHTS) + 1})
    assert resized.status_code == 409 and "was started as" in resized.json()["detail"]

    assert upload(owner, base, params, fake, "model.safetensors", WEIGHTS).status_code == 200
    assert upload(owner, base, params, fake, "config.json", CONFIG).status_code == 200
    # The third file is started but not finished.
    third = owner.post(f"{base}/begin", params=params, json={"path": "README.md", "size": len(CARD)}).json()
    assert third["part_count"] == 1 and third["done"] == []

    # Two finished files sit at their final keys, yet nothing lists the repository:
    # it has no manifest until it is finalized, however often the library is scanned.
    assert fake.objects["models/owner/direct/model.safetensors"] == WEIGHTS
    for _ in range(2):
        main.refresh_model_index()
        assert direct["database"].get_local_model(repo_id) is None
    assert TestClient(main.app).get(f"/api/models/{repo_id}").status_code == 401
    early = owner.post("/api/uploads/repositories/finalize", params=params, json={})
    assert early.status_code == 409 and early.json()["detail"] == "Finish all file uploads before finalizing the repository."

    assert upload(owner, base, params, fake, "README.md", CARD).status_code == 200
    finalized = owner.post("/api/uploads/repositories/finalize", params=params, json={"message": "First upload"})
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["status"] == "ready"

    model = direct["database"].get_local_model(repo_id)
    assert model["storage_target"] == "grid" and not model["cached"]
    # Described from the bucket: the card and the weight header, read by range.
    assert (model["pipeline_tag"], model["license"], model["parameter_count"]) == ("text-generation", "mit", 1234)
    manifest = json.loads(fake.objects["models/owner/direct/.hugginghack.json"])
    assert manifest["status"] == "complete" and manifest["file_count"] == 3 and manifest["change"]
    commits = owner.get("/api/library/commits", params={"repo_id": repo_id}).json()["items"]
    assert [commit["message"] for commit in commits] == ["First upload"]
    # It pulls from the bucket like any other bucket model.
    pulled = TestClient(main.app, follow_redirects=False).get(f"/{repo_id}/resolve/main/model.safetensors")
    assert pulled.status_code == 302 and urlsplit(pulled.headers["location"]).netloc == "s3.grid.example"
    assert direct["database"].list_direct_uploads(repo_id) == []
    assert direct["database"].get_upload_target(repo_id) is None
    # No part is left behind in the bucket.
    assert fake.multipart == {}


def test_uploads_to_a_bucket_without_direct_uploads_keep_going_through_the_server(direct):
    owner = login("owner")
    repo_id = new_repository(owner, "through", target="plain")
    begun = owner.post("/api/uploads/repositories/files/begin", params={"repo_id": repo_id}, json={"path": "a.bin", "size": 3})
    assert begun.json() == {"direct": False}
    chunk = owner.put(
        "/api/uploads/repositories/files",
        params={"repo_id": repo_id, "path": "a.bin"},
        content=b"abc",
        headers={"Upload-Offset": "0", "Upload-Length": "3", "Content-Type": "application/octet-stream"},
    )
    assert chunk.status_code == 200 and chunk.json()["complete"] is True
    # And the other way round: a direct repository takes no chunks.
    direct_repo = new_repository(owner, "straight")
    refused = owner.put(
        "/api/uploads/repositories/files",
        params={"repo_id": direct_repo, "path": "a.bin"},
        content=b"abc",
        headers={"Upload-Offset": "0", "Upload-Length": "3", "Content-Type": "application/octet-stream"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == "This upload goes straight to storage now. Reload the page and resume it."
    assert owner.get("/api/uploads/repositories/files/status", params={"repo_id": direct_repo, "path": "a.bin"}).status_code == 409


def test_direct_upload_requests_check_who_asks_and_what_they_ask(direct):
    owner, member, viewer = login("owner"), login("member"), login("viewer")
    repo_id = new_repository(owner, "checked")
    base, params = "/api/uploads/repositories/files", {"repo_id": repo_id}
    body = {"path": "model.safetensors", "size": len(WEIGHTS)}
    assert viewer.post(f"{base}/begin", params=params, json=body).status_code == 403
    # Someone else's repository does not exist for them.
    assert member.post(f"{base}/begin", params=params, json=body).status_code == 404
    assert TestClient(main.app).post(f"{base}/begin", params=params, json=body).status_code == 401
    too_big = owner.post(f"{base}/begin", params=params, json={"path": "huge.bin", "size": 2 * 1024**3})
    assert too_big.status_code == 400 and too_big.json()["detail"] == "One file cannot exceed 1 GB."
    assert owner.post(f"{base}/begin", params=params, json={"path": "../escape", "size": 1}).status_code == 400
    # HuggingHack's own records beside the files can never be written: a crafted manifest
    # or pending record would change what the next scan believes about the repository.
    for reserved in (".hugginghack.json", ".hugginghack-pending.json", ".hugginghack-changes/0/x.bin", "sub/.hugginghack-staging/y"):
        refused = owner.post(f"{base}/begin", params=params, json={"path": reserved, "size": 1})
        assert refused.status_code == 400, reserved
        assert reserved.split("/")[-1] not in str(direct["fakes"]["grid"].objects)
    assert owner.post(f"{base}/begin", params=params, json={"path": "a.bin", "size": -1}).status_code == 422
    # Links only for parts the file has, and only for a started file.
    assert owner.post(f"{base}/parts", params=params, json={"path": "model.safetensors", "parts": [1]}).status_code == 404
    owner.post(f"{base}/begin", params=params, json=body)
    # Every step checks who asks, not only the first.
    assert member.post(f"{base}/parts", params=params, json={"path": "model.safetensors", "parts": [1]}).status_code == 404
    assert member.post(f"{base}/complete", params=params, json={"path": "model.safetensors"}).status_code in {404, 422}
    wrong = owner.post(f"{base}/parts", params=params, json={"path": "model.safetensors", "parts": [4]})
    assert wrong.status_code == 400 and wrong.json()["detail"] == "Ask for 1 to 100 parts numbered 1 to 3."
    assert owner.post(f"{base}/parts", params=params, json={"path": "model.safetensors", "parts": list(range(1, 102))}).status_code == 422
    # An empty file needs no parts at all.
    empty = owner.post(f"{base}/begin", params=params, json={"path": "empty.txt", "size": 0}).json()
    assert empty["complete"] is True and empty["part_count"] == 0
    assert direct["fakes"]["grid"].objects["models/owner/checked/empty.txt"] == b""


def test_a_change_uploads_to_its_own_area_and_the_bucket_copies_it_into_place(direct):
    fake = direct["fakes"]["grid"]
    owner = login("owner")
    repo_id = publish_repository(
        direct, owner, "changing", {"config.json": CONFIG, "model.safetensors": WEIGHTS, "old.txt": b"old", "README.md": CARD}
    )
    assert direct["database"].get_local_model(repo_id)["license"] == "mit"
    new_weights = safetensors(99, 11 * MB)

    first = owner.post("/api/repos/changes", json={"repo_id": repo_id}).json()["id"]
    second = owner.post("/api/repos/changes", json={"repo_id": repo_id}).json()["id"]
    base = f"/api/repos/changes/{first}/files"
    assert upload(owner, base, {}, fake, "model.safetensors", new_weights).status_code == 200
    assert upload(owner, f"/api/repos/changes/{second}/files", {}, fake, "notes.md", b"# notes\n").status_code == 200

    # Until the commit, the published file is untouched and the upload waits apart.
    assert fake.objects["models/owner/changing/model.safetensors"] == WEIGHTS
    assert fake.objects[f"models/owner/changing/{CHANGES_DIRECTORY}/{first}/model.safetensors"] == new_weights
    details = owner.get(f"/api/library/models/{repo_id}")
    assert details.status_code == 200, details.text
    listed = [file["path"] for file in details.json()["files"]]
    assert "model.safetensors" in listed and CHANGES_DIRECTORY not in " ".join(listed)
    # The server refuses chunks for a session like this.
    chunk = owner.put(f"{base}", params={"path": "x.bin"}, content=b"x", headers={"Upload-Offset": "0", "Upload-Length": "1"})
    assert chunk.status_code == 409

    committed = owner.post(
        f"/api/repos/changes/{first}/commit", json={"message": "New weights", "deletions": ["old.txt", "README.md"]}
    )
    assert committed.status_code == 200, committed.text
    assert fake.objects["models/owner/changing/model.safetensors"] == new_weights
    assert "models/owner/changing/old.txt" not in fake.objects
    # The model is described again from what the bucket now holds: without the card,
    # its license is gone and its task comes from the config.
    model = direct["database"].get_local_model(repo_id)
    assert (model["parameter_count"], model["license"], model["pipeline_tag"]) == (99, None, "llama")
    manifest = json.loads(fake.objects["models/owner/changing/.hugginghack.json"])
    assert manifest["parameter_count"] == 99 and "license" not in manifest
    assert (f"models/owner/changing/{CHANGES_DIRECTORY}/{first}/model.safetensors", "models/owner/changing/model.safetensors") in fake.copies
    assert not any(key.startswith(f"models/owner/changing/{CHANGES_DIRECTORY}/{first}/") for key in fake.objects)
    # The other session's upload survived this commit's cleanup, and commits too.
    assert fake.objects[f"models/owner/changing/{CHANGES_DIRECTORY}/{second}/notes.md"] == b"# notes\n"
    assert owner.post(f"/api/repos/changes/{second}/commit", json={"message": "Notes"}).status_code == 200
    assert fake.objects["models/owner/changing/notes.md"] == b"# notes\n"
    assert PENDING_NAME not in " ".join(fake.objects)
    messages = [commit["message"] for commit in owner.get("/api/library/commits", params={"repo_id": repo_id}).json()["items"]]
    assert messages[:2] == ["Notes", "New weights"]
    # A rescan finds the repository as committed, without the upload areas.
    main.refresh_model_index()
    model = direct["database"].get_local_model(repo_id)
    assert model["file_count"] == 3


def test_cancelling_a_change_removes_its_uploads_from_the_bucket(direct):
    fake = direct["fakes"]["grid"]
    owner = login("owner")
    repo_id = publish_repository(direct, owner, "cancelled", {"config.json": CONFIG})
    session = owner.post("/api/repos/changes", json={"repo_id": repo_id}).json()["id"]
    base = f"/api/repos/changes/{session}/files"
    assert upload(owner, base, {}, fake, "small.txt", b"small").status_code == 200
    upload(owner, base, {}, fake, "model.safetensors", WEIGHTS, only={1})
    assert len(fake.multipart) == 1
    # Someone else cannot touch it.
    assert login("member").post(f"{base}/begin", json={"path": "x", "size": 1}).status_code in {403, 404}
    assert owner.delete(f"/api/repos/changes/{session}").status_code == 200
    assert fake.multipart == {}
    assert not any(CHANGES_DIRECTORY in key for key in fake.objects)
    assert direct["database"].get_direct_change_session(session) is None
    assert owner.post(f"{base}/begin", json={"path": "x", "size": 1}).status_code == 404


def test_deleting_an_unfinished_direct_upload_clears_the_bucket(direct):
    fake = direct["fakes"]["grid"]
    owner = login("owner")
    repo_id = new_repository(owner, "abandoned")
    base, params = "/api/uploads/repositories/files", {"repo_id": repo_id}
    assert upload(owner, base, params, fake, "config.json", CONFIG).status_code == 200
    upload(owner, base, params, fake, "model.safetensors", WEIGHTS, only={1, 2})
    deleted = owner.request("DELETE", "/api/uploads/repositories", params=params, json={"confirmation": repo_id})
    assert deleted.status_code == 200, deleted.text
    assert fake.multipart == {}
    assert not any(key.startswith("models/owner/abandoned/") for key in fake.objects)
    assert direct["database"].get_upload_target(repo_id) is None
    # The name is free again.
    assert new_repository(owner, "abandoned") == repo_id


def test_uploads_nobody_touched_for_a_day_are_given_up(direct):
    fake, database, uploads = direct["fakes"]["grid"], direct["database"], direct["uploads"]
    owner = login("owner")
    repo_id = new_repository(owner, "stale")
    upload(owner, "/api/uploads/repositories/files", {"repo_id": repo_id}, fake, "model.safetensors", WEIGHTS, only={1})
    published = publish_repository(direct, owner, "base", {"config.json": CONFIG})
    session = owner.post("/api/repos/changes", json={"repo_id": published}).json()["id"]
    assert upload(owner, f"/api/repos/changes/{session}/files", {}, fake, "notes.md", b"n").status_code == 200
    # A running session blocks renames and deletes, as staged ones do.
    assert uploads._busy(published) == "Someone is uploading changes to this repository."
    assert uploads.sweep_stale_uploads() == 0

    old = iso_seconds_ago(STALE_CHANGE_SECONDS + 60)
    with database.connect() as connection:
        connection.execute("UPDATE direct_uploads SET updated_at = ?", (old,))
        connection.execute("UPDATE direct_change_sessions SET updated_at = ?", (old,))
    assert uploads._busy(published) is None
    assert uploads.sweep_stale_uploads() == 2
    assert fake.multipart == {}
    assert not any(CHANGES_DIRECTORY in key for key in fake.objects)
    assert database.list_direct_uploads(repo_id) == [] and database.get_direct_change_session(session) is None
    # The repository itself is still there to resume: the file starts over.
    again = owner.post("/api/uploads/repositories/files/begin", params={"repo_id": repo_id}, json={"path": "model.safetensors", "size": len(WEIGHTS)})
    assert again.json()["done"] == []


def test_the_change_area_is_never_part_of_a_repository(direct):
    grid, fake = direct["grid"], direct["fakes"]["grid"]
    publish(fake, "models", "acme/kept", {"config.json": CONFIG, "a.bin": b"a"})
    area = f"models/acme/kept/{CHANGES_DIRECTORY}/{'0' * 32}/b.bin"
    fake.objects[area] = b"in flight"
    assert hidden_path(f"{CHANGES_DIRECTORY}/{'0' * 32}/b.bin")
    # Moves copy what object_files lists: never the change area.
    assert sorted(path for path, _ in grid.object_files("acme/kept")) == ["a.bin", "config.json"]
    assert all(CHANGES_DIRECTORY not in entry["path"] for entry in grid.list_repository_entries("acme/kept"))
    [record] = [record for record in grid.discover_repositories() if record["repo_id"] == "acme/kept"]
    assert all(CHANGES_DIRECTORY not in entry["path"] for entry in record["entries"]) and record["file_count"] == 2
    # Committing another change, or syncing a whole copy, leaves it alone.
    grid.apply_changes("acme/kept", {}, {"a.bin"}, {"status": "complete", "repo_id": "acme/kept"})
    assert fake.objects[area] == b"in flight"
    root = direct["settings"].model_storage / "acme" / "kept"
    root.mkdir(parents=True)
    (root / "config.json").write_bytes(CONFIG)
    (root / ".hugginghack.json").write_text(json.dumps({"status": "complete", "repo_id": "acme/kept"}))
    grid.sync_repository("acme/kept", root)
    assert fake.objects[area] == b"in flight"


def test_part_sizes_grow_so_no_file_needs_more_than_ten_thousand_parts():
    assert part_size_for(0, 64 * MB) == 64 * MB
    assert part_size_for(10 * MB, 1 * MB) == 5 * MB
    assert part_size_for(1024**4, 64 * MB) == 105 * MB
    assert -(-1024**4 // part_size_for(1024**4, 64 * MB)) <= 10_000


def test_the_page_may_send_parts_only_to_buckets_that_take_direct_uploads(direct):
    assert main.direct_upload_origins(main.storages) == ["https://s3.grid.example"]
    policy = main.content_security_policy(main.direct_upload_origins(main.storages))
    assert "connect-src 'self' https://s3.grid.example;" in policy
    assert "script-src 'self';" in policy and "default-src 'self';" in policy
    none = StorageRegistry(direct["settings"], [bucket(direct["settings"], "plain", MultipartS3Client())])
    assert main.direct_upload_origins(none) == []
    assert "connect-src 'self';" in main.content_security_policy([])


def test_a_cluster_takes_no_uploads_through_a_server_disk(direct, monkeypatch: pytest.MonkeyPatch):
    owner = login("owner")
    plain_repo = new_repository(owner, "legacy", target="plain")
    clustered = replace(direct["settings"], cluster_mode=True)
    monkeypatch.setattr(main, "settings", clustered)
    direct["uploads"].settings = clustered
    refused = owner.put(
        "/api/uploads/repositories/files",
        params={"repo_id": plain_repo, "path": "a.bin"},
        content=b"a",
        headers={"Upload-Offset": "0", "Upload-Length": "1", "Content-Type": "application/octet-stream"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == "This server takes uploads straight to storage. Reload the page and resume the upload."
    # A server's own disk is not offered for new repositories.
    choices = [choice["id"] for choice in direct["uploads"].storage_choices({"id": "x", "username": "owner", "role": "admin"})]
    assert "local" not in choices


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_direct_upload_sizes_beyond_two_gigabytes_survive_postgresql():
    database = Database(POSTGRES_URL)
    database.initialize()
    repo_id = f"big/{uuid4().hex}"
    try:
        record = database.add_direct_upload(
            {
                "id": uuid4().hex, "repo_id": repo_id, "scope": "repository", "path": "w.bin",
                "storage_target": "grid", "object_key": "k", "upload_id": "u",
                "size": 3 * 1024**4, "part_size": 320 * MB, "user_id": None,
                "created_at": "2026-09-26T00:00:00+00:00", "updated_at": "2026-09-26T00:00:00+00:00",
            }
        )
        assert (record["size"], record["part_size"], record["complete"]) == (3 * 1024**4, 320 * MB, False)
        # A second start of the same file keeps the first record.
        again = database.add_direct_upload({**record, "id": uuid4().hex, "upload_id": "other"})
        assert again["upload_id"] == "u"
    finally:
        database.delete_direct_uploads(repo_id)
        database.close()
