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
        "/api/uploads/repositories", json={"slug": "GLM-5.3-NVFP4", "namespace": "nvidia", "visibility": "organization"}
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
    # The Uploads page lists only repositories the account can upload to, with its role.
    assert [(item["repo_id"], item["my_role"]) for item in writer.get("/api/uploads/repositories").json()["items"]] == [(repo_id, "write")]
    assert repo_id not in [item["repo_id"] for item in reader.get("/api/uploads/repositories").json()["items"]]
    assert repo_id not in [item["repo_id"] for item in outsider.get("/api/uploads/repositories").json()["items"]]
    admin_view, _ = login("admin")
    assert (repo_id, "admin") in [(item["repo_id"], item["my_role"]) for item in admin_view.get("/api/uploads/repositories").json()["items"]]
    details = reader.get(f"/api/library/models/{repo_id}").json()
    assert details["organization"] == {"name": "Nvidia", "display_name": "NVIDIA"}
    assert details["can_edit"] is False

    # Readers cannot upload; writers can change; only org admins change visibility or delete.
    assert reader.post("/api/uploads/repositories", json={"slug": "x", "namespace": "Nvidia"}).status_code == 403
    assert outsider.post("/api/uploads/repositories", json={"slug": "x", "namespace": "Nvidia"}).status_code == 403
    assert reader.post("/api/repos/changes", json={"repo_id": repo_id}).status_code == 403
    session = writer.post("/api/repos/changes", json={"repo_id": repo_id})
    assert session.status_code == 201
    assert writer.patch("/api/uploads/repositories", params={"repo_id": repo_id}, json={"description": "", "visibility": "public"}).status_code == 404
    admin, _ = login("admin")
    assert admin.patch("/api/uploads/repositories", params={"repo_id": repo_id}, json={"description": "NVFP4 build", "visibility": "public"}).status_code == 200
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
    repo_id = writer.post("/api/uploads/repositories", json={"slug": "kept", "namespace": "Nvidia", "visibility": "organization"}).json()["repo_id"]
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
    repo_id = writer.post("/api/uploads/repositories", json={"slug": "secret-weights", "namespace": "Nvidia", "visibility": "organization"}).json()["repo_id"]
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


def test_organization_members_pull_organization_repositories_with_tokens(live_org, tmp_path: Path):
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


def test_new_accounts_can_start_in_organizations(org):
    admin, _ = login("admin")
    admin.post("/api/organizations", json={"name": "Meta"})
    account = {"username": "newhire", "display_name": "New Hire", "password": "a long enough passphrase", "role": "member"}

    # Any bad entry is rejected before the account exists.
    missing = admin.post("/api/users", json={**account, "organizations": [{"organization": "Nvidia", "role": "write"}, {"organization": "Nope", "role": "read"}]})
    assert missing.status_code == 404
    twice = admin.post("/api/users", json={**account, "organizations": [{"organization": "nvidia"}, {"organization": "NVIDIA", "role": "admin"}]})
    assert twice.status_code == 400 and "more than once" in twice.json()["detail"]
    bad_role = admin.post("/api/users", json={**account, "organizations": [{"organization": "Nvidia", "role": "owner"}]})
    assert bad_role.status_code == 422
    assert main.database.get_user_by_username("newhire") is None

    created = admin.post("/api/users", json={**account, "organizations": [{"organization": "nvidia", "role": "write"}, {"organization": "Meta"}]})
    assert created.status_code == 201, created.text
    assert [(item["name"], item["role"]) for item in created.json()["organizations"]] == [("Meta", "read"), ("Nvidia", "write")]
    members = {item["username"]: item["role"] for item in admin.get("/api/organizations/Nvidia").json()["members"]}
    assert members["newhire"] == "write"

    # A plain account creation is unchanged.
    plain = admin.post("/api/users", json={**account, "username": "plain"})
    assert plain.status_code == 201 and plain.json()["organizations"] == []

    member, _ = login("member")
    assert member.post("/api/users", json={**account, "username": "sneaky", "organizations": [{"organization": "Nvidia", "role": "admin"}]}).status_code == 403


def test_upload_permission_needs_both_server_and_organization_roles(org):
    writer, _ = login("writer")
    repo_id = writer.post("/api/uploads/repositories", json={"slug": "gated", "namespace": "Nvidia"}).json()["repo_id"]
    upload(writer, repo_id, {"config.json": b"{}"})
    admin, _ = login("admin")
    refused = admin.put("/api/organizations/Nvidia/members/viewer", json={"role": "write"})
    assert refused.status_code == 400 and "can only be Read" in refused.json()["detail"]
    # A Write role given before that rule existed still writes nothing.
    main.database.set_organization_member(
        main.database.get_organization("Nvidia")["id"], org["users"]["viewer"]["id"], "write", "2026-01-01T00:00:00+00:00"
    )

    # A server Viewer with an organization Write role still cannot upload anywhere.
    viewer, status = login("viewer")
    assert "repos.create" not in status["capabilities"]
    assert viewer.get("/api/uploads/repositories").status_code == 403
    assert viewer.get("/api/uploads/namespaces").json()["items"] == []
    assert viewer.post("/api/uploads/repositories", json={"slug": "x", "namespace": "Nvidia"}).status_code == 403
    assert viewer.put(
        "/api/uploads/repositories/files",
        params={"repo_id": repo_id, "path": "extra.json"},
        headers={"Upload-Offset": "0", "Upload-Length": "2"},
        content=b"{}",
    ).status_code == 403
    assert viewer.post("/api/repos/changes", json={"repo_id": repo_id}).status_code == 403
    assert viewer.get(f"/api/library/models/{repo_id}").json()["can_edit"] is False

    # A member with only Read in the organization cannot write to it either.
    reader, _ = login("reader")
    assert [item["name"] for item in reader.get("/api/uploads/namespaces").json()["items"]] == ["reader"]
    assert reader.put(
        "/api/uploads/repositories/files",
        params={"repo_id": repo_id, "path": "extra.json"},
        headers={"Upload-Offset": "0", "Upload-Length": "2"},
        content=b"{}",
    ).status_code == 404


def test_admin_organization_list_is_paged_and_filtered(org):
    admin, _ = login("admin")
    for index in range(30):
        created = admin.post("/api/organizations", json={"name": f"Lab{index:02d}", "display_name": f"Research Lab {index:02d}", "description": "gpu_team" if index % 5 == 0 else ""})
        assert created.status_code == 201, created.text
    member, _ = login("member")
    assert member.get("/api/admin/organizations").status_code == 403

    first = admin.get("/api/admin/organizations").json()
    assert (first["total"], first["pages"], len(first["items"])) == (31, 2, 25)
    assert first["counts"] == {"all": 31, "with_repositories": 0, "empty": 31, "mine": 31}
    assert first["items"][0]["name"] == "Lab00" and first["items"][0]["my_role"] == "admin"
    last = admin.get("/api/admin/organizations", params={"page": 9}).json()
    assert last["page"] == 2 and len(last["items"]) == 6

    writer, _ = login("writer")
    repo_id = writer.post("/api/uploads/repositories", json={"slug": "m", "namespace": "Nvidia"}).json()["repo_id"]
    assert repo_id
    with_repos = admin.get("/api/admin/organizations", params={"filter": "with_repositories"}).json()
    assert [item["name"] for item in with_repos["items"]] == ["Nvidia"]
    by_members = admin.get("/api/admin/organizations", params={"sort": "members", "per_page": 1}).json()
    assert by_members["items"][0]["name"] == "Nvidia" and by_members["items"][0]["member_count"] == 3

    searched = admin.get("/api/admin/organizations", params={"q": "research lab 1"}).json()
    assert searched["total"] == 10 and searched["counts"]["all"] == 10
    assert admin.get("/api/admin/organizations", params={"q": "GPU_TEAM"}).json()["total"] == 6
    assert admin.get("/api/admin/organizations", params={"q": "_"}).json()["total"] == 6  # the literal underscore
    assert admin.get("/api/admin/organizations", params={"q": "%"}).json()["total"] == 0
    assert admin.get("/api/admin/organizations", params={"filter": "nope"}).status_code == 422


def test_an_organization_description_is_markdown_with_room_to_write(org):  # noqa: F811
    admin, _ = login("admin")
    about = "## About NVIDIA\n\n**Accelerated computing** models, tuned for:\n\n- Blackwell\n- Hopper\n\n" + "x" * 2000
    saved = admin.patch("/api/organizations/nvidia", json={"description": f"  {about}\n"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["description"] == about  # line breaks kept, outer space trimmed
    assert admin.get("/api/organizations/nvidia").json()["description"] == about
    assert admin.patch("/api/organizations/nvidia", json={"description": "x" * 10_001}).status_code == 422


def test_only_members_who_may_write_are_offered_uploads(org):  # noqa: F811
    offered = {}
    for username in ("admin", "writer", "reader", "outsider", "viewer"):
        client, _ = login(username)
        response = client.get("/api/organizations/nvidia")
        offered[username] = response.json().get("can_upload") if response.status_code == 200 else None
    # The server admin who created it is its admin; writers upload; readers and others do not.
    assert offered == {"admin": True, "writer": True, "reader": False, "outsider": False, "viewer": False}
    # A viewer holding an older write role still cannot upload: the role decides first.
    main.database.set_organization_member(
        main.database.get_organization("nvidia")["id"], org["users"]["viewer"]["id"], "write", "2026-01-01T00:00:00+00:00"
    )
    viewer, _ = login("viewer")
    assert viewer.get("/api/organizations/nvidia").json()["can_upload"] is False
