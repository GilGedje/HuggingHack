import json
import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import httpx
import pytest
import uvicorn
from huggingface_hub import HfApi, hf_hub_download, snapshot_download
from huggingface_hub.errors import (
    EntryNotFoundError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)

import app.main as main
from app.auth import AuthService
from app.config import Settings
from app.database import Database
from app.git_mirror import GitMirrors, gitattributes_pattern
from app.hub_api import HubRepositories
from app.indexer import LocalModelIndexer
from app.storage import FilesystemModelStorage, S3ModelStorage
from app.uploads import UploadManager


WEIGHTS = bytes(range(256)) * 4096  # 1 MiB, stored through Git LFS
_HEADER = json.dumps(
    {"__metadata__": {"format": "pt"}, "w": {"dtype": "F32", "shape": [2, 2], "data_offsets": [0, 16]}}
).encode()
SAFETENSORS = len(_HEADER).to_bytes(8, "little") + _HEADER + b"\0" * 16
CONFIG = json.dumps({"model_type": "llama", "architectures": ["LlamaForCausalLM"]})


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture()
def hub_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage = (tmp_path / "models").resolve()
    settings = Settings(model_storage=storage, data_dir=(tmp_path / "data").resolve())
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    model_storage = FilesystemModelStorage(settings)
    indexer = LocalModelIndexer(settings, database)

    root = storage / "acme" / "tiny"
    (root / "nested dir").mkdir(parents=True)
    (root / "config.json").write_text(CONFIG, encoding="utf-8")
    (root / "model.safetensors").write_bytes(SAFETENSORS)
    (root / "nested dir" / "weights [v1].bin").write_bytes(WEIGHTS)
    (root / "README.md").write_text("---\nlicense: mit\n---\n# Tiny\n", encoding="utf-8")
    (root / "nested dir" / "notes [v1].txt").write_text("hello", encoding="utf-8")
    (root / ".gitattributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
    (root / ".hugginghack.json").write_text(
        json.dumps({"status": "complete", "repo_id": "acme/tiny"}), encoding="utf-8"
    )

    auth = AuthService(settings, database)
    owner = auth.create_user("owner", "Owner", "correct horse battery", "admin")
    uploads = UploadManager(settings, database, indexer, model_storage)
    private = uploads.create_repository(owner, "secret", "", "private")
    uploads.upload_chunk(private["repo_id"], owner["id"], "config.json", 0, 2, b"{}")
    uploads.finalize(private["repo_id"], owner["id"])
    indexer.scan()

    repositories = HubRepositories(settings, database, model_storage)
    for name, value in {
        "settings": settings,
        "database": database,
        "indexer": indexer,
        "model_storage": model_storage,
        "hub_repositories": repositories,
        "git_mirrors": GitMirrors(repositories),
    }.items():
        monkeypatch.setattr(main, name, value)

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="info", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Test server did not start.")
        time.sleep(0.05)
    try:
        yield {
            "url": f"http://127.0.0.1:{port}",
            "root": root,
            "private": private["repo_id"],
            "settings": settings,
            "repositories": repositories,
        }
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_huggingface_hub_client_pulls_from_the_local_library(hub_server, tmp_path: Path):
    endpoint = hub_server["url"]
    api = HfApi(endpoint=endpoint, token=False)

    info = api.model_info("acme/tiny", files_metadata=True)
    assert len(info.sha) == 40
    assert {sibling.rfilename for sibling in info.siblings} == {
        "README.md",
        "config.json",
        "model.safetensors",
        "nested dir/notes [v1].txt",
        "nested dir/weights [v1].bin",
    }
    assert next(s for s in info.siblings if s.rfilename == "model.safetensors").size == len(SAFETENSORS)
    tree = {item.path for item in api.list_repo_tree("acme/tiny", recursive=True)}
    assert "nested dir" in tree and "nested dir/notes [v1].txt" in tree
    assert {item.path for item in api.list_repo_tree("acme/tiny")} == {
        "README.md",
        "config.json",
        "model.safetensors",
        "nested dir",
    }

    local_dir = tmp_path / "pulled"
    snapshot_download("acme/tiny", endpoint=endpoint, token=False, local_dir=local_dir)
    assert (local_dir / "model.safetensors").read_bytes() == SAFETENSORS
    assert (local_dir / "nested dir" / "weights [v1].bin").read_bytes() == WEIGHTS
    assert (local_dir / "nested dir" / "notes [v1].txt").read_text() == "hello"
    assert not (local_dir / ".hugginghack.json").exists()
    assert not (local_dir / ".gitattributes").exists()

    cached = hf_hub_download(
        "acme/tiny", "config.json", endpoint=endpoint, token=False, cache_dir=tmp_path / "cache"
    )
    assert Path(cached).read_text() == CONFIG
    assert info.sha in cached

    with pytest.raises(EntryNotFoundError):
        hf_hub_download(
            "acme/tiny",
            "generation_config.json",
            endpoint=endpoint,
            token=False,
            cache_dir=tmp_path / "cache",
        )
    with pytest.raises(RevisionNotFoundError):
        api.model_info("acme/tiny", revision="v2")
    with pytest.raises(RepositoryNotFoundError):
        api.model_info(hub_server["private"])
    with pytest.raises(RepositoryNotFoundError):
        hf_hub_download(hub_server["private"], "config.json", endpoint=endpoint, token=False)

    ranged = httpx.get(
        f"{endpoint}/acme/tiny/resolve/main/model.safetensors", headers={"Range": "bytes=10-19"}
    )
    assert ranged.status_code == 206
    assert ranged.content == SAFETENSORS[10:20]
    assert ranged.headers["x-repo-commit"] == info.sha
    head = httpx.head(f"{endpoint}/acme/tiny/resolve/{info.sha}/model.safetensors")
    assert head.status_code == 200
    assert int(head.headers["content-length"]) == len(SAFETENSORS)
    assert head.headers["etag"].strip('"') == next(
        s.blob_id for s in info.siblings if s.rfilename == "model.safetensors"
    )
    for path in (".hugginghack.json", ".gitattributes", "../tiny/config.json"):
        assert httpx.head(f"{endpoint}/acme/tiny/resolve/main/{path}").status_code == 404


def test_hf_filesystem_and_safetensors_probes_used_by_vllm(hub_server):
    from huggingface_hub import HfFileSystem

    endpoint = hub_server["url"]
    fs = HfFileSystem(endpoint=endpoint, token=False)
    assert set(fs.ls("acme/tiny", detail=False, revision="main")) == {
        "acme/tiny/README.md",
        "acme/tiny/config.json",
        "acme/tiny/model.safetensors",
        "acme/tiny/nested dir",
    }
    detailed = {item["name"]: item for item in fs.ls("acme/tiny", detail=True)}
    assert detailed["acme/tiny/model.safetensors"]["size"] == len(SAFETENSORS)
    assert fs.info("acme/tiny/config.json")["size"] == len(CONFIG)
    assert fs.exists("acme/tiny/config.json")
    assert not fs.exists("acme/tiny/generation_config.json")
    assert fs.glob("acme/tiny/*.safetensors") == ["acme/tiny/model.safetensors"]
    assert fs.cat("acme/tiny/config.json").decode() == CONFIG

    metadata = HfApi(endpoint=endpoint, token=False).get_safetensors_metadata("acme/tiny")
    assert metadata.parameter_count == {"F32": 4}


def test_hub_api_can_be_disabled(hub_server, monkeypatch: pytest.MonkeyPatch):
    settings = replace(hub_server["settings"], hub_api_enabled=False)
    monkeypatch.setattr(hub_server["repositories"], "settings", settings)
    response = httpx.get(f"{hub_server['url']}/api/models/acme/tiny")
    assert response.status_code == 404
    assert response.headers["x-error-code"] == "RepoNotFound"


def git_environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    environment = {
        **os.environ,
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    subprocess.run(["git", "lfs", "install", "--skip-repo"], env=environment, check=True)
    return environment


@pytest.mark.skipif(
    shutil.which("git") is None
    or subprocess.run(["git", "lfs", "version"], capture_output=True).returncode != 0,
    reason="git and git-lfs are required for the clone test",
)
def test_git_clone_with_lfs_from_the_local_library(hub_server, tmp_path: Path):
    environment = git_environment(tmp_path)
    url = f"{hub_server['url']}/acme/tiny"

    clone = tmp_path / "clone"
    result = subprocess.run(
        ["git", "clone", url, str(clone)], env=environment, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert (clone / "model.safetensors").read_bytes() == SAFETENSORS
    assert (clone / "nested dir" / "weights [v1].bin").read_bytes() == WEIGHTS
    assert (clone / "config.json").read_text() == CONFIG
    assert (clone / "nested dir" / "notes [v1].txt").read_text() == "hello"
    assert not (clone / ".hugginghack.json").exists()
    tracked = subprocess.run(
        ["git", "lfs", "ls-files", "--name-only"],
        cwd=clone,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert tracked.stdout.splitlines() == ["model.safetensors", "nested dir/weights [v1].bin"]

    pointers = tmp_path / "pointers"
    subprocess.run(
        ["git", "clone", f"{url}.git", str(pointers)],
        env={**environment, "GIT_LFS_SKIP_SMUDGE": "1"},
        check=True,
        capture_output=True,
    )
    assert (pointers / "model.safetensors").read_text().startswith(
        "version https://git-lfs.github.com/spec/v1"
    )

    denied = subprocess.run(
        ["git", "clone", f"{hub_server['url']}/{hub_server['private']}", str(tmp_path / "no")],
        env=environment,
        capture_output=True,
    )
    assert denied.returncode != 0

    # Changing a file publishes a new commit on the next clone.
    (hub_server["root"] / "config.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "pull"], cwd=clone, env=environment, check=True, capture_output=True)
    assert (clone / "config.json").read_text() == "{}"


def test_gitattributes_patterns_match_one_literal_path():
    assert gitattributes_pattern("model.safetensors") == "/model.safetensors"
    assert gitattributes_pattern("a/b[1]*.bin") == "/a/b\\[1\\]\\*.bin"
    assert gitattributes_pattern("nested dir/x.bin") == '"/nested dir/x.bin"'


class RangeS3Client:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def get_paginator(self, operation: str):
        return self

    def paginate(self, *, Bucket: str, Prefix: str):
        return [
            {
                "Contents": [
                    {"Key": key, "Size": len(value), "LastModified": "2026-09-01T00:00:00+00:00"}
                    for key, value in sorted(self.objects.items())
                    if key.startswith(Prefix)
                ]
            }
        ]

    def get_object(self, *, Bucket: str, Key: str, Range: str | None = None):
        payload = self.objects[Key]
        if Range:
            start, end = (int(value) for value in Range.removeprefix("bytes=").split("-"))
            payload = payload[start : end + 1]
        return {"Body": BytesIO(payload)}


def test_s3_only_models_stream_and_hash_through_bounded_ranges(tmp_path: Path):
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        model_storage_backend="s3",
        s3_bucket="bucket",
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    database.upsert_local_model(
        {
            "repo_id": "acme/remote",
            "relative_path": "acme/remote",
            "size_bytes": len(WEIGHTS),
            "file_count": 1,
            "modified_at": "2026-09-01T00:00:00+00:00",
            "downloaded_at": None,
            "revision": None,
            "sha": None,
            "pipeline_tag": None,
            "library_name": None,
            "license": None,
            "tags_json": "[]",
            "config_json": "{}",
            "source_url": None,
            "managed": 1,
            "storage_backend": "s3",
            "cached": 0,
            "remote_uri": None,
        }
    )
    client = RangeS3Client(
        {
            "models/acme/remote/.hugginghack.json": b"{}",
            "models/acme/remote/model.safetensors": WEIGHTS,
        }
    )
    storage = S3ModelStorage(settings, client=client, transfer_config=object())
    repositories = HubRepositories(settings, database, storage)

    snapshot = repositories.snapshot("acme/remote")
    assert [entry.path for entry in snapshot.entries] == ["model.safetensors"]
    entry = snapshot.entries[0]
    storage_chunks = list(
        storage.iter_repository_file("acme/remote", entry.path, 0, entry.size - 1, chunk_size=300_000)
    )
    assert len(storage_chunks) == 4
    assert b"".join(repositories.iter_bytes(snapshot, entry, 5, 9)) == WEIGHTS[5:10]
    import hashlib

    assert repositories.sha256(snapshot, entry) == hashlib.sha256(WEIGHTS).hexdigest()
    assert database.get_file_digest("acme/remote", entry.path, entry.version)
