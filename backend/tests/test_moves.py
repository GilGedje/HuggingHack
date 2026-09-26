import hashlib
import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import uvicorn
from huggingface_hub import snapshot_download

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.auth import AuthService
from app.git_mirror import GitMirrors
from app.moves import MOVES_DIRECTORY, MoveManager
from app.reads import ReadTracker
from test_hub_api import free_port
from test_storage_targets import multi_target, publish  # noqa: F401  (fixture)

PASSWORDS = {"admin": "correct horse battery", "member": "another secure phrase"}
WEIGHTS = bytes(range(256)) * 40_000  # 10 MB, so copies and reads run in several chunks
FILES = {
    "config.json": b'{"model_type": "llama", "torch_dtype": "bfloat16"}',
    "README.md": b"# Big\n",
    "model.safetensors": WEIGHTS,
    ".gitattributes": b"*.safetensors filter=lfs\n",
}


@pytest.fixture()
def mover(multi_target, monkeypatch):  # noqa: F811
    tracker = ReadTracker()
    manager = MoveManager(
        multi_target["settings"],
        multi_target["database"],
        multi_target["registry"],
        main.hub_repositories,
        main.history,
        GitMirrors(main.hub_repositories),
        tracker,
        main.repository_busy,
    )
    monkeypatch.setattr(main, "moves", manager)
    monkeypatch.setattr(main, "reads", tracker)
    monkeypatch.setattr(main, "git_mirrors", manager.mirrors)
    main.uploads.move_guard = manager.moving
    root = multi_target["settings"].model_storage / "acme" / "big"
    root.mkdir(parents=True)
    for path, payload in FILES.items():
        (root / path).write_bytes(payload)
    main.refresh_model_index()
    monkeypatch.setattr(main, "auth", AuthService(multi_target["settings"], multi_target["database"]))
    yield {**multi_target, "manager": manager, "tracker": tracker, "root": root, "client": signed_in("admin")}


def signed_in(username: str) -> TestClient:
    client = TestClient(main.app)
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORDS[username]})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


def wait_for(manager, move_id, statuses, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        move = manager.database.get_move(move_id)
        if move["status"] in statuses:
            return move
        time.sleep(0.02)
    raise AssertionError(f"move stayed {manager.database.get_move(move_id)['status']}")


def start(client, repo_id, destination, **extra):
    return client.post(
        "/api/storage/moves",
        json={"repo_id": repo_id, "destination": destination, "confirmation": repo_id, **extra},
    )


def test_a_pull_in_progress_finishes_from_the_old_copy_and_its_revision_keeps_working(mover):
    client, manager, tracker = mover["client"], mover["manager"], mover["tracker"]
    bucket = mover["clients"]["bucket-a"]
    before = client.get("/api/models/acme/big").json()["sha"]
    commits = main.database.count_commits("acme/big")

    # Someone is part-way through downloading the weights.
    lease = tracker.acquire("acme/big")
    handle = (mover["root"] / "model.safetensors").open("rb")
    first = handle.read(1_000_000)

    started = start(client, "acme/big", "bucket-a")
    assert started.status_code == 202, started.text
    move = wait_for(manager, started.json()["id"], {"draining"})
    assert move["verified_bytes"] == move["total_bytes"] == sum(len(value) for value in FILES.values())
    # Switched: the model now lives in bucket-a, and the old files are still there.
    assert main.database.get_local_model("acme/big")["storage_target"] == "bucket-a"
    assert (mover["root"] / "model.safetensors").exists()
    assert bucket.objects["models/acme/big/.gitattributes"] == FILES[".gitattributes"]
    time.sleep(0.2)
    assert manager.database.get_move(move["id"])["status"] == "draining"

    rest = handle.read()
    handle.close()
    assert hashlib.sha256(first + rest).hexdigest() == hashlib.sha256(WEIGHTS).hexdigest()
    tracker.release(lease)
    done = wait_for(manager, move["id"], {"done", "failed"})
    assert done["status"] == "done", done
    assert not mover["root"].exists()  # the local copy was removed once nobody read it
    assert main.database.get_local_model("acme/big")["cached"] is False

    # The revision pinned before the move still resolves, under the same name.
    after = client.get("/api/models/acme/big").json()["sha"]
    assert after != before
    pinned = client.get(f"/acme/big/resolve/{before}/model.safetensors")
    assert pinned.status_code == 200 and pinned.headers["X-Repo-Commit"] == before
    assert pinned.content == WEIGHTS
    assert client.get(f"/api/models/acme/big/revision/{before}").json()["sha"] == before
    # The move is not a change: no new commit, even after a rescan.
    main.refresh_model_index()
    assert main.database.count_commits("acme/big") == commits

    # A real change later makes the old revision stale.
    bucket.objects["models/acme/big/extra.txt"] = b"new"
    bucket._touch("models/acme/big/extra.txt")
    main.refresh_model_index()
    assert client.get(f"/acme/big/resolve/{before}/config.json").status_code == 404


def test_moves_between_buckets_and_back_to_local_disk(mover):
    client, manager = mover["client"], mover["manager"]
    a, b = mover["clients"]["bucket-a"], mover["clients"]["bucket-b"]
    publish(a, "models", "acme/remote", {"config.json": b"{}", "w.safetensors": WEIGHTS[:3_000_000]})
    main.refresh_model_index()

    moved = wait_for(manager, start(client, "acme/remote", "bucket-b").json()["id"], {"done", "failed"})
    assert moved["status"] == "done", moved
    assert not [key for key in a.objects if "/acme/remote/" in key]
    assert b.objects["models/acme/remote/w.safetensors"] == WEIGHTS[:3_000_000]
    assert json.loads(b.objects["models/acme/remote/.hugginghack.json"])["storage_target"] == "bucket-b"
    main.refresh_model_index()
    assert main.database.get_local_model("acme/remote")["storage_target"] == "bucket-b"
    assert main.refresh_model_index()["conflicts"] == []

    home = wait_for(manager, start(client, "acme/remote", "local").json()["id"], {"done", "failed"})
    assert home["status"] == "done", home
    root = mover["settings"].model_storage / "acme" / "remote"
    assert (root / "w.safetensors").read_bytes() == WEIGHTS[:3_000_000]
    assert not [key for key in b.objects if "/acme/remote/" in key]
    model = main.database.get_local_model("acme/remote")
    assert (model["storage_target"], model["storage_backend"], model["cached"]) == ("local", "filesystem", True)
    main.refresh_model_index()
    assert main.database.get_local_model("acme/remote")["storage_target"] == "local"


def test_a_copy_that_does_not_match_is_thrown_away(mover, monkeypatch):
    client, manager = mover["client"], mover["manager"]
    target = mover["registry"].get("bucket-a")
    original = target.iter_repository_file

    def corrupted(repo_id, path, start, end, chunk_size=8 * 1024 * 1024):
        for chunk in original(repo_id, path, start, end, chunk_size):
            yield chunk[:-1] + b"X" if path == "model.safetensors" else chunk

    monkeypatch.setattr(target, "iter_repository_file", corrupted)
    failed = wait_for(manager, start(client, "acme/big", "bucket-a").json()["id"], {"done", "failed"})
    assert failed["status"] == "failed" and "does not match" in failed["error"]
    assert main.database.get_local_model("acme/big")["storage_target"] == "local"
    assert not [key for key in mover["clients"]["bucket-a"].objects if "/acme/big/" in key]
    assert (mover["root"] / "model.safetensors").read_bytes() == WEIGHTS


def test_moves_are_checked_and_block_other_changes(mover):
    client, manager = mover["client"], mover["manager"]
    assert start(client, "acme/big", "local").status_code == 409  # already there
    assert start(client, "acme/big", "nowhere").status_code == 409
    assert client.post("/api/storage/moves", json={"repo_id": "acme/big", "destination": "bucket-a", "confirmation": "acme/Big"}).status_code == 409
    assert start(client, "acme/missing", "bucket-a").status_code == 404
    mover["clients"]["bucket-a"].objects["models/acme/big/stray.bin"] = b"x"
    assert "already has files" in start(client, "acme/big", "bucket-a").json()["detail"]
    del mover["clients"]["bucket-a"].objects["models/acme/big/stray.bin"]
    assert start(signed_in("member"), "acme/big", "bucket-a").status_code == 403

    # While a move is under way, the model cannot change, move again, or be deleted.
    record = {
        "id": "f" * 32, "repo_id": "acme/big", "source_target": "local", "destination_target": "bucket-a",
        "keep_local": 0, "status": "copying", "message": "", "created_by": None,
        "created_at": "2026-09-25T00:00:00+00:00", "updated_at": "2026-09-25T00:00:00+00:00",
    }
    main.database.create_move(record)
    assert "moving" in start(client, "acme/big", "bucket-b").json()["detail"]
    assert "moving" in (main.uploads._busy("acme/big") or "")
    assert "moving" in client.post("/api/repos/changes", json={"repo_id": "acme/big"}).json()["detail"]
    assert client.request("DELETE", "/api/repos", params={"repo_id": "acme/big"}, json={"confirmation": "acme/big"}).status_code == 409

    # A restart undoes a move that had not switched, including its partial copy.
    mover["clients"]["bucket-a"].objects["models/acme/big/model.safetensors"] = b"partial"
    manager.recover()
    assert main.database.get_move(record["id"])["status"] == "failed"
    assert not [key for key in mover["clients"]["bucket-a"].objects if "/acme/big/" in key]
    assert main.uploads._busy("acme/big") is None

    queued = main.database.create_move({**record, "id": "e" * 32, "status": "queued"})
    assert client.post(f"/api/storage/moves/{queued['id']}/cancel").json()["status"] == "cancelled"


def test_a_scan_during_a_move_indexes_neither_half_copy(mover):
    staging = mover["settings"].model_storage / MOVES_DIRECTORY / ("a" * 32) / "acme" / "big"
    staging.mkdir(parents=True)
    (staging / "config.json").write_text("{}", encoding="utf-8")
    # Objects already in the destination but no manifest yet.
    mover["clients"]["bucket-b"].objects["models/acme/big/config.json"] = b"{}"
    main.refresh_model_index()
    models = {model["repo_id"]: model["storage_target"] for model in main.database.list_local_models()}
    assert models["acme/big"] == "local"
    assert not any("hugginghack-moves" in repo for repo in models)


def test_signed_links_to_the_old_bucket_copy_are_honoured_until_they_expire(mover):
    """Links handed out before the switch point at the old bucket and never show up as
    reads here; the old copy stays until the last of them has expired, even across
    a restart (the switch time is in the database)."""
    client, manager = mover["client"], mover["manager"]
    source = mover["registry"].get("bucket-a")
    source.direct_downloads = True
    source.target = replace(source.target, direct_downloads=True, presign_ttl_seconds=600)
    a = mover["clients"]["bucket-a"]
    publish(a, "models", "acme/linked", {"config.json": b"{}", "w.safetensors": WEIGHTS[:1_000_000]})
    main.refresh_model_index()

    move_id = start(client, "acme/linked", "bucket-b").json()["id"]
    waiting = wait_for(manager, move_id, {"draining", "done", "failed"})
    deadline = time.monotonic() + 5
    while "links" not in (waiting.get("message") or "") and time.monotonic() < deadline:
        time.sleep(0.02)
        waiting = main.database.get_move(move_id)
    assert waiting["status"] == "draining", waiting
    assert waiting["message"] == "Waiting about 10 minutes for download links to the old copy to expire"
    assert a.objects["models/acme/linked/w.safetensors"] == WEIGHTS[:1_000_000]
    assert main.database.get_local_model("acme/linked")["storage_target"] == "bucket-b"

    # Ten minutes later, as far as the record goes.
    earlier = datetime.now(timezone.utc) - timedelta(seconds=601)
    main.database.update_move(move_id, switched_at=earlier.isoformat())
    manager._wake.set()
    assert wait_for(manager, move_id, {"done", "failed"})["status"] == "done"
    assert not [key for key in a.objects if "/acme/linked/" in key]


def test_keeping_the_local_copy_needs_no_wait(mover):
    client, manager, tracker = mover["client"], mover["manager"], mover["tracker"]
    lease = tracker.acquire("acme/big")  # a download that never ends
    move = start(client, "acme/big", "bucket-a", keep_local=True).json()
    assert wait_for(manager, move["id"], {"done", "failed"})["status"] == "done"
    model = main.database.get_local_model("acme/big")
    assert (model["storage_target"], model["cached"]) == ("bucket-a", True)
    assert (mover["root"] / "model.safetensors").read_bytes() == WEIGHTS
    assert json.loads((mover["root"] / ".hugginghack.json").read_text())["storage_target"] == "bucket-a"
    tracker.release(lease)


def test_removal_starts_only_when_nothing_reads_and_holds_new_reads_back():
    tracker = ReadTracker()
    lease = tracker.acquire("acme/big")
    # A read under way blocks removal; checking and starting are one step.
    assert tracker.try_begin_removal("ACME/big") is False
    switched = time.monotonic()
    # For a bucket, reads that began after the switch go to the new copy and do not count.
    later = tracker.acquire("acme/big")
    assert tracker.active("acme/big", started_before=switched) == 1
    tracker.release(lease)
    assert tracker.try_begin_removal("acme/big", started_before=switched) is True
    order = []
    reader = threading.Thread(target=lambda: order.append(("read", tracker.acquire("acme/big"))))
    reader.start()
    time.sleep(0.1)
    order.append(("removed", None))
    tracker.end_removal("acme/big")
    reader.join(timeout=2)
    assert [step for step, _ in order] == ["removed", "read"]
    tracker.release(later)
    assert tracker.active("acme/big") == 1


def test_a_real_client_keeps_pulling_across_a_move(mover, tmp_path):
    manager, tracker = mover["manager"], mover["tracker"]
    big = bytes(range(256)) * (64 * 4096)  # 64 MB: more than socket buffers hold
    (mover["root"] / "model.safetensors").write_bytes(big)
    main.refresh_model_index()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}"
    try:
        before = httpx.get(f"{url}/api/models/acme/big").json()["sha"]
        with httpx.stream("GET", f"{url}/acme/big/resolve/main/model.safetensors") as response:
            chunks = response.iter_bytes(1024 * 1024)
            received = [next(chunks)]
            assert tracker.active("acme/big") == 1  # the open download holds a lease
            move = start(mover["client"], "acme/big", "bucket-a").json()
            wait_for(manager, move["id"], {"draining"})
            time.sleep(0.3)
            assert manager.database.get_move(move["id"])["status"] == "draining"
            received.extend(chunks)
        assert hashlib.sha256(b"".join(received)).digest() == hashlib.sha256(big).digest()
        assert wait_for(manager, move["id"], {"done", "failed"})["status"] == "done"
        assert tracker.active("acme/big") == 0

        # A client that pinned the revision before the move gets every file under it.
        folder = snapshot_download("acme/big", revision=before, endpoint=url, token=False, cache_dir=tmp_path / "cache")
        assert before in folder
        assert (Path(folder) / "model.safetensors").read_bytes() == big
        assert (Path(folder) / "config.json").read_bytes() == FILES["config.json"]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
