import sqlite3
from pathlib import Path

import pytest

import app.main as main
from app.database import Database
from test_organizations import add_user, login, org, upload  # noqa: F401  (fixtures)
from test_access import server  # noqa: F401  (fixture)
from test_storage_targets import multi_target  # noqa: F401  (fixture)


def test_visibility_levels_decide_who_sees_an_upload(org):  # noqa: F811
    writer, _ = login("writer")
    repos = {}
    for visibility in ("private", "organization", "public"):
        created = writer.post(
            "/api/uploads/repositories",
            json={"slug": f"glm-{visibility}", "namespace": "Nvidia", "visibility": visibility},
        )
        assert created.status_code == 201, created.text
        repos[visibility] = created.json()["repo_id"]
        upload(writer, repos[visibility], {"config.json": b"{}"})

    reader, _ = login("reader")
    outsider, _ = login("outsider")
    admin, _ = login("admin")

    def sees(client):
        return {
            visibility
            for visibility, repo_id in repos.items()
            if client.get(f"/api/library/models/{repo_id}").status_code == 200
        }

    # Private: the organization's admins and writers. Organization: every member.
    assert sees(writer) == {"private", "organization", "public"}
    assert sees(admin) == {"private", "organization", "public"}
    assert sees(reader) == {"organization", "public"}
    assert sees(outsider) == {"public"}
    # Public is also served to anonymous Hugging Face clients.
    assert main.database.get_public_local_model(repos["public"]) is not None
    assert main.database.get_public_local_model(repos["organization"]) is None

    # Organization visibility needs an organization.
    personal = writer.post(
        "/api/uploads/repositories", json={"slug": "mine", "visibility": "organization"}
    )
    assert personal.status_code == 409
    assert "organization repositories" in personal.json()["detail"]

    # The Uploads page lists what the account may write to, never other people's public repos.
    member, _ = login("member")
    member.post("/api/uploads/repositories", json={"slug": "own", "visibility": "public"})
    listed = [item["repo_id"] for item in writer.get("/api/uploads/repositories").json()["items"]]
    assert sorted(listed) == sorted(repos.values())
    assert "member/own" in [item["repo_id"] for item in member.get("/api/uploads/repositories").json()["items"]]
    assert "member/own" not in listed


def test_old_visibility_values_are_upgraded(tmp_path: Path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE organizations (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE owned_repositories (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                repo_id TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'private'
                    CHECK (visibility IN ('private', 'shared')),
                status TEXT NOT NULL DEFAULT 'uploading'
                    CHECK (status IN ('uploading', 'ready')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                organization_id TEXT REFERENCES organizations(id) ON DELETE RESTRICT
            );
            INSERT INTO users VALUES ('u1', 'ada', 'Ada', 'x', 'member', 't', 't');
            INSERT INTO organizations VALUES ('o1', 'Nvidia', 'NVIDIA', '', 't', 't');
            INSERT INTO owned_repositories VALUES
                ('r1', 'u1', 'ada/mine', '', 'private', 'ready', 't', 't', NULL),
                ('r2', 'u1', 'ada/open', '', 'shared', 'ready', 't', 't', NULL),
                ('r3', 'u1', 'Nvidia/team', '', 'private', 'ready', 't', 't', 'o1'),
                ('r4', 'u1', 'Nvidia/open', '', 'shared', 'ready', 't', 't', 'o1');
            """
        )
    database = Database(path)
    database.initialize()
    database.initialize()  # A second start leaves the upgraded values alone.
    assert {
        repo_id: database.get_owned_repository(repo_id)["visibility"]
        for repo_id in ("ada/mine", "ada/open", "Nvidia/team", "Nvidia/open")
    } == {
        "ada/mine": "private",
        "ada/open": "public",
        "Nvidia/team": "organization",
        "Nvidia/open": "public",
    }
    database.update_owned_repository("Nvidia/team", visibility="private")
    assert database.get_owned_repository("Nvidia/team")["visibility"] == "private"
    with pytest.raises(sqlite3.IntegrityError):
        database.update_owned_repository("ada/mine", visibility="shared")


def test_storage_grants_reserve_targets_for_their_owners(multi_target):  # noqa: F811
    uploads = main.uploads
    database = multi_target["database"]
    admin, member = multi_target["admin"], multi_target["member"]
    ada = main.AuthService(multi_target["settings"], database).create_user(
        "ada", "Ada", "yet another passphrase", "member"
    )

    def ids(user, namespace=None):
        return [item["id"] for item in uploads.storage_choices(user, namespace)]

    # With no grants every target is open.
    assert ids(member, "member") == ["local", "bucket-a", "bucket-b"]

    # bucket-a is dedicated to the member; bucket-b to an organization ada writes to.
    database.create_organization(
        {"id": "o1", "name": "Nvidia", "display_name": "NVIDIA", "description": "",
         "created_at": "t", "updated_at": "t"}
    )
    database.set_organization_member("o1", ada["id"], "write", "t")
    database.set_storage_grants("bucket-a", [member["id"]], [], "t")
    database.set_storage_grants("bucket-b", [], ["o1"], "t")

    assert ids(member, "member") == ["local", "bucket-a"]
    assert ids(ada, "ada") == ["local"]
    assert ids(ada, "Nvidia") == ["local", "bucket-b"]
    # Downloads have no namespace: any grant to the user or their organizations counts.
    assert ids(ada) == ["local", "bucket-b"]
    # Admins may use every target.
    assert ids(admin, "admin") == ["local", "bucket-a", "bucket-b"]

    # A dedicated target is the default; an ungranted one is refused.
    assert uploads.choose_storage(member, None, "member").id == "bucket-a"
    assert uploads.choose_storage(ada, None, "Nvidia").id == "bucket-b"
    assert uploads.choose_storage(ada, None, "ada").id == "local"
    # Downloads keep the server default even when a reserved target is allowed.
    assert uploads.choose_storage(ada, None).id == "bucket-b"
    assert uploads.choose_storage(member, None).id == "local"
    with pytest.raises(PermissionError):
        uploads.create_repository(ada, "sneaky", "", "private", "bucket-a", "ada")
    with pytest.raises(PermissionError):
        uploads.choose_storage(ada, "bucket-a")
    created = uploads.create_repository(ada, "team", "", "organization", None, "Nvidia")
    assert database.get_owned_repository(created["repo_id"])["repo_id"] == "Nvidia/team"
    manifest = (multi_target["settings"].model_storage / "Nvidia" / "team" / ".hugginghack.json").read_text()
    assert '"storage_target": "bucket-b"' in manifest

    grants = database.storage_grants()
    assert grants["bucket-a"] == [
        {"kind": "user", "id": member["id"], "name": "member", "display_name": "Member"}
    ]
    assert grants["bucket-b"][0]["name"] == "Nvidia"
    database.set_storage_grants("bucket-a", [], [], "t")
    assert "bucket-a" not in database.storage_grants()
    assert ids(ada, "ada") == ["local", "bucket-a"]


def test_admins_grant_storage_over_the_api(org):  # noqa: F811
    admin, _ = login("admin")
    writer, _ = login("writer")
    reader, _ = login("reader")
    assert writer.put("/api/storage/targets/local/grants", json={"users": ["writer"]}).status_code == 403
    assert admin.put("/api/storage/targets/local/grants", json={"users": ["nobody"]}).status_code == 400
    assert admin.put("/api/storage/targets/elsewhere/grants", json={}).status_code == 404
    granted = admin.put(
        "/api/storage/targets/local/grants", json={"users": ["writer"], "organizations": ["nvidia"]}
    )
    assert granted.status_code == 200, granted.text
    assert [(item["kind"], item["name"]) for item in granted.json()["grants"]] == [
        ("organization", "Nvidia"),
        ("user", "writer"),
    ]
    targets = admin.get("/api/storage/targets").json()["targets"]
    assert [item["name"] for item in targets[0]["grants"]] == ["Nvidia", "writer"]

    options = writer.get("/api/storage/options", params={"namespace": "writer"}).json()
    assert options["default"] == "local"
    assert options["items"][0]["dedicated"] is True
    assert options["items"][0]["free_bytes"] > 0
    # The reader has no grant of their own and cannot create in the organization.
    assert reader.get("/api/storage/options", params={"namespace": "reader"}).json()["items"] == []
    assert reader.get("/api/storage/options", params={"namespace": "Nvidia"}).status_code == 403
    refused = reader.post("/api/uploads/repositories", json={"slug": "blocked"})
    assert refused.status_code == 403
    assert "No storage location" in refused.json()["detail"]
    assert admin.put("/api/storage/targets/local/grants", json={}).json()["grants"] == []
    assert reader.post("/api/uploads/repositories", json={"slug": "open-again"}).status_code == 201
