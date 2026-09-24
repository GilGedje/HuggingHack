import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.errors import RepositoryNotFoundError

import app.main as main
from app.auth import AuthService
from app.catalog import LocalCatalog
from app.config import Settings
from app.database import Database
from app.git_mirror import GitMirrors
from app.history import RepoHistory
from app.hub_api import HubRepositories
from app.indexer import LocalModelIndexer
from app.permissions import CAPABILITIES, ROLE_CAPABILITIES
from app.runtimes import RuntimeManager
from app.storage import StorageRegistry
from app.uploads import UploadManager
from test_hub_api import free_port, git_environment

PASSWORDS = {
    "admin": "correct horse battery",
    "member": "another secure phrase",
    "viewer": "read only passphrase",
}
WEIGHTS = b"W" * 300_000


@pytest.fixture()
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage_path = (tmp_path / "models").resolve()
    settings = Settings(
        model_storage=storage_path,
        data_dir=(tmp_path / "data").resolve(),
        accounts_enabled=True,
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    auth = AuthService(settings, database)
    registry = StorageRegistry(settings)
    indexer = LocalModelIndexer(settings, database)
    repositories = HubRepositories(settings, database, registry)
    history = RepoHistory(database, repositories)
    uploads = UploadManager(settings, database, indexer, registry, history)
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
    users = {role: auth.create_user(role, role.title(), password, role) for role, password in PASSWORDS.items()}
    public = storage_path / "acme" / "open"
    public.mkdir(parents=True)
    (public / "config.json").write_text("{}", encoding="utf-8")
    private = uploads.create_repository(users["member"], "secret", "", "private")
    for path, payload in {"config.json": b'{"a": 1}', "model.safetensors": WEIGHTS}.items():
        uploads.upload_chunk(private["repo_id"], users["member"]["id"], path, 0, len(payload), payload)
    uploads.finalize(private["repo_id"], users["member"]["id"])
    main.refresh_model_index()
    return {"settings": settings, "database": database, "auth": auth, "users": users}


def login(role: str) -> tuple[TestClient, dict]:
    client = TestClient(main.app)
    response = client.post("/api/auth/login", json={"username": role, "password": PASSWORDS[role]})
    assert response.status_code == 200, response.text
    status = response.json()
    client.headers["X-CSRF-Token"] = status["csrf_token"]
    return client, status


def test_every_write_route_declares_who_may_call_it():
    """Adding an endpoint without a capability check fails this test."""
    allowed = {
        ("POST", "/api/auth/setup"),
        ("POST", "/api/auth/login"),
        ("POST", "/{owner}/{name}/info/lfs/objects/batch"),  # a read: pulls weights
        ("POST", "/api/runtimes/{target_id}/load"),  # runtime token or runtimes.use
    }

    def guarded(dependant) -> bool:
        for dependency in dependant.dependencies:
            call = dependency.call
            if getattr(call, "capability", None) or getattr(call, "personal", False):
                return True
            if guarded(dependency):
                return True
        return False

    unguarded = []
    for route in main.app.routes:
        methods = getattr(route, "methods", None) or set()
        for method in methods - {"GET", "HEAD"}:
            if (method, route.path) in allowed:
                continue
            if not guarded(route.dependant):
                unguarded.append((method, route.path))
    assert unguarded == []


def test_roles_follow_the_capability_table(server):
    assert ROLE_CAPABILITIES["admin"] == frozenset(CAPABILITIES)
    viewer, status = login("viewer")
    assert "repos.create" not in status["capabilities"]
    assert "models.browse" in status["capabilities"]
    assert viewer.post("/api/uploads/repositories", json={"slug": "nope"}).status_code == 403
    assert viewer.post("/api/local-models/scan").status_code == 403
    assert viewer.post("/api/downloads", json={"repo_id": "acme/x"}).status_code == 403
    assert viewer.post("/api/repos/changes", json={"repo_id": "acme/open"}).status_code == 403
    assert viewer.get("/api/storage/targets").status_code == 403
    assert viewer.get("/api/admin/users").status_code == 403
    # Personal features stay open to viewers.
    assert viewer.post("/api/saved-models", json={"repo_id": "acme/open"}).status_code == 200
    assert viewer.get("/api/library/models").status_code == 200

    member, _ = login("member")
    assert member.get("/api/storage/targets").status_code == 403
    assert member.post("/api/repos/changes", json={"repo_id": "acme/open"}).status_code == 403
    assert member.post("/api/repos/changes", json={"repo_id": "member/secret"}).status_code == 201

    admin, _ = login("admin")
    assert admin.post("/api/repos/changes", json={"repo_id": "acme/open"}).status_code == 201


def test_api_tokens_are_scoped_and_revocable(server):
    member, _ = login("member")
    created = member.post(
        "/api/account/tokens", json={"name": "laptop", "scope": "read", "expires_in_days": 30}
    ).json()
    raw = created["token"]
    assert raw.startswith("hht_") and created["prefix"] == raw[:10]
    listed = member.get("/api/account/tokens").json()["items"]
    assert [token["name"] for token in listed] == ["laptop"]
    assert "token" not in listed[0] and "token_hash" not in listed[0]

    api = TestClient(main.app)
    api.headers["Authorization"] = f"Bearer {raw}"
    assert api.get("/api/library/models/member/secret").status_code == 200
    assert api.post("/api/saved-models", json={"repo_id": "acme/open"}).status_code == 403
    for path in ("/api/account/tokens", "/api/account/sessions", "/api/admin/users", "/api/account"):
        assert api.get(path).status_code == 403, path
    # A header credential is never combined with a cookie.
    api.cookies.update(member.cookies)
    assert api.post("/api/saved-models", json={"repo_id": "acme/open"}).status_code == 403

    write = member.post("/api/account/tokens", json={"name": "ci", "scope": "write"}).json()
    writer = TestClient(main.app)
    writer.headers["Authorization"] = f"Bearer {write['token']}"
    assert writer.post("/api/saved-models", json={"repo_id": "acme/open"}).status_code == 200
    assert writer.post("/api/account/tokens", json={"name": "x"}).status_code == 403
    assert writer.patch("/api/account/password", json={"current_password": "a" * 12, "new_password": "b" * 12}).status_code == 403

    assert member.delete(f"/api/account/tokens/{created['id']}").status_code == 200
    assert api.get("/api/library/models").status_code == 401
    bad = TestClient(main.app)
    bad.headers["Authorization"] = "Bearer hht_not-a-real-token"
    assert bad.get("/api/library/models").status_code == 401


def test_disabling_an_account_cuts_off_sessions_and_tokens(server):
    member, _ = login("member")
    token = member.post("/api/account/tokens", json={"name": "t", "scope": "write"}).json()["token"]
    api = TestClient(main.app)
    api.headers["Authorization"] = f"Bearer {token}"
    assert api.get("/api/library/models").status_code == 200

    admin, _ = login("admin")
    member_id = server["users"]["member"]["id"]
    assert admin.patch(f"/api/admin/users/{member_id}", json={"disabled": True}).json()["disabled"] is True
    assert member.get("/api/library/models").status_code == 401
    assert api.get("/api/library/models").status_code == 401
    login_attempt = TestClient(main.app).post(
        "/api/auth/login", json={"username": "member", "password": PASSWORDS["member"]}
    )
    assert login_attempt.status_code == 403

    admin.patch(f"/api/admin/users/{member_id}", json={"disabled": False})
    assert api.get("/api/library/models").status_code == 200
    admin.post(f"/api/admin/users/{member_id}/revoke", json={"sessions": True, "tokens": True})
    assert api.get("/api/library/models").status_code == 401


def test_admin_user_management_guardrails(server):
    admin, _ = login("admin")
    users = server["users"]
    listing = admin.get("/api/admin/users").json()["items"]
    member_row = next(row for row in listing if row["username"] == "member")
    assert member_row["repositories"] == 1 and member_row["sessions"] == 0
    assert "password_hash" not in member_row

    assert admin.patch(f"/api/admin/users/{users['admin']['id']}", json={"role": "member"}).status_code == 409
    assert admin.patch(f"/api/admin/users/{users['admin']['id']}", json={"disabled": True}).status_code == 409
    assert admin.delete(f"/api/admin/users/{users['admin']['id']}").status_code == 409
    blocked = admin.delete(f"/api/admin/users/{users['member']['id']}")
    assert blocked.status_code == 409 and "member/secret" in blocked.json()["detail"]

    viewer_id = users["viewer"]["id"]
    assert admin.patch(f"/api/admin/users/{viewer_id}", json={"role": "member"}).json()["role"] == "member"
    viewer, status = login("viewer")
    assert "repos.create" in status["capabilities"]
    assert admin.post(f"/api/admin/users/{viewer_id}/password", json={"new_password": "short"}).status_code == 422
    assert admin.post(
        f"/api/admin/users/{viewer_id}/password", json={"new_password": "a brand new passphrase"}
    ).status_code == 200
    assert viewer.get("/api/library/models").status_code == 401
    assert admin.delete(f"/api/admin/users/{viewer_id}").status_code == 200
    created = admin.post(
        "/api/users",
        json={"username": "reader", "display_name": "Reader", "password": "reader passphrase", "role": "viewer", "email": "r@example.com"},
    ).json()
    assert created["role"] == "viewer" and created["email"] == "r@example.com"
    matrix = admin.get("/api/admin/permissions").json()
    assert [role["id"] for role in matrix["roles"]] == ["viewer", "member", "admin"]


def test_account_self_service(server):
    member, status = login("member")
    assert status["user"]["username"] == "member"
    assert member.patch("/api/account/profile", json={"display_name": "Mem", "email": "bad"}).status_code == 400
    profile = member.patch("/api/account/profile", json={"display_name": "Mem", "email": "m@example.com"}).json()
    assert profile["display_name"] == "Mem" and profile["email"] == "m@example.com"
    prefs = member.patch("/api/account/preferences", json={"theme": "dark", "catalog_sort": "size"}).json()
    assert prefs == {"theme": "dark", "catalog_sort": "size"}
    assert member.patch("/api/account/preferences", json={"default_storage_target": "nowhere"}).status_code == 400
    assert member.get("/api/account").json()["repositories"] == ["member/secret"]

    other, _ = login("member")
    sessions = member.get("/api/account/sessions").json()["items"]
    assert len(sessions) == 2 and sum(session["current"] for session in sessions) == 1
    assert "token_hash" not in sessions[0]
    other_id = next(session["id"] for session in sessions if not session["current"])
    assert member.delete(f"/api/account/sessions/{other_id}").status_code == 200
    assert other.get("/api/library/models").status_code == 401


def test_server_settings_never_expose_secret_values(server, monkeypatch: pytest.MonkeyPatch):
    secrets = {
        "hf": "hf_SECRETVALUE111",
        "runtime": "RUNTIME_SECRET_222",
        "agent": "AGENT_SECRET_333",
        "s3": "S3_SECRET_444",
        "db": "DBPASS_555",
    }
    monkeypatch.setenv("AGENT_TOKEN_FOR_TEST", secrets["agent"])
    settings = replace(
        server["settings"],
        hf_token=secrets["hf"],
        runtime_api_token=secrets["runtime"],
        database_url=f"postgresql://user:{secrets['db']}@db.internal:5432/hugginghack",
        runtime_targets_json=json.dumps(
            [{"id": "rig", "name": "Rig", "kind": "vllm", "base_url": "http://10.0.0.2:8090", "remote_model_root": "/mnt/models", "token_env": "AGENT_TOKEN_FOR_TEST"}]
        ),
    )
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "runtimes", RuntimeManager(settings, server["database"]))
    admin, _ = login("admin")
    response = admin.get("/api/admin/server")
    assert response.status_code == 200
    body = response.text
    for value in secrets.values():
        assert value not in body
    payload = response.json()
    assert payload["hugging_face"]["token_configured"] is True
    assert payload["runtimes"]["api_token_configured"] is True
    member, _ = login("member")
    assert member.get("/api/admin/server").status_code == 403


def test_runtime_automation_token_only_reaches_runtimes(server, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main, "settings", replace(server["settings"], runtime_api_token="rt-secret-token"))
    client = TestClient(main.app)
    client.headers["Authorization"] = "Bearer rt-secret-token"
    assert client.get("/api/runtimes").status_code == 200
    assert client.get("/api/admin/users").status_code == 401
    assert client.get("/api/library/models").status_code == 401
    # Personal tokens still work on runtime endpoints, by role.
    admin, _ = login("admin")
    member, _ = login("member")
    for session, expected in ((admin, 200), (member, 403)):
        token = session.post("/api/account/tokens", json={"name": "rt", "scope": "read"}).json()["token"]
        personal = TestClient(main.app)
        personal.headers["Authorization"] = f"Bearer {token}"
        assert personal.get("/api/runtimes").status_code == expected


@pytest.fixture()
def live(server):
    import threading
    import time

    import uvicorn

    port = free_port()
    instance = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    while not instance.started:
        time.sleep(0.05)
    member, _ = login("member")
    token = member.post("/api/account/tokens", json={"name": "pull", "scope": "read"}).json()["token"]
    try:
        yield {"url": f"http://127.0.0.1:{port}", "token": token}
    finally:
        instance.should_exit = True
        thread.join(timeout=10)


def test_private_repositories_pull_with_a_token(live, tmp_path: Path):
    url, token = live["url"], live["token"]
    with pytest.raises(RepositoryNotFoundError):
        HfApi(endpoint=url, token=False).model_info("member/secret")
    info = HfApi(endpoint=url, token=token).model_info("member/secret")
    assert info.private is True
    path = hf_hub_download("member/secret", "model.safetensors", endpoint=url, token=token, cache_dir=tmp_path / "hf")
    assert Path(path).read_bytes() == WEIGHTS
    assert httpx.get(f"{url}/api/models/member/secret", headers={"Authorization": "Bearer hht_wrong"}).status_code == 401
    # Anonymous pulls of public models keep working.
    assert HfApi(endpoint=url, token=False).model_info("acme/open").private is False
    # A stored Hugging Face token (or any foreign credential) must not break pulls.
    assert HfApi(endpoint=url, token="hf_someStaleHubToken").model_info("acme/open").private is False
    public = hf_hub_download("acme/open", "config.json", endpoint=url, token="hf_someStaleHubToken", cache_dir=tmp_path / "hf2")
    assert Path(public).read_text() == "{}"
    with pytest.raises(RepositoryNotFoundError):
        HfApi(endpoint=url, token="hf_someStaleHubToken").model_info("member/secret")


@pytest.mark.skipif(
    shutil.which("git") is None
    or subprocess.run(["git", "lfs", "version"], capture_output=True).returncode != 0,
    reason="git and git-lfs are required",
)
def test_private_repositories_clone_with_a_token(live, tmp_path: Path):
    environment = git_environment(tmp_path)
    base = live["url"].replace("http://", "")
    denied = subprocess.run(
        ["git", "clone", f"http://{base}/member/secret", str(tmp_path / "anon")],
        env=environment, capture_output=True, text=True,
    )
    assert denied.returncode != 0
    wrong = subprocess.run(
        ["git", "clone", f"http://member:hht_wrong@{base}/member/secret", str(tmp_path / "wrong")],
        env=environment, capture_output=True, text=True,
    )
    assert wrong.returncode != 0
    foreign = subprocess.run(
        ["git", "clone", f"http://x:ghp_someGithubToken@{base}/acme/open", str(tmp_path / "public")],
        env=environment, capture_output=True, text=True,
    )
    assert foreign.returncode == 0, foreign.stderr
    ok = subprocess.run(
        ["git", "clone", f"http://member:{live['token']}@{base}/member/secret", str(tmp_path / "clone")],
        env=environment, capture_output=True, text=True,
    )
    assert ok.returncode == 0, ok.stderr
    assert (tmp_path / "clone" / "model.safetensors").read_bytes() == WEIGHTS
    assert os.path.getsize(tmp_path / "clone" / "config.json") == 8
