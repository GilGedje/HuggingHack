import json
from pathlib import Path

import pytest

import app.main as main
from app.config import S3_CONCURRENCY_LIMIT, Settings
from app.database import Database
from app.storage import S3ModelStorage, StorageRegistry, parse_storage_targets
from test_organizations import add_user, login, org  # noqa: F401  (fixtures)
from test_access import server  # noqa: F401  (fixture)


def test_server_settings_round_trip(tmp_path: Path):
    database = Database(tmp_path / "settings.sqlite3")
    database.initialize()
    assert database.server_setting("s3_max_concurrency") is None
    database.set_server_setting("s3_max_concurrency", 16, "2026-09-27T10:00:00+00:00", "admin")
    database.set_server_setting("s3_max_concurrency", 48, "2026-09-27T11:00:00+00:00", "ada")
    assert database.server_setting("s3_max_concurrency") == {
        "name": "s3_max_concurrency",
        "value": 48,
        "updated_at": "2026-09-27T11:00:00+00:00",
        "updated_by": "ada",
    }
    database.delete_server_setting("s3_max_concurrency")
    assert database.server_setting("s3_max_concurrency") is None


def test_admins_set_the_transfer_concurrency_on_the_storage_page(org, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "_transfer_concurrency", {"value": None, "read_at": 0.0})
    admin, _ = login("admin")
    writer, _ = login("writer")
    default = main.settings.s3_max_concurrency

    transfers = admin.get("/api/storage/targets").json()["transfers"]
    assert transfers == {
        "max_concurrency": default,
        "default": default,
        "limit": S3_CONCURRENCY_LIMIT,
        "part_size_mb": main.settings.s3_multipart_chunk_mb,
        "source": "environment",
        "updated_at": None,
        "updated_by": None,
    }
    assert writer.put("/api/storage/transfers", json={"max_concurrency": 8}).status_code == 403
    for bad in ({"max_concurrency": 0}, {"max_concurrency": S3_CONCURRENCY_LIMIT + 1}, {}, {"max_concurrency": "many"}):
        assert admin.put("/api/storage/transfers", json=bad).status_code == 422, bad

    assert main.transfer_concurrency() == default
    changed = admin.put("/api/storage/transfers", json={"max_concurrency": S3_CONCURRENCY_LIMIT})
    assert changed.status_code == 200, changed.text
    assert changed.json()["max_concurrency"] == S3_CONCURRENCY_LIMIT
    assert changed.json()["source"] == "admin"
    assert changed.json()["updated_by"] == "admin"
    # The change applies to the next transfer on this server at once.
    assert main.transfer_concurrency() == S3_CONCURRENCY_LIMIT
    assert admin.get("/api/storage/targets").json()["transfers"]["max_concurrency"] == S3_CONCURRENCY_LIMIT

    reset = admin.put("/api/storage/transfers", json={"max_concurrency": None}).json()
    assert (reset["max_concurrency"], reset["source"], reset["updated_by"]) == (default, "environment", None)
    assert main.transfer_concurrency() == default


def test_other_servers_follow_a_change_within_seconds(org, monkeypatch):  # noqa: F811
    clock = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(main, "_transfer_concurrency", {"value": None, "read_at": 0.0})
    database = main.database
    assert main.transfer_concurrency() == main.settings.s3_max_concurrency
    # Another server writes the setting; this one keeps its answer until it is stale.
    database.set_server_setting(main.TRANSFER_CONCURRENCY, 24, "2026-09-27T10:00:00+00:00", "admin")
    assert main.transfer_concurrency() == main.settings.s3_max_concurrency
    clock[0] += main.TRANSFER_CONCURRENCY_TTL + 0.1
    assert main.transfer_concurrency() == 24

    # An unreadable database keeps the last value instead of failing a transfer.
    def broken(_name):
        raise RuntimeError("database down")

    monkeypatch.setattr(database, "server_setting", broken)
    clock[0] += main.TRANSFER_CONCURRENCY_TTL + 0.1
    assert main.transfer_concurrency() == 24


def test_buckets_ask_for_the_current_concurrency_on_every_transfer(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BUCKET_KEY", "key")
    monkeypatch.setenv("BUCKET_SECRET", "secret")
    [target] = parse_storage_targets(json.dumps([{
        "id": "bucket", "bucket": "models", "endpoint_url": "http://127.0.0.1:9",
        "access_key_env": "BUCKET_KEY", "secret_key_env": "BUCKET_SECRET", "addressing_style": "path",
    }]))
    settings = Settings(model_storage=tmp_path / "models", data_dir=tmp_path / "data")
    storage = S3ModelStorage(settings, target=target)  # builds boto3 clients; no request is made
    # Enough pooled connections for the most parts in flight.
    assert storage.client.meta.config.max_pool_connections == S3_CONCURRENCY_LIMIT

    parallel = [settings.s3_max_concurrency]
    StorageRegistry(settings, [storage]).use_concurrency(lambda: parallel[0])
    for asked, used in ((12, 12), (S3_CONCURRENCY_LIMIT, S3_CONCURRENCY_LIMIT), (500, S3_CONCURRENCY_LIMIT), (0, 1)):
        parallel[0] = asked
        config = storage._transfer_options()["Config"]
        # A move streams into the bucket, holding each part in flight in memory: the
        # memory allowance follows the setting, or boto3 would stop at 10 parts.
        assert (config.max_concurrency, config.max_in_memory_upload_chunks) == (used, used)
        assert config.multipart_chunksize == settings.s3_multipart_chunk_mb * 1024**2
