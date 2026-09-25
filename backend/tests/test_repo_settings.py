import json
import os

import app.main as main
from test_access import server  # noqa: F401  (fixture)
from test_organizations import login, org, upload  # noqa: F401  (fixtures)

# History tables keep the name a job ran under; everything else follows a rename.
HISTORY_TABLES = {"downloads", "runtime_jobs"}


def tables_mentioning(repo_id: str) -> set[str]:
    database = main.database
    with database.connect() as connection:
        tables = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        ]
        return {
            table
            for table in tables
            if "repo_id" in database._column_names(connection, table)
            and connection.execute(
                f"SELECT 1 FROM {table} WHERE repo_id = ?", (repo_id,)
            ).fetchone()
        }


def rename(client, repo_id, namespace, name, confirmation=None):
    return client.post(
        "/api/repos/rename",
        params={"repo_id": repo_id},
        json={"namespace": namespace, "name": name, "confirmation": confirmation or repo_id},
    )


def add_library_model(repo_id: str) -> None:
    folder = main.settings.model_storage / repo_id
    folder.mkdir(parents=True)
    (folder / "config.json").write_text('{"torch_dtype": "bfloat16"}', encoding="utf-8")
    main.refresh_model_index()


def test_renaming_and_transferring_keeps_everything(org):  # noqa: F811
    writer, _ = login("writer")
    created = writer.post("/api/uploads/repositories", json={"slug": "tiny", "visibility": "private"})
    assert created.status_code == 201, created.text
    upload(writer, "writer/tiny", {"config.json": b"{}", "README.md": b"# Tiny"})
    assert writer.post("/api/saved-models", json={"repo_id": "writer/tiny"}).status_code == 200
    assert writer.put(
        "/api/library/hardware", params={"repo_id": "writer/tiny"}, json={"hardware": ["l40"]}
    ).status_code == 200
    details = writer.get("/api/library/models/writer/tiny").json()
    assert details["can_manage"] is True
    assert writer.post(
        "/api/library/configs", params={"repo_id": "writer/tiny"},
        json={"message": "Baseline", "files": [{"path": "serve.sh", "content": "vllm serve"}]},
    ).status_code == 201

    assert rename(writer, "writer/tiny", "writer", "tiny-v2", "wrong").status_code == 409
    assert rename(writer, "writer/tiny", "writer", "has space").status_code == 409
    renamed = rename(writer, "writer/tiny", "writer", "tiny-v2")
    assert renamed.status_code == 200, renamed.text
    assert renamed.json() == {"repo_id": "writer/tiny-v2", "visibility": "private"}
    assert writer.get("/api/library/models/writer/tiny").status_code == 404
    moved = writer.get("/api/library/models/writer/tiny-v2").json()
    assert moved["commit_count"] == 1 and moved["saved"] is True and moved["hardware"] == ["l40"]
    assert moved["config_count"] == 1
    assert (main.settings.model_storage / "writer" / "tiny-v2" / "README.md").is_file()
    assert not (main.settings.model_storage / "writer" / "tiny").exists()
    assert tables_mentioning("writer/tiny") == set()
    manifest = json.loads((main.settings.model_storage / "writer/tiny-v2/.hugginghack.json").read_text())
    assert manifest["repo_id"] == "writer/tiny-v2"
    # A rescan finds it under the new name only.
    main.refresh_model_index()
    assert main.database.get_local_model("writer/tiny") is None
    assert main.database.get_local_model("writer/tiny-v2") is not None

    # Only capitalization changes.
    assert rename(writer, "writer/tiny-v2", "writer", "Tiny-V2").status_code == 200
    assert "Tiny-V2" in os.listdir(main.settings.model_storage / "writer")
    assert writer.get("/api/library/models/writer/Tiny-V2").status_code == 200

    # Transfer into the organization, then back; organization visibility cannot follow.
    reader, _ = login("reader")
    assert rename(writer, "writer/Tiny-V2", "nvidia", "Tiny-V2").json()["repo_id"] == "Nvidia/Tiny-V2"
    owned = main.database.get_owned_repository("Nvidia/Tiny-V2")
    assert owned["organization_id"] == main.database.get_organization("Nvidia")["id"]
    assert reader.get("/api/library/models/Nvidia/Tiny-V2").status_code == 404  # still private
    assert not (main.settings.model_storage / "writer").exists()
    # Writers of the organization are not its admins, so they no longer manage it.
    assert writer.get("/api/library/models/Nvidia/Tiny-V2").json()["can_manage"] is False
    assert rename(writer, "Nvidia/Tiny-V2", "writer", "back").status_code == 404
    admin, _ = login("admin")
    assert admin.patch(
        "/api/uploads/repositories", params={"repo_id": "Nvidia/Tiny-V2"},
        json={"description": "", "visibility": "organization"},
    ).status_code == 200
    assert reader.get("/api/library/models/Nvidia/Tiny-V2").status_code == 200
    back = rename(admin, "Nvidia/Tiny-V2", "writer", "Tiny-V2")
    assert back.status_code == 200, back.text
    assert back.json() == {"repo_id": "writer/Tiny-V2", "visibility": "private"}
    assert main.database.get_owned_repository("writer/Tiny-V2")["owner_id"] == main.database.get_user_by_username("writer")["id"]

    # Readers and outsiders cannot move repositories; names cannot collide.
    writer.post("/api/uploads/repositories", json={"slug": "other"})
    upload(writer, "writer/other", {"config.json": b"{}"})
    assert rename(writer, "writer/other", "writer", "Tiny-V2").status_code == 409
    assert rename(writer, "writer/other", "outsider", "mine").status_code == 403
    outsider, _ = login("outsider")
    assert rename(outsider, "writer/other", "outsider", "mine").status_code == 404


def test_administrators_rename_assign_and_delete_downloaded_models(org):  # noqa: F811
    add_library_model("labs/base")
    admin, _ = login("admin")
    writer, _ = login("writer")
    assert writer.get("/api/library/models/labs/base").json()["can_manage"] is False
    assert rename(writer, "labs/base", "labs", "base-2").status_code == 404

    # A free namespace keeps a downloaded model unowned and public.
    assert rename(admin, "labs/base", "labs", "base-2").json() == {"repo_id": "labs/base-2", "visibility": "public"}
    assert main.database.get_owned_repository("labs/base-2") is None
    assert writer.get("/api/library/models/labs/base-2").status_code == 200

    # Assigning an owner registers it as that owner's upload, still public.
    assigned = rename(admin, "labs/base-2", "Nvidia", "base")
    assert assigned.status_code == 200, assigned.text
    owned = main.database.get_owned_repository("Nvidia/base")
    assert owned["visibility"] == "public" and owned["status"] == "ready"
    manifest = json.loads((main.settings.model_storage / "Nvidia/base/.hugginghack.json").read_text())
    assert manifest["source"] == "user-upload" and manifest["origin"] == "library"
    assert not (main.settings.model_storage / "labs").exists()
    main.refresh_model_index()
    assert main.database.get_local_model("Nvidia/base") is not None
    # Once owned, it must stay with a user or an organization.
    assert rename(admin, "Nvidia/base", "labs", "base").status_code == 403

    # An owner folder that differs only in case is moved, not duplicated.
    add_library_model("nvidia-labs/x")
    assert rename(admin, "nvidia-labs/x", "Nvidia-Labs", "x").json()["repo_id"] == "Nvidia-Labs/x"
    assert "Nvidia-Labs" in os.listdir(main.settings.model_storage)
    assert "nvidia-labs" not in os.listdir(main.settings.model_storage)

    # Deleting: owners through their role, anything else only by administrators.
    member, _ = login("member")
    delete = lambda client, repo_id, confirmation=None: client.request(  # noqa: E731
        "DELETE", "/api/repos", params={"repo_id": repo_id}, json={"confirmation": confirmation or repo_id}
    )
    assert delete(member, "Nvidia-Labs/x").status_code == 404
    assert delete(admin, "Nvidia-Labs/x", "wrong").status_code == 409
    assert delete(admin, "Nvidia-Labs/x").status_code == 200
    assert not (main.settings.model_storage / "Nvidia-Labs").exists()
    assert main.database.get_local_model("Nvidia-Labs/x") is None
    assert admin.post(
        "/api/library/configs", params={"repo_id": "Nvidia/base"},
        json={"message": "Baseline", "files": [{"path": "serve.sh", "content": "vllm serve"}]},
    ).status_code == 201
    assert delete(admin, "Nvidia/base").status_code == 200
    assert tables_mentioning("Nvidia/base") == set()
