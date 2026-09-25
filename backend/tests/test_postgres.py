import json
import os
import uuid

import pytest

from app.database import INTEGRITY_ERRORS, Database


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_postgresql_database_crud_contract():
    database = Database(POSTGRES_URL or "")
    database.initialize()

    suffix = uuid.uuid4().hex
    user_id = f"user-{suffix}"
    username = f"Owner{suffix}"
    collection_id = f"collection-{suffix}"
    download_id = f"download-{suffix}"
    runtime_job_id = f"runtime-{suffix}"
    repo_id = f"owner-{suffix}/model"
    timestamp = "2026-07-24T12:00:00+00:00"

    try:
        user = database.create_user(
            {
                "id": user_id,
                "username": username,
                "display_name": "PostgreSQL Owner",
                "password_hash": "test-only",
                "role": "admin",
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        assert user["id"] == user_id
        assert database.get_user_by_username(username.lower())["id"] == user_id
        with pytest.raises(INTEGRITY_ERRORS):
            database.create_user(
                {
                    "id": f"duplicate-{suffix}",
                    "username": username.lower(),
                    "display_name": "Duplicate",
                    "password_hash": "test-only",
                    "role": "member",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            )

        database.create_session(
            {
                "token_hash": f"token-{suffix}",
                "user_id": user_id,
                "csrf_token": f"csrf-{suffix}",
                "created_at": timestamp,
                "expires_at": "2099-07-24T12:00:00+00:00",
            }
        )
        assert database.get_session(f"token-{suffix}")["user"]["id"] == user_id

        database.create_download(
            {
                "id": download_id,
                "repo_id": repo_id,
                "revision": "main",
                "status": "complete",
                "total_bytes": 7,
                "downloaded_bytes": 7,
                "progress": 100,
                "speed_bps": 0,
                "error": None,
                "target_path": f"/models/{repo_id}",
                "payload_json": json.dumps({"mode": "full"}),
                "metadata_json": json.dumps({"source": "test"}),
                "created_at": timestamp,
                "updated_at": timestamp,
                "completed_at": timestamp,
                "user_id": user_id,
            }
        )
        assert database.get_download(download_id)["payload"]["mode"] == "full"

        database.create_runtime_job(
            {
                "id": runtime_job_id,
                "target_id": f"target-{suffix}",
                "target_name": "PostgreSQL target",
                "target_kind": "ollama",
                "repo_id": repo_id,
                "runtime_model_name": f"model-{suffix}",
                "source_file": None,
                "status": "ready",
                "total_bytes": 7,
                "processed_bytes": 7,
                "progress": 100,
                "message": "Ready",
                "error": None,
                "created_at": timestamp,
                "updated_at": timestamp,
                "completed_at": timestamp,
                "user_id": user_id,
            }
        )
        assert database.get_runtime_job(runtime_job_id)["status"] == "ready"

        database.upsert_local_model(
            {
                "repo_id": repo_id,
                "relative_path": repo_id,
                "size_bytes": 7,
                "file_count": 1,
                "modified_at": timestamp,
                "downloaded_at": timestamp,
                "revision": "main",
                "sha": "abc123",
                "pipeline_tag": "text-generation",
                "library_name": "transformers",
                "license": "mit",
                "tags_json": json.dumps(["postgresql"]),
                "config_json": json.dumps({"model_type": "tiny"}),
                "source_url": None,
                "managed": 1,
                "storage_backend": "filesystem",
                "cached": 1,
                "remote_uri": None,
            }
        )
        assert repo_id in {
            model["repo_id"] for model in database.list_local_models("OWNER")
        }

        database.create_collection(
            {
                "id": collection_id,
                "user_id": user_id,
                "name": "Production",
                "description": "PostgreSQL integration",
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        saved = database.save_model(
            {
                "id": f"saved-{suffix}",
                "user_id": user_id,
                "repo_id": repo_id,
                "note": "Promote this model",
                "metadata_json": json.dumps({"pipeline_tag": "text-generation"}),
                "created_at": timestamp,
                "updated_at": timestamp,
            },
            [collection_id],
        )
        assert saved["collections"] == [collection_id]
        assert database.list_saved_models(user_id, "PROMOTE")[0]["repo_id"] == repo_id
        assert database.delete_collection(collection_id, user_id)
        assert database.list_collections(user_id) == []
        assert database.list_saved_models(user_id)[0]["collections"] == []

        database.create_owned_repository(
            {
                "id": f"owned-{suffix}",
                "owner_id": user_id,
                "repo_id": repo_id,
                "description": "Private model",
                "visibility": "private",
                "status": "ready",
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        assert database.get_visible_local_model(user_id, repo_id)["repo_id"] == repo_id
    finally:
        with database.connect() as connection:
            connection.execute(
                "DELETE FROM owned_repositories WHERE repo_id = ?",
                (repo_id,),
            )
            connection.execute(
                "DELETE FROM local_models WHERE repo_id = ?",
                (repo_id,),
            )
            connection.execute(
                "DELETE FROM downloads WHERE id = ?",
                (download_id,),
            )
            connection.execute(
                "DELETE FROM runtime_jobs WHERE id = ?",
                (runtime_job_id,),
            )
            connection.execute(
                "DELETE FROM users WHERE id = ?",
                (user_id,),
            )


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_postgresql_admin_user_search():
    database = Database(POSTGRES_URL or "")
    database.initialize()
    prefix = f"pg{uuid.uuid4().hex[:8]}"
    ids = []
    try:
        for index in range(12):
            ids.append(uuid.uuid4().hex)
            database.create_user(
                {
                    "id": ids[-1],
                    "username": f"{prefix}{index:02d}",
                    "display_name": f"Paged {index:02d}",
                    "password_hash": "test-only",
                    "role": "member" if index % 2 else "viewer",
                    "created_at": f"2026-07-24T12:00:{index:02d}+00:00",
                    "updated_at": "2026-07-24T12:00:00+00:00",
                    "email": f"{prefix}{index:02d}@example.internal",
                }
            )
        database.update_user(ids[3], disabled=1, last_login_at="2026-07-25T00:00:00+00:00")

        rows, total, counts = database.search_users(query=prefix.upper(), sort="name", limit=5, offset=5)
        assert total == 12 and [row["username"] for row in rows] == [f"{prefix}{n:02d}" for n in range(5, 10)]
        assert counts == {"all": 12, "admin": 0, "member": 6, "viewer": 6, "active": 11, "disabled": 1}
        _, total, _ = database.search_users(query=prefix, role="member", status="active")
        assert total == 5
        rows, _, _ = database.search_users(query=prefix, sort="last_login", limit=1)
        assert rows[0]["id"] == ids[3]
        rows, _, _ = database.search_users(query=prefix, sort="newest", limit=1)
        assert rows[0]["id"] == ids[11]
        assert database.search_users(query=f"{prefix}%")[1] == 0
        assert database.search_users(query=f"{prefix}0_")[1] == 0
        assert database.user_activity([]) == {}
        assert database.user_activity(ids[:2]) == {}
    finally:
        for user_id in ids:
            database.delete_user(user_id)


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_postgresql_admin_organization_search():
    database = Database(POSTGRES_URL or "")
    database.initialize()
    prefix = f"pgorg{uuid.uuid4().hex[:8]}"
    user_id = uuid.uuid4().hex
    timestamp = "2026-07-24T12:00:00+00:00"
    database.create_user(
        {"id": user_id, "username": f"{prefix}u", "display_name": "Org Admin", "password_hash": "test-only",
         "role": "member", "created_at": timestamp, "updated_at": timestamp}
    )
    organizations = []
    try:
        for index in range(7):
            organizations.append(
                database.create_organization(
                    {"id": uuid.uuid4().hex, "name": f"{prefix}{index}", "display_name": f"Team {index}",
                     "description": "has_underscore" if index < 2 else "", "created_at": f"2026-07-24T12:00:0{index}+00:00",
                     "updated_at": timestamp}
                )
            )
        database.set_organization_member(organizations[3]["id"], user_id, "admin", timestamp)

        rows, total, counts = database.search_organizations(user_id, query=prefix.upper(), sort="newest", limit=3, offset=3)
        assert total == 7 and [row["name"] for row in rows] == [f"{prefix}{n}" for n in (3, 2, 1)]
        assert counts == {"all": 7, "with_repositories": 0, "empty": 7, "mine": 1}
        rows, total, _ = database.search_organizations(user_id, query=prefix, filter="mine")
        assert total == 1 and rows[0]["my_role"] == "admin" and rows[0]["member_count"] == 1
        rows, _, _ = database.search_organizations(user_id, query=prefix, sort="members", limit=1)
        assert rows[0]["name"] == f"{prefix}3"
        assert database.search_organizations(user_id, query=f"{prefix}%")[1] == 0
        assert database.search_organizations(user_id, query="has_under")[1] == 2
    finally:
        for organization in organizations:
            database.remove_organization_member(organization["id"], user_id, force=True)
            database.delete_organization(organization["id"])
        database.delete_user(user_id)


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_postgresql_model_hardware_tags():
    database = Database(POSTGRES_URL or "")
    database.initialize()
    repo_id = f"pg-{uuid.uuid4().hex}/model"
    try:
        database.set_model_hardware(repo_id, ["l40", "b300"])
        assert database.model_hardware([repo_id]) == {repo_id: ["b300", "l40"]}
        database.set_model_hardware(repo_id, ["a100"])
        assert database.model_hardware([repo_id]) == {repo_id: ["a100"]}
        assert database.model_hardware([]) == {}
    finally:
        database.set_model_hardware(repo_id, [])
    assert repo_id not in database.model_hardware()


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_postgresql_visibility_upgrade_and_storage_grants():
    database = Database(POSTGRES_URL or "")
    database.initialize()
    suffix = uuid.uuid4().hex
    user_id, org_id = f"user-{suffix}", f"org-{suffix}"
    timestamp = "2026-09-25T12:00:00+00:00"
    repos = {f"u{suffix}/mine": None, f"u{suffix}/open": None, f"o{suffix}/team": org_id}
    try:
        database.create_user(
            {"id": user_id, "username": f"u{suffix}", "display_name": "Owner",
             "password_hash": "test-only", "role": "member",
             "created_at": timestamp, "updated_at": timestamp}
        )
        database.create_organization(
            {"id": org_id, "name": f"o{suffix}", "display_name": "Org", "description": "",
             "created_at": timestamp, "updated_at": timestamp}
        )
        # Put back the constraint of an older install; NOT VALID skips other tests' rows.
        with database.connect() as connection:
            connection.execute(
                "ALTER TABLE owned_repositories DROP CONSTRAINT owned_repositories_visibility_check"
            )
            connection.execute(
                "ALTER TABLE owned_repositories ADD CONSTRAINT old_visibility "
                "CHECK (visibility IN ('private', 'shared')) NOT VALID"
            )
        for repo_id, organization_id in repos.items():
            database.create_owned_repository(
                {"id": uuid.uuid4().hex, "owner_id": user_id, "repo_id": repo_id,
                 "description": "", "status": "ready", "created_at": timestamp,
                 "updated_at": timestamp, "organization_id": organization_id,
                 "visibility": "shared" if repo_id.endswith("/open") else "private"}
            )
        database.initialize()
        assert [database.get_owned_repository(repo_id)["visibility"] for repo_id in repos] == [
            "private", "public", "organization"
        ]
        with pytest.raises(INTEGRITY_ERRORS):
            database.update_owned_repository(f"u{suffix}/mine", visibility="shared")

        database.set_storage_grants("pg-bucket", [user_id], [org_id], timestamp)
        grants = database.storage_grants()["pg-bucket"]
        assert sorted((item["kind"], item["id"]) for item in grants) == [
            ("organization", org_id), ("user", user_id)
        ]
        with pytest.raises(INTEGRITY_ERRORS):
            database.set_storage_grants("pg-bucket", [user_id, user_id], [], timestamp)
    finally:
        database.set_storage_grants("pg-bucket", [], [], timestamp)
        for repo_id in repos:
            database.delete_owned_repository(repo_id)
        with database.connect() as connection:
            connection.execute("DELETE FROM organizations WHERE id = ?", (org_id,))
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
    assert "pg-bucket" not in database.storage_grants()


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_postgresql_repository_rename_moves_every_row():
    database = Database(POSTGRES_URL or "")
    database.initialize()
    suffix = uuid.uuid4().hex
    user_id, org_id = f"user-{suffix}", f"org-{suffix}"
    old, new, adopted = f"u{suffix}/old", f"o{suffix}/new", f"hf{suffix}/model"
    timestamp = "2026-09-25T12:00:00+00:00"
    try:
        database.create_user(
            {"id": user_id, "username": f"u{suffix}", "display_name": "Owner",
             "password_hash": "test-only", "role": "member",
             "created_at": timestamp, "updated_at": timestamp}
        )
        database.create_organization(
            {"id": org_id, "name": f"o{suffix}", "display_name": "Org", "description": "",
             "created_at": timestamp, "updated_at": timestamp}
        )
        database.create_owned_repository(
            {"id": uuid.uuid4().hex, "owner_id": user_id, "repo_id": old, "description": "",
             "visibility": "private", "status": "ready", "created_at": timestamp,
             "updated_at": timestamp}
        )
        database.set_model_hardware(old, ["l40"])
        database.rename_repository(
            old, new,
            {"owner_id": user_id, "organization_id": org_id, "visibility": "organization",
             "updated_at": timestamp},
        )
        moved = database.get_owned_repository(new)
        assert moved["organization_id"] == org_id and moved["visibility"] == "organization"
        assert database.get_owned_repository(old) is None
        assert database.model_hardware([new, old]) == {new: ["l40"]}

        # A downloaded model is registered as owned when it gets an owner.
        database.rename_repository(
            f"hf{suffix}/source", adopted,
            {"id": uuid.uuid4().hex, "owner_id": user_id, "organization_id": None,
             "visibility": "public", "updated_at": timestamp, "description": "",
             "status": "ready", "created_at": timestamp},
        )
        assert database.get_owned_repository(adopted)["visibility"] == "public"
    finally:
        for repo_id in (old, new, adopted):
            database.delete_owned_repository(repo_id)
        with database.connect() as connection:
            connection.execute("DELETE FROM organizations WHERE id = ?", (org_id,))
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_postgresql_config_revisions():
    database = Database(POSTGRES_URL or "")
    database.initialize()
    repo_id = f"pg-{uuid.uuid4().hex}/model"
    base = {
        "repo_id": repo_id, "message": "Baseline", "description": "", "author_id": None,
        "author_name": "Tester", "files_json": "[]", "changes_json": "[]",
        "results_json": '{"values": {"output_tps": 10}}', "results_updated_at": None,
        "results_updated_by": None, "created_at": "2026-09-25T12:00:00+00:00",
    }
    try:
        first = database.create_config_revision({**base, "id": uuid.uuid4().hex, "parent_id": None})
        assert first["sequence"] == 1 and first["results"]["values"]["output_tps"] == 10
        with pytest.raises(RuntimeError):
            database.create_config_revision({**base, "id": uuid.uuid4().hex, "parent_id": None})
        second = database.create_config_revision({**base, "id": uuid.uuid4().hex, "parent_id": first["id"]})
        assert second["sequence"] == 2
        assert database.latest_config_revision(repo_id)["id"] == second["id"]
        updated = database.update_config_results(repo_id, first["id"], '{"values": {}}', "t", "Tester")
        assert updated["results"] == {"values": {}} and updated["results_updated_by"] == "Tester"
        assert [item["sequence"] for item in database.list_config_revisions(repo_id)] == [2, 1]
        assert database.count_config_revisions(repo_id) == 2
    finally:
        database.delete_config_revisions(repo_id)
    assert database.count_config_revisions(repo_id) == 0


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_postgresql_admin_user_detail_queries():
    """What the admin account page reads, and revoking one token of one account."""
    database = Database(POSTGRES_URL or "")
    database.initialize()
    suffix = uuid.uuid4().hex
    timestamp = "2026-09-25T12:00:00+00:00"
    users = [f"detail-{suffix}", f"other-{suffix}"]
    try:
        for user_id in users:
            database.create_user(
                {
                    "id": user_id,
                    "username": user_id,
                    "display_name": user_id,
                    "password_hash": "test-only",
                    "role": "member",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            )
            database.create_api_token(
                {
                    "id": f"token-{user_id}",
                    "user_id": user_id,
                    "name": "ci",
                    "token_hash": f"hash-{user_id}",
                    "prefix": "hht_abcdef",
                    "scope": "read",
                    "created_at": timestamp,
                    "expires_at": None,
                }
            )
        database.create_session(
            {
                "token_hash": f"session-{suffix}",
                "user_id": users[0],
                "csrf_token": f"csrf-{suffix}",
                "created_at": timestamp,
                "expires_at": "2099-01-01T00:00:00+00:00",
                "user_agent": "pytest",
                "ip": "127.0.0.1",
            }
        )
        assert [session["ip"] for session in database.list_sessions(users[0])] == ["127.0.0.1"]
        tokens = database.list_api_tokens(users[0])
        assert [token["prefix"] for token in tokens] == ["hht_abcdef"] and "token_hash" not in tokens[0]
        assert database.user_organizations(users[0]) == []
        assert database.owned_repository_ids(users[0]) == []
        # A token id is only found under the account that owns it.
        assert not database.delete_api_token(users[0], f"token-{users[1]}")
        assert database.delete_api_token(users[0], f"token-{users[0]}")
        assert database.list_api_tokens(users[0]) == []
        assert len(database.list_api_tokens(users[1])) == 1
    finally:
        for user_id in users:
            database.delete_user_tokens(user_id)
            database.delete_user_sessions(user_id)
            database.delete_user(user_id)


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_postgresql_listing_corrections_merge_into_reads():
    database = Database(POSTGRES_URL or "")
    database.initialize()
    repo_id = f"pg-{uuid.uuid4().hex}/model"
    renamed = f"{repo_id}-v2"
    timestamp = "2026-09-25T12:00:00+00:00"
    try:
        database.upsert_local_model(
            {
                "repo_id": repo_id,
                "relative_path": repo_id,
                "size_bytes": 7,
                "file_count": 1,
                "modified_at": timestamp,
                "downloaded_at": None,
                "revision": None,
                "sha": None,
                "pipeline_tag": "text-generation",
                "library_name": "transformers",
                "license": "mit",
                "tags_json": json.dumps(["tiny"]),
                "config_json": json.dumps({"model_type": "llama", "precision": "fp8"}),
                "source_url": None,
                "managed": 0,
                "storage_backend": "filesystem",
                "cached": 1,
                "remote_uri": None,
                "parameter_count": 1000,
            }
        )
        database.set_listing_overrides(repo_id, {"precision": "nvfp4", "parameter_count": 8}, timestamp, None)
        for model in (
            database.get_local_model(repo_id),
            next(item for item in database.list_local_models() if item["repo_id"] == repo_id),
            next(item for item in database.list_local_models("pg-") if item["repo_id"] == repo_id),
        ):
            assert model["config"]["precision"] == "nvfp4" and model["parameter_count"] == 8
            assert model["detected"]["precision"] == "fp8" and model["detected"]["parameter_count"] == 1000
            assert model["listing_overrides"] == {"parameter_count": 8, "precision": "nvfp4"}
        database.rename_repository(repo_id, renamed)
        assert database.listing_overrides(renamed) == {"parameter_count": 8, "precision": "nvfp4"}
        assert database.listing_overrides(repo_id) == {}
        database.set_listing_overrides(renamed, {}, timestamp, None)
        assert database.get_local_model(renamed)["config"]["precision"] == "fp8"
    finally:
        for name in (repo_id, renamed):
            database.set_listing_overrides(name, {}, timestamp, None)
            database.delete_owned_repository(name)


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_postgresql_storage_moves_and_revision_aliases():
    database = Database(POSTGRES_URL or "")
    database.initialize()
    repo_id = f"pg-{uuid.uuid4().hex}/model"
    move_id = uuid.uuid4().hex
    timestamp = "2026-09-25T12:00:00+00:00"
    try:
        database.create_move(
            {
                "id": move_id, "repo_id": repo_id, "source_target": "local", "destination_target": "bucket-a",
                "keep_local": 0, "status": "queued", "message": "Waiting", "created_by": None,
                "created_at": timestamp, "updated_at": timestamp,
            }
        )
        assert [move["id"] for move in database.unfinished_moves() if move["repo_id"] == repo_id] == [move_id]
        moved = database.update_move(move_id, status="done", copied_bytes=10, ignored="x", finished_at=timestamp)
        assert (moved["status"], moved["copied_bytes"], moved["keep_local"]) == ("done", 10, False)
        assert move_id not in {move["id"] for move in database.unfinished_moves()}
        assert move_id in {move["id"] for move in database.list_moves()}

        # An older name follows its content to each new name.
        database.add_revision_alias(repo_id, "a" * 40, "b" * 40, timestamp)
        database.add_revision_alias(repo_id, "b" * 40, "c" * 40, timestamp)
        assert database.revision_alias_target(repo_id, "a" * 40) == "c" * 40
        assert database.revision_alias_target(repo_id, "b" * 40) == "c" * 40
        assert database.revision_alias_target(repo_id, "d" * 40) is None

        database.upsert_local_model(
            {
                "repo_id": repo_id, "relative_path": repo_id, "size_bytes": 1, "file_count": 1,
                "modified_at": timestamp, "downloaded_at": None, "revision": None, "sha": None,
                "pipeline_tag": None, "library_name": None, "license": None, "tags_json": "[]",
                "config_json": "{}", "source_url": None, "managed": 1, "storage_backend": "filesystem",
                "cached": 1, "remote_uri": None,
            }
        )
        database.set_local_model_location(repo_id, "s3", "bucket-a", "s3://bucket-a/model", False)
        model = database.get_local_model(repo_id)
        assert (model["storage_backend"], model["storage_target"], model["cached"]) == ("s3", "bucket-a", False)
        assert database.find_active_runtime_job_for_repo(repo_id) is None
    finally:
        with database.connect() as connection:
            connection.execute("DELETE FROM storage_moves WHERE id = ?", (move_id,))
            connection.execute("DELETE FROM revision_aliases WHERE repo_id = ?", (repo_id,))
        database.delete_owned_repository(repo_id)


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_postgresql_models_keep_their_base_model():
    database = Database(POSTGRES_URL or "")
    database.initialize()
    repo_id = f"pg-{uuid.uuid4().hex}/quant"
    timestamp = "2026-09-25T12:00:00+00:00"
    record = {
        "repo_id": repo_id, "relative_path": repo_id, "size_bytes": 1, "file_count": 1,
        "modified_at": timestamp, "downloaded_at": None, "revision": None, "sha": None,
        "pipeline_tag": None, "library_name": None, "license": None, "tags_json": "[]",
        "config_json": "{}", "source_url": None, "managed": 0, "storage_backend": "filesystem",
        "cached": 1, "remote_uri": None,
    }
    try:
        database.upsert_local_model(record)  # callers that know nothing of base models
        assert database.get_local_model(repo_id)["base_model"] is None
        database.upsert_local_model({**record, "base_model": "Qwen/Qwen3-0.6B", "base_model_relation": "quantized"})
        model = database.get_local_model(repo_id)
        assert (model["base_model"], model["base_model_relation"]) == ("Qwen/Qwen3-0.6B", "quantized")
        database.set_listing_overrides(repo_id, {"base_model_relation": "finetune"}, timestamp, None)
        model = database.get_local_model(repo_id)
        assert model["base_model_relation"] == "finetune" and model["detected"]["base_model_relation"] == "quantized"
    finally:
        database.set_listing_overrides(repo_id, {}, timestamp, None)
        database.delete_owned_repository(repo_id)


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_postgresql_profile_pictures():
    database = Database(POSTGRES_URL or "")
    database.initialize()
    suffix = uuid.uuid4().hex[:12]
    timestamp = "2026-09-25T12:00:00+00:00"
    user_id, org_id = f"user-{suffix}", f"org-{suffix}"
    try:
        database.create_user(
            {"id": user_id, "username": f"Pic{suffix}", "display_name": "Pic", "password_hash": "test-only",
             "role": "member", "created_at": timestamp, "updated_at": timestamp}
        )
        database.create_organization(
            {"id": org_id, "name": f"Org{suffix}", "display_name": "Org", "description": "",
             "created_at": timestamp, "updated_at": timestamp}
        )
        assert database.avatar_owner(f"pic{suffix}") is None
        database.set_avatar("user", user_id, timestamp)
        database.set_avatar("organization", org_id, "v2")
        assert database.avatar_owner(f"PIC{suffix}") == ("user", user_id, timestamp)
        assert database.avatar_owner(f"org{suffix}") == ("organization", org_id, "v2")
        versions = database.avatar_versions()
        assert versions[f"pic{suffix}"] == timestamp and versions[f"org{suffix}"] == "v2"
        assert database.get_user(user_id, include_secret=False)["avatar_updated_at"] == timestamp
        database.set_avatar("user", user_id, None)
        assert f"pic{suffix}" not in database.avatar_versions()
    finally:
        database.delete_organization(org_id)
        database.delete_user(user_id)
