from __future__ import annotations

import json
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from .config import Settings, repository_path, validate_repo_id
from .indexer import (
    LEGACY_S3_TARGET_ID,
    LOCAL_TARGET_ID,
    UNSAFE_EXTENSIONS,
    manifest_target,
    model_formats,
    repository_facts,
)


MANIFEST_NAME = ".hugginghack.json"
PART_SUFFIXES = (".hugginghack-part", ".hugginghack-s3-part")
TARGET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


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


class FilesystemModelStorage:
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

    def repository_manifest(self, repo_id: str) -> dict[str, Any] | None:
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

    def list_repository_entries(self, repo_id: str) -> list[dict[str, Any]] | None:
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
            client_options: dict[str, Any] = {
                "service_name": "s3",
                "use_ssl": target.use_ssl,
                "verify": target.verify_ssl,
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
        self.transfer_config = transfer_config

    def _prefix(self, value: str = "") -> str:
        parts = [part for part in (self.prefix, value.strip("/")) if part]
        return "/".join(parts)

    def _repo_prefix(self, repo_id: str) -> str:
        return f"{self._prefix(validate_repo_id(repo_id))}/"

    def _manifest_key(self, repo_id: str) -> str:
        return f"{self._repo_prefix(repo_id)}{MANIFEST_NAME}"

    def remote_uri(self, repo_id: str) -> str:
        return f"s3://{self.bucket}/{self._prefix(validate_repo_id(repo_id))}"

    def _objects(self, prefix: str) -> Iterable[dict[str, Any]]:
        paginator = self.client.get_paginator("list_objects_v2")
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
            error = str(exception).strip() or exception.__class__.__name__
            for secret in self.target.secrets:
                error = error.replace(secret, "[redacted]")
            error = error[:300]
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
        files: list[tuple[Path, str]] = []
        for current, directories, names in os.walk(root):
            directories[:] = [
                name
                for name in directories
                if name not in {".cache", "__pycache__"} and not name.startswith(".")
            ]
            for name in names:
                path = Path(current) / name
                if (
                    path.is_symlink()
                    or any(name.endswith(suffix) for suffix in PART_SUFFIXES)
                ):
                    continue
                files.append((path, path.relative_to(root).as_posix()))
        return files

    def sync_repository(
        self, repo_id: str, root: Path, changed: set[str] | None = None
    ) -> str:
        """Upload a complete repository, publishing its manifest last.

        With `changed`, only those paths are uploaded; other objects keep their
        timestamps so commit history does not see untouched files as modified.
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

        config_path = root / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                config = {}
        except (OSError, json.JSONDecodeError):
            config = {}
        manifest["storage_backend"] = "s3"
        manifest["storage_target"] = self.id
        manifest["remote_uri"] = self.remote_uri(validated)
        manifest["config"] = {
            "architectures": config.get("architectures"),
            "model_type": config.get("model_type"),
            "torch_dtype": config.get("torch_dtype"),
            "vocab_size": config.get("vocab_size"),
        }
        if not manifest.get("pipeline_tag") and config.get("model_type"):
            manifest["pipeline_tag"] = config["model_type"]
        manifest.update(repository_facts(root))
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        with self._lock:
            repo_prefix = self._repo_prefix(validated)
            manifest_key = self._manifest_key(validated)
            self._delete_keys([manifest_key])
            local_files = self._local_files(root)
            intended_keys = {f"{repo_prefix}{relative}" for _, relative in local_files}
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
            existing_keys = {item["Key"] for item in self._objects(repo_prefix)}
            self._delete_keys(existing_keys - intended_keys)
            self.client.upload_file(
                str(manifest_path),
                self.bucket,
                manifest_key,
                **self._upload_options(),
            )
        return self.remote_uri(validated)

    def apply_changes(
        self,
        repo_id: str,
        files: dict[str, Path],
        deletions: set[str],
        manifest: dict[str, Any],
    ) -> None:
        """Change a repository that has no local cache, directly in the bucket."""
        validated = validate_repo_id(repo_id)
        repo_prefix = self._repo_prefix(validated)
        with self._lock:
            self._delete_keys([self._manifest_key(validated)])
            for relative, path in files.items():
                key = f"{repo_prefix}{_safe_relative_key(relative).as_posix()}"
                self.client.upload_file(str(path), self.bucket, key, **self._upload_options())
            self._delete_keys(
                f"{repo_prefix}{_safe_relative_key(relative).as_posix()}" for relative in deletions
            )
            manifest = {
                **manifest,
                "storage_backend": "s3",
                "storage_target": self.id,
                "remote_uri": self.remote_uri(validated),
            }
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._manifest_key(validated),
                Body=json.dumps(manifest, indent=2).encode("utf-8"),
            )

    def delete_repository(self, repo_id: str) -> None:
        with self._lock:
            keys = [item["Key"] for item in self._objects(self._repo_prefix(repo_id))]
            self._delete_keys(keys)

    def repository_manifest(self, repo_id: str) -> dict[str, Any] | None:
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
            manifest = json.loads(payload.decode("utf-8"))
            return manifest if isinstance(manifest, dict) else None
        except Exception:
            return None

    def discover_repositories(self) -> list[dict[str, Any]]:
        root_prefix = f"{self._prefix()}/" if self._prefix() else ""
        records: list[dict[str, Any]] = []
        manifests: list[tuple[str, dict[str, Any]]] = []
        repository_keys: dict[str, list[str]] = {}
        repository_entries: dict[str, list[dict[str, Any]]] = {}
        for item in self._objects(root_prefix):
            key = item.get("Key") or ""
            relative_key = key[len(root_prefix) :] if root_prefix else key
            parts = PurePosixPath(relative_key).parts
            if len(parts) < 3:
                continue
            repo_id = f"{parts[0]}/{parts[1]}"
            if len(parts) == 3 and parts[-1] == MANIFEST_NAME:
                manifests.append((repo_id, item))
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
                    "file_count": int(manifest.get("file_count") or 0),
                    "modified_at": (
                        manifest.get("uploaded_at")
                        or manifest.get("downloaded_at")
                        or _iso(item.get("LastModified"))
                    ),
                    "downloaded_at": manifest.get("downloaded_at"),
                    "revision": manifest.get("revision"),
                    "sha": manifest.get("sha"),
                    "pipeline_tag": manifest.get("pipeline_tag"),
                    "library_name": manifest.get("library_name"),
                    "license": manifest.get("license"),
                    "tags": manifest.get("tags") or [],
                    "config": manifest.get("config") or {},
                    "source_url": manifest.get("source_url"),
                    "source": manifest.get("source"),
                    "owner_id": manifest.get("owner_id"),
                    "managed": True,
                    "storage_backend": "s3",
                    "storage_target": self.id,
                    "cached": cached,
                    "remote_uri": self.remote_uri(repo_id),
                    "parameter_count": manifest.get("parameter_count"),
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
        for item in self._objects(prefix):
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
        objects = list(self._objects(prefix))
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
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            return None
        total = int(head.get("ContentLength") or 0)
        if start >= total:
            return b"", total
        last = min(total - 1, end if end is not None else total - 1)
        if last - start + 1 > max_bytes:
            raise ValueError("The requested file is too large to read.")
        response = self.client.get_object(
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

    def list_repository_entries(self, repo_id: str) -> list[dict[str, Any]] | None:
        """List every object of a repository, without the browsing limit."""
        prefix = self._repo_prefix(repo_id)
        entries: list[dict[str, Any]] = []
        for item in self._objects(prefix):
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
            head = self.client.head_object(
                Bucket=self.bucket, Key=self._object_key(repo_id, relative_path)
            )
        except Exception:
            return None
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
