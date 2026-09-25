import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.git_mirror import GitMirrors
from app.system import LocalSystemStore, create_system_store, migrate_local_data
from test_storage_targets import multi_target  # noqa: F401  (fixture)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PASSWORDS = {"admin": "correct horse battery", "member": "another secure phrase"}


@pytest.fixture()
def in_s3(multi_target, monkeypatch):  # noqa: F811
    settings = dataclasses.replace(multi_target["settings"], system_storage_target="bucket-a")
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "auth", main.AuthService(settings, multi_target["database"]))
    monkeypatch.setattr(main, "git_mirrors", GitMirrors(main.hub_repositories, main.system_store))
    main.refresh_model_index()
    return {**multi_target, "settings": settings, "bucket": multi_target["clients"]["bucket-a"]}


def signed_in(username: str) -> TestClient:
    client = TestClient(main.app)
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORDS[username]})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


def test_pictures_live_in_the_system_folder_of_the_bucket(in_s3):
    member = signed_in("member")
    saved = member.put("/api/account/avatar", content=PNG, headers={"Content-Type": "image/png"})
    assert saved.status_code == 200, saved.text
    key = f"models/_system/avatars/users/{in_s3['member']['id']}"
    assert in_s3["bucket"].objects[key] == PNG
    assert member.get(saved.json()["avatar"]).content == PNG
    assert not (in_s3["settings"].data_dir / "system").exists()  # nothing on local disk
    # Nothing in the system folder is ever taken for a model.
    main.refresh_model_index()
    assert not [model for model in main.database.list_local_models() if model["repo_id"].startswith("_system")]
    assert member.delete("/api/account/avatar").status_code == 200
    assert key not in in_s3["bucket"].objects


def test_an_unreachable_bucket_is_an_error_not_a_broken_picture(in_s3, monkeypatch):
    member = signed_in("member")
    assert member.put("/api/account/avatar", content=PNG, headers={"Content-Type": "image/png"}).status_code == 200

    def down(**_):
        raise ConnectionError("bucket unreachable")

    monkeypatch.setattr(in_s3["bucket"], "put_object", down)
    monkeypatch.setattr(in_s3["bucket"], "get_object", down)
    failed = member.put("/api/account/avatar", content=PNG, headers={"Content-Type": "image/png"})
    assert failed.status_code == 503 and "Try again" in failed.json()["detail"]
    assert member.get("/api/avatars/member").status_code == 503
    system = signed_in("admin").get("/api/storage/targets").json()["system"]
    assert system["ok"] is False and system["remote"] is True and system["location"] == "s3://bucket-a/models/_system/"


def test_older_local_files_move_into_the_bucket_only_once_copied(in_s3, monkeypatch):
    data = in_s3["settings"].data_dir
    (data / "avatars" / "users").mkdir(parents=True)
    (data / "avatars" / "users" / "u1").write_bytes(PNG)
    (data / "system" / "avatars" / "organizations").mkdir(parents=True)
    (data / "system" / "avatars" / "organizations" / "o1").write_bytes(PNG)
    store = main.system_store()

    def broken(**_):
        raise ConnectionError("down")

    with monkeypatch.context() as patch:
        patch.setattr(in_s3["bucket"], "put_object", broken)
        assert migrate_local_data(in_s3["settings"], store) == {"copied": 0, "kept": 2}
    assert (data / "avatars" / "users" / "u1").exists()  # kept for the next start

    assert migrate_local_data(in_s3["settings"], store) == {"copied": 2, "kept": 0}
    assert in_s3["bucket"].objects["models/_system/avatars/users/u1"] == PNG
    assert in_s3["bucket"].objects["models/_system/avatars/organizations/o1"] == PNG
    assert not (data / "avatars" / "users" / "u1").exists()


def test_git_history_survives_losing_the_local_disk(in_s3):
    mirrors = main.git_mirrors
    first = mirrors.ensure("acme/on-disk")
    stored = [key for key in in_s3["bucket"].objects if key.startswith("models/_system/git-mirrors/acme/on-disk/")]
    assert any(key.endswith("hugginghack-mirror.json") for key in stored)

    # The server's disk is replaced: the mirror comes back from the bucket, same commit.
    import shutil

    shutil.rmtree(in_s3["settings"].data_dir / "git-mirrors")
    assert mirrors.ensure("acme/on-disk").commit == first.commit

    # A later change still builds on it, so `git pull` keeps working.
    (in_s3["settings"].model_storage / "acme" / "on-disk" / "notes.txt").write_text("new", encoding="utf-8")
    main.refresh_model_index()
    second = mirrors.ensure("acme/on-disk")
    commit = mirrors.read_file("acme/on-disk", f"objects/{second.commit[:2]}/{second.commit[2:]}")
    import zlib

    assert f"parent {first.commit}".encode() in zlib.decompress(commit)
    mirrors.forget("acme/on-disk")
    assert not [key for key in in_s3["bucket"].objects if "/git-mirrors/acme/on-disk/" in key]


def test_the_system_target_must_exist(in_s3):
    with pytest.raises(ValueError):
        create_system_store(dataclasses.replace(in_s3["settings"], system_storage_target="nowhere"), in_s3["registry"])
    local = create_system_store(dataclasses.replace(in_s3["settings"], system_storage_target="local"), in_s3["registry"])
    assert isinstance(local, LocalSystemStore)
    custom = create_system_store(
        dataclasses.replace(in_s3["settings"], system_storage_prefix="site/data"), in_s3["registry"]
    )
    assert custom.location() == "s3://bucket-a/site/data/"
    assert json.dumps(main.system_overview())  # the Storage page can always describe it
