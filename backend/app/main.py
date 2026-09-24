from __future__ import annotations

import asyncio
import hmac
import json
import logging
from urllib.parse import quote
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .auth import AuthService, utc_iso
from .catalog import LocalCatalog, search_catalog
from .config import settings, validate_repo_id
from .database import INTEGRITY_ERRORS, Database
from .downloads import DownloadManager
from .git_mirror import GitMirrors
from .history import RepoHistory, public_commit
from .hub_api import (
    HubError,
    HubRepositories,
    RepoEntry,
    RepoSnapshot,
    parse_range,
    remote_entries,
)
from .hub_service import HubService
from .indexer import LocalModelIndexer
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
git_mirrors = GitMirrors(hub_repositories)


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
            if model.get("source") == "user-upload":
                owner_id = model.get("owner_id")
                if not owner_id or not database.get_owned_repository(repo_id, owner_id):
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
    # Index in the background so the server answers immediately, even when a
    # bucket is offline or a large library records its first history.
    startup_scan = asyncio.create_task(run_in_threadpool(refresh_model_index))
    startup_scan.add_done_callback(log_startup_scan)
    downloads.resume_unfinished()
    yield
    downloads.shutdown()
    runtimes.shutdown()
    hub.close()


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


class CreateUserRequest(SetupRequest):
    pass


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
    visibility: Literal["private", "shared"] = "private"
    storage_target: str | None = Field(default=None, max_length=40)


class RepositoryUpdateRequest(BaseModel):
    description: str = Field(default="", max_length=500)
    visibility: Literal["private", "shared"] = "private"


class DeleteRepositoryRequest(BaseModel):
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


def require_user(request: Request) -> dict[str, Any]:
    if auth.setup_required():
        raise HTTPException(status_code=428, detail="Create the owner account first.")
    session = session_for_request(request)
    if not session or not session.get("user"):
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    request.state.auth_session = session
    return session["user"]


def require_write_user(
    request: Request, user: Annotated[dict[str, Any], Depends(require_user)]
) -> dict[str, Any]:
    session = request.state.auth_session
    if not auth.verify_csrf(session, request.headers.get("X-CSRF-Token")):
        raise HTTPException(status_code=403, detail="Security token is missing or expired.")
    return user


def require_admin(
    user: Annotated[dict[str, Any], Depends(require_write_user)]
) -> dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return user


CurrentUser = Annotated[dict[str, Any], Depends(require_user)]


def require_admin_reader(user: CurrentUser) -> dict[str, Any]:
    # Read-only admin views: GET requests carry no CSRF token.
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return user


AdminReader = Annotated[dict[str, Any], Depends(require_admin_reader)]
WriteUser = Annotated[dict[str, Any], Depends(require_write_user)]
AdminUser = Annotated[dict[str, Any], Depends(require_admin)]


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
    return {
        "accounts_enabled": settings.accounts_enabled,
        "setup_required": auth.setup_required(),
        "user": session.get("user") if session else None,
        "csrf_token": session.get("csrf_token") if session else None,
    }


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
def setup_account(payload: SetupRequest, response: Response) -> dict:
    if not settings.accounts_enabled:
        raise HTTPException(status_code=409, detail="Accounts are disabled.")
    if not auth.setup_required():
        raise HTTPException(status_code=409, detail="The owner account already exists.")
    try:
        user = auth.create_owner(payload.username, payload.display_name, payload.password)
    except (ValueError, *INTEGRITY_ERRORS) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    raw_token, csrf_token = auth.create_session(user["id"])
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
    if not user:
        raise HTTPException(status_code=401, detail="Username or password is incorrect.")
    raw_token, csrf_token = auth.create_session(user["id"])
    set_session_cookie(response, raw_token)
    public_user = database.get_user(user["id"], include_secret=False)
    return auth_payload({"user": public_user, "csrf_token": csrf_token})


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, _: WriteUser) -> dict:
    auth.revoke(request.cookies.get(auth.cookie_name))
    response.delete_cookie(auth.cookie_name, path="/")
    return {"status": "signed_out"}


@app.get("/api/users")
def list_users(user: CurrentUser) -> dict:
    if user["role"] != "admin":
        return {"items": [user]}
    return {"items": database.list_users()}


@app.post("/api/users", status_code=201)
def create_user(payload: CreateUserRequest, _: AdminUser) -> dict:
    try:
        return auth.create_user(
            payload.username, payload.display_name, payload.password, role="member"
        )
    except (ValueError, *INTEGRITY_ERRORS) as error:
        detail = (
            "That username is already in use."
            if isinstance(error, INTEGRITY_ERRORS)
            else str(error)
        )
        raise HTTPException(status_code=400, detail=detail) from error


@app.patch("/api/account/password")
def change_password(
    payload: PasswordChangeRequest, request: Request, user: WriteUser
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


@app.get("/api/hub/models")
async def search_hub_models(
    user: CurrentUser,
    search: Annotated[str, Query(max_length=200)] = "",
    sort: Literal["trending", "downloads", "updated", "likes"] = "trending",
    task: Annotated[str, Query(max_length=100)] = "",
    library: Annotated[str, Query(max_length=100)] = "",
    app_filter: Annotated[str, Query(alias="app", max_length=100)] = "",
    parameters: Annotated[str, Query(max_length=100)] = "",
    limit: Annotated[int, Query(ge=1, le=50)] = 30,
) -> dict:
    try:
        items = await run_in_threadpool(
            hub.search_models,
            search,
            sort,
            task,
            library,
            app_filter,
            parameters,
            limit,
        )
    except Exception as error:
        raise HTTPException(
            status_code=502, detail=f"Hugging Face Hub request failed: {error}"
        ) from error
    local_ids = {model["repo_id"] for model in database.list_local_models()}
    saved_ids = database.saved_repo_ids(user["id"])
    for item in items:
        item["local"] = item["id"] in local_ids
        item["saved"] = item["id"] in saved_ids
    return {"items": items, "count": len(items)}


@app.get("/api/hub/gguf-range")
async def hub_gguf_range(
    request: Request,
    _: CurrentUser,
    repo_id: Annotated[str, Query(max_length=200)],
    filename: Annotated[str, Query(max_length=500)],
    revision: Annotated[str, Query(max_length=200)] = "main",
) -> Response:
    try:
        result = await run_in_threadpool(
            hub.read_gguf_range,
            repo_id,
            filename,
            revision,
            request.headers.get("Range"),
        )
        return Response(
            content=result["content"],
            status_code=result["status_code"],
            media_type="application/octet-stream",
            headers=result["headers"],
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=416, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=502, detail=f"Unable to read GGUF metadata: {error}"
        ) from error


@app.get("/api/hub/models/{repo_id:path}")
async def hub_model(repo_id: str, user: CurrentUser, revision: str = "main") -> dict:
    try:
        validated = validate_repo_id(repo_id)
        details = await run_in_threadpool(hub.model_details, validated, revision)
        details["model_card"] = await run_in_threadpool(
            hub.read_model_card, validated, revision
        )
        details["local"] = database.get_local_model(validated) is not None
        details["saved"] = validated in database.saved_repo_ids(user["id"])
        return details
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Unable to load model: {error}") from error


@app.get("/api/library/models")
def search_library_models(
    user: CurrentUser,
    search: Annotated[str, Query(max_length=200)] = "",
    sort: Literal["updated", "name", "size", "parameters"] = "updated",
    task: Annotated[str, Query(max_length=100)] = "",
    library: Annotated[str, Query(max_length=100)] = "",
    app_filter: Annotated[str, Query(alias="app", max_length=100)] = "",
    parameters: Annotated[str, Query(max_length=100)] = "",
) -> dict:
    try:
        return search_catalog(
            database.list_visible_local_models(user["id"]),
            database.saved_repo_ids(user["id"]),
            search,
            sort,
            task,
            library,
            app_filter,
            parameters,
        )
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
    user: CurrentUser,
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
    user: CurrentUser,
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
    user: CurrentUser,
    repo_id: Annotated[str, Query(max_length=200)],
    path: Annotated[str, Query(max_length=500)],
) -> Response:
    """Download one file with the signed-in user's visibility, including private repos."""
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
    user: CurrentUser,
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
    user: CurrentUser,
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


@app.post("/api/repos/changes", status_code=201)
def start_repository_change(payload: ChangeStartRequest, user: WriteUser) -> dict:
    visible_model(payload.repo_id, user["id"])
    try:
        return uploads.start_change(payload.repo_id, user)
    except (PermissionError, FileNotFoundError, ValueError, OSError) as error:
        raise change_errors(error) from error


@app.get("/api/repos/changes/{session_id}/files/status")
def repository_change_file_status(
    session_id: str, user: CurrentUser, path: Annotated[str, Query(max_length=500)]
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
    user: WriteUser,
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
    session_id: str, payload: ChangeCommitRequest, user: WriteUser
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
def abort_repository_change(session_id: str, user: WriteUser) -> dict:
    try:
        uploads.abort_change(session_id, user)
    except FileNotFoundError as error:
        raise change_errors(error) from error
    return {"status": "aborted"}


@app.get("/api/library/models/{repo_id:path}")
async def library_model(repo_id: str, user: CurrentUser) -> dict:
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
    details["can_edit"] = uploads.can_edit(model["repo_id"], user)
    owned = database.get_owned_repository(model["repo_id"])
    details["visibility"] = owned["visibility"] if owned else "public"
    details["description"] = owned["description"] if owned else ""
    return details


def can_access_download(download: dict[str, Any], user: dict[str, Any]) -> bool:
    return (
        download.get("user_id") == user["id"]
        or (user["role"] == "admin" and download.get("user_id") is None)
    )


@app.get("/api/downloads")
def list_downloads(user: CurrentUser) -> dict:
    items = database.list_downloads(
        user_id=user["id"], include_unowned=user["role"] == "admin"
    )
    return {
        "items": items,
        "active": sum(
            item["status"] in {"queued", "preparing", "downloading"} for item in items
        ),
    }


@app.get("/api/downloads/{download_id}")
def get_download(download_id: str, user: CurrentUser) -> dict:
    download = database.get_download(download_id)
    if not download or not can_access_download(download, user):
        raise HTTPException(status_code=404, detail="Download not found")
    return download


@app.post("/api/downloads", status_code=202)
def start_download(payload: DownloadRequest, user: WriteUser) -> dict:
    if database.get_owned_repository(payload.repo_id):
        raise HTTPException(
            status_code=409,
            detail="An account-owned repository already uses this storage path.",
        )
    try:
        return downloads.queue(
            payload.repo_id,
            payload.revision,
            payload.allow_patterns,
            payload.ignore_patterns,
            payload.mode,
            user_id=user["id"],
            storage_target=payload.storage_target,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/downloads/{download_id}/cancel")
def cancel_download(download_id: str, user: WriteUser) -> dict:
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
    user: CurrentUser, query: Annotated[str, Query(max_length=200)] = ""
) -> dict:
    items = database.list_visible_local_models(user["id"], query)
    return {
        "items": items,
        "count": len(items),
        "total_bytes": sum(item["size_bytes"] for item in items),
    }


@app.post("/api/local-models/scan")
async def scan_local_models(_: WriteUser) -> dict:
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
async def restore_local_model(repo_id: str, user: WriteUser) -> dict:
    model = visible_model(repo_id, user["id"])
    if model["storage_backend"] != "s3":
        raise HTTPException(status_code=409, detail="This model is not backed by S3.")
    if database.find_active_download(model["repo_id"]):
        raise HTTPException(
            status_code=409,
            detail="Wait for the active download to finish before restoring this cache.",
        )
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
async def evict_local_model_cache(repo_id: str, user: WriteUser) -> dict:
    model = visible_model(repo_id, user["id"])
    if model["storage_backend"] != "s3":
        raise HTTPException(status_code=409, detail="Only S3-backed models have a removable cache.")
    if database.find_active_download(model["repo_id"]):
        raise HTTPException(
            status_code=409,
            detail="Wait for the active download to finish before removing this cache.",
        )
    try:
        await run_in_threadpool(
            storages.for_model(model).evict_repository_cache, model["repo_id"]
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    updated = database.set_local_model_cached(model["repo_id"], False)
    return {"status": "evicted", "model": updated}


@app.get("/api/local-models/{repo_id:path}")
def local_model(repo_id: str, user: CurrentUser) -> dict:
    model = visible_model(repo_id, user["id"])
    result = library_listing(model)
    result["model"] = database.get_local_model(model["repo_id"])
    return result


@app.get("/api/storage/options")
def storage_options(_: CurrentUser) -> dict:
    """Targets an uploader can choose, without connection details."""
    return {
        "default": storages.default_id,
        "items": [
            {"id": storage.id, "name": storage.name, "kind": storage.backend}
            for storage in storages.all()
        ],
    }


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
async def storage_targets(_: AdminReader) -> dict:
    models = database.list_local_models()
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


def require_runtime_admin(user: dict[str, Any]) -> None:
    if user["role"] != "admin":
        raise HTTPException(
            status_code=403, detail="Administrator access is required."
        )


def runtime_api_principal(request: Request) -> dict[str, Any] | None:
    expected = settings.runtime_api_token
    authorization = request.headers.get("Authorization") or ""
    if not expected or not authorization.startswith("Bearer "):
        return None
    supplied = authorization.removeprefix("Bearer ")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid runtime API token.")
    return {"id": None, "role": "admin", "runtime_api": True}


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
    user = require_user(request)
    session = request.state.auth_session
    if not auth.verify_csrf(session, request.headers.get("X-CSRF-Token")):
        raise HTTPException(status_code=403, detail="Security token is missing or expired.")
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
def list_collections(user: CurrentUser) -> dict:
    return {"items": database.list_collections(user["id"])}


@app.post("/api/collections", status_code=201)
def create_collection(payload: CollectionRequest, user: WriteUser) -> dict:
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
def delete_collection(collection_id: str, user: WriteUser) -> dict:
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
    user: CurrentUser,
    query: Annotated[str, Query(max_length=200)] = "",
    collection_id: Annotated[str, Query(max_length=100)] = "",
) -> dict:
    items = database.list_saved_models(user["id"], query, collection_id)
    return {"items": items, "count": len(items)}


@app.post("/api/saved-models")
def save_model(payload: SavedModelRequest, user: WriteUser) -> dict:
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
def unsave_model(repo_id: str, user: WriteUser) -> dict:
    try:
        validated = validate_repo_id(repo_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not database.delete_saved_model(user["id"], validated):
        raise HTTPException(status_code=404, detail="Saved model not found.")
    return {"status": "removed"}


@app.get("/api/uploads/repositories")
def list_upload_repositories(user: CurrentUser) -> dict:
    return {"items": database.list_owned_repositories(user["id"])}


@app.post("/api/uploads/repositories", status_code=201)
def create_upload_repository(payload: RepositoryRequest, user: WriteUser) -> dict:
    try:
        return uploads.create_repository(
            user,
            payload.slug,
            payload.description,
            payload.visibility,
            payload.storage_target,
        )
    except (ValueError, FileExistsError, *INTEGRITY_ERRORS) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.patch("/api/uploads/repositories")
def update_upload_repository(
    payload: RepositoryUpdateRequest,
    user: WriteUser,
    repo_id: Annotated[str, Query(max_length=200)],
) -> dict:
    try:
        return uploads.update_repository(
            validate_repo_id(repo_id),
            user["id"],
            payload.description,
            payload.visibility,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/uploads/repositories/files/status")
def upload_file_status(
    user: CurrentUser,
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
    user: WriteUser,
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
    user: WriteUser,
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
    user: WriteUser,
    repo_id: Annotated[str, Query(max_length=200)],
) -> dict:
    try:
        await run_in_threadpool(
            uploads.delete_repository, repo_id, user["id"], payload.confirmation
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
    return JSONResponse({"error": error.message}, status_code=error.status_code, headers=headers)


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
def hub_api_model_info(owner: str, name: str, blobs: bool = False) -> dict:
    return hub_repositories.model_info(f"{owner}/{name}", "main", blobs)


@app.get("/api/models/{owner}/{name}/revision/{revision:path}")
def hub_api_model_revision(owner: str, name: str, revision: str, blobs: bool = False) -> dict:
    return hub_repositories.model_info(f"{owner}/{name}", revision, blobs)


@app.get("/api/models/{owner}/{name}/tree/{revision}")
def hub_api_tree(owner: str, name: str, revision: str, recursive: bool = False) -> list:
    return hub_repositories.tree(f"{owner}/{name}", revision, "", recursive)


@app.get("/api/models/{owner}/{name}/tree/{revision}/{path:path}")
def hub_api_tree_path(
    owner: str, name: str, revision: str, path: str, recursive: bool = False
) -> list:
    return hub_repositories.tree(f"{owner}/{name}", revision, path, recursive)


@app.api_route("/{owner}/{name}/resolve/{revision}/{path:path}", methods=["GET", "HEAD"])
def hub_resolve(owner: str, name: str, revision: str, path: str, request: Request) -> Response:
    snapshot, entry = hub_repositories.resolve(f"{owner}/{name}", revision, path)
    return repository_file_response(
        request,
        snapshot,
        entry,
        {"X-Repo-Commit": snapshot.sha, "ETag": f'"{entry.oid}"'},
    )


def git_repo_id(owner: str, name: str) -> str:
    return f"{owner}/{name.removesuffix('.git')}"


def git_file(owner: str, name: str, relative: str) -> Response:
    content = git_mirrors.read_file(git_repo_id(owner, name), relative)
    if content is None:
        raise HubError("EntryNotFound", "Git object not found.")
    return Response(
        content,
        media_type="application/octet-stream" if relative.startswith("objects/") else "text/plain",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/{owner}/{name}/info/refs")
def git_info_refs(owner: str, name: str) -> Response:
    # Answering with text/plain, even for ?service=git-upload-pack, makes git use the
    # dumb HTTP protocol, which only needs these static files.
    git_mirrors.ensure(git_repo_id(owner, name))
    return git_file(owner, name, "info/refs")


@app.get("/{owner}/{name}/HEAD")
def git_head(owner: str, name: str) -> Response:
    return git_file(owner, name, "HEAD")


@app.get("/{owner}/{name}/objects/{path:path}")
def git_object(owner: str, name: str, path: str) -> Response:
    return git_file(owner, name, f"objects/{path}")


LFS_MEDIA_TYPE = "application/vnd.git-lfs+json"


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
    mirror = await run_in_threadpool(git_mirrors.ensure, repo_id)
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
    found = git_mirrors.lfs_entry(git_repo_id(owner, name), oid)
    if found is None:
        raise HubError("EntryNotFound", "LFS object not found.")
    snapshot, entry = found
    return repository_file_response(request, snapshot, entry, {"ETag": f'"{oid}"'})


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
