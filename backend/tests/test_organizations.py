import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.errors import RepositoryNotFoundError

import app.main as main
from test_access import PASSWORDS, free_port, server  # noqa: F401  (fixture)
from test_hub_api import git_environment

WEIGHTS = b"N" * 200_000
EXTRA_PASSWORDS: dict[str, str] = {}


def add_user(state: dict, username: str, role: str) -> dict:
    EXTRA_PASSWORDS[username] = f"{username} long passphrase"
    return state["auth"].create_user(username, username.title(), EXTRA_PASSWORDS[username], role)


def login(username: str) -> tuple[TestClient, dict]:
    client = TestClient(main.app)
    password = PASSWORDS.get(username) or EXTRA_PASSWORDS[username]
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    status = response.json()
    client.headers["X-CSRF-Token"] = status["csrf_token"]
    return client, status


def upload(client: TestClient, repo_id: str, files: dict[str, bytes], message: str = "Initial") -> None:
    for path, payload in files.items():
        response = client.put(
            "/api/uploads/repositories/files",
            params={"repo_id": repo_id, "path": path},
            headers={"Upload-Offset": "0", "Upload-Length": str(len(payload))},
            content=payload,
        )
        assert response.status_code == 200, response.text
    finalized = client.post(
        "/api/uploads/repositories/finalize", params={"repo_id": repo_id}, json={"message": message}
    )
    assert finalized.status_code == 200, finalized.text


@pytest.fixture()
def org(server):  # noqa: F811
    add_user(server, "writer", "member")
    add_user(server, "reader", "member")
    add_user(server, "outsider", "member")
    admin, _ = login("admin")
    created = admin.post(
        "/api/organizations", json={"name": "Nvidia", "display_name": "NVIDIA", "description": "GPU models"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["my_role"] == "admin"
    for username, role in (("writer", "write"), ("reader", "read")):
        assert admin.put(f"/api/organizations/nvidia/members/{username}", json={"role": role}).status_code == 200
    return server


def test_organization_names_share_one_namespace_with_users(org):
    admin, _ = login("admin")
    for name in ("nvidia", "NVIDIA", "member", "Member", "api", "a", "-bad", "has space"):
        response = admin.post("/api/organizations", json={"name": name})
        assert response.status_code in {409, 422}, name
    with pytest.raises(ValueError):
        org["auth"].create_user("nvidia", "Imposter", "some long passphrase", "member")
    with pytest.raises(ValueError):
        org["auth"].create_user("api", "Api", "some long passphrase", "member")
    # Single sign-on usernames step around organization names too.
    created = org["auth"].provision_external_user("oidc", {"sub": "x1", "preferred_username": "NVIDIA"}, "preferred_username", "viewer")
    assert created["username"] == "nvidia-2"
    member, _ = login("member")
    assert member.post("/api/organizations", json={"name": "Meta"}).status_code == 403


def test_writers_upload_into_the_organization_and_roles_control_access(org):
    writer, _ = login("writer")
    namespaces = [item["name"] for item in writer.get("/api/uploads/namespaces").json()["items"]]
    assert namespaces == ["writer", "Nvidia"]
    created = writer.post(
        "/api/uploads/repositories", json={"slug": "GLM-5.3-NVFP4", "namespace": "nvidia", "visibility": "private"}
    )
    assert created.status_code == 201, created.text
    repo_id = created.json()["repo_id"]
    assert repo_id == "Nvidia/GLM-5.3-NVFP4"
    upload(writer, repo_id, {"config.json": b"{}", "model.safetensors": WEIGHTS})
    assert (org["settings"].model_storage / "Nvidia" / "GLM-5.3-NVFP4" / "model.safetensors").is_file()
    manifest = json.loads((org["settings"].model_storage / "Nvidia" / "GLM-5.3-NVFP4" / ".hugginghack.json").read_text())
    assert manifest["organization_id"] == main.database.get_organization("nvidia")["id"]
    assert manifest["owner_id"] == main.database.get_user_by_username("writer")["id"]

    reader, _ = login("reader")
    outsider, _ = login("outsider")
    assert reader.get(f"/api/library/models/{repo_id}").status_code == 200
    assert outsider.get(f"/api/library/models/{repo_id}").status_code == 404
    assert repo_id in [item["id"] for item in reader.get("/api/library/models", params={"owner": "nvidia"}).json()["items"]]
    assert outsider.get("/api/library/models", params={"owner": "nvidia"}).json()["count"] == 0
    assert repo_id in [item["repo_id"] for item in reader.get("/api/uploads/repositories").json()["items"]]
    assert repo_id not in [item["repo_id"] for item in outsider.get("/api/uploads/repositories").json()["items"]]
    details = reader.get(f"/api/library/models/{repo_id}").json()
    assert details["organization"] == {"name": "Nvidia", "display_name": "NVIDIA"}
    assert details["can_edit"] is False

    # Readers cannot upload; writers can change; only org admins change visibility or delete.
    assert reader.post("/api/uploads/repositories", json={"slug": "x", "namespace": "Nvidia"}).status_code == 403
    assert outsider.post("/api/uploads/repositories", json={"slug": "x", "namespace": "Nvidia"}).status_code == 403
    assert reader.post("/api/repos/changes", json={"repo_id": repo_id}).status_code == 403
    session = writer.post("/api/repos/changes", json={"repo_id": repo_id})
    assert session.status_code == 201
    assert writer.patch("/api/uploads/repositories", params={"repo_id": repo_id}, json={"description": "", "visibility": "shared"}).status_code == 404
    admin, _ = login("admin")
    assert admin.patch("/api/uploads/repositories", params={"repo_id": repo_id}, json={"description": "NVFP4 build", "visibility": "shared"}).status_code == 200
    assert outsider.get(f"/api/library/models/{repo_id}").status_code == 200

    # Removing a member cuts access immediately, including a staged change.
    admin.patch("/api/uploads/repositories", params={"repo_id": repo_id}, json={"description": "", "visibility": "private"})
    assert admin.delete("/api/organizations/Nvidia/members/writer").status_code == 200
    committed = writer.post(f"/api/repos/changes/{session.json()['id']}/commit", json={"message": "late"})
    assert committed.status_code == 403
    assert writer.get(f"/api/library/models/{repo_id}").status_code == 404

    # An organization with repositories cannot be deleted.
    assert admin.delete("/api/organizations/Nvidia").status_code == 409
    deleted = admin.request("DELETE", "/api/uploads/repositories", params={"repo_id": repo_id}, json={"confirmation": repo_id})
    assert deleted.status_code == 200, deleted.text
    assert admin.delete("/api/organizations/Nvidia").status_code == 200


def test_organization_admins_manage_members_and_keep_one_admin(org):
    admin, _ = login("admin")
    member_role_admin = admin.put("/api/organizations/Nvidia/members/member", json={"role": "admin"})
    assert member_role_admin.status_code == 200
    admin.delete(f"/api/organizations/Nvidia/members/admin")
    orgadmin, _ = login("member")
    payload = orgadmin.get("/api/organizations/nvidia").json()
    assert payload["my_role"] == "admin" and payload["can_manage"] is True
    assert orgadmin.put("/api/organizations/Nvidia/members/outsider", json={"role": "read"}).status_code == 200
    assert orgadmin.patch("/api/organizations/Nvidia", json={"description": "Updated"}).json()["description"] == "Updated"
    # The last organization admin cannot step down or leave.
    assert orgadmin.put("/api/organizations/Nvidia/members/member", json={"role": "write"}).status_code == 409
    assert orgadmin.delete("/api/organizations/Nvidia/members/member").status_code == 409
    writer, _ = login("writer")
    assert writer.put("/api/organizations/Nvidia/members/outsider", json={"role": "admin"}).status_code == 403
    assert writer.delete("/api/organizations/Nvidia/members/writer").status_code == 200  # leaving is allowed
    # Server admins can override the safeguard.
    assert admin.delete("/api/organizations/Nvidia/members/member").status_code == 200


def test_deleting_the_creator_keeps_organization_repositories(org):
    writer, _ = login("writer")
    repo_id = writer.post("/api/uploads/repositories", json={"slug": "kept", "namespace": "Nvidia"}).json()["repo_id"]
    upload(writer, repo_id, {"config.json": b"{}"})
    admin, _ = login("admin")
    writer_id = main.database.get_user_by_username("writer")["id"]
    assert admin.delete(f"/api/admin/users/{writer_id}").status_code == 200
    repository = main.database.get_owned_repository(repo_id)
    assert repository["owner_id"] == org["users"]["admin"]["id"]
    main.refresh_model_index()
    assert main.database.get_local_model(repo_id) is not None
    reader, _ = login("reader")
    assert reader.get(f"/api/library/models/{repo_id}").status_code == 200


@pytest.fixture()
def live_org(org):
    import threading
    import time

    import uvicorn

    writer, _ = login("writer")
    repo_id = writer.post("/api/uploads/repositories", json={"slug": "secret-weights", "namespace": "Nvidia"}).json()["repo_id"]
    upload(writer, repo_id, {"config.json": b'{"x": 1}', "model.safetensors": WEIGHTS})
    tokens = {}
    for username in ("reader", "outsider"):
        client, _ = login(username)
        tokens[username] = client.post("/api/account/tokens", json={"name": "pull", "scope": "read"}).json()["token"]
    port = free_port()
    instance = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    while not instance.started:
        time.sleep(0.05)
    try:
        yield {"url": f"http://127.0.0.1:{port}", "repo": repo_id, "tokens": tokens}
    finally:
        instance.should_exit = True
        thread.join(timeout=10)


def test_organization_members_pull_private_repositories_with_tokens(live_org, tmp_path: Path):
    url, repo, tokens = live_org["url"], live_org["repo"], live_org["tokens"]
    path = hf_hub_download(repo, "model.safetensors", endpoint=url, token=tokens["reader"], cache_dir=tmp_path / "hf")
    assert Path(path).read_bytes() == WEIGHTS
    with pytest.raises(RepositoryNotFoundError):
        HfApi(endpoint=url, token=tokens["outsider"]).model_info(repo)
    with pytest.raises(RepositoryNotFoundError):
        HfApi(endpoint=url, token=False).model_info(repo)
    if shutil.which("git") is None or subprocess.run(["git", "lfs", "version"], capture_output=True).returncode != 0:
        pytest.skip("git and git-lfs are required for the clone check")
    environment = git_environment(tmp_path)
    host = url.replace("http://", "")
    clone = subprocess.run(
        ["git", "clone", f"http://reader:{tokens['reader']}@{host}/{repo}", str(tmp_path / "clone")],
        env=environment, capture_output=True, text=True,
    )
    assert clone.returncode == 0, clone.stderr
    assert (tmp_path / "clone" / "model.safetensors").read_bytes() == WEIGHTS
