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
