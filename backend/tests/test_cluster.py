"""Several HuggingHack servers sharing one PostgreSQL database (CLUSTER_MODE).

main.py builds its services at import, so two servers cannot run in one test
process. Instead each "server" here is its own set of Database and service
objects, with its own in-process locks, against one fresh PostgreSQL database:
exactly what two processes share, and nothing they don't."""

import dataclasses
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest

from app import main
from app.auth import AuthService
from app.cluster import Cluster, startup_problems, utc_iso
from app.config import Settings
from app.database import Database
from app.git_mirror import GitMirrors
from app.history import RepoHistory
from app.hub_api import HubRepositories, RepoEntry
from app.indexer import LocalModelIndexer
from app.runtimes import RuntimeManager
from app.system import LocalSystemStore
from app.storage import StorageRegistry, create_storage_registry

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
needs_postgres = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")


@pytest.fixture
def shared_url():
    """A database of its own for one test, dropped afterwards."""
    import psycopg

    name = f"hh_cluster_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    parts = urlsplit(POSTGRES_URL)
    url = urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))
    try:
        yield url
    finally:
        with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def server(url: str, tmp_path: Path, name: str, **overrides) -> tuple[Settings, Database]:
    """One server's settings and database handle; pods share models only through buckets,
    so each gets a data folder of its own."""
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / f"data-{name}").resolve(),
        database_url=url,
        instance_id=name,
        **overrides,
    )
    settings.ensure_directories()
    database = Database(url, pool_size=4)
    database.initialize()
    return settings, database


def together(*calls):
    """Run the calls at the same moment on separate threads; return results or errors."""
    barrier = threading.Barrier(len(calls))

    def run(call):
        barrier.wait()
        try:
            return call()
        except Exception as error:  # noqa: BLE001
            return error

    with ThreadPoolExecutor(len(calls)) as pool:
        return list(pool.map(run, calls))


# ---- startup refusals ------------------------------------------------------------


def test_cluster_mode_refuses_setups_other_servers_cannot_see(tmp_path: Path):
    settings = Settings(
        model_storage=tmp_path / "models", data_dir=tmp_path / "data",
        cluster_mode=True, hf_downloads_enabled=True,
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    problems = startup_problems(settings, database, create_storage_registry(settings), system_remote=False)
    assert [problem.split(":")[0].split(" needs ")[0] for problem in problems] == [
        "CLUSTER_MODE", "CLUSTER_MODE", "CLUSTER_MODE", "CLUSTER_MODE does not support HF_DOWNLOADS_ENABLED yet",
    ]
    assert "postgresql://" in problems[0]
    assert "DEFAULT_STORAGE_TARGET" in problems[1]
    assert "SYSTEM_STORAGE_TARGET" in problems[2]
    # Nothing to refuse when cluster mode is off.
    assert startup_problems(dataclasses.replace(settings, cluster_mode=False), database, create_storage_registry(settings), False) == []


@needs_postgres
def test_cluster_mode_accepts_postgres_and_buckets_but_not_models_on_a_disk(shared_url, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GRID_KEY", "key")
    monkeypatch.setenv("GRID_SECRET", "secret")
    settings, database = server(
        shared_url, tmp_path, "a", cluster_mode=True,
        storage_targets_json='[{"id":"grid","bucket":"models","endpoint_url":"http://grid.test",'
        '"access_key_env":"GRID_KEY","secret_key_env":"GRID_SECRET","direct_uploads":true}]',
        default_storage_target="grid",
    )
    storages = create_storage_registry(settings)
    try:
        assert startup_problems(settings, database, storages, system_remote=True) == []
        # A bucket that takes uploads through a server's disk cannot be shared.
        staging = dataclasses.replace(settings, storage_targets_json=settings.storage_targets_json.replace(
            '"direct_uploads":true', '"direct_uploads":false'
        ))
        [problem] = startup_problems(staging, database, create_storage_registry(staging), system_remote=True)
        assert problem.startswith("CLUSTER_MODE needs direct uploads on every bucket") and "grid" in problem
        folder = settings.model_storage / "acme" / "on-disk"
        folder.mkdir(parents=True)
        (folder / "config.json").write_text("{}")
        LocalModelIndexer(settings, database).scan()
        [problem] = startup_problems(settings, database, storages, system_remote=True)
        assert problem.startswith("1 model is stored on a server's own disk (first: acme/on-disk).")
    finally:
        database.close()


# ---- invariants across servers ------------------------------------------------------


@needs_postgres
def test_two_servers_cannot_both_remove_one_of_the_last_two_admins(shared_url, tmp_path: Path):
    (_, first), (_, second) = server(shared_url, tmp_path, "a"), server(shared_url, tmp_path, "b")
    try:
        for round_number in range(5):
            admins = [
                first.create_user({
                    "id": uuid.uuid4().hex, "username": f"admin{round_number}{which}", "display_name": "Admin",
                    "password_hash": "x", "role": "admin", "created_at": utc_iso(), "updated_at": utc_iso(),
                })
                for which in "xy"
            ]
            # Only these two are active admins.
            with first.connect() as connection:
                connection.execute(
                    "UPDATE users SET disabled = 1 WHERE role = 'admin' AND id NOT IN (?, ?)",
                    (admins[0]["id"], admins[1]["id"]),
                )
            results = together(
                lambda: first.update_user_guarded(admins[0]["id"], {"role": "member"}),
                lambda: second.update_user_guarded(admins[1]["id"], {"role": "member"}),
            )
            refused = [result for result in results if isinstance(result, ValueError)]
            assert len(refused) == 1, results
            assert first.count_active_admins() == 1
            # Deleting races the same way.
            with first.connect() as connection:
                connection.execute("UPDATE users SET role = 'admin', disabled = 0 WHERE id IN (?, ?)",
                                   (admins[0]["id"], admins[1]["id"]))
            results = together(lambda: first.delete_user(admins[0]["id"]), lambda: second.delete_user(admins[1]["id"]))
            assert sum(isinstance(result, ValueError) for result in results) == 1
            assert first.count_active_admins() == 1
    finally:
        first.close()
        second.close()


@needs_postgres
def test_two_servers_cannot_both_remove_one_of_the_last_two_org_admins(shared_url, tmp_path: Path):
    (_, first), (_, second) = server(shared_url, tmp_path, "a"), server(shared_url, tmp_path, "b")
    try:
        users = [
            first.create_user({
                "id": uuid.uuid4().hex, "username": f"member{which}", "display_name": "M",
                "password_hash": "x", "role": "member", "created_at": utc_iso(), "updated_at": utc_iso(),
            })
            for which in "xy"
        ]
        for round_number in range(5):
            organization = first.create_organization({
                "id": uuid.uuid4().hex, "name": f"org{round_number}", "display_name": "Org",
                "description": "", "created_at": utc_iso(), "updated_at": utc_iso(),
            })
            for user in users:
                first.set_organization_member(organization["id"], user["id"], "admin", utc_iso())
            results = together(
                lambda: first.remove_organization_member(organization["id"], users[0]["id"]),
                lambda: second.set_organization_member(organization["id"], users[1]["id"], "read", utc_iso()),
            )
            assert sum(isinstance(result, ValueError) for result in results) == 1, results
        # A name is taken once, whichever server asks.
        results = together(
            lambda: first.create_organization({"id": uuid.uuid4().hex, "name": "Twin", "display_name": "T",
                                               "description": "", "created_at": utc_iso(), "updated_at": utc_iso()}),
            lambda: second.create_organization({"id": uuid.uuid4().hex, "name": "twin", "display_name": "T",
                                                "description": "", "created_at": utc_iso(), "updated_at": utc_iso()}),
        )
        assert sum(isinstance(result, ValueError) for result in results) == 1, results
    finally:
        first.close()
        second.close()


@needs_postgres
def test_only_one_owner_account_is_created_when_two_servers_set_up_at_once(shared_url, tmp_path: Path):
    (settings_a, first), (settings_b, second) = server(shared_url, tmp_path, "a"), server(shared_url, tmp_path, "b")
    try:
        results = together(
            lambda: AuthService(settings_a, first).create_owner("ownera", "A", "correct horse battery"),
            lambda: AuthService(settings_b, second).create_owner("ownerb", "B", "correct horse battery"),
        )
        assert sum(isinstance(result, dict) for result in results) == 1, results
        assert first.count_users() == 1
    finally:
        first.close()
        second.close()


@needs_postgres
def test_commits_of_one_repository_from_two_servers_are_all_kept_in_order(shared_url, tmp_path: Path):
    (settings_a, first), (settings_b, second) = server(shared_url, tmp_path, "a"), server(shared_url, tmp_path, "b")
    try:
        histories = [
            RepoHistory(database, HubRepositories(settings, database, StorageRegistry.wrap(None, settings)))
            for settings, database in ((settings_a, first), (settings_b, second))
        ]
        model = {"repo_id": "acme/shared", "storage_target": "local"}
        base = [RepoEntry("weights.bin", 10, "v0")]
        histories[0].record(model, "Initial", entries=base)
        for round_number in range(4):
            together(*(
                (lambda history=history, which=which: history.record(
                    model, f"Change {round_number}{which}",
                    entries=[*base, RepoEntry(f"{round_number}{which}.bin", 5, f"v{round_number}{which}")],
                ))
                for history, which in zip(histories, "ab")
            ))
        commits = sorted(first.list_commits("acme/shared", limit=50, full=True), key=lambda commit: commit["sequence"])
        # Every commit was recorded, each on top of the one before it, and the
        # last one holds every file both servers added.
        assert len(commits) == 9
        assert [commit["parent_id"] for commit in commits[1:]] == [commit["id"] for commit in commits[:-1]]
        assert len(commits[-1]["snapshot"]) == 2
    finally:
        first.close()
        second.close()


@needs_postgres
def test_two_servers_build_one_git_mirror(shared_url, tmp_path: Path, monkeypatch):
    (settings_a, first), (settings_b, second) = server(shared_url, tmp_path, "a"), server(shared_url, tmp_path, "b")
    # Both use one data folder here, so a second build would show as a second commit.
    settings_b = dataclasses.replace(settings_b, data_dir=settings_a.data_dir)
    folder = settings_a.model_storage / "acme" / "mirrored"
    folder.mkdir(parents=True)
    (folder / "config.json").write_text('{"model_type": "llama"}')
    LocalModelIndexer(settings_a, first).scan()
    built: list[str] = []
    original = GitMirrors._build

    def counting(self, snapshot, root):
        built.append(snapshot.repo_id)
        time.sleep(0.2)
        return original(self, snapshot, root)

    monkeypatch.setattr(GitMirrors, "_build", counting)
    try:
        mirrors = [
            GitMirrors(HubRepositories(settings, database, StorageRegistry.wrap(None, settings)))
            for settings, database in ((settings_a, first), (settings_b, second))
        ]
        results = together(*(lambda mirror=mirror: mirror.ensure("acme/mirrored") for mirror in mirrors))
        assert built == ["acme/mirrored"]
        assert results[0].commit == results[1].commit
    finally:
        first.close()
        second.close()


@needs_postgres
def test_a_server_serves_objects_of_a_mirror_another_server_built(shared_url, tmp_path: Path):
    # Behind one Service, git reads info/refs from one pod and objects from another. Each
    # pod has its own disk; the mirrors meet in the system folder (a bucket in a cluster).
    (settings_a, first), (settings_b, second) = (
        server(shared_url, tmp_path, "a", cluster_mode=True), server(shared_url, tmp_path, "b", cluster_mode=True)
    )
    bucket = LocalSystemStore(dataclasses.replace(settings_a, data_dir=tmp_path / "bucket"))
    bucket.remote = True
    folder = settings_a.model_storage / "acme" / "spread"
    folder.mkdir(parents=True)
    (folder / "config.json").write_text('{"model_type": "llama"}')
    LocalModelIndexer(settings_a, first).scan()
    try:
        pod_a, pod_b = (
            GitMirrors(HubRepositories(settings, database, StorageRegistry.wrap(None, settings)), system=lambda: bucket)
            for settings, database in ((settings_a, first), (settings_b, second))
        )
        built = pod_a.ensure("acme/spread")
        refs = pod_a.read_file("acme/spread", "info/refs").decode()
        assert built.commit in refs
        commit_object = f"objects/{built.commit[:2]}/{built.commit[2:]}"
        # Pod B never saw a pull of this repository: it fetches the mirror and answers.
        assert pod_b.read_file("acme/spread", commit_object) == (built.root / commit_object).read_bytes()
        assert pod_b.read_file("acme/spread", "objects/00/" + "0" * 38) is None
    finally:
        first.close()
        second.close()


# ---- leader, heartbeats, cancels ------------------------------------------------------


@needs_postgres
def test_exactly_one_server_leads_and_another_takes_over(shared_url, tmp_path: Path):
    (settings_a, first), (settings_b, second) = (
        server(shared_url, tmp_path, "a", cluster_mode=True), server(shared_url, tmp_path, "b", cluster_mode=True)
    )
    elected: list[str] = []
    clusters = [Cluster(settings, database) for settings, database in ((settings_a, first), (settings_b, second))]
    for cluster in clusters:
        cluster.when_elected(lambda name=cluster.instance_id: elected.append(name))
    try:
        for cluster in clusters:
            cluster.step()
        assert [cluster.is_leader() for cluster in clusters].count(True) == 1
        leader, follower = sorted(clusters, key=lambda cluster: not cluster.is_leader())
        # Staying leader across ticks, and the follower still cannot take over.
        leader.step()
        follower.step()
        assert leader.is_leader() and not follower.is_leader()
        # The leader's server dies: its connection goes, and with it the lock.
        leader._close()
        follower.step()
        assert follower.is_leader()
        leader.step()
        assert not leader.is_leader()
        deadline = time.monotonic() + 2
        while len(elected) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert elected == [leader.instance_id, follower.instance_id]
        assert follower.status() == {"enabled": True, "instance_id": follower.instance_id, "leader": True}
    finally:
        for cluster in clusters:
            cluster.stop()
        first.close()
        second.close()


def runtime_job(database: Database, worker: str, heartbeat: str, repo_id: str = "acme/tiny") -> dict:
    now = utc_iso()
    return database.create_runtime_job({
        "id": uuid.uuid4().hex, "target_id": "rig", "target_name": "Rig", "target_kind": "ollama",
        "repo_id": repo_id, "runtime_model_name": "tiny", "source_file": None, "status": "loading",
        "total_bytes": 0, "processed_bytes": 0, "progress": 0, "message": "", "error": None,
        "created_at": now, "updated_at": now, "completed_at": None, "user_id": None,
        "worker_id": worker, "heartbeat_at": heartbeat,
    })


@needs_postgres
def test_a_job_whose_server_went_quiet_is_failed_once_and_live_ones_are_kept(shared_url, tmp_path: Path):
    (_, first), (_, second) = server(shared_url, tmp_path, "a"), server(shared_url, tmp_path, "b")
    try:
        quiet = runtime_job(first, "gone", utc_iso(120), "acme/quiet")
        alive = runtime_job(first, "b", utc_iso(), "acme/alive")
        stale_before = utc_iso(30)
        counts = together(
            lambda: first.fail_stale_runtime_jobs(stale_before, utc_iso()),
            lambda: second.fail_stale_runtime_jobs(stale_before, utc_iso()),
        )
        assert sorted(counts) == [0, 1]
        assert first.get_runtime_job(quiet["id"])["status"] == "failed"
        assert "stopped before it finished" in first.get_runtime_job(quiet["id"])["error"]
        assert first.get_runtime_job(alive["id"])["status"] == "loading"
        # The heartbeat keeps a server's own work alive.
        first.heartbeat("b", utc_iso())
        assert first.get_runtime_job(alive["id"])["heartbeat_at"] > stale_before
        # A restart under the same name fails only that server's jobs.
        first.fail_unfinished_runtime_jobs(utc_iso(), worker_id="someone-else")
        assert first.get_runtime_job(alive["id"])["status"] == "loading"
        first.fail_unfinished_runtime_jobs(utc_iso(), worker_id="b")
        assert first.get_runtime_job(alive["id"])["status"] == "failed"
        # Moves are claimed by one server at a time.
        move = first.create_move({
            "id": uuid.uuid4().hex, "repo_id": "acme/moving", "source_target": "grid", "destination_target": "grid2",
            "keep_local": 0, "status": "queued", "message": "", "created_by": None,
            "created_at": utc_iso(), "updated_at": utc_iso(),
        })
        claims = together(
            lambda: first.claim_move(move["id"], "a", utc_iso(), stale_before),
            lambda: second.claim_move(move["id"], "b", utc_iso(), stale_before),
        )
        assert sorted(claims) == [False, True]
    finally:
        first.close()
        second.close()


@needs_postgres
def test_a_cancel_through_another_server_stops_the_job_where_it_runs(shared_url, tmp_path: Path):
    targets = json.dumps([{"id": "rig", "name": "Rig", "kind": "ollama", "base_url": "http://ollama.test:11434"}])
    (settings_a, first), (settings_b, second) = (
        server(shared_url, tmp_path, "a", runtime_targets_json=targets),
        server(shared_url, tmp_path, "b", runtime_targets_json=targets),
    )
    folder = settings_a.model_storage / "acme" / "slow"
    folder.mkdir(parents=True)
    (folder / "tiny.gguf").write_bytes(b"tiny-gguf-weights")
    LocalModelIndexer(settings_a, first).scan()
    release = threading.Event()

    def handler(request: httpx.Request):
        request.read()
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.url.path == "/api/generate":
            release.wait(10)  # a runtime holding the connection open
            return httpx.Response(200, json={"done": True})
        return httpx.Response(200, json={"status": "success"})

    transport = httpx.MockTransport(handler)
    running = RuntimeManager(settings_a, first, client_factory=lambda _: httpx.Client(transport=transport))
    elsewhere = RuntimeManager(settings_b, second, client_factory=lambda _: httpx.Client(transport=transport))
    try:
        job = running.queue("rig", first.get_local_model("acme/slow"), "acme-slow", None, None)
        assert job["worker_id"] == "a"
        deadline = time.monotonic() + 3
        while first.get_runtime_job(job["id"])["progress"] < 96 and time.monotonic() < deadline:
            time.sleep(0.01)
        cancelled = elsewhere.cancel(job["id"])
        # The job shows as cancelled at once, wherever someone looks.
        assert cancelled["status"] == "cancelled"
        # The running server stops it on its next tick; its request is abandoned.
        assert running.stop_cancelled() == [job["id"]]
        assert running._cancel_event(job["id"]).is_set()
        assert running.stop_cancelled() == []
        release.set()
        time.sleep(0.5)
        # Progress from the abandoned request never undoes the cancel.
        assert first.get_runtime_job(job["id"])["status"] == "cancelled"
    finally:
        release.set()
        running.shutdown()
        elsewhere.shutdown()
        first.close()
        second.close()


@needs_postgres
def test_failed_sign_ins_count_across_servers(shared_url, tmp_path: Path):
    (settings_a, first), (settings_b, second) = (
        server(shared_url, tmp_path, "a", cluster_mode=True), server(shared_url, tmp_path, "b", cluster_mode=True)
    )
    services = [AuthService(settings_a, first), AuthService(settings_b, second)]
    try:
        services[0].create_user("victim", "Victim", "correct horse battery")
        for attempt in range(8):
            assert services[attempt % 2].authenticate("victim", "wrong password!!", "10.0.0.9") is None
        for service in services:
            with pytest.raises(ValueError, match="Too many sign-in attempts"):
                service.authenticate("victim", "correct horse battery", "10.0.0.9")
        # Another address is not held back by these.
        assert services[1].authenticate("victim", "correct horse battery", "10.0.0.10")["username"] == "victim"
    finally:
        first.close()
        second.close()


def test_cluster_mode_turns_off_the_local_cache_with_a_sentence(monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, cluster_mode=True))
    assert {"library.cache", "runtimes.use"} <= main.disabled_capabilities()
    for capability in ("library.cache", "runtimes.use"):
        status, detail = main.DISABLED_FEATURES[capability]
        assert status == 409 and "CLUSTER_MODE" in detail
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, cluster_mode=False))
    assert not {"library.cache", "runtimes.use"} & main.disabled_capabilities()


def test_runtime_routes_refuse_with_a_sentence_in_cluster_mode(monkeypatch, tmp_path: Path):
    from fastapi.testclient import TestClient

    database = Database(tmp_path / "hub.sqlite3")
    database.initialize()
    monkeypatch.setattr(main, "database", database)
    auth = AuthService(main.settings, database)
    auth.create_user("owner", "Owner", "correct horse battery", "admin")
    monkeypatch.setattr(main, "auth", auth)
    token = {"Authorization": "Bearer runtime-automation-token"}
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, runtime_api_token="runtime-automation-token"))
    client = TestClient(main.app)
    assert client.get("/api/runtimes", headers=token).status_code == 200
    monkeypatch.setattr(
        main, "settings",
        dataclasses.replace(main.settings, cluster_mode=True, runtime_api_token="runtime-automation-token"),
    )
    for method, path in (("get", "/api/runtimes"), ("get", "/api/runtime-jobs"), ("post", "/api/runtimes/any/load")):
        answer = getattr(client, method)(path, headers=token, **({"json": {"repo_id": "a/b", "runtime_model_name": "x"}} if method == "post" else {}))
        assert answer.status_code == 409, path
        assert "CLUSTER_MODE" in answer.json()["detail"]
    # Who asks is still checked first: a wrong token or no one learns nothing about the setup.
    assert client.get("/api/runtimes", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/runtimes").status_code == 401


def test_abandoned_uploads_are_swept_hourly_by_the_leader_only(monkeypatch):
    calls = []
    leader = {"now": False}
    monkeypatch.setattr(main, "SWEEP_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(main.uploads, "sweep_stale_uploads", lambda: calls.append(1))
    monkeypatch.setattr(main.cluster, "is_leader", lambda: leader["now"])
    main.sweep_stop.clear()
    worker = threading.Thread(target=main.sweep_abandoned_uploads_forever, daemon=True)
    worker.start()
    try:
        time.sleep(0.1)
        assert calls == []  # a follower leaves it to the leader
        leader["now"] = True
        deadline = time.time() + 2
        while not calls and time.time() < deadline:
            time.sleep(0.01)
        assert calls
    finally:
        main.sweep_stop.set()
        worker.join(2)
    assert not worker.is_alive()
