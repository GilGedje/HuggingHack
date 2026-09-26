"""Settings, signing and locking that direct transfers and cluster mode build on
(docs/SCALING.md)."""

import os
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from app.config import Settings
from app.database import Database
from app.storage import S3ModelStorage, S3TargetConfig, parse_storage_targets

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")


def storage_for(tmp_path: Path, **overrides) -> S3ModelStorage:
    settings = Settings(model_storage=tmp_path / "models", data_dir=tmp_path / "data")
    target = S3TargetConfig(
        id="grid",
        name="Grid",
        bucket="models",
        prefix="library",
        endpoint_url="http://grid-internal:9000",
        region="eu-west-1",
        access_key_id="AKIDEXAMPLE",
        secret_access_key="secret-key-example",
        addressing_style="path",
        **overrides,
    )
    return S3ModelStorage(settings, target=target)


def test_storage_targets_read_direct_transfer_settings(monkeypatch):
    monkeypatch.setenv("GRID_KEY", "key")
    monkeypatch.setenv("GRID_SECRET", "secret")
    [target] = parse_storage_targets(
        '[{"id":"grid","bucket":"models","access_key_env":"GRID_KEY","secret_key_env":"GRID_SECRET",'
        '"public_endpoint_url":"https://s3.grid.example","ca_bundle":"/etc/hugginghack/ca.pem",'
        '"direct_downloads":true,"presign_ttl_seconds":600,"direct_uploads":true,"part_size_mb":128}]'
    )
    assert target.public_endpoint_url == "https://s3.grid.example"
    assert target.ca_bundle == "/etc/hugginghack/ca.pem"
    assert (target.direct_downloads, target.direct_uploads) == (True, True)
    assert (target.presign_ttl_seconds, target.part_size_mb, target.upload_presign_ttl_seconds) == (600, 128, 3600)

    [defaults] = parse_storage_targets('[{"id":"plain","bucket":"models"}]')
    assert (defaults.direct_downloads, defaults.direct_uploads) == (False, False)
    assert defaults.public_endpoint_url is None and defaults.ca_bundle is None

    for bad in ('"presign_ttl_seconds":10', '"part_size_mb":4', '"upload_presign_ttl_seconds":"soon"'):
        with pytest.raises(ValueError, match="must be a whole number"):
            parse_storage_targets('[{"id":"grid","bucket":"models",' + bad + "}]")


def test_signed_links_name_the_public_endpoint_and_force_a_filename(tmp_path: Path):
    storage = storage_for(tmp_path, public_endpoint_url="https://s3.grid.example")
    url = storage.presigned_get("Qwen/Qwen3-0.6B", "weights/model.safetensors", filename="weights/model.safetensors")
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    # Signed for the address clients use, never the internal one this server uses.
    assert parts.netloc == "s3.grid.example"
    assert parts.path == "/models/library/Qwen/Qwen3-0.6B/weights/model.safetensors"
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["900"]
    assert "/eu-west-1/s3/" in query["X-Amz-Credential"][0]
    assert query["response-content-disposition"] == [
        "attachment; filename=\"model.safetensors\"; filename*=UTF-8''model.safetensors"
    ]
    signature = query["X-Amz-Signature"][0]
    redacted = storage.redact(f"GET {url} failed")
    assert signature not in redacted and "X-Amz-Signature=[redacted]" in redacted
    assert "secret-key-example" not in storage.redact("secret-key-example leaked")

    # Without a public endpoint, links name the endpoint itself.
    plain = storage_for(tmp_path, public_endpoint_url=None)
    assert urlsplit(plain.presigned_get("a/b", "c.bin", ttl=120)).netloc == "grid-internal:9000"
    with pytest.raises(ValueError):
        plain.presigned_get("../escape", "c.bin")


def test_a_missing_ca_bundle_stops_startup_with_a_sentence(tmp_path: Path):
    with pytest.raises(ValueError, match="does not exist"):
        storage_for(tmp_path, ca_bundle=str(tmp_path / "missing.pem"))
    bundle = tmp_path / "ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n")
    assert storage_for(tmp_path, ca_bundle=str(bundle)).direct_downloads is False


def test_cluster_settings_default_to_a_single_named_process(monkeypatch):
    settings = Settings()
    assert settings.cluster_mode is False
    assert settings.instance_id


def overlapping_holders(databases: list[Database], key: str) -> int:
    """How many threads were inside `cluster_lock(key)` at once, at most."""
    inside = 0
    most = 0
    guard = threading.Lock()

    def hold(database: Database) -> None:
        nonlocal inside, most
        with database.cluster_lock(key):
            with guard:
                inside += 1
                most = max(most, inside)
            time.sleep(0.05)
            with guard:
                inside -= 1

    threads = [threading.Thread(target=hold, args=(databases[index % len(databases)],)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return most


def test_cluster_lock_admits_one_holder_at_a_time_on_sqlite(tmp_path: Path):
    database = Database(tmp_path / "hub.sqlite3")
    assert overlapping_holders([database], "repo:a/b") == 1
    # Different keys do not wait for each other.
    with database.cluster_lock("repo:a/b"):
        with database.cluster_lock("repo:c/d"):
            pass


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")
def test_cluster_lock_spans_processes_sharing_postgresql():
    # Two Database objects have separate in-process locks, like two pods.
    first, second = Database(POSTGRES_URL, pool_size=2), Database(POSTGRES_URL, pool_size=2)
    key = f"test:{uuid4().hex}"
    try:
        assert overlapping_holders([first, second], key) == 1
        # A holder's queries still get connections while it holds the lock.
        with first.cluster_lock(key), first.connect() as connection:
            assert connection.execute("SELECT 1 AS one").fetchone()["one"] == 1
        # An exception inside releases the lock for the other process.
        with pytest.raises(RuntimeError):
            with first.cluster_lock(key):
                raise RuntimeError("boom")
        acquired = threading.Event()

        def take() -> None:
            with second.cluster_lock(key):
                acquired.set()

        thread = threading.Thread(target=take)
        thread.start()
        assert acquired.wait(5)
        thread.join()
    finally:
        first.close()
        second.close()


def test_an_untrusted_certificate_is_named_as_such(tmp_path: Path):
    from botocore.exceptions import SSLError

    storage = storage_for(tmp_path)
    untrusted = SSLError(
        endpoint_url="https://grid-internal:9000/models",
        error="[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in certificate chain",
    )
    sentence = storage.describe_error(untrusted)
    assert sentence.startswith("The certificate of s3://models at http://grid-internal:9000 is not trusted.")
    assert "ca_bundle" in sentence
    other = storage.describe_error(SSLError(endpoint_url="https://grid-internal:9000", error="wrong version number"))
    assert other.startswith("The TLS connection to s3://models")
