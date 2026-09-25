"""What each caller may learn: storage locations, private repositories, and server
details stay with those allowed to see them, and Viewers' roles only read."""

import dataclasses

import httpx
import pytest
from fastapi.testclient import TestClient
from huggingface_hub.errors import RepositoryNotFoundError

import app.main as main
from app.auth import AuthService
from app.config import Settings, _optional_boolean
from test_access import server  # noqa: F401  (fixture)
from test_organizations import login, org, upload  # noqa: F401  (fixtures)
from test_storage_targets import multi_target, publish  # noqa: F401  (fixture)

LOCATION = {"relative_path", "local_path", "remote_uri", "storage_target", "storage_target_name"}


def test_a_scan_answers_only_with_models_the_caller_may_see(org):  # noqa: F811
    outsider, _ = login("outsider")
    result = outsider.post("/api/local-models/scan")
    assert result.status_code == 200, result.text
    body = result.json()
    # No conflicts, target errors, or per-target counts for a member.
    assert set(body) == {"count", "models", "scanned_at"}
    ids = {model["repo_id"] for model in body["models"]}
    assert "acme/open" in ids and "member/secret" not in ids
    assert body["count"] == len(ids)
    assert all(not LOCATION & set(model) for model in body["models"])

    admin, _ = login("admin")
    full = admin.post("/api/local-models/scan").json()
    assert {"conflicts", "remote_error", "local_count", "remote_count"} <= set(full)
    assert "member/secret" in {model["repo_id"] for model in full["models"]}
    assert all("storage_target" in model for model in full["models"])


def test_scan_errors_never_carry_target_credentials(multi_target, monkeypatch):  # noqa: F811
    remote = multi_target["registry"].get("bucket-a")
    monkeypatch.setattr(
        remote, "target", dataclasses.replace(remote.target, access_key_id="AKIALEAK", secret_access_key="s3cr3t-value")
    )

    def fail() -> list:
        raise RuntimeError("AccessDenied for AKIALEAK signed with s3cr3t-value")

    monkeypatch.setattr(remote, "discover_repositories", fail)
    error = main.refresh_model_index()["remote_error"]
    assert "AKIALEAK" not in error and "s3cr3t-value" not in error
    assert "[redacted]" in error


def test_storage_locations_and_the_manifest_stay_with_storage_viewers(server):  # noqa: F811
    member, _ = login("member")
    admin, _ = login("admin")

    listed = member.get("/api/local-models").json()["items"]
    assert listed and all(not LOCATION & set(item) for item in listed)
    assert all("storage_target" in item for item in admin.get("/api/local-models").json()["items"])

    details = member.get("/api/local-models/member/secret").json()
    assert not LOCATION & set(details["model"])
    paths = {file["path"] for file in details["files"]}
    assert paths == {"config.json", "model.safetensors"}  # never .hugginghack.json
    assert "storage_target" in admin.get("/api/local-models/acme/open").json()["model"]

    # The Explore list keeps on-disk or S3-only, not which target holds it.
    items = member.get("/api/library/models").json()["items"]
    assert items and all("storage_target" not in item and "storage_backend" in item for item in items)
    assert all("storage_target" in item for item in admin.get("/api/library/models").json()["items"])


def test_restoring_and_evicting_answer_without_the_bucket(multi_target, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "auth", AuthService(multi_target["settings"], multi_target["database"]))
    monkeypatch.setattr(main.moves, "database", multi_target["database"])
    publish(multi_target["clients"]["bucket-a"], "models", "acme/alpha", {"config.json": b"{}", "w.gguf": b"GGUF"})
    main.refresh_model_index()
    member, _ = login("member")
    restored = member.post("/api/local-models/acme/alpha/restore")
    assert restored.status_code == 200, restored.text
    assert not LOCATION & set(restored.json()["model"])
    assert {file["path"] for file in restored.json()["files"]} == {"config.json", "w.gguf"}
    evicted = member.delete("/api/local-models/acme/alpha/cache")
    assert evicted.status_code == 200, evicted.text
    assert not LOCATION & set(evicted.json()["model"])


def test_health_tells_strangers_only_that_the_server_is_up(server):  # noqa: F811
    anonymous = TestClient(main.app).get("/api/health")
    assert anonymous.status_code == 200
    assert set(anonymous.json()) == {"status", "app", "version"}
    assert anonymous.json()["status"] in {"ok", "degraded"}
    # A bad token is treated as nobody, never as an error.
    forged = TestClient(main.app).get("/api/health", headers={"Authorization": "Bearer hht_wrong"})
    assert forged.status_code == 200 and set(forged.json()) == {"status", "app", "version"}

    member, _ = login("member")
    seen = member.get("/api/health").json()
    # What the web UI needs for uploads and the "Use this model" dialog.
    assert {"upload_chunk_bytes", "max_upload_size_bytes", "public_url", "hub_api_enabled"} <= set(seen)
    for detail in ("storage", "object_storage", "hf_endpoint", "hf_token_configured", "database_backend"):
        assert detail not in seen

    admin, _ = login("admin")
    full = admin.get("/api/health").json()
    assert full["storage"]["path"] and full["database_backend"] == "sqlite" and "hf_endpoint" in full


def test_viewers_organization_roles_only_read(org):  # noqa: F811
    writer, _ = login("writer")
    repo_id = writer.post(
        "/api/uploads/repositories", json={"slug": "kept", "namespace": "Nvidia", "visibility": "private"}
    ).json()["repo_id"]
    upload(writer, repo_id, {"config.json": b"{}"})
    nvidia = main.database.get_organization("Nvidia")

    admin, _ = login("admin")
    for role in ("admin", "write"):
        refused = admin.put("/api/organizations/Nvidia/members/viewer", json={"role": role})
        assert refused.status_code == 400 and "can only be Read" in refused.json()["detail"]
    assert admin.put("/api/organizations/Nvidia/members/viewer", json={"role": "read"}).status_code == 200
    account = {"username": "newviewer", "password": "a long enough passphrase", "role": "viewer"}
    created = admin.post("/api/users", json={**account, "organizations": [{"organization": "Nvidia", "role": "write"}]})
    assert created.status_code == 400
    assert main.database.get_user_by_username("newviewer") is None

    # An Admin role given before that rule existed manages nothing.
    main.database.set_organization_member(nvidia["id"], org["users"]["viewer"]["id"], "admin", "2026-01-01T00:00:00+00:00")
    viewer, _ = login("viewer")
    assert viewer.get(f"/api/library/models/{repo_id}").json()["can_manage"] is False
    renamed = viewer.post(
        "/api/repos/rename", params={"repo_id": repo_id}, json={"namespace": "Nvidia", "name": "taken", "confirmation": repo_id}
    )
    assert renamed.status_code == 404
    assert viewer.request("DELETE", "/api/repos", params={"repo_id": repo_id}, json={"confirmation": repo_id}).status_code == 404
    assert main.database.get_owned_repository(repo_id) is not None
    assert viewer.get("/api/organizations/Nvidia").json()["can_manage"] is False
    assert viewer.patch("/api/organizations/Nvidia", json={"description": "mine now"}).status_code == 403
    assert viewer.put("/api/organizations/Nvidia/members/outsider", json={"role": "admin"}).status_code == 403
    assert viewer.request("DELETE", "/api/organizations/Nvidia/members/writer").status_code == 403

    # A member moved down to Viewer can no longer delete their own uploads.
    member_id = org["users"]["member"]["id"]
    assert admin.patch(f"/api/admin/users/{member_id}", json={"role": "viewer"}).status_code == 200
    demoted, _ = login("member")
    deleted = demoted.request("DELETE", "/api/repos", params={"repo_id": "member/secret"}, json={"confirmation": "member/secret"})
    assert deleted.status_code == 404
    assert main.database.get_owned_repository("member/secret") is not None


class FakeHub:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def access(self, repo_id: str, revision: str = "main") -> dict:
        self.asked.append(repo_id)
        if repo_id.endswith("/missing"):
            request = httpx.Request("GET", f"https://huggingface.co/api/models/{repo_id}")
            raise RepositoryNotFoundError("404 Client Error", response=httpx.Response(404, request=request))
        if repo_id.endswith("/offline"):
            raise OSError("Network is unreachable")
        return {"private": repo_id.endswith("/private"), "gated": repo_id.endswith("/gated")}


class FakeDownloads:
    def queue(self, repo_id: str, *_: object, **options: object) -> dict:
        return {"id": "d1", "repo_id": repo_id, "status": "queued", "target_path": f"/models/{repo_id}"}


def test_private_and_gated_hub_models_need_an_administrator(org, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, hf_downloads_enabled=True))
    hub = FakeHub()
    monkeypatch.setattr(main, "hub", hub)
    monkeypatch.setattr(main, "downloads", FakeDownloads())
    monkeypatch.setattr(main.moves, "database", main.database)
    outsider, _ = login("outsider")

    for kind in ("private", "gated"):
        refused = outsider.post("/api/downloads", json={"repo_id": f"acme/{kind}"})
        assert refused.status_code == 403 and f"is {kind} on Hugging Face" in refused.json()["detail"]
    assert outsider.post("/api/downloads", json={"repo_id": "acme/missing"}).status_code == 404
    assert outsider.post("/api/downloads", json={"repo_id": "acme/offline"}).status_code == 502
    started = outsider.post("/api/downloads", json={"repo_id": "acme/public"})
    assert started.status_code == 202, started.text
    assert "target_path" not in started.json()  # the server folder is for storage viewers

    # A private upload's name is neither confirmed nor denied to those who cannot see it.
    hidden = outsider.post("/api/downloads", json={"repo_id": "member/secret"})
    assert hidden.status_code == 409 and "account-owned" not in hidden.json()["detail"]
    owner, _ = login("member")
    assert "account-owned" in owner.post("/api/downloads", json={"repo_id": "member/secret"}).json()["detail"]

    admin, _ = login("admin")
    hub.asked.clear()
    allowed = admin.post("/api/downloads", json={"repo_id": "acme/private"})
    assert allowed.status_code == 202 and allowed.json()["target_path"] == "/models/acme/private"
    assert hub.asked == []  # administrators are not held up by the lookup


def test_organization_pages_show_only_what_the_viewer_may_see(org):  # noqa: F811
    writer, _ = login("writer")
    for slug, visibility in (("hidden", "private"), ("shared", "organization")):
        created = writer.post(
            "/api/uploads/repositories", json={"slug": slug, "namespace": "Nvidia", "visibility": visibility}
        )
        assert created.status_code == 201, created.text

    def count(username: str) -> int:
        client, _ = login(username)
        [nvidia] = [item for item in client.get("/api/organizations").json()["items"] if item["name"] == "Nvidia"]
        return nvidia["repository_count"]

    assert (count("writer"), count("reader"), count("outsider"), count("admin")) == (2, 1, 0, 2)

    outsider, _ = login("outsider")
    members = outsider.get("/api/organizations/Nvidia").json()["members"]
    assert members and all("server_role" not in item and "disabled" not in item for item in members)
    admin, _ = login("admin")
    assert all("server_role" in item for item in admin.get("/api/organizations/Nvidia").json()["members"])


def test_a_rename_moves_saves_only_for_those_who_still_see_it(org):  # noqa: F811
    writer, _ = login("writer")
    repo_id = writer.post(
        "/api/uploads/repositories", json={"slug": "tiny", "namespace": "Nvidia", "visibility": "organization"}
    ).json()["repo_id"]
    upload(writer, repo_id, {"config.json": b"{}"})
    reader, _ = login("reader")
    for client in (writer, reader):
        assert client.post("/api/saved-models", json={"repo_id": repo_id}).status_code == 200

    # Moving it to the writer makes it private to them; the reader loses sight of it.
    admin, _ = login("admin")
    moved = admin.post(
        "/api/repos/rename", params={"repo_id": repo_id}, json={"namespace": "writer", "name": "renamed", "confirmation": repo_id}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json() == {"repo_id": "writer/renamed", "visibility": "private"}

    def saved(client: TestClient) -> list[str]:
        return [item["repo_id"] for item in client.get("/api/saved-models").json()["items"]]

    assert saved(writer) == ["writer/renamed"]
    assert saved(reader) == [repo_id]  # the old name, as after a deletion


def test_cookies_are_secure_over_https_unless_set_otherwise(server, monkeypatch):  # noqa: F811
    def cookie(client: TestClient, **headers: str) -> str:
        response = client.post(
            "/api/auth/login", json={"username": "member", "password": "another secure phrase"}, headers=headers
        )
        assert response.status_code == 200, response.text
        return response.headers["set-cookie"].lower()

    assert main.settings.secure_cookies is None  # auto by default
    assert "secure" not in cookie(TestClient(main.app))  # plain HTTP keeps working
    assert "secure" in cookie(TestClient(main.app, base_url="https://testserver"))
    assert "secure" in cookie(TestClient(main.app), **{"X-Forwarded-Proto": "https"})
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, secure_cookies=False))
    assert "secure" not in cookie(TestClient(main.app, base_url="https://testserver"))
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, secure_cookies=True))
    assert "secure" in cookie(TestClient(main.app))


@pytest.mark.parametrize(("value", "expected"), [(None, None), ("auto", None), ("true", True), ("off", False)])
def test_secure_cookies_setting_reads_auto_true_or_false(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("SECURE_COOKIES", raising=False)
    else:
        monkeypatch.setenv("SECURE_COOKIES", value)
    assert _optional_boolean("SECURE_COOKIES") is expected


def test_cross_origin_calls_need_an_explicit_origin():
    preflight = TestClient(main.app).options(
        "/api/auth/status",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in preflight.headers
    assert Settings(cors_origins=" https://ui.example/, http://localhost:5173 ").cors_origin_list == [
        "https://ui.example",
        "http://localhost:5173",
    ]
