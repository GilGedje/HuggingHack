"""Safeguards found in review: repository takeovers, administrator visibility,
retryable S3 commits, sign-in throttling, headers, cross-site writes, and counts."""

import base64
import dataclasses
import json

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import _TrustedHosts

import app.main as main
from app.auth import AuthService
from test_access import PASSWORDS, server  # noqa: F401  (fixture)
from test_history import library  # noqa: F401  (fixture)
from test_organizations import add_user, login, org, upload  # noqa: F401  (fixtures)
from test_storage_targets import multi_target, publish  # noqa: F401  (fixture)


def bucket_failure(*args, **kwargs):
    raise ClientError(
        {"Error": {"Code": "SlowDown", "Message": "endpoint s3.internal.example rejected AKIALEAK"}},
        "PutObject",
    )


# 1. An upload never takes over a model already in the library or the bucket.


def test_new_uploads_cannot_take_over_an_s3_only_model_or_bucket_objects(multi_target, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "auth", AuthService(multi_target["settings"], multi_target["database"]))
    clients = multi_target["clients"]
    publish(clients["bucket-a"], "models", "member/indexed", {"config.json": b"{}"})
    main.refresh_model_index()
    assert main.database.get_local_model("member/indexed") is not None
    # Objects without a manifest are not listed, but still belong to someone.
    clients["bucket-b"].objects["models/member/leftover/w.safetensors"] = b"W"

    member, _ = login("member")
    for slug in ("indexed", "leftover"):
        refused = member.post("/api/uploads/repositories", json={"slug": slug, "visibility": "private"})
        assert refused.status_code == 409 and refused.json()["detail"] == "That repository already exists."
        assert main.database.get_owned_repository(f"member/{slug}") is None
    assert "models/member/leftover/w.safetensors" in clients["bucket-b"].objects

    monkeypatch.setattr(multi_target["registry"].get("bucket-b"), "has_objects", bucket_failure)
    unreachable = member.post("/api/uploads/repositories", json={"slug": "fresh", "visibility": "private"})
    assert unreachable.status_code == 502 and "AKIALEAK" not in unreachable.text


# 2. Server administrators see every repository, private ones included.


def test_admins_see_and_pull_private_repositories_and_others_still_cannot(server):  # noqa: F811
    admin, _ = login("admin")
    assert admin.get("/api/library/models/member/secret").status_code == 200
    assert "member/secret" in {item["id"] for item in admin.get("/api/library/models").json()["items"]}
    viewer, _ = login("viewer")
    assert viewer.get("/api/library/models/member/secret").status_code == 404

    tokens = {}
    for role, client in (("admin", admin), ("viewer", viewer)):
        tokens[role] = client.post("/api/account/tokens", json={"name": "pull", "scope": "read"}).json()["token"]
    anonymous = TestClient(main.app)
    pulled = anonymous.get(
        "/member/secret/resolve/main/config.json", headers={"Authorization": f"Bearer {tokens['admin']}"}
    )
    assert pulled.status_code == 200 and pulled.content == b'{"a": 1}'
    info = anonymous.get("/api/models/member/secret", headers={"Authorization": f"Bearer {tokens['admin']}"})
    assert info.status_code == 200 and info.json()["private"] is True
    refused = anonymous.get(
        "/member/secret/resolve/main/config.json", headers={"Authorization": f"Bearer {tokens['viewer']}"}
    )
    assert refused.status_code == 404
    assert anonymous.get("/member/secret/resolve/main/config.json").status_code == 401
    # git sends the token as a Basic password.
    for role, expected in (("admin", 200), ("viewer", 404)):
        basic = base64.b64encode(f"git:{tokens[role]}".encode()).decode()
        refs = anonymous.get("/member/secret.git/info/refs", headers={"Authorization": f"Basic {basic}"})
        assert refs.status_code == expected, role

    # A disabled administrator's token stops working like anyone else's.
    main.database.update_user(server["users"]["admin"]["id"], disabled=1)
    blocked = anonymous.get(
        "/member/secret/resolve/main/config.json", headers={"Authorization": f"Bearer {tokens['admin']}"}
    )
    assert blocked.status_code == 401


# 3. A failed S3 write leaves the change session intact, to commit again or cancel.


def stage(uploads, session: dict, user: dict, files: dict[str, bytes]) -> None:
    for path, payload in files.items():
        uploads.change_chunk(session["id"], user, path, 0, len(payload), payload)


def test_a_failed_sync_puts_every_file_back_and_the_same_change_commits_later(library, monkeypatch):  # noqa: F811
    admin, uploads, bucket = library["admin"], library["uploads"], library["bucket"]
    main.refresh_model_index()
    main.storages.get("lake").restore_repository("acme/lake-model")
    main.refresh_model_index()
    root = library["settings"].model_storage / "acme" / "lake-model"
    manifest_before = (root / ".hugginghack.json").read_bytes()
    objects_before = dict(bucket.objects)

    session = uploads.start_change("acme/lake-model", admin)
    stage(uploads, session, admin, {"config.json": b'{"a": 2}\n', "docs/card.md": b"# Card\n"})
    working_upload = bucket.upload_file
    bucket.upload_file = bucket_failure
    main.app.dependency_overrides[main.require_user] = lambda: admin
    main.app.dependency_overrides[main.require_write_user] = lambda: admin
    try:
        client = TestClient(main.app)
        failed = client.post(
            f"/api/repos/changes/{session['id']}/commit", json={"message": "Edit", "deletions": ["w.bin"]}
        )
        assert failed.status_code == 502, failed.text
        assert "AKIALEAK" not in failed.text and "s3.internal" not in failed.text
        # The repository is exactly as it was, locally and in the bucket.
        assert (root / "config.json").read_bytes() == b'{"a": 1}\n'
        assert (root / "w.bin").read_bytes() == b"12345"
        assert not (root / "docs").exists()
        assert (root / ".hugginghack.json").read_bytes() == manifest_before
        assert bucket.objects == objects_before
        assert main.hub_repositories.snapshot("acme/lake-model").entry("docs/card.md") is None
        assert uploads.change_file_status(session["id"], admin, "docs/card.md")["complete"] is True

        bucket.upload_file = working_upload
        retried = client.post(
            f"/api/repos/changes/{session['id']}/commit", json={"message": "Edit", "deletions": ["w.bin"]}
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["commit"]["summary"] == {"added": 1, "modified": 1, "deleted": 1}
    finally:
        main.app.dependency_overrides.clear()
    assert (root / "config.json").read_bytes() == b'{"a": 2}\n' and not (root / "w.bin").exists()
    assert bucket.objects["models/acme/lake-model/docs/card.md"] == b"# Card\n"
    assert "models/acme/lake-model/w.bin" not in bucket.objects
    assert uploads._busy("acme/lake-model") is None
    assert not any((library["settings"].model_storage / ".hugginghack-staging").iterdir())


def test_a_failed_bucket_only_change_keeps_its_manifest_and_can_be_cancelled(library, monkeypatch):  # noqa: F811
    admin, uploads, bucket = library["admin"], library["uploads"], library["bucket"]
    main.refresh_model_index()
    manifest_key = "models/acme/lake-model/.hugginghack.json"

    session = uploads.start_change("acme/lake-model", admin)
    stage(uploads, session, admin, {"README.md": b"# Lake\n"})
    monkeypatch.setattr(bucket, "upload_file", bucket_failure)
    with pytest.raises(main.StorageUnavailableError):
        uploads.commit_change(session["id"], admin, "Card", deletions=["w.bin"])
    assert manifest_key in bucket.objects and "models/acme/lake-model/w.bin" in bucket.objects
    assert uploads._busy("acme/lake-model") is not None  # still open, to retry or cancel
    uploads.abort_change(session["id"], admin)
    assert uploads._busy("acme/lake-model") is None
    main.refresh_model_index()
    assert main.database.get_local_model("acme/lake-model") is not None


def test_an_unreadable_manifest_fails_the_commit_instead_of_being_replaced(library, monkeypatch):  # noqa: F811
    admin, uploads, bucket = library["admin"], library["uploads"], library["bucket"]
    main.refresh_model_index()
    manifest_key = "models/acme/lake-model/.hugginghack.json"
    bucket.objects[manifest_key] = json.dumps(
        {"status": "complete", "repo_id": "acme/lake-model", "license": "apache-2.0", "source_url": "kept"}
    ).encode()

    session = uploads.start_change("acme/lake-model", admin)
    stage(uploads, session, admin, {"README.md": b"# Lake\n"})
    working_read = bucket.get_object
    bucket.get_object = bucket_failure
    with pytest.raises(main.StorageUnavailableError) as failure:
        uploads.commit_change(session["id"], admin, "Card")
    assert "AKIALEAK" not in str(failure.value)
    bucket.get_object = working_read
    assert json.loads(bucket.objects[manifest_key])["source_url"] == "kept"
    assert "models/acme/lake-model/README.md" not in bucket.objects

    uploads.commit_change(session["id"], admin, "Card")
    manifest = json.loads(bucket.objects[manifest_key])
    assert manifest["source_url"] == "kept" and manifest["license"] == "apache-2.0"
    assert bucket.objects["models/acme/lake-model/README.md"] == b"# Lake\n"


def test_a_missing_manifest_is_rebuilt_with_the_uploads_owner(library):  # noqa: F811
    owner, uploads, bucket = library["owner"], library["uploads"], library["bucket"]
    repository = uploads.create_repository(owner, "kept", "", "private", storage_target="lake")
    repo_id = repository["repo_id"]
    uploads.upload_chunk(repo_id, owner["id"], "config.json", 0, 2, b"{}")
    uploads.finalize(repo_id, owner["id"])
    main.storages.get("lake").evict_repository_cache(repo_id)
    main.refresh_model_index()
    assert main.database.get_local_model(repo_id)["cached"] is False
    del bucket.objects[f"models/{repo_id}/.hugginghack.json"]

    session = uploads.start_change(repo_id, owner)
    stage(uploads, session, owner, {"README.md": b"# Kept\n"})
    uploads.commit_change(session["id"], owner, "Card")
    manifest = json.loads(bucket.objects[f"models/{repo_id}/.hugginghack.json"])
    assert manifest["source"] == "user-upload" and manifest["owner_id"] == owner["id"]
    main.refresh_model_index()
    assert main.database.get_visible_local_model(owner["id"], repo_id) is not None
    assert main.database.get_visible_local_model(library["member"]["id"], repo_id) is None
    assert main.database.get_owned_repository(repo_id)["visibility"] == "private"


def test_a_sync_never_unlists_the_repository_first(library):  # noqa: F811
    """The manifest is written after the files and never deleted, so a failed
    upload cannot make a repository disappear from the next scan."""
    main.refresh_model_index()
    storage = main.storages.get("lake")
    root = storage.restore_repository("acme/lake-model")
    calls: list[tuple[str, str]] = []
    bucket = library["bucket"]
    original_upload, original_delete = bucket.upload_file, bucket.delete_objects

    def upload_file(filename, bucket_name, key, **kwargs):
        calls.append(("upload", key.rsplit("/", 1)[-1]))
        return original_upload(filename, bucket_name, key, **kwargs)

    def delete_objects(*, Bucket, Delete):
        calls.extend(("delete", item["Key"].rsplit("/", 1)[-1]) for item in Delete["Objects"])
        return original_delete(Bucket=Bucket, Delete=Delete)

    bucket.upload_file, bucket.delete_objects = upload_file, delete_objects
    (root / "w.bin").unlink()
    (root / "new.txt").write_text("new", encoding="utf-8")
    storage.sync_repository("acme/lake-model", root, {"new.txt", "w.bin"})
    assert calls == [("upload", "new.txt"), ("upload", ".hugginghack.json"), ("delete", "w.bin")]


# 4. Sign-in throttling: per account and address, per address across accounts, bounded.


def test_password_spraying_from_one_address_is_throttled_without_locking_accounts(server, monkeypatch):  # noqa: F811
    service = server["auth"]
    for index in range(30):
        assert service.authenticate(f"nobody{index}", "wrong password here", "10.0.0.9") is None
    with pytest.raises(ValueError):
        service.authenticate("member", PASSWORDS["member"], "10.0.0.9")
    # The same account still signs in from elsewhere: nothing locks it by name.
    assert service.authenticate("member", PASSWORDS["member"], "10.0.0.10")["username"] == "member"
    for _ in range(8):
        service.authenticate("viewer", "wrong password here", "10.0.0.11")
    with pytest.raises(ValueError):
        service.authenticate("viewer", PASSWORDS["viewer"], "10.0.0.11")
    assert service.authenticate("viewer", PASSWORDS["viewer"], "10.0.0.12") is not None

    monkeypatch.setattr("app.auth.MAX_TRACKED_KEYS", 50)
    for index in range(500):
        service.authenticate(f"user{index}", "wrong password here", f"192.168.{index // 250}.{index % 250}")
    assert len(service._attempts) <= 50


# 6. Content Security Policy, and no API reference unless asked for.


def test_pages_carry_a_content_security_policy_and_the_api_reference_is_off(server):  # noqa: F811
    client = TestClient(main.app)
    for path in ("/api/health", "/"):
        policy = client.get(path).headers.get("content-security-policy", "")
        assert "script-src 'self'" in policy and "frame-ancestors 'none'" in policy, path
    for path in ("/api/docs", "/openapi.json"):
        assert client.get(path).status_code == 404, path

    image = main.settings.model_storage / "acme" / "open" / "card.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    admin, _ = login("admin")
    asset = admin.get("/api/library/asset", params={"repo_id": "acme/open", "path": "card.png"})
    assert asset.status_code == 200
    assert asset.headers["content-security-policy"] == "default-src 'none'; sandbox"


# 7. Error headers built from a request path stay valid HTTP.


def test_non_latin_paths_answer_not_found_instead_of_failing(server):  # noqa: F811
    folder = main.settings.model_storage / "Qwen" / "Qwen3-0.6B"
    folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}", encoding="utf-8")
    main.refresh_model_index()
    client = TestClient(main.app)
    missing = client.get("/Qwen/Qwen3-0.6B/resolve/main/%E6%A8%A1%E5%9E%8B.txt")
    assert missing.status_code == 404
    assert missing.headers["x-error-code"] == "EntryNotFound"
    assert missing.headers["x-error-message"].isascii() and "%E6%A8%A1" in missing.headers["x-error-message"]
    assert "模型.txt" in missing.json()["error"]
    tree = client.get("/api/models/Qwen/Qwen3-0.6B/tree/main/%E6%A8%A1%E5%9E%8B")
    assert tree.status_code == 404 and tree.headers["x-error-code"] == "EntryNotFound"
    revision = client.get("/api/models/Qwen/Qwen3-0.6B/revision/%E6%A8%A1%E5%9E%8B")
    assert revision.status_code == 404 and revision.headers["x-error-code"] == "RevisionNotFound"
    head = client.head("/Qwen/Qwen3-0.6B/resolve/%E6%A8%A1/config.json")
    assert head.status_code == 404


# 8. A new password revokes API tokens as well as other sessions.


def test_password_changes_and_resets_revoke_api_tokens(server):  # noqa: F811
    member, _ = login("member")
    token = member.post("/api/account/tokens", json={"name": "ci", "scope": "read"}).json()["token"]
    pull = TestClient(main.app)
    pull.headers["Authorization"] = f"Bearer {token}"
    assert pull.get("/api/models/member/secret").status_code == 200
    other, _ = login("member")

    changed = member.patch(
        "/api/account/password",
        json={"current_password": PASSWORDS["member"], "new_password": "a brand new passphrase"},
    )
    assert changed.status_code == 200
    assert changed.json() == {"status": "password_changed", "api_tokens_revoked": 1}
    assert pull.get("/api/models/member/secret").status_code == 401
    assert member.get("/api/account").status_code == 200  # this session stays
    assert other.get("/api/account").status_code == 401

    renewed = TestClient(main.app)
    status = renewed.post(
        "/api/auth/login", json={"username": "member", "password": "a brand new passphrase"}
    ).json()
    renewed.headers["X-CSRF-Token"] = status["csrf_token"]
    second = renewed.post("/api/account/tokens", json={"name": "ci", "scope": "read"}).json()["token"]
    admin, _ = login("admin")
    reset = admin.post(
        f"/api/admin/users/{server['users']['member']['id']}/password",
        json={"new_password": "reset by the administrator"},
    )
    assert reset.json() == {"status": "password_reset", "api_tokens_revoked": 1}
    pull.headers["Authorization"] = f"Bearer {second}"
    assert pull.get("/api/models/member/secret").status_code == 401
    assert main.database.list_api_tokens(server["users"]["member"]["id"]) == []


# 9. Deleting an account refuses before it changes anything.


def test_deleting_a_user_refuses_before_reassigning_anything(org):  # noqa: F811
    writer, _ = login("writer")
    repo_id = writer.post(
        "/api/uploads/repositories", json={"slug": "team", "namespace": "Nvidia", "visibility": "organization"}
    ).json()["repo_id"]
    upload(writer, repo_id, {"config.json": b"{}"})
    personal = writer.post("/api/uploads/repositories", json={"slug": "mine", "visibility": "private"})
    upload(writer, personal.json()["repo_id"], {"config.json": b"{}"})
    writer_id = main.database.get_user_by_username("writer")["id"]
    admin, _ = login("admin")

    refused = admin.delete(f"/api/admin/users/{writer_id}")
    assert refused.status_code == 409 and "writer/mine" in refused.json()["detail"]
    assert main.database.get_owned_repository(repo_id)["owner_id"] == writer_id

    # The only admin of an organization stays until someone else is one.
    add_user(org, "lead", "member")
    lead_id = main.database.get_user_by_username("lead")["id"]
    assert admin.post("/api/organizations", json={"name": "Solo"}).status_code == 201
    assert admin.put("/api/organizations/Solo/members/lead", json={"role": "admin"}).status_code == 200
    assert admin.delete("/api/organizations/Solo/members/admin").status_code == 200
    blocked = admin.delete(f"/api/admin/users/{lead_id}")
    assert blocked.status_code == 409 and "only admin of Solo" in blocked.json()["detail"]
    assert main.database.get_user(lead_id) is not None
    assert admin.put("/api/organizations/Solo/members/reader", json={"role": "admin"}).status_code == 200
    assert admin.delete(f"/api/admin/users/{lead_id}").status_code == 200


def test_only_admins_who_can_act_count_as_an_organizations_last_admin(org):  # noqa: F811
    admin, _ = login("admin")
    add_user(org, "lead", "member")
    assert admin.post("/api/organizations", json={"name": "Duo"}).status_code == 201
    for username in ("lead", "reader"):
        assert admin.put(f"/api/organizations/Duo/members/{username}", json={"role": "admin"}).status_code == 200
    assert admin.delete("/api/organizations/Duo/members/admin").status_code == 200
    lead_id = main.database.get_user_by_username("lead")["id"]
    reader_id = main.database.get_user_by_username("reader")["id"]

    # A Viewer's admin row only reads, so the reader is the last admin who acts.
    assert admin.patch(f"/api/admin/users/{lead_id}", json={"role": "viewer"}).status_code == 200
    refused = admin.put("/api/organizations/Duo/members/reader", json={"role": "write"})
    assert refused.status_code == 409 and "at least one admin" in refused.json()["detail"]
    reader, _ = login("reader")
    assert reader.delete("/api/organizations/Duo/members/reader").status_code == 409
    assert admin.delete(f"/api/admin/users/{reader_id}").status_code == 409
    assert admin.delete("/api/organizations/Duo/members/lead").status_code == 200

    # With the last acting admin disabled, a server admin can still appoint one.
    assert admin.patch(f"/api/admin/users/{reader_id}", json={"disabled": True}).status_code == 200
    assert admin.put("/api/organizations/Duo/members/writer", json={"role": "admin"}).status_code == 200
    assert admin.delete("/api/organizations/Duo/members/reader").status_code == 200
    roles = {item["username"]: item["role"] for item in admin.get("/api/organizations/Duo").json()["members"]}
    assert roles == {"writer": "admin"}


# 10. Other web pages cannot post to the server; git, hf, and curl send no Origin.


def test_cross_site_writes_are_refused_and_clients_without_origin_are_not(server, monkeypatch):  # noqa: F811
    admin, _ = login("admin")
    scan = "/api/local-models/scan"
    assert admin.post(scan).status_code == 200
    assert admin.post(scan, headers={"Origin": "http://testserver"}).status_code == 200
    for origin in ("https://evil.example", "null", "http://testserver.evil.example"):
        refused = admin.post(scan, headers={"Origin": origin})
        assert refused.status_code == 403, origin
        assert refused.headers.get("x-content-type-options") == "nosniff"
    # X-Forwarded-Host names the site only when a proxy in FORWARDED_ALLOW_IPS sends it.
    forwarded = {"Origin": "https://models.example.com", "X-Forwarded-Host": "models.example.com"}
    assert admin.post(scan, headers=forwarded).status_code == 403
    monkeypatch.setattr(main, "TRUSTED_PROXIES", _TrustedHosts("testclient"))
    assert admin.post(scan, headers=forwarded).status_code == 200
    monkeypatch.setattr(main, "TRUSTED_PROXIES", _TrustedHosts("127.0.0.1"))
    # Behind a trusted proxy uvicorn has already replaced the client with the
    # browser's address and port 0.
    proxied = TestClient(main.app, client=("192.0.2.7", 0), cookies=admin.cookies)
    proxied.headers["X-CSRF-Token"] = admin.headers["X-CSRF-Token"]
    assert proxied.post(scan, headers={**forwarded, "X-Forwarded-For": "192.0.2.7"}).status_code == 200
    assert proxied.post(scan, headers=forwarded).status_code == 403
    # At PUBLIC_URL.
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, public_url="https://hub.example.com"))
    assert admin.post(scan, headers={"Origin": "https://hub.example.com:443"}).status_code == 200
    assert admin.post(scan, headers={"Origin": "https://hub.example.com:8443"}).status_code == 403

    # git-lfs posts its batch request without an Origin.
    batch = TestClient(main.app).post(
        "/acme/open.git/info/lfs/objects/batch", json={"operation": "download", "objects": []}
    )
    assert batch.status_code == 200


def test_accounts_disabled_still_refuses_writes_from_other_sites(server, monkeypatch):  # noqa: F811
    settings = dataclasses.replace(main.settings, accounts_enabled=False)
    service = AuthService(settings, main.database)
    service.ensure_local_user()
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "auth", service)
    client = TestClient(main.app)
    assert client.post("/api/local-models/scan").status_code == 200
    assert client.post("/api/local-models/scan", headers={"Origin": "https://evil.example"}).status_code == 403


def test_allowed_hosts_blocks_other_host_names(server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, allowed_hosts="models.example.com"))
    assert TestClient(main.app).get("/api/health").status_code == 400
    assert TestClient(main.app, base_url="http://models.example.com").get("/api/health").status_code == 200
    # The container health check calls 127.0.0.1.
    assert TestClient(main.app, base_url="http://127.0.0.1:7860").get("/api/health").status_code == 200
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, allowed_hosts=""))
    assert TestClient(main.app).get("/api/health").status_code == 200


# 11. File counts match the file list, which leaves out hidden files.


def test_file_counts_leave_out_hidden_files_like_the_list_does(server):  # noqa: F811
    open_model = main.settings.model_storage / "acme" / "open"
    (open_model / ".gitattributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
    (open_model / "__pycache__").mkdir()
    (open_model / "__pycache__" / "x.pyc").write_bytes(b"x")
    main.refresh_model_index()
    assert main.database.get_local_model("acme/open")["file_count"] == 1
    assert main.database.get_local_model("member/secret")["file_count"] == 2
    # The Details card's count is the length of the file list beside it.
    member, _ = login("member")
    for repo_id in ("acme/open", "member/secret"):
        details = member.get(f"/api/library/models/{repo_id}").json()
        assert details["file_count"] == len(details["files"]), repo_id
        listed = member.get(f"/api/local-models/{repo_id}").json()["files"]
        assert details["file_count"] == len(listed), repo_id


def test_a_stale_bucket_manifest_count_is_corrected_by_the_next_scan(multi_target):  # noqa: F811
    bucket = multi_target["clients"]["bucket-a"]
    publish(bucket, "models", "acme/remote", {"config.json": b"{}", ".gitattributes": b"*"})
    manifest_key = "models/acme/remote/.hugginghack.json"
    bucket.objects[manifest_key] = json.dumps({"status": "complete", "repo_id": "acme/remote", "file_count": 11}).encode()
    main.refresh_model_index()
    assert main.database.get_local_model("acme/remote")["file_count"] == 1


def test_the_default_upload_message_counts_only_listed_files(org):  # noqa: F811
    writer, _ = login("writer")
    repo_id = writer.post("/api/uploads/repositories", json={"slug": "attrs", "visibility": "private"}).json()["repo_id"]
    for path, payload in {"config.json": b"{}", ".gitattributes": b"*.bin filter=lfs\n"}.items():
        writer.put(
            "/api/uploads/repositories/files",
            params={"repo_id": repo_id, "path": path},
            headers={"Upload-Offset": "0", "Upload-Length": str(len(payload))},
            content=payload,
        )
    assert writer.post("/api/uploads/repositories/finalize", params={"repo_id": repo_id}).status_code == 200
    [commit] = main.database.list_commits(repo_id)
    assert commit["message"] == "Upload 1 file"
    assert main.database.get_local_model(repo_id)["file_count"] == 1


# The download worker starts from any directory, in Docker and in a checkout.


def test_the_download_worker_imports_wherever_the_server_was_started(tmp_path):
    import os
    import subprocess
    import sys

    from app.downloads import WORKER_MODULE, worker_environment

    base = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    started = subprocess.run(
        [sys.executable, "-m", WORKER_MODULE, "--help"],
        cwd=tmp_path, env=worker_environment(base), capture_output=True, text=True, timeout=60,
    )
    assert started.returncode == 0, started.stderr
    assert "--repo-id" in started.stdout
    kept = worker_environment({**base, "PYTHONPATH": "/opt/extra"})["PYTHONPATH"].split(os.pathsep)
    assert kept[1:] == ["/opt/extra"]


# A Viewer's organization role only reads, including for private repositories.


def test_a_viewer_with_an_old_write_role_cannot_see_private_organization_repositories(org):  # noqa: F811
    writer, _ = login("writer")
    repo_id = writer.post(
        "/api/uploads/repositories", json={"slug": "private-team", "namespace": "Nvidia", "visibility": "private"}
    ).json()["repo_id"]
    upload(writer, repo_id, {"config.json": b"{}"})
    shared = writer.post(
        "/api/uploads/repositories", json={"slug": "team", "namespace": "Nvidia", "visibility": "organization"}
    ).json()["repo_id"]
    upload(writer, shared, {"config.json": b"{}"})
    nvidia = main.database.get_organization("Nvidia")
    # A role given before the Viewer cap existed, or before a demotion to Viewer.
    main.database.set_organization_member(nvidia["id"], org["users"]["viewer"]["id"], "write", "2026-01-01T00:00:00+00:00")

    anonymous = TestClient(main.app)
    for username, expected in (("viewer", 404), ("writer", 200)):
        client, _ = login(username)
        assert client.get(f"/api/library/models/{repo_id}").status_code == expected, username
        assert client.get(f"/api/library/models/{shared}").status_code == 200, username
        token = client.post("/api/account/tokens", json={"name": "pull", "scope": "read"}).json()["token"]
        pulled = anonymous.get(
            f"/{repo_id}/resolve/main/config.json", headers={"Authorization": f"Bearer {token}"}
        )
        assert pulled.status_code == expected, username
        basic = base64.b64encode(f"git:{token}".encode()).decode()
        refs = anonymous.get(f"/{repo_id}.git/info/refs", headers={"Authorization": f"Basic {basic}"})
        assert refs.status_code == expected, username
    counts = {item["name"]: item["repository_count"] for item in login("viewer")[0].get("/api/organizations").json()["items"]}
    assert counts["Nvidia"] == 1


# The health check never lists the bucket unless an administrator asks.


def test_health_checks_object_storage_only_for_settings_viewers(server, monkeypatch):  # noqa: F811
    calls = []
    storage = main.storages.default
    working = storage.health
    monkeypatch.setattr(storage, "health", lambda: calls.append(1) or working())
    assert TestClient(main.app).get("/api/health").json()["status"] == "ok"
    member, _ = login("member")
    assert member.get("/api/health").status_code == 200
    assert calls == []
    admin, _ = login("admin")
    assert "object_storage" in admin.get("/api/health").json()
    assert calls == [1]
