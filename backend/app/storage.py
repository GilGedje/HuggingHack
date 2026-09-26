from __future__ import annotations

import json
import logging
import os
import re
import shutil
import struct
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from uuid import uuid4
from urllib.parse import quote, urlsplit, urlunsplit

from .config import Settings, repository_path, validate_repo_id
from .indexer import (
    LEGACY_S3_TARGET_ID,
    LOCAL_TARGET_ID,
    PART_SUFFIXES,
    SAFETENSORS_MAX_HEADER_BYTES,
    UNSAFE_EXTENSIONS,
    hidden_path,
    manifest_target,
    model_formats,
    repository_facts,
)


logger = logging.getLogger("hugginghack")
MANIFEST_NAME = ".hugginghack.json"
# Names the objects a change is writing before its manifest is published. A change
# that fails part-way leaves them behind; until one succeeds, they are not part of
# the repository, so a scan never adopts them as a commit.
PENDING_NAME = ".hugginghack-pending.json"
# Where the browser uploads a change to a published repository, one folder per
# change session, until the commit copies it into place. Never part of the
# repository: listings, scans, moves and cleanup all leave it alone.
CHANGES_DIRECTORY = ".hugginghack-changes"
# S3 multipart limits: at most 10,000 parts, each at least 5 MB except the last.
MAX_PARTS = 10_000
MIN_PART_BYTES = 5 * 1024**2
# What finalizing a directly uploaded repository reads to describe it: these files
# whole, and the start of each weight file (GGUF metadata can hold a large vocabulary).
SKETCH_WHOLE_FILES = {"config.json", "hf_quant_config.json", "README.md"}
SKETCH_WHOLE_MAX_BYTES = 1_000_000
SKETCH_GGUF_HEADER_BYTES = 64 * 1024**2
# What a manifest says about a model that its files decide.
DESCRIBED_KEYS = {
    "config", "parameter_count", "formats", "precision", "pipeline_tag", "library_name",
    "license", "tags", "base_model", "base_model_relation",
}
TARGET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# The parts of a signed link that grant access; never logged or shown.
SIGNED_QUERY = re.compile(r"((?:X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token|Signature|AWSAccessKeyId)=)[^&\s\"'<>]+")


def in_change_area(relative: str) -> bool:
    return relative == CHANGES_DIRECTORY or relative.startswith(f"{CHANGES_DIRECTORY}/")


def part_size_for(size: int, preferred: int) -> int:
    """The part size for a file: the preferred one, grown in whole megabytes when a
    file that large would need more than MAX_PARTS parts."""
    needed = -(-size // MAX_PARTS) if size else 0
    part = max(preferred, MIN_PART_BYTES, needed)
    megabyte = 1024**2
    return -(-part // megabyte) * megabyte


def content_disposition(filename: str) -> str:
    """`attachment` with the name in both the plain and the RFC 5987 form."""
    name = PurePosixPath(filename).name or "download"
    plain = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name)
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"

try:
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
except ImportError:  # boto3 is only needed for S3 targets
    BotoCoreError = ClientError = NoCredentialsError = None  # type: ignore[assignment,misc]
# Errors a bucket raises when it fails or cannot be reached.
BOTO_ERRORS: tuple[type[Exception], ...] = tuple(
    error for error in (BotoCoreError, ClientError) if error is not None
)


class StorageUnavailableError(Exception):
    """Object storage failed or was unreachable. The message is safe to show; the
    underlying error, which may name the bucket or endpoint, is only logged."""


def _error_code(error: Exception) -> str:
    details = (getattr(error, "response", None) or {}).get("Error") or {}
    return str(details.get("Code") or "")


def _missing_object(error: Exception) -> bool:
    """Whether an S3 error says the object does not exist, rather than that the
    request failed."""
    return _error_code(error) in {"NoSuchKey", "404", "NotFound"}


@dataclass(frozen=True)
class S3TargetConfig:
    """One S3-compatible bucket (and prefix) that holds complete repositories."""

    id: str
    name: str
    bucket: str
    prefix: str = "models"
    endpoint_url: str | None = None
    region: str | None = None
    access_key_id: str | None = field(default=None, repr=False)
    secret_access_key: str | None = field(default=None, repr=False)
    session_token: str | None = field(default=None, repr=False)
    use_ssl: bool = True
    verify_ssl: bool = True
    addressing_style: str = "auto"
    storage_class: str | None = None
    # Direct transfers: signed links name public_endpoint_url, which must match a
    # name on the endpoint's certificate; ca_bundle verifies a private CA.
    public_endpoint_url: str | None = None
    ca_bundle: str | None = None
    direct_downloads: bool = False
    presign_ttl_seconds: int = 900
    direct_uploads: bool = False
    part_size_mb: int = 64
    upload_presign_ttl_seconds: int = 3600

    @classmethod
    def from_settings(cls, settings: Settings) -> "S3TargetConfig":
        """The single bucket configured through MODEL_STORAGE_BACKEND=s3 and S3_*."""
        return cls(
            id=LEGACY_S3_TARGET_ID,
            name=f"S3 bucket {settings.s3_bucket}" if settings.s3_bucket else "S3 bucket",
            bucket=settings.s3_bucket or "",
            prefix=settings.s3_prefix,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            session_token=settings.s3_session_token,
            use_ssl=settings.s3_use_ssl,
            verify_ssl=settings.s3_verify_ssl,
            addressing_style=settings.s3_addressing_style,
            storage_class=settings.s3_storage_class,
            public_endpoint_url=settings.s3_public_endpoint_url,
            ca_bundle=settings.s3_ca_bundle,
            direct_downloads=settings.s3_direct_downloads,
            presign_ttl_seconds=settings.s3_presign_ttl_seconds,
            direct_uploads=settings.s3_direct_uploads,
            part_size_mb=settings.s3_part_size_mb,
            upload_presign_ttl_seconds=settings.s3_upload_presign_ttl_seconds,
        )

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (self.access_key_id, self.secret_access_key, self.session_token)
            if value
        )


def _env_value(item: dict[str, Any], key: str) -> str | None:
    name = item.get(key)
    if name is None:
        return None
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
        raise ValueError(f"{key} must be an environment variable name.")
    return os.getenv(name) or None


def _bounded(item: dict[str, Any], key: str, default: int, minimum: int, maximum: int, target_id: str) -> int:
    value = item.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"Storage target {target_id} {key} must be a whole number from {minimum} to {maximum}.")
    return value


def parse_storage_targets(raw: str) -> list[S3TargetConfig]:
    """Parse STORAGE_TARGETS_JSON. Credentials are referenced by env var name only."""
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError as error:
        raise ValueError("STORAGE_TARGETS_JSON must be a JSON list.") from error
    if not isinstance(items, list):
        raise ValueError("STORAGE_TARGETS_JSON must be a JSON list.")
    targets: list[S3TargetConfig] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each storage target must be a JSON object.")
        target_id = str(item.get("id") or "")
        if not TARGET_ID_PATTERN.fullmatch(target_id) or target_id == LOCAL_TARGET_ID:
            raise ValueError(
                f"Storage target id {target_id!r} must be 1-40 lowercase letters, digits, "
                "or hyphens, and cannot be 'local'."
            )
        if target_id in seen:
            raise ValueError(f"Storage target id {target_id!r} is used twice.")
        seen.add(target_id)
        if (item.get("kind") or "s3") != "s3":
            raise ValueError(f"Storage target {target_id} must have kind 's3'.")
        bucket = str(item.get("bucket") or "").strip()
        if not bucket:
            raise ValueError(f"Storage target {target_id} needs a bucket.")
        addressing_style = str(item.get("addressing_style") or "auto").lower()
        if addressing_style not in {"auto", "path", "virtual"}:
            raise ValueError(f"Storage target {target_id} addressing_style is invalid.")
        target = S3TargetConfig(
            id=target_id,
            name=str(item.get("name") or target_id)[:80],
            bucket=bucket,
            prefix=str(item.get("prefix", "models") or "").strip().strip("/"),
            endpoint_url=item.get("endpoint_url") or None,
            region=item.get("region") or None,
            access_key_id=_env_value(item, "access_key_env"),
            secret_access_key=_env_value(item, "secret_key_env"),
            session_token=_env_value(item, "session_token_env"),
            use_ssl=bool(item.get("use_ssl", True)),
            verify_ssl=bool(item.get("verify_ssl", True)),
            addressing_style=addressing_style,
            storage_class=item.get("storage_class") or None,
            public_endpoint_url=item.get("public_endpoint_url") or None,
            ca_bundle=item.get("ca_bundle") or None,
            direct_downloads=bool(item.get("direct_downloads", False)),
            presign_ttl_seconds=_bounded(item, "presign_ttl_seconds", 900, 60, 604800, target_id),
            direct_uploads=bool(item.get("direct_uploads", False)),
            part_size_mb=_bounded(item, "part_size_mb", 64, 5, 5120, target_id),
            upload_presign_ttl_seconds=_bounded(item, "upload_presign_ttl_seconds", 3600, 60, 604800, target_id),
        )
        if bool(target.access_key_id) != bool(target.secret_access_key):
            raise ValueError(
                f"Storage target {target_id} needs both access_key_env and secret_key_env values."
            )
        targets.append(target)
    return targets


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str) and value:
        return value
    return datetime.now(timezone.utc).isoformat()


def _safe_relative_key(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("S3 object key escapes the repository cache.")
    return path


def _public_endpoint(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if parsed.port:
            hostname = f"{hostname}:{parsed.port}"
        return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))
    except ValueError:
        return "[invalid endpoint]"


def repository_files(root: Path) -> list[tuple[Path, str]]:
    """The files that make up a repository folder, as S3 syncs and moves copy them:
    no symbolic links, partial downloads, caches, or hidden folders."""
    files: list[tuple[Path, str]] = []
    for current, directories, names in os.walk(root):
        directories[:] = [
            name
            for name in directories
            if name not in {".cache", "__pycache__"} and not name.startswith(".")
        ]
        for name in names:
            path = Path(current) / name
            if path.is_symlink() or any(name.endswith(suffix) for suffix in PART_SUFFIXES):
                continue
            files.append((path, path.relative_to(root).as_posix()))
    return files


class FilesystemModelStorage:
    # Only buckets can hand clients signed links to their objects.
    direct_downloads = False
    direct_uploads = False
    backend = "filesystem"
    remote = False
    id = LOCAL_TARGET_ID
    name = "Local disk"

    def __init__(self, settings: Settings):
        self.settings = settings

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.backend,
            "bucket": None,
            "prefix": None,
            "endpoint": None,
            "region": None,
            "path": str(self.settings.model_storage),
        }

    def redact(self, text: str) -> str:
        return text

    def health(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "enabled": False,
            "connected": True,
            "bucket": None,
            "prefix": None,
            "endpoint": None,
            "error": None,
        }

    def sync_repository(
        self, repo_id: str, root: Path, changed: set[str] | None = None
    ) -> str | None:
        return None

    def apply_changes(
        self,
        repo_id: str,
        files: dict[str, Path],
        deletions: set[str],
        manifest: dict[str, Any],
    ) -> None:
        raise ValueError("Filesystem repositories are changed in place.")

    def delete_repository(self, repo_id: str) -> None:
        return None

    def discover_repositories(self) -> list[dict[str, Any]]:
        return []

    def repository_manifest(self, repo_id: str, *, strict: bool = False) -> dict[str, Any] | None:
        return None

    def list_repository_files(
        self, repo_id: str, limit: int = 500
    ) -> dict[str, Any] | None:
        return None

    def restore_repository(self, repo_id: str) -> Path:
        raise ValueError("This model is not stored in S3.")

    def read_repository_file(
        self,
        repo_id: str,
        relative_path: str,
        start: int = 0,
        end: int | None = None,
        max_bytes: int = 1_000_000,
    ) -> tuple[bytes, int] | None:
        return None

    def list_repository_entries(
        self, repo_id: str, *, interactive: bool = False
    ) -> list[dict[str, Any]] | None:
        return None

    def stat_repository_file(self, repo_id: str, relative_path: str) -> dict[str, Any] | None:
        return None

    def iter_repository_file(
        self,
        repo_id: str,
        relative_path: str,
        start: int,
        end: int,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> Iterable[bytes]:
        raise FileNotFoundError("This model is not stored in S3.")

    def evict_repository_cache(self, repo_id: str) -> None:
        raise ValueError("Filesystem models do not have a separate durable copy.")


class S3ModelStorage(FilesystemModelStorage):
    backend = "s3"
    remote = True

    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        transfer_config: Any | None = None,
        target: S3TargetConfig | None = None,
    ):
        super().__init__(settings)
        target = target or S3TargetConfig.from_settings(settings)
        if not target.bucket:
            raise ValueError("S3_BUCKET is required when MODEL_STORAGE_BACKEND=s3.")
        if target.addressing_style not in {"auto", "path", "virtual"}:
            raise ValueError("S3_ADDRESSING_STYLE must be auto, path, or virtual.")
        if bool(target.access_key_id) != bool(target.secret_access_key):
            raise ValueError(
                "S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be configured together."
            )
        self.target = target
        self.id = target.id
        self.name = target.name
        self.bucket = target.bucket
        self.prefix = target.prefix
        self.endpoint = target.endpoint_url
        self._lock = threading.RLock()
        if client is None:
            try:
                import boto3
                from boto3.s3.transfer import TransferConfig
                from botocore.config import Config
            except ImportError as error:
                raise RuntimeError(
                    "S3 storage requires boto3. Install backend requirements first."
                ) from error
            verify: bool | str = target.verify_ssl
            if target.ca_bundle:
                if not Path(target.ca_bundle).is_file():
                    raise ValueError(
                        f"The CA bundle {target.ca_bundle} for storage target {target.id} does not exist. "
                        "Mount the root CA's PEM file there or remove ca_bundle."
                    )
                verify = target.ca_bundle
            client_options: dict[str, Any] = {
                "service_name": "s3",
                "use_ssl": target.use_ssl,
                "verify": verify,
                "config": Config(
                    connect_timeout=3,
                    read_timeout=10,
                    retries={"max_attempts": 5, "mode": "standard"},
                    s3={"addressing_style": target.addressing_style},
                ),
            }
            if target.endpoint_url:
                client_options["endpoint_url"] = target.endpoint_url
            if target.region:
                client_options["region_name"] = target.region
            if target.access_key_id:
                client_options["aws_access_key_id"] = target.access_key_id
            if target.secret_access_key:
                client_options["aws_secret_access_key"] = target.secret_access_key
            if target.session_token:
                client_options["aws_session_token"] = target.session_token
            client = boto3.client(**client_options)
            # Status checks fail fast so the Storage page stays responsive when a
            # bucket is unreachable; transfers keep the patient retrying client.
            self.health_client = boto3.client(
                **{
                    **client_options,
                    "config": Config(
                        connect_timeout=2,
                        read_timeout=5,
                        retries={"max_attempts": 1, "mode": "standard"},
                        s3={"addressing_style": target.addressing_style},
                    ),
                }
            )
            # Listings, metadata and small reads that someone is waiting on give up
            # after one retry, so a bucket outage answers in seconds, not minutes.
            self.read_client = boto3.client(
                **{
                    **client_options,
                    "config": Config(
                        connect_timeout=3,
                        read_timeout=5,
                        retries={"total_max_attempts": 2, "mode": "standard"},
                        s3={"addressing_style": target.addressing_style},
                    ),
                }
            )
            # Signing happens offline, for the address clients use, which can differ
            # from the one this server reaches the bucket at.
            self.signing_client = boto3.client(
                **{
                    **client_options,
                    "endpoint_url": target.public_endpoint_url or target.endpoint_url,
                    "region_name": target.region or "us-east-1",
                    "config": Config(
                        signature_version="s3v4",
                        s3={"addressing_style": target.addressing_style},
                    ),
                }
            )
            chunk_bytes = settings.s3_multipart_chunk_mb * 1024**2
            transfer_config = TransferConfig(
                multipart_threshold=chunk_bytes,
                multipart_chunksize=chunk_bytes,
                max_concurrency=settings.s3_max_concurrency,
                use_threads=True,
            )
        self.client = client
        if not hasattr(self, "health_client"):
            self.health_client = client
        if not hasattr(self, "read_client"):
            self.read_client = client
        if not hasattr(self, "signing_client"):
            self.signing_client = client
        self.transfer_config = transfer_config
        self.direct_downloads = target.direct_downloads
        self.direct_uploads = target.direct_uploads

    def _prefix(self, value: str = "") -> str:
        parts = [part for part in (self.prefix, value.strip("/")) if part]
        return "/".join(parts)

    def _repo_prefix(self, repo_id: str) -> str:
        return f"{self._prefix(validate_repo_id(repo_id))}/"

    def _manifest_key(self, repo_id: str) -> str:
        return f"{self._repo_prefix(repo_id)}{MANIFEST_NAME}"

    def unreachable_message(self) -> str:
        return f"The storage location {self.name} is not reachable. Try again in a moment."

    def remote_uri(self, repo_id: str) -> str:
        return f"s3://{self.bucket}/{self._prefix(validate_repo_id(repo_id))}"

    def _objects(self, prefix: str, client: Any | None = None) -> Iterable[dict[str, Any]]:
        paginator = (client or self.client).get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            yield from page.get("Contents") or []

    def _delete_keys(self, keys: Iterable[str]) -> None:
        batch: list[dict[str, str]] = []
        for key in keys:
            batch.append({"Key": key})
            if len(batch) == 1000:
                self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": batch, "Quiet": True},
                )
                batch = []
        if batch:
            self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": batch, "Quiet": True},
            )

    def _pending_key(self, repo_id: str) -> str:
        return f"{self._repo_prefix(repo_id)}{PENDING_NAME}"

    def _pending(self, repo_id: str) -> dict[str, Any] | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._pending_key(repo_id))
            body = response["Body"]
            try:
                record = json.loads(body.read().decode("utf-8"))
            finally:
                close = getattr(body, "close", None)
                if close:
                    close()
        except ValueError:
            return None
        except Exception as error:
            if _missing_object(error):
                return None
            raise
        return record if isinstance(record, dict) else None

    def _unpublished(self, repo_id: str, manifest: dict[str, Any] | None) -> set[str]:
        """Paths a change wrote without publishing its manifest. A record naming the
        change the manifest now carries was published, so none of it counts."""
        record = self._pending(repo_id)
        if not record or record.get("change") == (manifest or {}).get("change"):
            return set()
        files = record.get("files")
        return {item for item in files if isinstance(item, str)} if isinstance(files, list) else set()

    def _begin_change(
        self, repo_id: str, writing: set[str], manifest: dict[str, Any] | None = None
    ) -> tuple[str, set[str], bool]:
        """Record the new objects a change is about to write, before writing any.
        Returns the change's id, the leftovers of earlier failed attempts, and
        whether a record now exists."""
        prefix = self._repo_prefix(repo_id)
        present = {
            relative
            for item in self._objects(prefix)
            if not in_change_area(relative := (item.get("Key") or "")[len(prefix) :])
        }
        unpublished: set[str] = set()
        if PENDING_NAME in present:
            unpublished = self._unpublished(
                repo_id, manifest if manifest is not None else self.repository_manifest(repo_id, strict=True)
            )
        change = uuid4().hex
        new = (writing - (present - unpublished)) | unpublished
        if new:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._pending_key(repo_id),
                Body=json.dumps({"change": change, "files": sorted(new)}).encode("utf-8"),
            )
        return change, unpublished, bool(new) or PENDING_NAME in present

    def _finish_change(self, repo_id: str, remove: set[str], recorded: bool) -> None:
        """After a change is published, remove the objects it replaced: deleted
        files and leftovers of failed attempts. They are recorded as pending first,
        so if removing them fails, they still stay out of the repository."""
        prefix = self._repo_prefix(repo_id)
        remove = remove - {MANIFEST_NAME, PENDING_NAME}
        try:
            if remove:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=self._pending_key(repo_id),
                    Body=json.dumps({"change": uuid4().hex, "files": sorted(remove)}).encode("utf-8"),
                )
                self._delete_keys(f"{prefix}{relative}" for relative in sorted(remove))
            if remove or recorded:
                self._delete_keys([self._pending_key(repo_id)])
        except Exception as error:
            logger.warning(
                "Could not remove old objects of %s from %s: %s",
                repo_id, self.id, self.redact(str(error) or error.__class__.__name__),
            )

    def _repository_objects(
        self, repo_id: str, manifest: dict[str, Any] | None = None, client: Any | None = None
    ) -> list[dict[str, Any]]:
        """A repository's objects, without those of a change that was never published."""
        prefix = self._repo_prefix(repo_id)
        objects = list(self._objects(prefix, client))
        excluded: set[str] = set()
        if any(item.get("Key") == f"{prefix}{PENDING_NAME}" for item in objects):
            excluded = self._unpublished(
                repo_id, manifest if manifest is not None else self.repository_manifest(repo_id)
            )
        excluded.add(PENDING_NAME)
        return [
            item
            for item in objects
            if (relative := (item.get("Key") or "")[len(prefix) :]) not in excluded
            and not in_change_area(relative)
        ]

    def _transfer_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.transfer_config is not None:
            options["Config"] = self.transfer_config
        return options

    def _upload_options(self) -> dict[str, Any]:
        options = self._transfer_options()
        if self.target.storage_class:
            options["ExtraArgs"] = {"StorageClass": self.target.storage_class}
        return options

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.backend,
            "bucket": self.bucket,
            "prefix": self.prefix,
            "endpoint": _public_endpoint(self.endpoint),
            "region": self.target.region,
            "path": None,
        }

    def redact(self, text: str) -> str:
        """An error message without this target's credentials or link signatures."""
        for secret in self.target.secrets:
            text = text.replace(secret, "[redacted]")
        return SIGNED_QUERY.sub(r"\1[redacted]", text)

    def _presign(self, operation: str, params: dict[str, Any], ttl: int) -> str:
        return self.signing_client.generate_presigned_url(
            operation, Params={"Bucket": self.bucket, **params}, ExpiresIn=ttl
        )

    def presigned_get(
        self, repo_id: str, relative_path: str, *, filename: str | None = None, ttl: int | None = None
    ) -> str:
        """A short-lived link that lets whoever holds it GET one file of a repository,
        with Range requests. With `filename`, browsers save it under that name."""
        params: dict[str, Any] = {"Key": self._object_key(validate_repo_id(repo_id), relative_path)}
        if filename:
            params["ResponseContentDisposition"] = content_disposition(filename)
        return self._presign("get_object", params, ttl or self.target.presign_ttl_seconds)

    def describe_error(self, error: Exception) -> str:
        """A sentence for people about a failed bucket request. botocore's own text
        carries request URLs with query strings, so it only goes to the log."""
        where = f"s3://{self.bucket}"
        endpoint = _public_endpoint(self.endpoint)
        if endpoint:
            where = f"{where} at {endpoint}"
        code = _error_code(error)
        if code == "NoSuchBucket":
            return f"The bucket {self.bucket} does not exist. Check the bucket name."
        if code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "403"}:
            return f"{where} refused the request ({code}). Check the credentials and the bucket policy."
        if code:
            return f"{where} answered with an error ({code})."
        if NoCredentialsError is not None and isinstance(error, NoCredentialsError):
            return f"No credentials are configured for {where}."
        if BotoCoreError is not None and isinstance(error, BotoCoreError):
            return f"Cannot reach {where}. Check the endpoint and that the storage server is running."
        return self.redact(str(error).strip() or f"Could not read {where} ({error.__class__.__name__}).")

    def health(self) -> dict[str, Any]:
        error = None
        connected = False
        try:
            self.health_client.list_objects_v2(
                Bucket=self.bucket,
                Prefix=self._prefix(),
                MaxKeys=1,
            )
            connected = True
        except Exception as exception:
            logger.info("Storage %s health check failed: %s", self.id, self.redact(str(exception)))
            error = self.describe_error(exception)[:300]
        return {
            "backend": self.backend,
            "enabled": True,
            "connected": connected,
            "bucket": self.bucket,
            "prefix": self.prefix,
            "endpoint": _public_endpoint(self.endpoint),
            "error": error,
        }

    def _local_files(self, root: Path) -> list[tuple[Path, str]]:
        return repository_files(root)

    def _describe(self, repo_id: str, root: Path, manifest: dict[str, Any]) -> None:
        """Fill a manifest with where the repository lives and what the files in
        `root` say about it: config, task, license, size, precision."""
        config_path = root / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                config = {}
        except (OSError, json.JSONDecodeError):
            config = {}
        manifest["storage_backend"] = "s3"
        manifest["storage_target"] = self.id
        manifest["remote_uri"] = self.remote_uri(repo_id)
        manifest["config"] = {
            "architectures": config.get("architectures"),
            "model_type": config.get("model_type"),
            "torch_dtype": config.get("torch_dtype"),
            "vocab_size": config.get("vocab_size"),
        }
        facts = repository_facts(root, manifest)
        manifest.update({key: facts[key] for key in ("parameter_count", "formats", "precision")})
        # The card's task, license, library, and tags, so a rescan of the bucket lists
        # the model as the local copy did; the manifest's own (from the Hub) win.
        manifest.update(
            {key: facts[key] for key in ("pipeline_tag", "library_name", "license", "tags") if facts[key]}
        )
        # What the model derives from comes from its card; a manifest that already
        # names one (from the Hub) keeps it.
        if not manifest.get("base_model") and facts.get("base_model"):
            manifest["base_model"] = facts["base_model"]
            manifest["base_model_relation"] = facts["base_model_relation"]

    def sync_repository(
        self, repo_id: str, root: Path, changed: set[str] | None = None
    ) -> str:
        """Upload a complete repository, publishing its manifest after its files.

        With `changed`, only those paths are uploaded; other objects keep their
        timestamps so commit history does not see untouched files as modified.
        The manifest is never removed first, so a failed sync leaves the previous
        version listed; objects no longer in the folder are removed last.
        """
        validated = validate_repo_id(repo_id)
        expected_root = repository_path(validated, self.settings.model_storage)
        if root.resolve() != expected_root:
            raise ValueError("Repository cache path does not match its repository ID.")
        manifest_path = root / MANIFEST_NAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("A complete repository manifest is required for S3 sync.") from error
        if manifest.get("status") != "complete" or manifest.get("repo_id") != validated:
            raise ValueError("Only complete repositories can be synced to S3.")

        self._describe(validated, root, manifest)
        with self._lock:
            repo_prefix = self._repo_prefix(validated)
            manifest_key = self._manifest_key(validated)
            local_files = self._local_files(root)
            intended = {relative for _, relative in local_files}
            change, _, recorded = self._begin_change(
                validated,
                {relative for relative in intended if changed is None or relative in changed} - {MANIFEST_NAME},
            )
            manifest["change"] = change
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            for path, relative in local_files:
                if relative == MANIFEST_NAME:
                    continue
                if changed is not None and relative not in changed:
                    continue
                self.client.upload_file(
                    str(path),
                    self.bucket,
                    f"{repo_prefix}{relative}",
                    **self._upload_options(),
                )
            self.client.upload_file(
                str(manifest_path),
                self.bucket,
                manifest_key,
                **self._upload_options(),
            )
            # The new version is published; leftovers only cost space, and the
            # next sync of this repository removes whatever this one could not.
            # Change sessions upload into the change area meanwhile: never theirs to remove.
            try:
                existing = {
                    relative
                    for item in self._objects(repo_prefix)
                    if not in_change_area(relative := (item.get("Key") or "")[len(repo_prefix) :])
                }
            except Exception as error:
                existing = set()
                logger.warning(
                    "Could not list old objects of %s in %s: %s",
                    validated, self.id, self.redact(str(error) or error.__class__.__name__),
                )
            self._finish_change(validated, existing - intended, recorded)
        return self.remote_uri(validated)

    def apply_changes(
        self,
        repo_id: str,
        files: dict[str, Path],
        deletions: set[str],
        manifest: dict[str, Any],
    ) -> None:
        """Change a repository that has no local cache, directly in the bucket.

        Files are uploaded, then the manifest, then deletions made; the manifest is
        never removed, so a failure part-way leaves the repository listed and the
        same change can simply be applied again. New files are recorded as pending
        first, so a failure never adds them to the repository at the next scan.
        """
        validated = validate_repo_id(repo_id)
        repo_prefix = self._repo_prefix(validated)
        with self._lock:
            uploads = {_safe_relative_key(relative).as_posix(): path for relative, path in files.items()}
            change, leftovers, recorded = self._begin_change(validated, set(uploads), manifest)
            for relative, path in uploads.items():
                self.client.upload_file(str(path), self.bucket, f"{repo_prefix}{relative}", **self._upload_options())
            manifest = {
                **manifest,
                "storage_backend": "s3",
                "storage_target": self.id,
                "remote_uri": self.remote_uri(validated),
                "change": change,
            }
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._manifest_key(validated),
                Body=json.dumps(manifest, indent=2).encode("utf-8"),
            )
            removed = {_safe_relative_key(relative).as_posix() for relative in deletions}
            self._finish_change(validated, removed | (leftovers - set(uploads)), recorded)

    def delete_repository(self, repo_id: str) -> None:
        with self._lock:
            keys = [item["Key"] for item in self._objects(self._repo_prefix(repo_id))]
            self._delete_keys(keys)

    def repository_manifest(self, repo_id: str, *, strict: bool = False) -> dict[str, Any] | None:
        """The repository's manifest, or None without one. Normally any failure also
        reads as None; with `strict`, only a manifest that does not exist does, and
        an unreachable bucket or unreadable manifest raises, for callers that would
        otherwise write a new manifest over the real one."""
        try:
            response = self.client.get_object(
                Bucket=self.bucket,
                Key=self._manifest_key(repo_id),
            )
            body = response["Body"]
            payload = body.read()
            close = getattr(body, "close", None)
            if close:
                close()
        except Exception as error:
            if strict and not _missing_object(error):
                raise
            return None
        try:
            manifest = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            if strict:
                raise ValueError("The repository's manifest in object storage is unreadable.") from error
            return None
        if not isinstance(manifest, dict):
            if strict:
                raise ValueError("The repository's manifest in object storage is unreadable.")
            return None
        return manifest

    def discover_repositories(self) -> list[dict[str, Any]]:
        root_prefix = f"{self._prefix()}/" if self._prefix() else ""
        records: list[dict[str, Any]] = []
        manifests: list[tuple[str, dict[str, Any]]] = []
        repository_keys: dict[str, list[str]] = {}
        repository_entries: dict[str, list[dict[str, Any]]] = {}
        pending: set[str] = set()
        for item in self._objects(root_prefix):
            key = item.get("Key") or ""
            relative_key = key[len(root_prefix) :] if root_prefix else key
            parts = PurePosixPath(relative_key).parts
            if len(parts) < 3:
                continue
            repo_id = f"{parts[0]}/{parts[1]}"
            if parts[2] == CHANGES_DIRECTORY:
                continue
            if len(parts) == 3 and parts[-1] == MANIFEST_NAME:
                manifests.append((repo_id, item))
            elif len(parts) == 3 and parts[-1] == PENDING_NAME:
                pending.add(repo_id)
            else:
                repository_keys.setdefault(repo_id, []).append(parts[-1])
                repository_entries.setdefault(repo_id, []).append(
                    {
                        "path": "/".join(parts[2:]),
                        "size": int(item.get("Size") or 0),
                        "version": _iso(item.get("LastModified")),
                    }
                )
        for repo_id, item in manifests:
            try:
                validate_repo_id(repo_id)
            except ValueError:
                continue
            manifest = self.repository_manifest(repo_id)
            if (
                not manifest
                or manifest.get("status") != "complete"
                or manifest.get("repo_id") != repo_id
            ):
                continue
            if repo_id in pending:
                unpublished = self._unpublished(repo_id, manifest)
                repository_entries[repo_id] = [
                    entry for entry in repository_entries.get(repo_id, []) if entry["path"] not in unpublished
                ]
                repository_keys[repo_id] = [
                    PurePosixPath(entry["path"]).name for entry in repository_entries[repo_id]
                ]
            cache_root = repository_path(repo_id, self.settings.model_storage)
            cached_manifest = cache_root / MANIFEST_NAME
            cached = False
            if cached_manifest.is_file():
                try:
                    local_manifest = json.loads(cached_manifest.read_text(encoding="utf-8"))
                    cached = (
                        local_manifest.get("status") == "complete"
                        and manifest_target(local_manifest) == self.id
                    )
                except (OSError, json.JSONDecodeError):
                    cached = False
            records.append(
                {
                    "repo_id": repo_id,
                    "relative_path": repo_id,
                    "size_bytes": int(manifest.get("total_bytes") or 0),
                    # Counted from the objects, as the file list shows them, so a
                    # manifest written before hidden files were left out is corrected.
                    "file_count": sum(
                        1 for entry in repository_entries.get(repo_id, []) if not hidden_path(entry["path"])
                    ),
                    "modified_at": (
                        manifest.get("uploaded_at")
                        or manifest.get("downloaded_at")
                        or _iso(item.get("LastModified"))
                    ),
                    "downloaded_at": manifest.get("downloaded_at"),
                    "revision": manifest.get("revision"),
                    "sha": manifest.get("sha"),
                    "pipeline_tag": manifest.get("pipeline_tag"),
                    "base_model": manifest.get("base_model"),
                    "base_model_relation": manifest.get("base_model_relation"),
                    "library_name": manifest.get("library_name"),
                    "license": manifest.get("license"),
                    "tags": manifest.get("tags") or [],
                    "config": manifest.get("config") or {},
                    "source_url": manifest.get("source_url"),
                    "source": manifest.get("source"),
                    "owner_id": manifest.get("owner_id"),
                    "organization_id": manifest.get("organization_id"),
                    "managed": True,
                    "storage_backend": "s3",
                    "storage_target": self.id,
                    "cached": cached,
                    "remote_uri": self.remote_uri(repo_id),
                    "parameter_count": manifest.get("parameter_count"),
                    "precision": manifest.get("precision"),
                    "formats": model_formats(repository_keys.get(repo_id, [])),
                    "entries": repository_entries.get(repo_id, []),
                }
            )
        return records

    def list_repository_files(
        self, repo_id: str, limit: int = 500
    ) -> dict[str, Any] | None:
        prefix = self._repo_prefix(repo_id)
        files: list[dict[str, Any]] = []
        unsafe_count = 0
        for item in self._repository_objects(repo_id, client=self.read_client):
            key = item.get("Key") or ""
            relative = key[len(prefix) :]
            if not relative:
                continue
            _safe_relative_key(relative)
            unsafe = PurePosixPath(relative).suffix.lower() in UNSAFE_EXTENSIONS
            unsafe_count += int(unsafe)
            files.append(
                {
                    "path": relative,
                    "size": int(item.get("Size") or 0),
                    "modified_at": _iso(item.get("LastModified")),
                    "unsafe_serialization": unsafe,
                }
            )
            if len(files) >= limit:
                break
        if not files:
            return None
        files.sort(key=lambda file: (-file["size"], file["path"]))
        return {
            "files": files,
            "unsafe_file_count": unsafe_count,
            "truncated": len(files) >= limit,
        }

    def restore_repository(self, repo_id: str) -> Path:
        validated = validate_repo_id(repo_id)
        manifest = self.repository_manifest(validated)
        if not manifest or manifest.get("status") != "complete":
            raise FileNotFoundError("A complete S3 copy of this repository was not found.")
        prefix = self._repo_prefix(validated)
        objects = self._repository_objects(validated, manifest)
        if not objects:
            raise FileNotFoundError("The S3 repository is empty.")
        root = repository_path(validated, self.settings.model_storage)
        with self._lock:
            root.mkdir(parents=True, exist_ok=True)
            ordered = sorted(
                objects,
                key=lambda item: (item.get("Key", "").endswith(f"/{MANIFEST_NAME}"), item.get("Key", "")),
            )
            for item in ordered:
                key = item.get("Key") or ""
                relative_value = key[len(prefix) :]
                if not relative_value:
                    continue
                relative = _safe_relative_key(relative_value)
                target = root.joinpath(*relative.parts)
                resolved_parent = target.parent.resolve()
                if root != resolved_parent and root not in resolved_parent.parents:
                    raise ValueError("S3 object key escapes the repository cache.")
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.with_name(f".{target.name}.hugginghack-s3-part")
                if target.is_symlink() or partial.is_symlink():
                    raise ValueError("S3 restore cannot overwrite a symbolic link.")
                self.client.download_file(
                    self.bucket,
                    key,
                    str(partial),
                    **self._transfer_options(),
                )
                partial.replace(target)
            # The local copy belongs to this target even if the bucket manifest
            # predates storage targets.
            manifest_path = root / MANIFEST_NAME
            try:
                restored = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                restored = dict(manifest)
            restored.update({"storage_backend": "s3", "storage_target": self.id})
            manifest_path.write_text(json.dumps(restored, indent=2), encoding="utf-8")
        return root

    def read_repository_file(
        self,
        repo_id: str,
        relative_path: str,
        start: int = 0,
        end: int | None = None,
        max_bytes: int = 1_000_000,
    ) -> tuple[bytes, int] | None:
        """Read a bounded byte range of one repository object.

        Returns the bytes and the object's total size, or None when it is missing.
        """
        relative = _safe_relative_key(relative_path)
        if relative.name == MANIFEST_NAME:
            return None
        key = f"{self._repo_prefix(repo_id)}{relative.as_posix()}"
        try:
            head = self.read_client.head_object(Bucket=self.bucket, Key=key)
        except Exception as error:
            if _missing_object(error):
                return None
            raise StorageUnavailableError(self.unreachable_message()) from error
        total = int(head.get("ContentLength") or 0)
        if start >= total:
            return b"", total
        last = min(total - 1, end if end is not None else total - 1)
        if last - start + 1 > max_bytes:
            raise ValueError("The requested file is too large to read.")
        response = self.read_client.get_object(
            Bucket=self.bucket,
            Key=key,
            Range=f"bytes={start}-{last}",
        )
        body = response["Body"]
        try:
            payload = body.read(max_bytes + 1)
        finally:
            close = getattr(body, "close", None)
            if close:
                close()
        if len(payload) > max_bytes:
            raise ValueError("The requested file is too large to read.")
        return payload, total

    def list_repository_entries(
        self, repo_id: str, *, interactive: bool = False
    ) -> list[dict[str, Any]] | None:
        """List every object of a repository, without the browsing limit. An
        interactive listing, for someone waiting on it, gives up sooner."""
        prefix = self._repo_prefix(repo_id)
        entries: list[dict[str, Any]] = []
        for item in self._repository_objects(
            repo_id, client=self.read_client if interactive else None
        ):
            relative = (item.get("Key") or "")[len(prefix) :]
            if not relative:
                continue
            _safe_relative_key(relative)
            entries.append(
                {
                    "path": relative,
                    "size": int(item.get("Size") or 0),
                    "version": _iso(item.get("LastModified")),
                }
            )
        return entries or None

    def _object_key(self, repo_id: str, relative_path: str) -> str:
        relative = _safe_relative_key(relative_path)
        return f"{self._repo_prefix(repo_id)}{relative.as_posix()}"

    def stat_repository_file(self, repo_id: str, relative_path: str) -> dict[str, Any] | None:
        try:
            head = self.read_client.head_object(
                Bucket=self.bucket, Key=self._object_key(repo_id, relative_path)
            )
        except Exception as error:
            if _missing_object(error):
                return None
            raise StorageUnavailableError(self.unreachable_message()) from error
        return {
            "size": int(head.get("ContentLength") or 0),
            "version": _iso(head.get("LastModified")),
        }

    def iter_repository_file(
        self,
        repo_id: str,
        relative_path: str,
        start: int,
        end: int,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> Iterable[bytes]:
        """Stream an inclusive byte range of one object in bounded ranged requests."""
        key = self._object_key(repo_id, relative_path)
        position = start
        while position <= end:
            last = min(end, position + chunk_size - 1)
            response = self.client.get_object(
                Bucket=self.bucket, Key=key, Range=f"bytes={position}-{last}"
            )
            body = response["Body"]
            try:
                payload = body.read()
            finally:
                close = getattr(body, "close", None)
                if close:
                    close()
            if not payload:
                return
            yield payload
            position += len(payload)

    # Uploads straight from the browser (direct_uploads): each file is one multipart
    # upload whose parts the browser PUTs to signed links, so no byte passes through
    # this server. A new repository's files go to their final keys, and nothing lists
    # them until its manifest is published; a change to a published repository goes
    # to its change area and is copied into place, inside the bucket, on commit.

    def change_key(self, repo_id: str, session_id: str, relative_path: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise ValueError("Change session not found.")
        relative = _safe_relative_key(relative_path)
        return f"{self._repo_prefix(validate_repo_id(repo_id))}{CHANGES_DIRECTORY}/{session_id}/{relative.as_posix()}"

    def file_key(self, repo_id: str, relative_path: str) -> str:
        return self._object_key(validate_repo_id(repo_id), relative_path)

    def start_multipart(self, key: str) -> str:
        options: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if self.target.storage_class:
            options["StorageClass"] = self.target.storage_class
        return self.client.create_multipart_upload(**options)["UploadId"]

    def presigned_part(self, key: str, upload_id: str, number: int) -> str:
        return self._presign(
            "upload_part",
            {"Key": key, "UploadId": upload_id, "PartNumber": number},
            self.target.upload_presign_ttl_seconds,
        )

    def uploaded_parts(self, key: str, upload_id: str) -> list[dict[str, Any]]:
        """The parts the bucket has for a multipart upload, from the bucket itself, so
        a browser that reloaded carries on from what really arrived."""
        parts: list[dict[str, Any]] = []
        marker = 0
        while True:
            response = self.read_client.list_parts(
                Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumberMarker=marker, MaxParts=1000
            )
            parts.extend(
                {"number": int(item["PartNumber"]), "size": int(item.get("Size") or 0), "etag": item.get("ETag")}
                for item in response.get("Parts") or []
            )
            if not response.get("IsTruncated"):
                return parts
            marker = int(response.get("NextPartNumberMarker") or 0)

    def complete_multipart(self, key: str, upload_id: str, parts: list[dict[str, Any]]) -> None:
        self.client.complete_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": [{"PartNumber": part["number"], "ETag": part["etag"]} for part in parts]},
        )

    def abort_multipart(self, key: str, upload_id: str) -> None:
        """Give up an unfinished upload so its parts stop taking space. Best effort:
        one the bucket already forgot is fine."""
        try:
            self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
        except Exception as error:
            if _error_code(error) not in {"NoSuchUpload", "404"}:
                logger.warning(
                    "Could not abort an upload in %s: %s", self.id, self.redact(str(error) or error.__class__.__name__)
                )

    def put_empty(self, key: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=b"")

    def object_size(self, key: str) -> int | None:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as error:
            if _missing_object(error):
                return None
            raise
        return int(head.get("ContentLength") or 0)

    def delete_change_area(self, repo_id: str, session_id: str) -> None:
        prefix = self.change_key(repo_id, session_id, "x")[: -len("x")]
        try:
            self._delete_keys(item["Key"] for item in self._objects(prefix))
        except Exception as error:
            logger.warning(
                "Could not remove an upload area of %s in %s: %s",
                repo_id, self.id, self.redact(str(error) or error.__class__.__name__),
            )

    def publish_uploaded_repository(self, repo_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """Publish a new repository whose files were uploaded straight to their keys:
        read what the files say about the model, then write the manifest, which is
        what makes the repository exist. Returns the published manifest."""
        validated = validate_repo_id(repo_id)
        with self._lock, tempfile.TemporaryDirectory(prefix="hugginghack-sketch-") as workspace:
            root = Path(workspace) / "repository"
            self._sketch(validated, root)
            manifest = {**manifest}
            self._describe(validated, root, manifest)
            manifest["change"] = uuid4().hex
            self.publish_manifest(validated, manifest)
        return manifest

    def describe_published(self, repo_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """Describe a published repository again from its files in the bucket, after a
        change replaced or removed some, and publish the manifest with what they say
        now. An upload's facts come from its files alone: a card that was deleted
        takes its license and task with it."""
        validated = validate_repo_id(repo_id)
        described = {
            key: value
            for key, value in manifest.items()
            if manifest.get("source") != "user-upload" or key not in DESCRIBED_KEYS
        }
        with self._lock, tempfile.TemporaryDirectory(prefix="hugginghack-sketch-") as workspace:
            root = Path(workspace) / "repository"
            self._sketch(validated, root)
            self._describe(validated, root, described)
            self.publish_manifest(validated, described)
        return described

    def _sketch(self, repo_id: str, root: Path) -> None:
        """A local stand-in for a bucket repository, enough for repository_facts:
        every file by name, the small metadata files whole, and only the headers of
        weight files, fetched by range."""
        prefix = self._repo_prefix(repo_id)
        sizes: dict[str, int] = {}
        for item in self._repository_objects(repo_id):
            relative = (item.get("Key") or "")[len(prefix) :]
            if not relative or relative in {MANIFEST_NAME, PENDING_NAME}:
                continue
            sizes[_safe_relative_key(relative).as_posix()] = int(item.get("Size") or 0)
        root.mkdir(parents=True)
        for relative in sizes:
            target = root.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        for relative, size in sizes.items():
            name = relative.lower()
            key = f"{prefix}{relative}"
            target = root.joinpath(*PurePosixPath(relative).parts)
            if relative in SKETCH_WHOLE_FILES and size <= SKETCH_WHOLE_MAX_BYTES:
                target.write_bytes(self._range(key, 0, size - 1) if size else b"")
            elif name.endswith(".safetensors") and size >= 8:
                (length,) = struct.unpack("<Q", self._range(key, 0, 7))
                if 0 < length <= SAFETENSORS_MAX_HEADER_BYTES and 8 + length <= size:
                    target.write_bytes(self._range(key, 0, 7 + length))
            elif name.endswith(".gguf") and size:
                target.write_bytes(self._range(key, 0, min(size, SKETCH_GGUF_HEADER_BYTES) - 1))

    def _range(self, key: str, start: int, end: int) -> bytes:
        body = self.client.get_object(Bucket=self.bucket, Key=key, Range=f"bytes={start}-{end}")["Body"]
        try:
            return body.read()
        finally:
            close = getattr(body, "close", None)
            if close:
                close()

    def apply_uploaded_changes(
        self,
        repo_id: str,
        uploaded: dict[str, str],
        deletions: set[str],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Commit a change whose files the browser uploaded to the change area: copy
        them into place inside the bucket, publish the manifest, then make the
        deletions. The same order and pending record as apply_changes, so a failure
        part-way leaves the published version as it was."""
        validated = validate_repo_id(repo_id)
        repo_prefix = self._repo_prefix(validated)
        with self._lock:
            copies = {_safe_relative_key(relative).as_posix(): key for relative, key in uploaded.items()}
            change, leftovers, recorded = self._begin_change(validated, set(copies), manifest)
            for relative, source in copies.items():
                self.client.copy(
                    {"Bucket": self.bucket, "Key": source},
                    self.bucket,
                    f"{repo_prefix}{relative}",
                    **self._upload_options(),
                )
            manifest = {
                **manifest,
                "storage_backend": "s3",
                "storage_target": self.id,
                "remote_uri": self.remote_uri(validated),
                "change": change,
            }
            self.publish_manifest(validated, manifest)
            removed = {_safe_relative_key(relative).as_posix() for relative in deletions}
            self._finish_change(validated, removed | (leftovers - set(copies)), recorded)
        return manifest

    def download_files(self, repo_id: str, relatives: Iterable[str], root: Path) -> None:
        """Bring the local cache of a repository up to date with some of its files."""
        for relative in relatives:
            relative_path = _safe_relative_key(relative)
            target = root.joinpath(*relative_path.parts)
            resolved_parent = target.parent.resolve() if target.parent.exists() else target.parent
            if root != resolved_parent and root not in resolved_parent.parents:
                raise ValueError("S3 object key escapes the repository cache.")
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(f".{target.name}.hugginghack-s3-part")
            if target.is_symlink() or partial.is_symlink():
                raise ValueError("S3 restore cannot overwrite a symbolic link.")
            self.client.download_file(
                self.bucket, self._object_key(repo_id, relative_path.as_posix()), str(partial), **self._transfer_options()
            )
            partial.replace(target)

    # Object-level access for storage moves: the mover writes files one by one and
    # publishes the manifest last, so a half-copied repository is never discovered.
    def object_files(self, repo_id: str) -> list[tuple[str, int]]:
        """Every file of a repository in this bucket, except its manifest."""
        prefix = self._repo_prefix(repo_id)
        files = []
        for item in self._repository_objects(repo_id):
            relative = (item.get("Key") or "")[len(prefix) :]
            if relative and relative != MANIFEST_NAME:
                files.append((_safe_relative_key(relative).as_posix(), int(item.get("Size") or 0)))
        return files

    def has_objects(self, repo_id: str) -> bool:
        return any(True for _ in self._objects(self._repo_prefix(repo_id)))

    def write_object(self, repo_id: str, relative_path: str, stream: Any) -> None:
        self.client.upload_fileobj(
            stream, self.bucket, self._object_key(repo_id, relative_path), **self._upload_options()
        )

    def publish_manifest(self, repo_id: str, manifest: dict[str, Any]) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self._manifest_key(repo_id),
            Body=json.dumps(manifest, indent=2).encode("utf-8"),
        )

    def delete_manifest(self, repo_id: str) -> None:
        with self._lock:
            self._delete_keys([self._manifest_key(repo_id)])

    def evict_repository_cache(self, repo_id: str) -> None:
        with self._lock:
            manifest = self.repository_manifest(repo_id)
            if not manifest or manifest.get("status") != "complete":
                raise ValueError("The local cache cannot be removed until S3 has a complete copy.")
            root = repository_path(repo_id, self.settings.model_storage)
            if root.exists():
                shutil.rmtree(root)


class StorageRegistry:
    """Every configured storage target, in priority order.

    The local model folder is always present: it holds filesystem repositories and
    is the working cache for every S3 target. When the same repository exists in
    several targets, the earlier target wins.
    """

    def __init__(
        self,
        settings: Settings,
        remotes: list[S3ModelStorage] | None = None,
        default_target: str | None = None,
        local: FilesystemModelStorage | None = None,
    ):
        self.settings = settings
        self.local = local or FilesystemModelStorage(settings)
        self._targets: dict[str, FilesystemModelStorage] = {self.local.id: self.local}
        for remote in remotes or []:
            if remote.id in self._targets:
                raise ValueError(f"Storage target id {remote.id!r} is used twice.")
            self._targets[remote.id] = remote
        if default_target and default_target not in self._targets:
            raise ValueError(f"DEFAULT_STORAGE_TARGET {default_target!r} is not configured.")
        if default_target:
            self.default_id = default_target
        elif LEGACY_S3_TARGET_ID in self._targets:
            self.default_id = LEGACY_S3_TARGET_ID
        else:
            self.default_id = self.local.id

    @classmethod
    def wrap(cls, storage: "FilesystemModelStorage | StorageRegistry | None", settings: Settings) -> "StorageRegistry":
        """Accept a registry or one storage object (used by tests and simple setups)."""
        if isinstance(storage, StorageRegistry):
            return storage
        if isinstance(storage, S3ModelStorage):
            return cls(settings, [storage])
        return cls(settings, local=storage)

    @property
    def default(self) -> FilesystemModelStorage:
        return self._targets[self.default_id]

    @property
    def remotes(self) -> list[S3ModelStorage]:
        return [target for target in self._targets.values() if target.remote]  # type: ignore[misc]

    def all(self) -> list[FilesystemModelStorage]:
        return list(self._targets.values())

    def ids(self) -> list[str]:
        return list(self._targets)

    def get(self, target_id: str | None) -> FilesystemModelStorage:
        if not target_id:
            return self.default
        try:
            return self._targets[target_id]
        except KeyError as error:
            raise ValueError(f"Storage target {target_id!r} is not configured.") from error

    def for_model(self, model: dict[str, Any]) -> FilesystemModelStorage:
        target = model.get("storage_target")
        if target in self._targets:
            return self._targets[target]
        if model.get("storage_backend") == "s3" and self.remotes:
            return self.remotes[0]
        return self.local

    def for_manifest(self, manifest: dict[str, Any] | None) -> FilesystemModelStorage:
        return self.get(manifest_target(manifest or {}))


def create_storage_registry(settings: Settings) -> StorageRegistry:
    if settings.model_storage_backend not in {"filesystem", "s3"}:
        raise ValueError("MODEL_STORAGE_BACKEND must be filesystem or s3.")
    remotes: list[S3ModelStorage] = []
    if settings.model_storage_backend == "s3":
        remotes.append(S3ModelStorage(settings))
    for target in parse_storage_targets(settings.storage_targets_json):
        if target.id == LEGACY_S3_TARGET_ID and remotes:
            raise ValueError("Storage target id 's3' is reserved for MODEL_STORAGE_BACKEND=s3.")
        remotes.append(S3ModelStorage(settings, target=target))
    return StorageRegistry(settings, remotes, settings.default_storage_target)
