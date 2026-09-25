from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
import json
import logging
import re
from urllib.parse import quote, urlsplit
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Callable, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .auth import (
    API_TOKEN_PREFIX,
    AuthService,
    hash_password,
    utc_iso,
    utc_now,
    validate_password,
)
from .catalog import HARDWARE, LocalCatalog, model_task, nominal_parameters, search_catalog
from .config import settings, validate_namespace, validate_repo_id
from .database import INTEGRITY_ERRORS, Database
from .downloads import DownloadManager
from .git_mirror import GitMirrors
from .deploy_configs import METRICS, ConfigRevisions
from .history import RepoHistory, public_commit
from .oidc import OidcClient, OidcError, claim_groups, pkce_pair
from .permissions import CAPABILITIES, can, capabilities_for, permission_matrix
from .hub_api import (
    HubError,
    HubRepositories,
    RepoEntry,
    RepoSnapshot,
    parse_range,
    remote_entries,
)
from .hub_service import HubService
from .indexer import LocalModelIndexer, upload_is_registered
from .moves import MoveManager
from .reads import LeasedResponse, reads
from .listing import PRECISIONS, preview as preview_listing, validate_overrides
from .runtimes import RuntimeManager
from .storage import create_storage_registry
from .uploads import UploadManager


logger = logging.getLogger("hugginghack")
database = Database(settings.database_target)
hub = HubService(settings)
indexer = LocalModelIndexer(settings, database)
storages = create_storage_registry(settings)
hub_repositories = HubRepositories(settings, database, storages)
history = RepoHistory(database, hub_repositories)
downloads = DownloadManager(settings, database, hub, indexer, storages, history)
auth = AuthService(settings, database)
uploads = UploadManager(settings, database, indexer, storages, history)
runtimes = RuntimeManager(settings, database)
catalog = LocalCatalog(settings, storages)
oidc = OidcClient(settings)
git_mirrors = GitMirrors(hub_repositories)


def repository_busy(repo_id: str) -> str | None:
    """Why a repository's files cannot be moved right now."""
    if database.find_active_runtime_job_for_repo(repo_id):
        return "A runtime is loading this model. Try again when it finishes."
    return uploads._busy(repo_id)


moves = MoveManager(
    settings, database, storages, hub_repositories, history, git_mirrors, reads, repository_busy
)
uploads.move_guard = moves.moving


# Repositories found in more than one storage target during the last scan.
storage_conflicts: list[dict[str, str]] = []
storage_errors: dict[str, str] = {}


def record_history(model: dict[str, Any], entries: list[RepoEntry] | None = None) -> None:
    try:
        history.record_scan(model, entries)
    except Exception:
        # History must never block indexing; the next scan retries.
        logger.exception("Could not record history for %s", model.get("repo_id"))


def refresh_model_index() -> dict[str, Any]:
    result = indexer.scan()
    owners = {model["repo_id"]: model["storage_target"] for model in database.list_local_models()}
    conflicts: list[dict[str, str]] = []
    errors: dict[str, str] = {}
    remote_count = 0
    for storage in storages.remotes:
        status = storage.health()
        if not status.get("connected"):
            # Skip the slower, retrying listing for a bucket that is down.
            errors[storage.id] = (status.get("error") or "Storage is unreachable.")[:500]
            continue
        try:
            discovered = storage.discover_repositories()
        except Exception as error:
            # Keep the existing index for a target that is temporarily unreachable.
            errors[storage.id] = (str(error).strip() or error.__class__.__name__)[:500]
            continue
        found: set[str] = set()
        for model in discovered:
            repo_id = model["repo_id"]
            if model.get("source") == "user-upload" and not upload_is_registered(
                database, repo_id, model
            ):
                continue
            owner = owners.get(repo_id)
            if owner is not None and owner != storage.id:
                conflicts.append(
                    {"repo_id": repo_id, "kept_target": owner, "skipped_target": storage.id}
                )
                continue
            indexed = indexer.index_remote(model)
            owners[repo_id] = storage.id
            found.add(repo_id)
            if indexed:
                record_history(indexed, remote_entries(model.get("entries") or []))
        database.prune_remote_models(storage.id, found)
        remote_count += len(found)
    database.prune_unknown_targets(set(storages.ids()))
    for model in database.list_local_models():
        if model["storage_target"] == storages.local.id:
            record_history(model)
    storage_conflicts[:] = conflicts
    storage_errors.clear()
    storage_errors.update(errors)
    models = database.list_local_models()
    return {
        "count": len(models),
        "models": models,
        "local_count": result["count"],
        "remote_count": remote_count,
        "remote_error": "; ".join(f"{key}: {value}" for key, value in errors.items()) or None,
        "conflicts": conflicts,
        "scanned_at": result["scanned_at"],
    }


def log_startup_scan(task: "asyncio.Task[Any]") -> None:
    if not task.cancelled() and task.exception():
        logger.error("Startup library scan failed", exc_info=task.exception())


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.ensure_directories()
    database.initialize()
    database.fail_unfinished_runtime_jobs(utc_iso())
    auth.ensure_local_user()
    # Before the first scan: undo moves cut short, and finish those that switched.
    await run_in_threadpool(moves.recover)
    # Index in the background so the server answers immediately, even when a
    # bucket is offline or a large library records its first history.
    startup_scan = asyncio.create_task(run_in_threadpool(refresh_model_index))
    startup_scan.add_done_callback(log_startup_scan)
    if settings.hf_downloads_enabled:
        downloads.resume_unfinished()
    yield
    downloads.shutdown()
    runtimes.shutdown()
    oidc.close()


app = FastAPI(
    title="HuggingHack API",
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "X-CSRF-Token",
        "Upload-Offset",
        "Upload-Length",
    ],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if response.headers.get("content-type", "").startswith("text/html"):
        # Asset names are content hashed; the page itself must be revalidated so a
        # redeploy never leaves browsers on an old build.
        response.headers["Cache-Control"] = "no-cache"
    return response


class CredentialsRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class SetupRequest(CredentialsRequest):
    display_name: str = Field(default="", max_length=80)


class InitialMembership(BaseModel):
    organization: str = Field(min_length=1, max_length=100)
    role: Literal["admin", "write", "read"] = "read"


class CreateUserRequest(SetupRequest):
    role: Literal["admin", "member", "viewer"] = "member"
    email: str | None = Field(default=None, max_length=254)
    organizations: list[InitialMembership] = Field(default_factory=list, max_length=50)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class DownloadRequest(BaseModel):
    repo_id: str
    revision: str = Field(default="main", max_length=200)
    allow_patterns: list[str] = Field(default_factory=list, max_length=50)
    ignore_patterns: list[str] = Field(default_factory=list, max_length=50)
    mode: Literal["full", "safetensors", "gguf", "metadata", "custom"] = "full"
    storage_target: str | None = Field(default=None, max_length=40)

    @field_validator("repo_id")
    @classmethod
    def repo_is_valid(cls, value: str) -> str:
        return validate_repo_id(value)

    @field_validator("allow_patterns", "ignore_patterns")
    @classmethod
    def patterns_are_bounded(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            item = value.strip()
            if len(item) > 200:
                raise ValueError("File patterns must be 200 characters or fewer.")
            if item:
                cleaned.append(item)
        return cleaned


class CollectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=240)


class SavedModelRequest(BaseModel):
    repo_id: str
    note: str = Field(default="", max_length=1000)
    collection_ids: list[str] = Field(default_factory=list, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("repo_id")
    @classmethod
    def saved_repo_is_valid(cls, value: str) -> str:
        return validate_repo_id(value)


class RepositoryRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=96)
    description: str = Field(default="", max_length=500)
    visibility: Literal["private", "organization", "public"] = "private"
    storage_target: str | None = Field(default=None, max_length=40)
    namespace: str | None = Field(default=None, max_length=64)


class RepositoryUpdateRequest(BaseModel):
    description: str = Field(default="", max_length=500)
    visibility: Literal["private", "organization", "public"] = "private"


class StorageGrantRequest(BaseModel):
    users: list[str] = Field(default_factory=list, max_length=200)
    organizations: list[str] = Field(default_factory=list, max_length=200)


class DeleteRepositoryRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=200)


class RenameRepositoryRequest(BaseModel):
    namespace: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=96)
    confirmation: str = Field(min_length=1, max_length=200)


class RuntimeLoadRequest(BaseModel):
    repo_id: str
    runtime_model_name: str | None = Field(default=None, max_length=128)
    source_file: str | None = Field(default=None, max_length=500)

    @field_validator("repo_id")
    @classmethod
    def runtime_repo_is_valid(cls, value: str) -> str:
        return validate_repo_id(value)


def session_for_request(request: Request) -> dict[str, Any] | None:
    return auth.session(request.cookies.get(auth.cookie_name))


def resolve_principal(request: Request) -> dict[str, Any] | None:
    """Who is calling: a personal API token or a browser session, never both.

    When an Authorization header is present it is the only credential used, so a
    token request can never ride on a cookie (and therefore needs no CSRF token).
    """
    authorization = request.headers.get("Authorization")
    if authorization and settings.accounts_enabled:
        scheme, _, credential = authorization.partition(" ")
        credential = credential.strip()
        if scheme.lower() != "bearer" or not credential.startswith(API_TOKEN_PREFIX):
            return None
        principal = auth.token_principal(credential)
        if not principal:
            raise HTTPException(
                status_code=401, detail="The API token is invalid, expired, or revoked."
            )
        return {
            "user": principal["user"],
            "via": "token",
            "scope": principal["token"]["scope"],
        }
    session = session_for_request(request)
    if not session or not session.get("user"):
        return None
    return {"user": session["user"], "via": "session", "session": session}


def require_user(request: Request) -> dict[str, Any]:
    if auth.setup_required():
        raise HTTPException(status_code=428, detail="Create the owner account first.")
    principal = resolve_principal(request)
    if not principal:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    request.state.principal = principal
    request.state.auth_session = principal.get("session")
    return principal["user"]


def require_write_user(
    request: Request, user: Annotated[dict[str, Any], Depends(require_user)]
) -> dict[str, Any]:
    principal = request.state.principal
    if principal["via"] == "token":
        if principal["scope"] != "write":
            raise HTTPException(status_code=403, detail="This API token is read-only.")
        return user
    if not auth.verify_csrf(principal["session"], request.headers.get("X-CSRF-Token")):
        raise HTTPException(status_code=403, detail="Security token is missing or expired.")
    return user


def require_session(request: Request) -> None:
    if request.state.principal["via"] == "token":
        raise HTTPException(
            status_code=403,
            detail="Sign in to the web interface for this action; API tokens cannot use it.",
        )


def disabled_capabilities() -> frozenset[str]:
    """Capabilities for features this server has turned off."""
    return frozenset() if settings.hf_downloads_enabled else frozenset({"hub.download"})


def user_capabilities(user: dict[str, Any] | None) -> frozenset[str]:
    return capabilities_for(user) - disabled_capabilities()


def requires(capability: str, *, write: bool = False, session_only: bool = False) -> Any:
    """A dependency that allows only users whose role grants `capability`."""
    base = require_write_user if write else require_user

    # A default-value Depends, because string annotations cannot see `base`.
    def dependency(request: Request, user: dict = Depends(base)) -> dict[str, Any]:  # noqa: B008
        if capability in disabled_capabilities():
            raise HTTPException(
                status_code=404,
                detail="Downloading from Hugging Face is turned off on this server (HF_DOWNLOADS_ENABLED).",
            )
        if session_only:
            require_session(request)
        if not can(user, capability):
            raise HTTPException(
                status_code=403,
                detail=f"Your role does not allow this: {CAPABILITIES[capability].lower()}.",
            )
        return user

    dependency.capability = capability  # type: ignore[attr-defined]
    return Annotated[dict[str, Any], Depends(dependency)]


def personal(*, write: bool = False) -> Any:
    """Account self-service: any role, browser session only."""
    base = require_write_user if write else require_user

    # A default-value Depends, because string annotations cannot see `base`.
    def dependency(request: Request, user: dict = Depends(base)) -> dict[str, Any]:  # noqa: B008
        require_session(request)
        return user

    dependency.personal = True  # type: ignore[attr-defined]
    return Annotated[dict[str, Any], Depends(dependency)]


Browser = requires("models.browse")
Saver = requires("models.save", write=True)
HubReader = requires("hub.download")
HubWriter = requires("hub.download", write=True)
Uploader = requires("repos.create", write=True)
UploadLister = requires("repos.create")
Editor = requires("repos.edit_own", write=True)
Scanner = requires("library.scan", write=True)
CacheManager = requires("library.cache", write=True)
StorageViewer = requires("storage.view")
StorageManager = requires("storage.manage", write=True, session_only=True)
# Repository settings: who may change a repository is decided per repository.
RepoManager = requires("models.browse", write=True, session_only=True)
UserManager = requires("users.manage", session_only=True)
UserAdmin = requires("users.manage", write=True, session_only=True)
SettingsViewer = requires("settings.view", session_only=True)
OrgCreator = requires("orgs.manage", write=True, session_only=True)
OrgOverseer = requires("orgs.manage", session_only=True)
OrgEditor = requires("models.browse", write=True, session_only=True)
TokenOwner = requires("tokens.manage", session_only=True)
TokenWriter = requires("tokens.manage", write=True, session_only=True)
SessionUser = personal()
SessionWriter = personal(write=True)


def set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        auth.cookie_name,
        raw_token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )


def auth_payload(session: dict[str, Any] | None = None) -> dict[str, Any]:
    user = session.get("user") if session else None
    return {
        "accounts_enabled": settings.accounts_enabled,
        "oidc": {"enabled": settings.oidc_enabled, "name": settings.oidc_provider_name},
        "setup_required": auth.setup_required(),
        "user": user,
        "capabilities": sorted(user_capabilities(user)),
        "csrf_token": session.get("csrf_token") if session else None,
    }


def client_details(request: Request) -> tuple[str | None, str | None]:
    return (
        request.headers.get("User-Agent"),
        request.client.host if request.client else None,
    )


@app.get("/api/health")
def health() -> dict:
    settings.ensure_directories()
    usage = shutil.disk_usage(settings.model_storage)
    object_storage = storages.default.health()
    return {
        "status": "ok" if object_storage["connected"] else "degraded",
        "app": settings.app_name,
        "version": settings.app_version,
        "database_backend": database.backend,
        "storage": {
            "path": str(settings.model_storage),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "writable": os_access_writable(settings.model_storage),
        },
        "object_storage": object_storage,
        "hf_token_configured": bool(settings.hf_token),
        "hf_endpoint": settings.hf_endpoint,
        "accounts_enabled": settings.accounts_enabled,
        "upload_chunk_bytes": settings.upload_chunk_mb * 1024**2,
        "max_upload_size_bytes": settings.max_upload_size_gb * 1024**3,
        "runtime_target_count": len(runtimes.targets),
        "runtime_api_token_configured": bool(settings.runtime_api_token),
        "hub_api_enabled": settings.hub_api_enabled,
        "public_url": settings.public_url,
    }


def os_access_writable(path: Path) -> bool:
    try:
        probe = path / ".hugginghack-write-test"
        probe.touch(exist_ok=True)
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict:
    return auth_payload(session_for_request(request))


@app.post("/api/auth/setup", status_code=201)
def setup_account(payload: SetupRequest, request: Request, response: Response) -> dict:
    if not settings.accounts_enabled:
        raise HTTPException(status_code=409, detail="Accounts are disabled.")
    if not auth.setup_required():
        raise HTTPException(status_code=409, detail="The owner account already exists.")
    try:
        user = auth.create_owner(payload.username, payload.display_name, payload.password)
    except (ValueError, *INTEGRITY_ERRORS) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    raw_token, csrf_token = auth.create_session(user["id"], *client_details(request))
    set_session_cookie(response, raw_token)
    return auth_payload({"user": user, "csrf_token": csrf_token})


@app.post("/api/auth/login")
def login(payload: CredentialsRequest, request: Request, response: Response) -> dict:
    if not settings.accounts_enabled:
        raise HTTPException(status_code=409, detail="Accounts are disabled.")
    client = request.client.host if request.client else "unknown"
    try:
        user = auth.authenticate(
            payload.username, payload.password, f"{client}:{payload.username.lower()}"
        )
    except ValueError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    if not user:
        raise HTTPException(status_code=401, detail="Username or password is incorrect.")
    raw_token, csrf_token = auth.create_session(user["id"], *client_details(request))
    set_session_cookie(response, raw_token)
    public_user = database.get_user(user["id"], include_secret=False)
    return auth_payload({"user": public_user, "csrf_token": csrf_token})


OIDC_PROVIDER = "oidc"
OIDC_BROWSER_COOKIE = "hugginghack_oidc"
OIDC_STATE_SECONDS = 600


def safe_next_path(value: str | None) -> str:
    """Only same-app paths, so sign-in can never redirect somewhere else."""
    path = (value or "").strip()
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or ":" in path.split("?", 1)[0]
        or any(ord(character) < 32 for character in path)
        or len(path) > 500
    ):
        return "/models"
    return path


def oidc_redirect_uri(request: Request) -> str:
    """The callback registered with the identity provider; must match exactly."""
    if settings.oidc_redirect_url:
        return settings.oidc_redirect_url
    base = settings.public_url or str(request.base_url).rstrip("/")
    return f"{base}/api/auth/oidc/callback"


def sso_error(message: str) -> RedirectResponse:
    response = RedirectResponse(f"/#/?sso_error={quote(message)}", status_code=303)
    response.delete_cookie(OIDC_BROWSER_COOKIE, path="/api/auth/oidc")
    return response


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@app.get("/api/auth/oidc/login")
def oidc_login(request: Request, next: str = "/models") -> Response:
    if not settings.oidc_enabled:
        raise HTTPException(status_code=404, detail="Single sign-on is not configured.")
    if auth.setup_required():
        return sso_error("Create the owner account with a password first; then single sign-on is available.")
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    browser = secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    redirect_uri = oidc_redirect_uri(request)
    try:
        location = oidc.authorization_url(state, nonce, challenge, redirect_uri)
    except OidcError as error:
        return sso_error(str(error))
    now = utc_now()
    database.create_oidc_state(
        {
            "state_hash": hash_secret(state),
            "browser_hash": hash_secret(browser),
            "nonce": nonce,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "next_path": safe_next_path(next),
            "created_at": utc_iso(now),
            "expires_before": utc_iso(now - timedelta(seconds=OIDC_STATE_SECONDS)),
        }
    )
    response = RedirectResponse(location, status_code=303)
    # Ties the sign-in to this browser, so a callback link from someone else fails.
    response.set_cookie(
        OIDC_BROWSER_COOKIE,
        browser,
        max_age=OIDC_STATE_SECONDS,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/api/auth/oidc",
    )
    return response


@app.get("/api/auth/oidc/callback")
def oidc_callback(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
    error_description: str = "",
) -> Response:
    if not settings.oidc_enabled:
        raise HTTPException(status_code=404, detail="Single sign-on is not configured.")
    if error:
        return sso_error(error_description or f"The identity provider refused the sign-in ({error}).")
    if auth.setup_required():
        return sso_error("Create the owner account with a password first.")
    pending = database.take_oidc_state(hash_secret(state)) if state else None
    if not pending:
        return sso_error("This sign-in link expired or was already used. Try again.")
    try:
        started = datetime.fromisoformat(pending["created_at"])
    except ValueError:
        started = utc_now() - timedelta(days=1)
    browser = request.cookies.get(OIDC_BROWSER_COOKIE) or ""
    if not browser or not hmac.compare_digest(hash_secret(browser), pending["browser_hash"]):
        return sso_error("Sign-in must finish in the same browser that started it. Try again.")
    if (utc_now() - started).total_seconds() > OIDC_STATE_SECONDS:
        return sso_error("The sign-in took too long. Try again.")
    if not code:
        return sso_error("The identity provider did not return a sign-in code.")
    try:
        tokens = oidc.exchange(code, pending["code_verifier"], pending["redirect_uri"])
        claims = oidc.validate_id_token(tokens["id_token"], pending["nonce"])
        extra = oidc.userinfo(tokens.get("access_token"))
        if extra.get("sub") == claims["sub"]:
            claims = {**extra, **claims}
            if settings.oidc_groups_claim not in claims and settings.oidc_groups_claim in extra:
                claims[settings.oidc_groups_claim] = extra[settings.oidc_groups_claim]
    except OidcError as failure:
        return sso_error(str(failure))
    allowed = settings.oidc_groups
    if allowed:
        groups = claim_groups(claims, settings.oidc_groups_claim)
        if groups is None:
            return sso_error(
                f"The identity provider did not send the '{settings.oidc_groups_claim}' claim, "
                "so group membership could not be checked."
            )
        if not set(groups) & set(allowed):
            return sso_error("Your account is not in a group allowed to use HuggingHack.")
    try:
        user = auth.provision_external_user(
            OIDC_PROVIDER, claims, settings.oidc_username_claim, settings.oidc_default_role
        )
    except PermissionError as failure:
        return sso_error(str(failure))
    except (ValueError, *INTEGRITY_ERRORS) as failure:
        return sso_error(str(failure) or "Your account could not be created.")
    raw_token, _ = auth.create_session(user["id"], *client_details(request))
    response = RedirectResponse(f"/#{pending['next_path']}", status_code=303)
    set_session_cookie(response, raw_token)
    response.delete_cookie(OIDC_BROWSER_COOKIE, path="/api/auth/oidc")
    return response


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, _: SessionWriter) -> dict:
    auth.revoke(request.cookies.get(auth.cookie_name))
    response.delete_cookie(auth.cookie_name, path="/")
    return {"status": "signed_out"}


@app.get("/api/users")
def list_users(user: UserManager) -> dict:
    return {"items": database.list_users()}


@app.post("/api/users", status_code=201)
def create_user(payload: CreateUserRequest, user: UserAdmin) -> dict:
    # Check every organization before creating anything, so a bad entry leaves
    # no half-set-up account behind.
    memberships: list[tuple[dict[str, Any], str]] = []
    for item in payload.organizations:
        organization = organization_or_404(item.organization)
        if any(chosen["id"] == organization["id"] for chosen, _ in memberships):
            raise HTTPException(
                status_code=400, detail=f"{organization['name']} is listed more than once."
            )
        require_org_admin(organization, user)
        memberships.append((organization, item.role))
    try:
        created = auth.create_user(
            payload.username, payload.display_name, payload.password, role=payload.role
        )
        if payload.email:
            created = database.update_user(created["id"], email=clean_email(payload.email))
    except (ValueError, *INTEGRITY_ERRORS) as error:
        detail = (
            "That username is already in use."
            if isinstance(error, INTEGRITY_ERRORS)
            else str(error)
        )
        raise HTTPException(status_code=400, detail=detail) from error
    for organization, role in memberships:
        database.set_organization_member(organization["id"], created["id"], role, utc_iso())
    return {**created, "organizations": database.user_organizations(created["id"])}


@app.patch("/api/account/password")
def change_password(
    payload: PasswordChangeRequest, request: Request, user: SessionWriter
) -> dict:
    raw_token = request.cookies.get(auth.cookie_name)
    if not raw_token or not settings.accounts_enabled:
        raise HTTPException(
            status_code=409,
            detail="Password changes are unavailable when accounts are disabled.",
        )
    try:
        auth.change_password(
            user["id"], payload.current_password, payload.new_password, raw_token
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"status": "password_changed"}


EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}$")


def clean_email(value: str | None) -> str | None:
    email = (value or "").strip()
    if not email:
        return None
    if not EMAIL_PATTERN.fullmatch(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    return email


def public_session(session: dict[str, Any], current_hash: str | None) -> dict[str, Any]:
    return {
        "id": session["id"],
        "created_at": session["created_at"],
        "expires_at": session["expires_at"],
        "last_seen_at": session.get("last_seen_at"),
        "user_agent": session.get("user_agent"),
        "ip": session.get("ip"),
        "current": session["token_hash"] == current_hash,
    }


def require_accounts() -> None:
    if not settings.accounts_enabled:
        raise HTTPException(
            status_code=409, detail="Accounts are disabled; there is nothing to manage."
        )


class ProfileRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    email: str | None = Field(default=None, max_length=254)


class PreferencesRequest(BaseModel):
    theme: Literal["system", "light", "dark"] | None = None
    catalog_sort: Literal["updated", "name", "size", "parameters"] | None = None
    default_storage_target: str | None = Field(default=None, max_length=40)


class TokenRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scope: Literal["read", "write"] = "read"
    expires_in_days: int | None = Field(default=90, ge=1, le=3650)


class AdminUserUpdate(BaseModel):
    role: Literal["admin", "member", "viewer"] | None = None
    disabled: bool | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    email: str | None = Field(default=None, max_length=254)


class AdminPasswordReset(BaseModel):
    new_password: str = Field(min_length=12, max_length=256)


class AdminRevokeRequest(BaseModel):
    sessions: bool = True
    tokens: bool = False


@app.get("/api/account")
def account_overview(user: SessionUser) -> dict:
    owned = database.owned_repository_ids(user["id"])
    return {
        "user": user,
        "capabilities": [
            {"id": capability, "description": CAPABILITIES[capability]}
            for capability in sorted(user_capabilities(user))
        ],
        "accounts_enabled": settings.accounts_enabled,
        "local_password": user.get("auth_provider", "local") == "local" and settings.accounts_enabled,
        "repositories": owned,
        "organizations": database.user_organizations(user["id"]),
        "saved_count": len(database.saved_repo_ids(user["id"])),
    }


def is_external(user: dict[str, Any]) -> bool:
    """Single sign-on accounts take their name and email from the identity
    provider at every sign-in, so HuggingHack does not edit them."""
    return user.get("auth_provider", "local") != "local"


EXTERNAL_PROFILE = "Name and email come from your organization's identity provider; change them there."


@app.patch("/api/account/profile")
def update_profile(payload: ProfileRequest, user: SessionWriter) -> dict:
    require_accounts()
    if is_external(user):
        raise HTTPException(status_code=409, detail=EXTERNAL_PROFILE)
    return database.update_user(
        user["id"],
        display_name=payload.display_name.strip(),
        email=clean_email(payload.email),
        updated_at=utc_iso(),
    )


@app.get("/api/account/preferences")
def get_preferences(user: SessionUser) -> dict:
    return user.get("preferences") or {}


@app.patch("/api/account/preferences")
def update_preferences(payload: PreferencesRequest, user: SessionWriter) -> dict:
    preferences = dict(user.get("preferences") or {})
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("default_storage_target"):
        try:
            storages.get(changes["default_storage_target"])
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    for key, value in changes.items():
        if value is None:
            preferences.pop(key, None)
        else:
            preferences[key] = value
    database.update_user(user["id"], preferences_json=json.dumps(preferences))
    return preferences


@app.get("/api/account/sessions")
def list_account_sessions(request: Request, user: SessionUser) -> dict:
    require_accounts()
    current = (request.state.auth_session or {}).get("token_hash")
    return {
        "items": [
            public_session(session, current) for session in database.list_sessions(user["id"])
        ]
    }


@app.delete("/api/account/sessions/{session_id}")
def revoke_account_session(session_id: str, user: SessionWriter) -> dict:
    require_accounts()
    if not database.delete_session_by_id(user["id"], session_id):
        raise HTTPException(status_code=404, detail="Session not found.")
    return {"status": "revoked"}


@app.post("/api/account/sessions/revoke-others")
def revoke_other_sessions(request: Request, user: SessionWriter) -> dict:
    require_accounts()
    database.delete_other_sessions(user["id"], request.state.auth_session["token_hash"])
    return {"status": "revoked"}


@app.get("/api/account/tokens")
def list_tokens(user: TokenOwner) -> dict:
    require_accounts()
    return {"items": database.list_api_tokens(user["id"])}


@app.post("/api/account/tokens", status_code=201)
def create_token(payload: TokenRequest, user: TokenWriter) -> dict:
    require_accounts()
    try:
        raw_token, record = auth.create_api_token(
            user["id"], payload.name, payload.scope, payload.expires_in_days
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {**record, "token": raw_token}


@app.delete("/api/account/tokens/{token_id}")
def delete_token(token_id: str, user: TokenWriter) -> dict:
    require_accounts()
    if not database.delete_api_token(user["id"], token_id):
        raise HTTPException(status_code=404, detail="Token not found.")
    return {"status": "revoked"}


USER_PAGE_SIZES = (10, 25, 50, 100)


@app.get("/api/admin/users")
def admin_list_users(
    _: UserManager,
    q: Annotated[str, Query(max_length=200)] = "",
    role: Literal["", "admin", "member", "viewer"] = "",
    status: Literal["", "active", "disabled"] = "",
    sort: Literal["role", "name", "last_login", "newest"] = "role",
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=max(USER_PAGE_SIZES))] = 25,
) -> dict:
    filters = {"query": q, "role": role or None, "status": status or None, "sort": sort}
    users, total, counts = database.search_users(
        **filters, limit=per_page, offset=(page - 1) * per_page
    )
    pages = max(1, -(-total // per_page))
    if page > pages:
        # A deletion or a narrower filter can leave the requested page empty;
        # answer with the last page that has accounts instead.
        page = pages
        users, total, counts = database.search_users(
            **filters, limit=per_page, offset=(page - 1) * per_page
        )
    activity = database.user_activity([user["id"] for user in users])
    return {
        "items": [
            {**user, **{"sessions": 0, "tokens": 0, "repositories": 0}, **activity.get(user["id"], {})}
            for user in users
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "counts": counts,
        "accounts_enabled": settings.accounts_enabled,
    }


def admin_target(user_id: str) -> dict[str, Any]:
    target = database.get_user(user_id, include_secret=False)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    return target


@app.get("/api/admin/users/{user_id}")
def admin_user_detail(user_id: str, _: UserManager) -> dict:
    """One account as an administrator sees it. Tokens show only the prefix
    kept when they were created; hashes and passwords never leave the server."""
    target = admin_target(user_id)
    accounts = settings.accounts_enabled
    return {
        "user": target,
        "external": is_external(target),
        "accounts_enabled": accounts,
        "local_password": accounts and not is_external(target),
        "organizations": database.user_organizations(user_id),
        "repositories": database.owned_repository_ids(user_id),
        "sessions": (
            [public_session(session, None) for session in database.list_sessions(user_id)]
            if accounts
            else []
        ),
        "tokens": database.list_api_tokens(user_id) if accounts else [],
    }


@app.delete("/api/admin/users/{user_id}/tokens/{token_id}")
def admin_revoke_token(user_id: str, token_id: str, _: UserAdmin) -> dict:
    require_accounts()
    admin_target(user_id)
    if not database.delete_api_token(user_id, token_id):
        raise HTTPException(status_code=404, detail="Token not found.")
    return {"status": "revoked"}


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: str, payload: AdminUserUpdate, admin: UserAdmin) -> dict:
    target = admin_target(user_id)
    changes: dict[str, Any] = {}
    if payload.role is not None and payload.role != target["role"]:
        if user_id == admin["id"]:
            raise HTTPException(status_code=409, detail="You cannot change your own role.")
        changes["role"] = payload.role
    if payload.disabled is not None and payload.disabled != target["disabled"]:
        if user_id == admin["id"]:
            raise HTTPException(status_code=409, detail="You cannot disable your own account.")
        changes["disabled"] = int(payload.disabled)
    if (payload.display_name is not None or payload.email is not None) and is_external(target):
        raise HTTPException(
            status_code=409,
            detail="This account's name and email come from the identity provider; change them there.",
        )
    if payload.display_name is not None:
        changes["display_name"] = payload.display_name.strip()
    if payload.email is not None:
        changes["email"] = clean_email(payload.email)
    if not changes:
        return target
    changes["updated_at"] = utc_iso()
    try:
        updated = database.update_user_guarded(user_id, changes)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if changes.get("disabled"):
        database.delete_user_sessions(user_id)
    return updated


@app.post("/api/admin/users/{user_id}/password")
def admin_reset_password(user_id: str, payload: AdminPasswordReset, admin: UserAdmin) -> dict:
    require_accounts()
    target = admin_target(user_id)
    if user_id == admin["id"]:
        # A reset skips the current password and signs out every session,
        # including this one; your own password changes from your account.
        raise HTTPException(
            status_code=409, detail="Change your own password from your account's Profile tab."
        )
    if is_external(target):
        raise HTTPException(
            status_code=409, detail="This account signs in through an external provider."
        )
    try:
        validate_password(payload.new_password)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    database.update_user_password(user_id, hash_password(payload.new_password), utc_iso())
    database.delete_user_sessions(user_id)
    return {"status": "password_reset"}


@app.post("/api/admin/users/{user_id}/revoke")
def admin_revoke(user_id: str, payload: AdminRevokeRequest, _: UserAdmin) -> dict:
    admin_target(user_id)
    if payload.sessions:
        database.delete_user_sessions(user_id)
    if payload.tokens:
        database.delete_user_tokens(user_id)
    return {"status": "revoked"}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: str, admin: UserAdmin) -> dict:
    admin_target(user_id)
    if user_id == admin["id"]:
        raise HTTPException(status_code=409, detail="You cannot delete your own account.")
    # Organization repositories stay with the organization; the admin becomes their creator.
    database.reassign_organization_repositories(user_id, admin["id"])
    owned = database.owned_repository_ids(user_id)
    if owned:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This account owns {len(owned)} repositor{'y' if len(owned) == 1 else 'ies'} "
                f"({', '.join(owned[:5])}). Disable the account instead, or delete them first."
            ),
        )
    try:
        database.delete_user(user_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"status": "deleted"}


@app.get("/api/admin/permissions")
def admin_permissions(_: UserManager) -> dict:
    return permission_matrix(disabled_capabilities())


def public_database_target() -> str:
    if database.backend == "sqlite":
        return str(settings.database_path)
    parts = urlsplit(settings.database_url or "")
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{parts.scheme}://{host}{parts.path}"


@app.get("/api/admin/server")
def admin_server(_: SettingsViewer) -> dict:
    """Read-only configuration. Secret values are reported only as configured or not."""
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "accounts": {
            "enabled": settings.accounts_enabled,
            "secure_cookies": settings.secure_cookies,
            "session_ttl_hours": settings.session_ttl_hours,
        },
        "database": {"backend": database.backend, "target": public_database_target()},
        "storage": {
            "model_path": str(settings.model_storage),
            "data_path": str(settings.data_dir),
            "default_target": storages.default_id,
            "targets": [
                {
                    **storage.describe(),
                    "credentials_configured": bool(getattr(getattr(storage, "target", None), "secrets", ())),
                }
                for storage in storages.all()
            ],
        },
        "uploads": {
            "chunk_mb": settings.upload_chunk_mb,
            "max_file_gb": settings.max_upload_size_gb,
        },
        "pulls": {
            "hub_api_enabled": settings.hub_api_enabled,
            "public_url": settings.public_url,
        },
        "sso": {
            "enabled": settings.oidc_enabled,
            "provider_name": settings.oidc_provider_name,
            "issuer": settings.oidc_issuer,
            "client_id": settings.oidc_client_id,
            "client_secret_configured": bool(settings.oidc_client_secret),
            "scopes": settings.oidc_scopes,
            "default_role": settings.oidc_default_role,
            "allowed_groups": list(settings.oidc_groups),
            "redirect_url": settings.oidc_redirect_url
            or (f"{settings.public_url}/api/auth/oidc/callback" if settings.public_url else None),
        },
        "hugging_face": {
            "downloads_enabled": settings.hf_downloads_enabled,
            "endpoint": settings.hf_endpoint,
            "token_configured": bool(settings.hf_token),
            "max_concurrent_downloads": settings.max_concurrent_downloads,
            "workers_per_download": settings.download_workers_per_job,
        },
        "runtimes": {
            "targets": runtimes.public_targets(),
            "api_token_configured": bool(settings.runtime_api_token),
        },
    }


@app.get("/api/library/models")
def search_library_models(
    user: Browser,
    search: Annotated[str, Query(max_length=200)] = "",
    sort: Literal["updated", "name", "size", "parameters"] = "updated",
    task: Annotated[str, Query(max_length=400)] = "",
    precision: Annotated[str, Query(max_length=100)] = "",
    hardware: Annotated[str, Query(max_length=200)] = "",
    parameters: Annotated[str, Query(max_length=100)] = "",
    owner: Annotated[str, Query(max_length=64)] = "",
) -> dict:
    models = database.list_visible_local_models(user["id"])
    try:
        return search_catalog(
            models,
            database.saved_repo_ids(user["id"]),
            search=search,
            sort=sort,
            task=task,
            precision=precision,
            hardware=hardware,
            parameters=parameters,
            owner=owner,
            # Only tags of models this user can see, so counts reveal nothing else.
            hardware_tags=database.model_hardware([model["repo_id"] for model in models]),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


class HardwareRequest(BaseModel):
    hardware: list[str] = Field(default_factory=list, max_length=len(HARDWARE))


@app.put("/api/library/hardware")
def update_model_hardware(
    payload: HardwareRequest,
    user: Editor,
    repo_id: Annotated[str, Query(max_length=200)],
) -> dict:
    """Tag a model with the GPUs it is known to run on. Anyone who may upload
    changes to the repository may edit its tags."""
    model = visible_model(repo_id, user["id"])
    if not uploads.can_edit(model["repo_id"], user):
        raise HTTPException(status_code=403, detail="You cannot edit this model.")
    unknown = sorted(set(payload.hardware) - set(HARDWARE))
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown hardware: {', '.join(unknown)}.")
    chosen = [item for item in HARDWARE if item in payload.hardware]
    database.set_model_hardware(model["repo_id"], chosen)
    return {"repo_id": model["repo_id"], "hardware": chosen}


class ListingPreviewRequest(BaseModel):
    repo_id: str | None = Field(default=None, max_length=200)
    paths: list[str] = Field(default_factory=list)
    config: str | None = None
    quant_config: str | None = None
    readme: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class ListingRequest(BaseModel):
    overrides: dict[str, Any] = Field(default_factory=dict)


def detected_listing(model: dict[str, Any]) -> dict[str, Any]:
    return {**(model.get("detected") or {}), "model_type": (model.get("config") or {}).get("model_type")}


def listing_view(repo_id: str, detected: dict[str, Any], overrides: dict[str, Any]) -> dict:
    """What the files say, what people changed, and how the model ends up listed."""
    listed = {**detected, **overrides}
    return {
        "detected": detected,
        "overrides": overrides,
        # The library shows no task for a config.model_type fallback, and a size the
        # name agrees with over the exact count.
        "listed_task": model_task({"pipeline_tag": listed.get("pipeline_tag"), "config": {"model_type": detected.get("model_type")}}),
        "nominal_parameters": nominal_parameters(
            {
                "id": repo_id,
                "parameter_count": listed.get("parameter_count"),
                "parameters_corrected": "parameter_count" in overrides,
            }
        ),
        "precisions": PRECISIONS,
    }


@app.post("/api/uploads/preview")
def preview_upload_listing(payload: ListingPreviewRequest, _: Uploader) -> dict:
    """How the library will list a model, read from the files an upload is about to
    send: its config, quantization config, model card, and weight headers."""
    try:
        detected = preview_listing(
            payload.paths, payload.config, payload.quant_config, payload.readme, payload.headers
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return listing_view(payload.repo_id or "local/model", detected, {})


@app.put("/api/repos/listing")
def update_listing(
    payload: ListingRequest,
    user: Editor,
    repo_id: Annotated[str, Query(max_length=200)],
) -> dict:
    """Correct how a model is listed. Works as soon as an upload's repository
    exists, before its files arrive."""
    try:
        validated = validate_repo_id(repo_id)
        overrides = validate_overrides(payload.overrides)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    owned = database.get_owned_repository(validated)
    model = database.get_visible_local_model(user["id"], validated)
    if owned:
        if uploads.access(owned, user["id"]) is None and not can(user, "repos.edit_any"):
            raise HTTPException(status_code=404, detail="Repository not found.")
    elif not model:
        raise HTTPException(status_code=404, detail="Repository not found.")
    if not uploads.can_edit(validated, user):
        raise HTTPException(status_code=403, detail="You cannot edit this model.")
    database.set_listing_overrides(validated, overrides, utc_iso(), user["id"])
    current = database.get_local_model(validated)
    return listing_view(validated, detected_listing(current) if current else {}, overrides)


class ConfigFile(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=600_000)


class ConfigRevisionRequest(BaseModel):
    parent_id: str | None = Field(default=None, max_length=40)
    message: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    files: list[ConfigFile] = Field(default_factory=list, max_length=60)
    deletions: list[str] = Field(default_factory=list, max_length=60)
    results: dict[str, Any] | None = None


class ConfigResultsRequest(BaseModel):
    results: dict[str, Any]


def config_revisions() -> ConfigRevisions:
    return ConfigRevisions(database, history)


def editable_model(repo_id: str, user: dict[str, Any]) -> dict[str, Any]:
    model = visible_model(repo_id, user["id"])
    if not uploads.can_edit(model["repo_id"], user):
        raise HTTPException(status_code=403, detail="You cannot edit this model.")
    return model


@app.get("/api/library/configs")
def list_config_revisions(
    user: Browser, repo_id: Annotated[str, Query(max_length=200)]
) -> dict:
    """Deployment config revisions of a model, newest first, with their results."""
    model = visible_model(repo_id, user["id"])
    return {
        "items": config_revisions().list(model["repo_id"]),
        "metrics": METRICS,
        "hardware": [[key, label] for key, label in HARDWARE.items()],
        "can_edit": uploads.can_edit(model["repo_id"], user),
    }


@app.get("/api/library/config")
def get_config_revision(
    user: Browser,
    repo_id: Annotated[str, Query(max_length=200)],
    revision_id: Annotated[str, Query(max_length=40)],
) -> dict:
    model = visible_model(repo_id, user["id"])
    try:
        return config_revisions().detail(model["repo_id"], revision_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/library/config/archive")
def download_config_revision(
    user: Browser,
    repo_id: Annotated[str, Query(max_length=200)],
    revision_id: Annotated[str, Query(max_length=40)],
) -> Response:
    """Every file of one revision as a zip, for copying to a GPU host."""
    model = visible_model(repo_id, user["id"])
    try:
        name, payload = config_revisions().archive(model["repo_id"], revision_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(
        payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.post("/api/library/configs", status_code=201)
def create_config_revision(
    payload: ConfigRevisionRequest,
    user: Editor,
    repo_id: Annotated[str, Query(max_length=200)],
) -> dict:
    model = editable_model(repo_id, user)
    try:
        return config_revisions().create(
            model["repo_id"],
            user,
            payload.parent_id,
            [item.model_dump() for item in payload.files],
            payload.deletions,
            payload.message,
            payload.description,
            payload.results,
        )
    except (RuntimeError, *INTEGRITY_ERRORS) as error:
        raise HTTPException(
            status_code=409,
            detail="Someone added a revision since you started. Reload and try again.",
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.put("/api/library/config/results")
def update_config_results(
    payload: ConfigResultsRequest,
    user: Editor,
    repo_id: Annotated[str, Query(max_length=200)],
    revision_id: Annotated[str, Query(max_length=40)],
) -> dict:
    model = editable_model(repo_id, user)
    try:
        return config_revisions().set_results(model["repo_id"], revision_id, user, payload.results)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def library_listing(model: dict[str, Any]) -> dict[str, Any]:
    root = settings.model_storage / model["relative_path"]
    if model["storage_backend"] == "s3" and (not model["cached"] or not root.is_dir()):
        database.set_local_model_cached(model["repo_id"], False)
        model["cached"] = False
        listing = storages.for_model(model).list_repository_files(model["repo_id"])
    else:
        listing = indexer.files_for_model(model["repo_id"])
    if not listing:
        raise HTTPException(status_code=404, detail="Model files were not found.")
    return listing


@app.get("/api/library/gguf-range")
async def library_gguf_range(
    request: Request,
    user: Browser,
    repo_id: Annotated[str, Query(max_length=200)],
    filename: Annotated[str, Query(max_length=500)],
) -> Response:
    model = visible_model(repo_id, user["id"])
    try:
        result = await run_in_threadpool(
            catalog.gguf_range, model, filename, request.headers.get("Range")
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=416, detail=str(error)) from error
    return Response(
        content=result["content"],
        status_code=result["status_code"],
        media_type="application/octet-stream",
        headers=result["headers"],
    )


@app.get("/api/library/asset")
async def library_asset(
    user: Browser,
    repo_id: Annotated[str, Query(max_length=200)],
    path: Annotated[str, Query(max_length=500)],
) -> Response:
    model = visible_model(repo_id, user["id"])
    try:
        content, content_type = await run_in_threadpool(catalog.asset, model, path)
    except PermissionError as error:
        raise HTTPException(status_code=415, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@app.get("/api/library/file")
def library_file(
    request: Request,
    user: Browser,
    repo_id: Annotated[str, Query(max_length=200)],
    path: Annotated[str, Query(max_length=500)],
) -> Response:
    """Download one file with the signed-in user's visibility, including private repos."""
    return leased(repo_id, lambda: _library_file(request, user, repo_id, path))


def _library_file(request: Request, user: dict[str, Any], repo_id: str, path: str) -> Response:
    model = visible_model(repo_id, user["id"])
    snapshot = hub_repositories.snapshot_for_model(model)
    entry = snapshot.entry(path)
    if entry is None:
        raise HTTPException(status_code=404, detail="File not found in this model.")
    name = PurePosixPath(entry.path).name
    fallback = "".join(
        character if character.isascii() and character.isprintable() and character not in '"\\' else "_"
        for character in name
    )
    return repository_file_response(
        request,
        snapshot,
        entry,
        {
            "Content-Disposition": (
                f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe="")}'
            )
        },
    )


class ChangeStartRequest(BaseModel):
    repo_id: str

    @field_validator("repo_id")
    @classmethod
    def repo_is_valid(cls, value: str) -> str:
        return validate_repo_id(value)


class ChangeCommitRequest(BaseModel):
    message: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    deletions: list[str] = Field(default_factory=list, max_length=10000)


class FinalizeRequest(BaseModel):
    message: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=5000)


@app.get("/api/library/commits")
def library_commits(
    user: Browser,
    repo_id: Annotated[str, Query(max_length=200)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    model = visible_model(repo_id, user["id"])
    return {
        "items": database.list_commits(model["repo_id"], limit, offset),
        "total": database.count_commits(model["repo_id"]),
    }


@app.get("/api/library/commit")
def library_commit(
    user: Browser,
    repo_id: Annotated[str, Query(max_length=200)],
    commit_id: Annotated[str, Query(min_length=40, max_length=40)],
) -> dict:
    model = visible_model(repo_id, user["id"])
    commit = database.get_commit(model["repo_id"], commit_id)
    if not commit:
        raise HTTPException(status_code=404, detail="Commit not found.")
    result = public_commit(commit)
    result["changes"] = [history.diff(change) for change in commit["changes"]]
    return result


def change_errors(error: Exception) -> HTTPException:
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, (FileExistsError, RuntimeError)):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))


@app.post("/api/repos/rename")
async def rename_repository(
    payload: RenameRepositoryRequest,
    user: RepoManager,
    repo_id: Annotated[str, Query(max_length=200)],
) -> dict:
    """Rename a repository or move it to another owner."""
    try:
        return await run_in_threadpool(
            uploads.rename_repository,
            repo_id,
            user,
            payload.namespace,
            payload.name,
            payload.confirmation,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (FileExistsError, *INTEGRITY_ERRORS) as error:
        raise HTTPException(status_code=409, detail=str(error) or "That name is taken.") from error
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.delete("/api/repos")
async def delete_model_repository(
    payload: DeleteRepositoryRequest,
    user: RepoManager,
    repo_id: Annotated[str, Query(max_length=200)],
) -> dict:
    """Delete an upload (its admins) or any other model (server administrators)."""
    try:
        await run_in_threadpool(uploads.delete_model, repo_id, user, payload.confirmation)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"status": "deleted"}


@app.post("/api/repos/changes", status_code=201)
def start_repository_change(payload: ChangeStartRequest, user: Editor) -> dict:
    visible_model(payload.repo_id, user["id"])
    try:
        return uploads.start_change(payload.repo_id, user)
    except (PermissionError, FileNotFoundError, ValueError, OSError) as error:
        raise change_errors(error) from error


@app.get("/api/repos/changes/{session_id}/files/status")
def repository_change_file_status(
    session_id: str, user: Browser, path: Annotated[str, Query(max_length=500)]
) -> dict:
    try:
        return uploads.change_file_status(session_id, user, path)
    except (FileNotFoundError, ValueError) as error:
        raise change_errors(error) from error


async def read_upload_chunk(request: Request) -> tuple[int, int, bytes]:
    try:
        offset = int(request.headers.get("Upload-Offset", "-1"))
        total = int(request.headers.get("Upload-Length", "-1"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Upload headers are invalid.") from error
    limit = settings.upload_chunk_mb * 1024**2
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise HTTPException(status_code=413, detail="Upload chunk is too large.")
        chunks.append(chunk)
    return offset, total, b"".join(chunks)


@app.put("/api/repos/changes/{session_id}/files")
async def repository_change_chunk(
    session_id: str,
    request: Request,
    user: Editor,
    path: Annotated[str, Query(max_length=500)],
) -> dict:
    offset, total, payload = await read_upload_chunk(request)
    try:
        return await run_in_threadpool(
            uploads.change_chunk, session_id, user, path, offset, total, payload
        )
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError, OSError) as error:
        raise change_errors(error) from error


@app.post("/api/repos/changes/{session_id}/commit")
async def commit_repository_change(
    session_id: str, payload: ChangeCommitRequest, user: Editor
) -> dict:
    try:
        result = await run_in_threadpool(
            uploads.commit_change,
            session_id,
            user,
            payload.message,
            payload.description,
            payload.deletions,
        )
    except (PermissionError, FileNotFoundError, ValueError, OSError) as error:
        raise change_errors(error) from error
    return {
        "model": result["model"],
        "commit": public_commit(result["commit"]) if result["commit"] else None,
    }


@app.delete("/api/repos/changes/{session_id}")
def abort_repository_change(session_id: str, user: Editor) -> dict:
    try:
        uploads.abort_change(session_id, user)
    except FileNotFoundError as error:
        raise change_errors(error) from error
    return {"status": "aborted"}


@app.get("/api/library/models/{repo_id:path}")
async def library_model(repo_id: str, user: Browser) -> dict:
    model = visible_model(repo_id, user["id"])
    listing = await run_in_threadpool(library_listing, model)
    details = await run_in_threadpool(
        catalog.details, model, listing, database.saved_repo_ids(user["id"])
    )
    latest = database.latest_commit(model["repo_id"])
    last_commits = await run_in_threadpool(
        history.last_commits, model["repo_id"], [file["path"] for file in details["files"]]
    )
    for file in details["files"]:
        file["last_commit"] = last_commits.get(file["path"])
    details["latest_commit"] = public_commit(latest) if latest else None
    details["commit_count"] = database.count_commits(model["repo_id"])
    details["config_count"] = database.count_config_revisions(model["repo_id"])
    details["can_edit"] = uploads.can_edit(model["repo_id"], user)
    details["can_manage"] = uploads.can_manage(model["repo_id"], user)
    tagged = database.model_hardware([model["repo_id"]]).get(model["repo_id"], [])
    details["hardware"] = [key for key in HARDWARE if key in tagged]
    details["hardware_options"] = [[key, label] for key, label in HARDWARE.items()]
    details["listing"] = listing_view(
        model["repo_id"], detected_listing(model), model.get("listing_overrides") or {}
    )
    owned = database.get_owned_repository(model["repo_id"])
    details["visibility"] = owned["visibility"] if owned else "public"
    details["owned"] = bool(owned)
    details["storage_target_name"] = storages.for_model(model).name
    details["description"] = owned["description"] if owned else ""
    organization = database.get_organization(model["repo_id"].split("/", 1)[0])
    details["organization"] = (
        {"name": organization["name"], "display_name": organization["display_name"]}
        if organization
        else None
    )
    return details


def can_access_download(download: dict[str, Any], user: dict[str, Any]) -> bool:
    return (
        download.get("user_id") == user["id"]
        or (can(user, "users.manage") and download.get("user_id") is None)
    )


@app.get("/api/downloads")
def list_downloads(user: HubReader) -> dict:
    items = database.list_downloads(
        user_id=user["id"], include_unowned=can(user, "users.manage")
    )
    return {
        "items": items,
        "active": sum(
            item["status"] in {"queued", "preparing", "downloading"} for item in items
        ),
    }


@app.get("/api/downloads/{download_id}")
def get_download(download_id: str, user: HubReader) -> dict:
    download = database.get_download(download_id)
    if not download or not can_access_download(download, user):
        raise HTTPException(status_code=404, detail="Download not found")
    return download


@app.post("/api/downloads", status_code=202)
def start_download(payload: DownloadRequest, user: HubWriter) -> dict:
    if moving := moves.moving(payload.repo_id):
        raise HTTPException(status_code=409, detail=moving)
    if database.get_owned_repository(payload.repo_id):
        raise HTTPException(
            status_code=409,
            detail="An account-owned repository already uses this storage path.",
        )
    storage_target = payload.storage_target
    if storage_target or not database.get_local_model(payload.repo_id):
        try:
            storage_target = uploads.choose_storage(user, storage_target).id
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
    try:
        return downloads.queue(
            payload.repo_id,
            payload.revision,
            payload.allow_patterns,
            payload.ignore_patterns,
            payload.mode,
            user_id=user["id"],
            storage_target=storage_target,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/downloads/{download_id}/cancel")
def cancel_download(download_id: str, user: HubWriter) -> dict:
    existing = database.get_download(download_id)
    if not existing or not can_access_download(existing, user):
        raise HTTPException(status_code=404, detail="Download not found")
    try:
        download = downloads.cancel(download_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return download


@app.get("/api/local-models")
def local_models(
    user: Browser, query: Annotated[str, Query(max_length=200)] = ""
) -> dict:
    items = database.list_visible_local_models(user["id"], query)
    return {
        "items": items,
        "count": len(items),
        "total_bytes": sum(item["size_bytes"] for item in items),
    }


@app.post("/api/local-models/scan")
async def scan_local_models(_: Scanner) -> dict:
    return await run_in_threadpool(refresh_model_index)


def visible_model(repo_id: str, user_id: str) -> dict[str, Any]:
    try:
        validated = validate_repo_id(repo_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    model = database.get_visible_local_model(user_id, validated)
    if not model:
        raise HTTPException(status_code=404, detail="Local model not found")
    return model


@app.post("/api/local-models/{repo_id:path}/restore")
async def restore_local_model(repo_id: str, user: CacheManager) -> dict:
    model = visible_model(repo_id, user["id"])
    if model["storage_backend"] != "s3":
        raise HTTPException(status_code=409, detail="This model is not backed by S3.")
    if database.find_active_download(model["repo_id"]):
        raise HTTPException(
            status_code=409,
            detail="Wait for the active download to finish before restoring this cache.",
        )
    if moving := moves.moving(model["repo_id"]):
        raise HTTPException(status_code=409, detail=moving)
    try:
        root = await run_in_threadpool(
            storages.for_model(model).restore_repository, model["repo_id"]
        )
        await run_in_threadpool(indexer.index_path, root)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    result = indexer.files_for_model(model["repo_id"])
    if not result:
        raise HTTPException(status_code=404, detail="Local model not found")
    return result


@app.delete("/api/local-models/{repo_id:path}/cache")
async def evict_local_model_cache(repo_id: str, user: CacheManager) -> dict:
    model = visible_model(repo_id, user["id"])
    if model["storage_backend"] != "s3":
        raise HTTPException(status_code=409, detail="Only S3-backed models have a removable cache.")
    if database.find_active_download(model["repo_id"]):
        raise HTTPException(
            status_code=409,
            detail="Wait for the active download to finish before removing this cache.",
        )
    if moving := moves.moving(model["repo_id"]):
        raise HTTPException(status_code=409, detail=moving)
    try:
        await run_in_threadpool(
            storages.for_model(model).evict_repository_cache, model["repo_id"]
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    updated = database.set_local_model_cached(model["repo_id"], False)
    return {"status": "evicted", "model": updated}


@app.get("/api/local-models/{repo_id:path}")
def local_model(repo_id: str, user: Browser) -> dict:
    model = visible_model(repo_id, user["id"])
    result = library_listing(model)
    result["model"] = database.get_local_model(model["repo_id"])
    return result


@app.get("/api/storage/options")
def storage_options(
    user: Browser,
    namespace: Annotated[str | None, Query(max_length=64)] = None,
) -> dict:
    """Targets this user may put a new repository in (for `namespace` when given),
    without connection details."""
    if not can(user, "repos.create") and not can(user, "hub.download"):
        return {"default": None, "items": []}
    try:
        items = uploads.storage_choices(user, namespace)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    try:
        default = uploads.choose_storage(user, None, namespace).id
    except PermissionError:
        default = None
    free = shutil.disk_usage(settings.model_storage).free
    for item in items:
        item["free_bytes"] = None if storages.get(item["id"]).remote else free
    return {"default": default, "items": items}


def storage_model_summary(model: dict[str, Any]) -> dict[str, Any]:
    owned = database.get_owned_repository(model["repo_id"])
    return {
        "repo_id": model["repo_id"],
        "size_bytes": int(model.get("size_bytes") or 0),
        "file_count": int(model.get("file_count") or 0),
        "parameter_count": model.get("parameter_count"),
        "formats": model.get("formats") or [],
        "cached": bool(model.get("cached")),
        "storage_backend": model.get("storage_backend"),
        "modified_at": model.get("modified_at"),
        "visibility": owned["visibility"] if owned else "public",
    }


@app.get("/api/storage/targets")
async def storage_targets(_: StorageViewer) -> dict:
    models = database.list_local_models()
    grants = database.storage_grants()
    usage = shutil.disk_usage(settings.model_storage)
    healths = await asyncio.gather(
        *(run_in_threadpool(storage.health) for storage in storages.all())
    )
    targets = []
    for storage, health_result in zip(storages.all(), healths):
        items = [
            storage_model_summary(model)
            for model in models
            if storages.for_model(model).id == storage.id
        ]
        items.sort(key=lambda item: -item["size_bytes"])
        target = {
            **storage.describe(),
            "default": storage.id == storages.default_id,
            "connected": bool(health_result.get("connected")),
            "error": storage_errors.get(storage.id) or health_result.get("error"),
            "model_count": len(items),
            "total_bytes": sum(item["size_bytes"] for item in items),
            "cached_count": sum(1 for item in items if item["cached"]),
            "models": items,
            "capacity": None,
            "grants": grants.get(storage.id, []),
        }
        if not storage.remote:
            target["capacity"] = {
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
            }
        targets.append(target)
    cached = [model for model in models if model.get("cached")]
    return {
        "default": storages.default_id,
        "cache": {
            "path": str(settings.model_storage),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "model_count": len(cached),
            "model_bytes": sum(int(model.get("size_bytes") or 0) for model in cached),
        },
        "targets": targets,
        "conflicts": storage_conflicts,
    }


class MoveRequest(BaseModel):
    repo_id: str = Field(min_length=3, max_length=200)
    destination: str = Field(min_length=1, max_length=40)
    confirmation: str = Field(max_length=200)
    keep_local: bool = False


@app.get("/api/storage/moves")
def list_storage_moves(_: StorageViewer) -> dict:
    return {"items": moves.overview()}


@app.post("/api/storage/moves", status_code=202)
def start_storage_move(payload: MoveRequest, user: StorageManager) -> dict:
    """Copy a model to another location, check every hash, switch it over, and
    remove the old copy once nobody is downloading from it."""
    try:
        return moves.start(user, payload.repo_id, payload.destination, payload.confirmation, payload.keep_local)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/storage/moves/{move_id}/cancel")
def cancel_storage_move(move_id: str, _: StorageManager) -> dict:
    try:
        return moves.cancel(move_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.put("/api/storage/targets/{target_id}/grants")
def update_storage_grants(
    target_id: str, payload: StorageGrantRequest, _: StorageManager
) -> dict:
    """Reserve a storage target for these users and organizations; none opens it to all."""
    try:
        storage = storages.get(target_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    user_ids = []
    for username in dict.fromkeys(payload.users):
        account = database.get_user_by_username(username)
        if not account:
            raise HTTPException(status_code=400, detail=f"No user named {username}.")
        user_ids.append(account["id"])
    organization_ids = []
    for name in dict.fromkeys(payload.organizations):
        organization = database.get_organization(name)
        if not organization:
            raise HTTPException(status_code=400, detail=f"No organization named {name}.")
        organization_ids.append(organization["id"])
    database.set_storage_grants(storage.id, user_ids, organization_ids, utc_iso())
    return {"target_id": storage.id, "grants": database.storage_grants().get(storage.id, [])}


def require_runtime_admin(user: dict[str, Any]) -> None:
    if not can(user, "runtimes.use"):
        raise HTTPException(
            status_code=403, detail="Your role cannot use runtimes."
        )


def runtime_api_principal(request: Request) -> dict[str, Any] | None:
    expected = settings.runtime_api_token
    authorization = request.headers.get("Authorization") or ""
    if not expected or not authorization.startswith("Bearer "):
        return None
    supplied = authorization.removeprefix("Bearer ")
    if supplied.startswith(API_TOKEN_PREFIX):
        # A personal token: handled by the normal account and capability checks.
        return None
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid runtime API token.")
    # The runtime automation token may only use runtimes, never anything else.
    return {"id": None, "role": "runtime", "capabilities": {"runtimes.use"}, "runtime_api": True}


def require_runtime_reader(request: Request) -> dict[str, Any]:
    principal = runtime_api_principal(request)
    if principal:
        return principal
    user = require_user(request)
    require_runtime_admin(user)
    return user


def require_runtime_writer(request: Request) -> dict[str, Any]:
    principal = runtime_api_principal(request)
    if principal:
        return principal
    user = require_write_user(request, require_user(request))
    require_runtime_admin(user)
    return user


RuntimeReader = Annotated[dict[str, Any], Depends(require_runtime_reader)]
RuntimeWriter = Annotated[dict[str, Any], Depends(require_runtime_writer)]


@app.get("/api/runtimes")
def list_runtimes(_: RuntimeReader) -> dict:
    return {"items": runtimes.public_targets()}


@app.get("/api/runtime-jobs")
def list_runtime_jobs(
    _: RuntimeReader, limit: Annotated[int, Query(ge=1, le=200)] = 100
) -> dict:
    items = database.list_runtime_jobs(limit)
    return {
        "items": items,
        "active": sum(
            item["status"] in {"queued", "preparing", "transferring", "loading"}
            for item in items
        ),
    }


@app.get("/api/runtime-jobs/{job_id}")
def get_runtime_job(job_id: str, _: RuntimeReader) -> dict:
    job = database.get_runtime_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Runtime job not found.")
    return job


@app.post("/api/runtimes/{target_id}/load", status_code=202)
def load_runtime_model(
    target_id: str, payload: RuntimeLoadRequest, principal: RuntimeWriter
) -> dict:
    model = (
        database.get_local_model(payload.repo_id)
        if principal.get("runtime_api")
        else visible_model(payload.repo_id, principal["id"])
    )
    if not model:
        raise HTTPException(status_code=404, detail="Local model not found")
    if moving := moves.moving(model["repo_id"]):
        raise HTTPException(status_code=409, detail=moving)
    try:
        return runtimes.queue(
            target_id,
            model,
            payload.runtime_model_name,
            payload.source_file,
            principal["id"],
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/collections")
def list_collections(user: Browser) -> dict:
    return {"items": database.list_collections(user["id"])}


@app.post("/api/collections", status_code=201)
def create_collection(payload: CollectionRequest, user: Saver) -> dict:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Collection name is required.")
    timestamp = utc_iso()
    try:
        return database.create_collection(
            {
                "id": uuid.uuid4().hex,
                "user_id": user["id"],
                "name": name,
                "description": payload.description.strip(),
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
    except INTEGRITY_ERRORS as error:
        raise HTTPException(
            status_code=409, detail="You already have a collection with that name."
        ) from error


@app.delete("/api/collections/{collection_id}")
def delete_collection(collection_id: str, user: Saver) -> dict:
    if not database.delete_collection(collection_id, user["id"]):
        raise HTTPException(status_code=404, detail="Collection not found.")
    return {"status": "deleted"}


def safe_saved_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "author",
        "pipeline_tag",
        "library_name",
        "license",
        "parameter_count",
        "last_modified",
        "local",
    }
    result = {key: value for key, value in metadata.items() if key in allowed}
    if len(json.dumps(result)) > 16_000:
        raise ValueError("Saved model metadata is too large.")
    return result


@app.get("/api/saved-models")
def list_saved_models(
    user: Browser,
    query: Annotated[str, Query(max_length=200)] = "",
    collection_id: Annotated[str, Query(max_length=100)] = "",
) -> dict:
    items = database.list_saved_models(user["id"], query, collection_id)
    return {"items": items, "count": len(items)}


@app.post("/api/saved-models")
def save_model(payload: SavedModelRequest, user: Saver) -> dict:
    timestamp = utc_iso()
    existing = database.get_saved_model(user["id"], payload.repo_id)
    try:
        return database.save_model(
            {
                "id": existing["id"] if existing else uuid.uuid4().hex,
                "user_id": user["id"],
                "repo_id": payload.repo_id,
                "note": payload.note.strip(),
                "metadata_json": json.dumps(safe_saved_metadata(payload.metadata)),
                "created_at": existing["created_at"] if existing else timestamp,
                "updated_at": timestamp,
            },
            payload.collection_ids,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.delete("/api/saved-models/{repo_id:path}")
def unsave_model(repo_id: str, user: Saver) -> dict:
    try:
        validated = validate_repo_id(repo_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not database.delete_saved_model(user["id"], validated):
        raise HTTPException(status_code=404, detail="Saved model not found.")
    return {"status": "removed"}


class OrganizationRequest(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    display_name: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=500)


class OrganizationUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)


class MemberRequest(BaseModel):
    role: Literal["admin", "write", "read"]


def organization_or_404(name: str) -> dict[str, Any]:
    organization = database.get_organization(name)
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return organization


def require_org_admin(organization: dict[str, Any], user: dict[str, Any]) -> bool:
    """True for server-wide organization managers, who may bypass org safeguards."""
    if can(user, "orgs.manage"):
        return True
    if database.organization_role(organization["id"], user["id"]) != "admin":
        raise HTTPException(status_code=403, detail="Only this organization's admins can do that.")
    return False


def organization_payload(organization: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    role = database.organization_role(organization["id"], user["id"])
    return {
        **organization,
        "my_role": role,
        "can_manage": can(user, "orgs.manage") or role == "admin",
        "can_upload": can(user, "repos.create") and role in {"admin", "write"},
        "members": database.organization_members(organization["id"]),
    }


@app.get("/api/organizations")
def list_organizations(user: Browser) -> dict:
    return {"items": database.list_organizations(user["id"])}


@app.get("/api/admin/organizations")
def admin_list_organizations(
    user: OrgOverseer,
    q: Annotated[str, Query(max_length=200)] = "",
    filter: Literal["", "with_repositories", "empty", "mine"] = "",
    sort: Literal["name", "newest", "repositories", "members"] = "name",
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=max(USER_PAGE_SIZES))] = 25,
) -> dict:
    options = {"query": q, "filter": filter or None, "sort": sort}
    items, total, counts = database.search_organizations(
        user["id"], **options, limit=per_page, offset=(page - 1) * per_page
    )
    pages = max(1, -(-total // per_page))
    if page > pages:
        page = pages
        items, total, counts = database.search_organizations(
            user["id"], **options, limit=per_page, offset=(page - 1) * per_page
        )
    return {"items": items, "total": total, "page": page, "per_page": per_page, "pages": pages, "counts": counts}


@app.post("/api/organizations", status_code=201)
def create_organization(payload: OrganizationRequest, user: OrgCreator) -> dict:
    try:
        name = validate_namespace(payload.name)
        timestamp = utc_iso()
        organization = database.create_organization(
            {
                "id": uuid.uuid4().hex,
                "name": name,
                "display_name": payload.display_name.strip() or name,
                "description": payload.description.strip(),
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
    except (ValueError, *INTEGRITY_ERRORS) as error:
        detail = (
            "That name is already used by a user or organization."
            if isinstance(error, INTEGRITY_ERRORS)
            else str(error)
        )
        raise HTTPException(status_code=409, detail=detail) from error
    database.set_organization_member(organization["id"], user["id"], "admin", timestamp)
    return organization_payload(organization, user)


@app.get("/api/organizations/{name}")
def get_organization(name: str, user: Browser) -> dict:
    return organization_payload(organization_or_404(name), user)


@app.patch("/api/organizations/{name}")
def update_organization(name: str, payload: OrganizationUpdate, user: OrgEditor) -> dict:
    organization = organization_or_404(name)
    require_org_admin(organization, user)
    changes = payload.model_dump(exclude_unset=True)
    if "display_name" in changes:
        changes["display_name"] = changes["display_name"].strip()
    if "description" in changes:
        changes["description"] = (changes["description"] or "").strip()
    database.update_organization(organization["id"], **changes, updated_at=utc_iso())
    return organization_payload(organization_or_404(name), user)


@app.delete("/api/organizations/{name}")
def delete_organization(name: str, _: OrgCreator) -> dict:
    organization = organization_or_404(name)
    try:
        database.delete_organization(organization["id"])
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"status": "deleted"}


@app.put("/api/organizations/{name}/members/{username}")
def set_organization_member(name: str, username: str, payload: MemberRequest, user: OrgEditor) -> dict:
    organization = organization_or_404(name)
    force = require_org_admin(organization, user)
    member = database.get_user_by_username(username)
    if not member:
        raise HTTPException(status_code=404, detail="User not found.")
    try:
        database.set_organization_member(
            organization["id"], member["id"], payload.role, utc_iso(), force=force
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return organization_payload(organization, user)


@app.delete("/api/organizations/{name}/members/{username}")
def remove_organization_member(name: str, username: str, user: OrgEditor) -> dict:
    organization = organization_or_404(name)
    member = database.get_user_by_username(username)
    if not member:
        raise HTTPException(status_code=404, detail="User not found.")
    # Anyone may leave; removing someone else takes an organization admin.
    force = False if member["id"] == user["id"] else require_org_admin(organization, user)
    try:
        removed = database.remove_organization_member(organization["id"], member["id"], force=force)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if not removed:
        raise HTTPException(status_code=404, detail="That user is not a member.")
    return organization_payload(organization, user)


@app.get("/api/uploads/namespaces")
def upload_namespaces(user: Browser) -> dict:
    return {"items": uploads.namespaces(user) if can(user, "repos.create") else []}


@app.get("/api/uploads/repositories")
def list_upload_repositories(user: UploadLister) -> dict:
    """Repositories this user can upload to (their own and writable organizations'),
    each with the role that decides which actions the Uploads page offers."""
    items = []
    for repository in database.list_owned_repositories(user["id"]):
        role = uploads.access(repository, user["id"])
        if role in {"admin", "write"}:
            # Listing corrections come along so resuming an upload shows and keeps them.
            items.append(
                {
                    **repository,
                    "my_role": role,
                    "listing_overrides": database.listing_overrides(repository["repo_id"]),
                }
            )
    return {"items": items}


@app.post("/api/uploads/repositories", status_code=201)
def create_upload_repository(payload: RepositoryRequest, user: Uploader) -> dict:
    try:
        return uploads.create_repository(
            user,
            payload.slug,
            payload.description,
            payload.visibility,
            payload.storage_target,
            payload.namespace,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (ValueError, FileExistsError, *INTEGRITY_ERRORS) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.patch("/api/uploads/repositories")
def update_upload_repository(
    payload: RepositoryUpdateRequest,
    user: Uploader,
    repo_id: Annotated[str, Query(max_length=200)],
) -> dict:
    try:
        return uploads.update_repository(
            validate_repo_id(repo_id),
            user,
            payload.description,
            payload.visibility,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/uploads/repositories/files/status")
def upload_file_status(
    user: Browser,
    repo_id: Annotated[str, Query(max_length=200)],
    path: Annotated[str, Query(max_length=500)],
) -> dict:
    try:
        return uploads.file_status(repo_id, user["id"], path)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.put("/api/uploads/repositories/files")
async def upload_file_chunk(
    request: Request,
    user: Uploader,
    repo_id: Annotated[str, Query(max_length=200)],
    path: Annotated[str, Query(max_length=500)],
) -> dict:
    try:
        offset = int(request.headers.get("Upload-Offset", "-1"))
        total = int(request.headers.get("Upload-Length", "-1"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Upload headers are invalid.") from error
    try:
        content_length = int(request.headers.get("Content-Length", "0") or 0)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Content length is invalid.") from error
    if content_length > settings.upload_chunk_mb * 1024**2:
        raise HTTPException(status_code=413, detail="Upload chunk is too large.")
    chunks: list[bytes] = []
    received = 0
    limit = settings.upload_chunk_mb * 1024**2
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise HTTPException(status_code=413, detail="Upload chunk is too large.")
        chunks.append(chunk)
    payload = b"".join(chunks)
    try:
        return await run_in_threadpool(
            uploads.upload_chunk,
            repo_id,
            user["id"],
            path,
            offset,
            total,
            payload,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/uploads/repositories/finalize")
async def finalize_upload_repository(
    user: Uploader,
    repo_id: Annotated[str, Query(max_length=200)],
    payload: FinalizeRequest | None = None,
) -> dict:
    payload = payload or FinalizeRequest()
    try:
        return await run_in_threadpool(
            uploads.finalize, repo_id, user["id"], payload.message, payload.description
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.delete("/api/uploads/repositories")
async def delete_upload_repository(
    payload: DeleteRepositoryRequest,
    user: Uploader,
    repo_id: Annotated[str, Query(max_length=200)],
) -> dict:
    try:
        await run_in_threadpool(
            uploads.delete_repository, repo_id, user, payload.confirmation
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"status": "deleted"}


# Hugging Face Hub protocol for vLLM, Transformers, and the hf CLI (HF_ENDPOINT),
# plus read-only git and Git LFS for `git clone`. Anonymous by design: only models
# visible to every account are served, and private uploads never are.


@app.exception_handler(HubError)
async def hub_error_handler(_: Request, error: HubError) -> JSONResponse:
    headers = {"X-Error-Code": error.code, "X-Error-Message": error.message}
    if error.commit:
        headers["X-Repo-Commit"] = error.commit
    if error.status_code == 401:
        # git only sends credentials after a Basic challenge.
        headers["WWW-Authenticate"] = 'Basic realm="HuggingHack"'
    return JSONResponse({"error": error.message}, status_code=error.status_code, headers=headers)


def pull_user(request: Request) -> dict[str, Any] | None:
    """The account behind a pull, from a personal API token or a browser session.

    huggingface_hub sends `Authorization: Bearer <token>`; git and git-lfs send
    Basic auth with the token as the password. Anonymous pulls return None.
    """
    if not settings.accounts_enabled:
        return None
    authorization = request.headers.get("Authorization") or ""
    scheme, _, credential = authorization.partition(" ")
    token = ""
    if scheme.lower() == "bearer":
        token = credential.strip()
    elif scheme.lower() == "basic":
        try:
            _, _, token = base64.b64decode(credential.strip()).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            token = ""
    # Only HuggingHack tokens are checked. Clients often send a stored Hugging Face
    # token (HF_TOKEN, `hf auth login`) or a git credential meant for another host;
    # those pull anonymously instead of failing.
    if token.startswith(API_TOKEN_PREFIX):
        principal = auth.token_principal(token)
        if not principal:
            raise HubError("Unauthorized", "Invalid credentials.", status_code=401)
        return principal["user"]
    session = session_for_request(request)
    return session["user"] if session and session.get("user") else None


def leased(repo_id: str, build: Callable[[], Response]) -> Response:
    """A file response that holds a read lease on its repository from before its
    files are looked up until the last byte is sent, so a storage move never
    removes a copy someone is reading."""
    lease = reads.acquire(repo_id)
    try:
        response = build()
    except BaseException:
        reads.release(lease)
        raise
    return LeasedResponse(response, reads, lease)


def repository_file_response(
    request: Request,
    snapshot: RepoSnapshot,
    entry: RepoEntry,
    headers: dict[str, str],
) -> Response:
    headers = {**headers, "Accept-Ranges": "bytes"}
    local = hub_repositories.local_file(snapshot, entry)
    if local is not None:
        return FileResponse(local, headers=headers, media_type="application/octet-stream")
    if request.method == "HEAD":
        return Response(
            headers={**headers, "Content-Length": str(entry.size)},
            media_type="application/octet-stream",
        )
    byte_range = parse_range(request.headers.get("range"), entry.size)
    start, end = byte_range or (0, entry.size - 1)
    status_code = 200
    if byte_range:
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{entry.size}"
    headers["Content-Length"] = str(max(0, end - start + 1))
    return StreamingResponse(
        hub_repositories.iter_bytes(snapshot, entry, start, end),
        status_code=status_code,
        headers=headers,
        media_type="application/octet-stream",
    )


@app.get("/api/models/{owner}/{name}")
def hub_api_model_info(owner: str, name: str, request: Request, blobs: bool = False) -> dict:
    return hub_repositories.model_info(f"{owner}/{name}", "main", blobs, pull_user(request))


@app.get("/api/models/{owner}/{name}/revision/{revision:path}")
def hub_api_model_revision(
    owner: str, name: str, revision: str, request: Request, blobs: bool = False
) -> dict:
    return hub_repositories.model_info(f"{owner}/{name}", revision, blobs, pull_user(request))


@app.get("/api/models/{owner}/{name}/tree/{revision}")
def hub_api_tree(
    owner: str, name: str, revision: str, request: Request, recursive: bool = False
) -> list:
    return hub_repositories.tree(f"{owner}/{name}", revision, "", recursive, pull_user(request))


@app.get("/api/models/{owner}/{name}/tree/{revision}/{path:path}")
def hub_api_tree_path(
    owner: str, name: str, revision: str, path: str, request: Request, recursive: bool = False
) -> list:
    return hub_repositories.tree(
        f"{owner}/{name}", revision, path, recursive, pull_user(request)
    )


@app.api_route("/{owner}/{name}/resolve/{revision}/{path:path}", methods=["GET", "HEAD"])
def hub_resolve(owner: str, name: str, revision: str, path: str, request: Request) -> Response:
    def build() -> Response:
        snapshot, entry = hub_repositories.resolve(
            f"{owner}/{name}", revision, path, pull_user(request)
        )
        return repository_file_response(
            request,
            snapshot,
            entry,
            {"X-Repo-Commit": snapshot.commit, "ETag": f'"{entry.oid}"'},
        )

    return leased(f"{owner}/{name}", build)


def git_repo_id(owner: str, name: str) -> str:
    return f"{owner}/{name.removesuffix('.git')}"


def git_file(owner: str, name: str, relative: str, request: Request) -> Response:
    content = git_mirrors.read_file(git_repo_id(owner, name), relative, pull_user(request))
    if content is None:
        raise HubError("EntryNotFound", "Git object not found.")
    return Response(
        content,
        media_type="application/octet-stream" if relative.startswith("objects/") else "text/plain",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/{owner}/{name}/info/refs")
def git_info_refs(owner: str, name: str, request: Request) -> Response:
    # Answering with text/plain, even for ?service=git-upload-pack, makes git use the
    # dumb HTTP protocol, which only needs these static files.
    with reads.hold(git_repo_id(owner, name)):
        git_mirrors.ensure(git_repo_id(owner, name), pull_user(request))
    return git_file(owner, name, "info/refs", request)


@app.get("/{owner}/{name}/HEAD")
def git_head(owner: str, name: str, request: Request) -> Response:
    return git_file(owner, name, "HEAD", request)


@app.get("/{owner}/{name}/objects/{path:path}")
def git_object(owner: str, name: str, path: str, request: Request) -> Response:
    return git_file(owner, name, f"objects/{path}", request)


LFS_MEDIA_TYPE = "application/vnd.git-lfs+json"


def ensure_mirror(repo_id: str, user: dict[str, Any] | None) -> Any:
    # Building a mirror reads every file, so it holds a read lease like a pull.
    with reads.hold(repo_id):
        return git_mirrors.ensure(repo_id, user)


@app.post("/{owner}/{name}/info/lfs/objects/batch")
async def git_lfs_batch(owner: str, name: str, request: Request) -> Response:
    repo_id = git_repo_id(owner, name)
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict) or payload.get("operation") != "download":
        return JSONResponse(
            {"message": "HuggingHack repositories are read-only."},
            status_code=403,
            media_type=LFS_MEDIA_TYPE,
        )
    user = await run_in_threadpool(pull_user, request)
    mirror = await run_in_threadpool(ensure_mirror, repo_id, user)
    # Authenticated clones must present the same credentials for the weights.
    download_header = (
        {"Authorization": request.headers["Authorization"]}
        if user and request.headers.get("Authorization")
        else None
    )
    # Link back to the address this client used; PUBLIC_URL only affects the
    # commands shown in the UI, so a stale value can never break a clone.
    base = str(request.base_url).rstrip("/")
    objects = []
    for item in (payload.get("objects") or [])[:10000]:
        if not isinstance(item, dict):
            continue
        oid = str(item.get("oid") or "")
        size = item.get("size")
        if oid in mirror.lfs:
            objects.append(
                {
                    "oid": oid,
                    "size": size,
                    "authenticated": True,
                    "actions": {
                        "download": {
                            "href": f"{base}/{repo_id}.git/info/lfs/objects/{oid}",
                            "expires_in": 86400,
                            **({"header": download_header} if download_header else {}),
                        }
                    },
                }
            )
        else:
            objects.append(
                {"oid": oid, "size": size, "error": {"code": 404, "message": "Object does not exist."}}
            )
    return JSONResponse(
        {"transfer": "basic", "objects": objects, "hash_algo": "sha256"},
        media_type=LFS_MEDIA_TYPE,
    )


@app.get("/{owner}/{name}/info/lfs/objects/{oid}")
def git_lfs_download(owner: str, name: str, oid: str, request: Request) -> Response:
    def build() -> Response:
        found = git_mirrors.lfs_entry(git_repo_id(owner, name), oid, pull_user(request))
        if found is None:
            raise HubError("EntryNotFound", "LFS object not found.")
        snapshot, entry = found
        return repository_file_response(request, snapshot, entry, {"ETag": f'"{oid}"'})

    return leased(git_repo_id(owner, name), build)


app_directory = Path(__file__).resolve().parent
static_directory = next(
    (
        candidate
        for candidate in (
            app_directory.parent / "static",
            app_directory.parent.parent / "frontend" / "dist",
        )
        if (candidate / "index.html").is_file()
    ),
    None,
)
if static_directory is not None:
    app.mount("/", StaticFiles(directory=static_directory, html=True), name="frontend")
