from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path


ORGANIZATION_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")
# Names that would collide with server paths such as /api/models/... or /assets/...,
# or read like the web app's own pages (#/orgs, #/models, #/account) in repo ids.
RESERVED_NAMESPACES = frozenset({"api", "assets", "static", "orgs", "models", "account"})
# Organizations may not be called "admin" either. Accounts may, since it is the name
# many owners pick for themselves, and existing sign-ins must keep working.
RESERVED_ORGANIZATION_NAMES = RESERVED_NAMESPACES | {"admin"}


def validate_namespace(name: str) -> str:
    value = name.strip()
    if not ORGANIZATION_PATTERN.fullmatch(value) or len(value) < 2:
        raise ValueError(
            "Names use 2-64 letters, numbers, dots, underscores, or hyphens, "
            "and start and end with a letter or number."
        )
    if value.lower() in RESERVED_ORGANIZATION_NAMES:
        raise ValueError(f"{value!r} is reserved; choose another name.")
    return value


REPO_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def _positive_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, min(value, maximum))


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_boolean(name: str) -> bool | None:
    """True or False when set to one, None for "auto" or unset."""
    value = (os.getenv(name) or "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "HuggingHack")
    app_version: str = os.getenv("APP_VERSION", "1.3.1")
    model_storage: Path = Path(os.getenv("MODEL_STORAGE", "/models")).expanduser().resolve()
    model_storage_backend: str = os.getenv("MODEL_STORAGE_BACKEND", "filesystem").strip().lower()
    data_dir: Path = Path(os.getenv("DATA_DIR", "/data")).expanduser().resolve()
    database_url: str | None = field(
        default=(os.getenv("DATABASE_URL") or "").strip() or None,
        repr=False,
    )
    # PostgreSQL connections kept open for reuse (SQLite opens one per query).
    database_pool_size: int = _positive_int("DATABASE_POOL_SIZE", 10, 100)
    hf_endpoint: str = os.getenv("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    hf_token: str | None = os.getenv("HF_TOKEN") or None
    max_concurrent_downloads: int = _positive_int("MAX_CONCURRENT_DOWNLOADS", 2, 8)
    download_workers_per_job: int = _positive_int("DOWNLOAD_WORKERS_PER_JOB", 4, 16)
    accounts_enabled: bool = _boolean("ACCOUNTS_ENABLED", True)
    # None ("auto"): cookies are Secure when the request arrived over HTTPS.
    secure_cookies: bool | None = _optional_boolean("SECURE_COOKIES")
    session_ttl_hours: int = _positive_int("SESSION_TTL_HOURS", 720, 8760)
    upload_chunk_mb: int = _positive_int("UPLOAD_CHUNK_MB", 8, 64)
    max_upload_size_gb: int = _positive_int("MAX_UPLOAD_SIZE_GB", 1024, 16384)
    runtime_targets_json: str = os.getenv("RUNTIME_TARGETS_JSON", "[]")
    runtime_workers: int = _positive_int("RUNTIME_WORKERS", 2, 8)
    runtime_api_token: str | None = os.getenv("RUNTIME_API_TOKEN") or None
    # Several HuggingHack processes share one PostgreSQL database and the buckets
    # (docs/SCALING.md, phase 3); each names itself so it can tell its own work apart.
    cluster_mode: bool = _boolean("CLUSTER_MODE", False)
    instance_id: str = (os.getenv("INSTANCE_ID") or "").strip()[:64] or socket.gethostname()[:64] or "hugginghack"
    s3_bucket: str | None = os.getenv("S3_BUCKET") or None
    s3_prefix: str = os.getenv("S3_PREFIX", "models").strip().strip("/")
    s3_endpoint_url: str | None = os.getenv("S3_ENDPOINT_URL") or None
    s3_region: str | None = os.getenv("S3_REGION") or None
    s3_access_key_id: str | None = os.getenv("S3_ACCESS_KEY_ID") or None
    s3_secret_access_key: str | None = os.getenv("S3_SECRET_ACCESS_KEY") or None
    s3_session_token: str | None = os.getenv("S3_SESSION_TOKEN") or None
    s3_use_ssl: bool = _boolean("S3_USE_SSL", True)
    s3_verify_ssl: bool = _boolean("S3_VERIFY_SSL", True)
    s3_addressing_style: str = os.getenv("S3_ADDRESSING_STYLE", "auto").strip().lower()
    s3_storage_class: str | None = os.getenv("S3_STORAGE_CLASS") or None
    # Direct transfers (docs/SCALING.md): clients move bytes to and from the bucket
    # through short-lived signed links, signed for the endpoint clients can reach.
    s3_public_endpoint_url: str | None = os.getenv("S3_PUBLIC_ENDPOINT_URL") or None
    s3_ca_bundle: str | None = (os.getenv("S3_CA_BUNDLE") or "").strip() or None
    s3_direct_downloads: bool = _boolean("S3_DIRECT_DOWNLOADS", False)
    s3_presign_ttl_seconds: int = _bounded_int("S3_PRESIGN_TTL_SECONDS", 900, 60, 604800)
    s3_direct_uploads: bool = _boolean("S3_DIRECT_UPLOADS", False)
    s3_part_size_mb: int = _bounded_int("S3_PART_SIZE_MB", 64, 5, 5120)
    s3_upload_presign_ttl_seconds: int = _bounded_int("S3_UPLOAD_PRESIGN_TTL_SECONDS", 3600, 60, 604800)
    s3_max_concurrency: int = _positive_int("S3_MAX_CONCURRENCY", 4, 32)
    s3_multipart_chunk_mb: int = _positive_int("S3_MULTIPART_CHUNK_MB", 64, 512)
    storage_targets_json: str = os.getenv("STORAGE_TARGETS_JSON", "[]")
    default_storage_target: str | None = (os.getenv("DEFAULT_STORAGE_TARGET") or "").strip() or None
    # Where the site keeps its own files (profile pictures, git mirrors): "local" (the
    # data folder) or the id of an S3 target, under <its prefix>/_system unless
    # SYSTEM_STORAGE_PREFIX says otherwise.
    system_storage_target: str = (os.getenv("SYSTEM_STORAGE_TARGET") or "local").strip() or "local"
    system_storage_prefix: str | None = (os.getenv("SYSTEM_STORAGE_PREFIX") or "").strip().strip("/") or None
    hub_api_enabled: bool = _boolean("HUB_API_ENABLED", True)
    # Downloading models from Hugging Face needs internet access, so air-gapped
    # servers leave it off; turn it on only where HF_ENDPOINT is reachable.
    hf_downloads_enabled: bool = _boolean("HF_DOWNLOADS_ENABLED", False)
    oidc_issuer: str | None = (os.getenv("OIDC_ISSUER") or "").strip() or None
    oidc_client_id: str | None = (os.getenv("OIDC_CLIENT_ID") or "").strip() or None
    oidc_client_secret: str | None = field(
        default=(os.getenv("OIDC_CLIENT_SECRET") or "").strip() or None, repr=False
    )
    oidc_scopes: str = os.getenv("OIDC_SCOPES", "openid profile email").strip()
    oidc_provider_name: str = os.getenv("OIDC_PROVIDER_NAME", "single sign-on").strip()
    oidc_default_role: str = os.getenv("OIDC_DEFAULT_ROLE", "viewer").strip().lower()
    oidc_allowed_groups: str = os.getenv("OIDC_ALLOWED_GROUPS", "").strip()
    oidc_groups_claim: str = os.getenv("OIDC_GROUPS_CLAIM", "groups").strip()
    oidc_username_claim: str = os.getenv("OIDC_USERNAME_CLAIM", "preferred_username").strip()
    oidc_redirect_url: str | None = (os.getenv("OIDC_REDIRECT_URL") or "").strip() or None
    oidc_ca_bundle: str | None = (os.getenv("OIDC_CA_BUNDLE") or "").strip() or None
    oidc_verify_ssl: bool = _boolean("OIDC_VERIFY_SSL", True)
    public_url: str | None = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/") or None
    # Other origins (such as a separately served web UI) allowed to call the API
    # with the user's cookies. The Vite dev server proxies /api, so it needs none.
    cors_origins: str = os.getenv("CORS_ORIGINS", "").strip()
    # Host names this server answers to, such as "models.example.com,localhost";
    # empty answers to any, as before. Blocks DNS rebinding when set.
    allowed_hosts: str = os.getenv("ALLOWED_HOSTS", "").strip()
    # The interactive API reference at /api/docs and its /openapi.json schema.
    api_docs_enabled: bool = _boolean("API_DOCS_ENABLED", False)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "hugginghack.sqlite3"

    @property
    def database_target(self) -> Path | str:
        return self.database_url or self.database_path

    def ensure_directories(self) -> None:
        self.model_storage.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.accounts_enabled and self.oidc_issuer and self.oidc_client_id)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip().rstrip("/") for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def allowed_host_list(self) -> list[str]:
        return [host.strip().lower() for host in self.allowed_hosts.split(",") if host.strip()]

    @property
    def oidc_groups(self) -> tuple[str, ...]:
        return tuple(group.strip() for group in self.oidc_allowed_groups.split(",") if group.strip())


settings = Settings()


def validate_repo_id(repo_id: str) -> str:
    value = repo_id.strip()
    if not REPO_ID_PATTERN.fullmatch(value):
        raise ValueError("Repository ID must use the form owner/model-name.")
    return value


def repository_path(repo_id: str, root: Path | None = None) -> Path:
    validated = validate_repo_id(repo_id)
    storage_root = (root or settings.model_storage).resolve()
    target = (storage_root / validated).resolve()
    if storage_root != target and storage_root not in target.parents:
        raise ValueError("Repository path escapes the configured model storage.")
    return target
