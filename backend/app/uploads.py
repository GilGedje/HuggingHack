from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Iterator

from .config import Settings, validate_repo_id
from .database import VISIBILITIES, Database
from .indexer import (
    PART_SUFFIXES,
    LocalModelIndexer,
    directory_stats,
    hidden_path,
    manifest_target,
    model_formats,
    utc_now,
)
from .permissions import can
from .storage import (
    FilesystemModelStorage,
    StorageRegistry,
    StorageUnavailableError,
    part_size_for,
)

if TYPE_CHECKING:
    from .history import RepoHistory


logger = logging.getLogger("hugginghack")
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
RESERVED_FILENAMES = {".hugginghack.json"}
RESERVED_PREFIX = ".hugginghack"
RESERVED_PARTS = {".git", ".cache", "__pycache__"}
PART_SUFFIX = ".hugginghack-part"
STAGING_DIRECTORY = ".hugginghack-staging"
# Beside a change session: the files a commit replaced or deleted, kept until the
# change is safely stored so a failure can put them back.
BACKUP_SUFFIX = ".backup"
SESSION_PATTERN = re.compile(r"^[0-9a-f]{32}$")
# A change session untouched this long is abandoned and no longer blocks a rename.
STALE_CHANGE_SECONDS = 24 * 60 * 60
# Direct uploads: the scope of a new repository's files (a change session's files
# use the session id), and how many signed part links one request may ask for.
REPOSITORY_SCOPE = "repository"
MAX_PART_LINKS = 100
UPLOADS_TO_STORAGE = (
    "This upload goes straight to storage now. Reload the page and resume it."
)


def part_count(size: int, part_size: int) -> int:
    return max(1, -(-size // part_size)) if size else 0


def expected_part_size(size: int, part_size: int, number: int) -> int:
    count = part_count(size, part_size)
    return part_size if number < count else size - part_size * (count - 1)


def iso_seconds_ago(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() - seconds, timezone.utc).isoformat()


def validate_slug(value: str) -> str:
    slug = value.strip()
    if not SLUG_PATTERN.fullmatch(slug):
        raise ValueError(
            "Repository name must be 1-96 letters, numbers, dots, underscores, or hyphens."
        )
    return slug


def validate_visibility(value: str, organization: bool) -> str:
    """Private is the owner only (an organization's admins and writers), organization
    is every member, and public is every account plus anonymous pulls."""
    if value not in VISIBILITIES:
        raise ValueError("Visibility must be private, organization, or public.")
    if value == "organization" and not organization:
        raise ValueError("Organization visibility is only for organization repositories.")
    return value


def validate_upload_path(value: str) -> PurePosixPath:
    cleaned = value.strip().replace("\\", "/")
    path = PurePosixPath(cleaned)
    if (
        not cleaned
        or path.is_absolute()
        or len(cleaned) > 500
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(part in RESERVED_PARTS for part in path.parts)
        or path.name in RESERVED_FILENAMES
        # HuggingHack's own records live beside the files: the manifest, the pending
        # record, the change area and staging. Nobody may write or delete them.
        or any(part.startswith(RESERVED_PREFIX) for part in path.parts)
        or any(path.name.endswith(suffix) for suffix in PART_SUFFIXES)
    ):
        raise ValueError("Upload path is invalid or reserved.")
    # Control characters (a newline, say) can be stored but never requested by name.
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in cleaned):
        raise ValueError("File names cannot contain control characters such as line breaks or tabs.")
    return path


class UploadManager:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        indexer: LocalModelIndexer,
        model_storage: FilesystemModelStorage | StorageRegistry | None = None,
        history: "RepoHistory | None" = None,
    ):
        self.settings = settings
        self.database = database
        self.indexer = indexer
        self.storages = StorageRegistry.wrap(model_storage, settings)
        self.history = history
        # Set by the app: why a repository cannot change while a storage move runs.
        self.move_guard: Callable[[str], str | None] | None = None
        # Set by the app: remove a repository's git mirror, locally and in the system folder.
        self.mirror_forget: Callable[[str], None] | None = None
        self._write_lock = threading.RLock()
        # The length each unfinished file was started with, so a later chunk cannot
        # change it. Kept in memory: after a restart the next chunk sets it again.
        self._lengths: dict[str, int] = {}

    def _repository_root(self, repo_id: str) -> Path:
        validated = validate_repo_id(repo_id)
        target = (self.settings.model_storage / validated).resolve()
        if (
            self.settings.model_storage != target
            and self.settings.model_storage not in target.parents
        ):
            raise ValueError("Repository path escapes configured model storage.")
        return target

    @staticmethod
    def _read_manifest(root: Path) -> dict[str, Any] | None:
        try:
            manifest = json.loads((root / ".hugginghack.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return manifest if isinstance(manifest, dict) else None

    def access(self, repository: dict[str, Any], user_id: str) -> str | None:
        """How a user may treat an uploaded repository: admin, write, read, or None.

        Personal repositories belong to their creator. Organization repositories
        follow the organization's roles only, so leaving the organization removes
        access even for the account that created them.
        """
        if repository.get("organization_id"):
            return self.database.organization_role(repository["organization_id"], user_id)
        return "admin" if repository["owner_id"] == user_id else None

    def _owned(
        self, repo_id: str, user_id: str, roles: frozenset[str] = frozenset({"admin", "write"})
    ) -> dict[str, Any]:
        repository = self.database.get_owned_repository(repo_id)
        if not repository or self.access(repository, user_id) not in roles:
            raise FileNotFoundError("Uploaded repository not found.")
        return repository

    def can_manage(self, repo_id: str, user: dict[str, Any]) -> bool:
        """Whether the user may change a repository's settings: rename, transfer,
        visibility, and delete. Repository admins may for their uploads, when their
        server role lets them create repositories (a Viewer's roles only read); server
        administrators may for every model, including downloaded ones."""
        if can(user, "storage.manage"):
            return True
        if not can(user, "repos.create"):
            return False
        repository = self.database.get_owned_repository(repo_id)
        return bool(repository) and self.access(repository, user["id"]) == "admin"

    def _managed(self, repo_id: str, user: dict[str, Any]) -> dict[str, Any]:
        repository = self.database.get_owned_repository(repo_id)
        if not repository or not self.can_manage(repo_id, user):
            raise FileNotFoundError("Uploaded repository not found.")
        return repository

    def _busy(self, repo_id: str) -> str | None:
        """Why a repository cannot be renamed or deleted right now, if it cannot."""
        moving = self.move_guard(repo_id) if self.move_guard else None
        if moving:
            return moving
        repository = self.database.get_owned_repository(repo_id)
        if repository and repository["status"] != "ready":
            return "Finish or delete the unfinished upload first."
        if self.database.find_active_download(repo_id):
            return "A download into this repository is still running."
        cutoff = time.time() - STALE_CHANGE_SECONDS
        for path in self._staging_root().glob("*.json"):
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("repo_id") != repo_id:
                    continue
            except (OSError, json.JSONDecodeError):
                continue
            if path.stat().st_mtime > cutoff:
                return "Someone is uploading changes to this repository."
        recent = iso_seconds_ago(STALE_CHANGE_SECONDS)
        if any(session["updated_at"] > recent for session in self.database.list_direct_change_sessions(repo_id)):
            return "Someone is uploading changes to this repository."
        return None

    def _drop_stale_changes(self, repo_id: str) -> None:
        for path in self._staging_root().glob("*.json"):
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("repo_id") == repo_id:
                    shutil.rmtree(path.with_suffix(""), ignore_errors=True)
                    shutil.rmtree(path.with_suffix(BACKUP_SUFFIX), ignore_errors=True)
                    path.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError):
                continue
        for session in self.database.list_direct_change_sessions(repo_id):
            self._discard_direct_session(session)

    def namespaces(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        """Where this user may create repositories: their own name and writable orgs."""
        account = self.database.get_user(user["id"], include_secret=False) or user
        choices = [
            {
                "name": user["username"],
                "kind": "user",
                "display_name": user["display_name"],
                "avatar_updated_at": account.get("avatar_updated_at"),
            }
        ]
        for organization in self.database.user_organizations(user["id"]):
            if organization["role"] in {"admin", "write"}:
                choices.append(
                    {
                        "name": organization["name"],
                        "kind": "organization",
                        "display_name": organization["display_name"],
                        "avatar_updated_at": organization.get("avatar_updated_at"),
                    }
                )
        return choices

    def _namespace_owner(
        self, user: dict[str, Any], namespace: str | None
    ) -> dict[str, Any] | None:
        """The organization a new repository would belong to, or None for the user's
        own namespace. Raises when the user may not create repositories there."""
        if not namespace or namespace.lower() == user["username"].lower():
            return None
        organization = self.database.get_organization(namespace)
        role = (
            self.database.organization_role(organization["id"], user["id"])
            if organization
            else None
        )
        if role not in {"admin", "write"}:
            raise PermissionError("You cannot create repositories in that namespace.")
        return organization

    def storage_choices(
        self, user: dict[str, Any], namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Storage targets this user may put a new repository in, each marked
        `dedicated` when it is reserved for that repository's owner.

        With a namespace, a restricted target must be granted to that namespace:
        the user for personal repositories, the organization for its repositories.
        Without one (Hugging Face downloads), a grant to the user or to an
        organization they write to is enough.
        """
        organization = self._namespace_owner(user, namespace) if namespace else None
        manage = can(user, "storage.manage")
        grants = self.database.storage_grants()
        if namespace:
            owners = {("organization", organization["id"])} if organization else {("user", user["id"])}
        else:
            owners = {("user", user["id"])} | {
                ("organization", item["id"])
                for item in self.database.user_organizations(user["id"])
                if item["role"] in {"admin", "write"}
            }
        choices = []
        for storage in self.storages.all():
            # In a cluster, a server's own disk is gone with the server.
            if self.settings.cluster_mode and not storage.remote:
                continue
            granted = {(item["kind"], item["id"]) for item in grants.get(storage.id, [])}
            dedicated = bool(granted & owners)
            if manage or not granted or dedicated:
                choices.append(
                    {
                        "id": storage.id,
                        "name": storage.name,
                        "kind": storage.backend,
                        "restricted": bool(granted),
                        "dedicated": dedicated,
                    }
                )
        return choices

    def choose_storage(
        self, user: dict[str, Any], requested: str | None, namespace: str | None = None
    ) -> FilesystemModelStorage:
        """The target for a new repository: the requested one when allowed, otherwise
        a target dedicated to its owner (uploads only, since a download has no owner
        of its own), then the server default, then any allowed."""
        if requested:
            self.storages.get(requested)
        choices = self.storage_choices(user, namespace)
        allowed = [item["id"] for item in choices]
        if requested:
            if requested not in allowed:
                raise PermissionError("You cannot upload to that storage location.")
            return self.storages.get(requested)
        for choice in choices if namespace else ():
            if choice["dedicated"]:
                return self.storages.get(choice["id"])
        if self.storages.default_id in allowed:
            return self.storages.default
        if not allowed:
            raise PermissionError("No storage location accepts your uploads.")
        return self.storages.get(allowed[0])

    def _upload_target(self, repo_id: str, file_path: str) -> tuple[Path, Path, PurePosixPath]:
        relative = validate_upload_path(file_path)
        root = self._repository_root(repo_id)
        target = root.joinpath(*relative.parts)
        resolved_parent = target.parent.resolve()
        if root != resolved_parent and root not in resolved_parent.parents:
            raise ValueError("Upload path escapes the owned repository.")
        partial = target.with_name(f".{target.name}{PART_SUFFIX}")
        if target.is_symlink() or partial.is_symlink():
            raise ValueError("Symbolic links are not valid upload targets.")
        return target, partial, relative

    def create_repository(
        self,
        user: dict[str, Any],
        slug: str,
        description: str,
        visibility: str,
        storage_target: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        name = validate_slug(slug)
        organization = self._namespace_owner(user, namespace)
        validate_visibility(visibility, organization is not None)
        target_storage = self.choose_storage(user, storage_target, namespace)
        detail = description.strip()
        if len(detail) > 500:
            raise ValueError("Description must be 500 characters or fewer.")
        owner_name = organization["name"] if organization else user["username"]
        repo_id = validate_repo_id(f"{owner_name}/{name}")
        target = self._repository_root(repo_id)
        direct = self._direct(target_storage)
        # One server at a time decides whether a name is free.
        with self.database.cluster_lock(f"repo-name:{repo_id.lower()}"):
            # Also a model in the library without a local folder (kept only in S3),
            # or objects already in the bucket: an upload must never take one over.
            if (
                target.exists()
                or self.database.repository_id_taken(repo_id)
                or self._bucket_has(target_storage, repo_id)
            ):
                raise FileExistsError("That repository already exists.")
            timestamp = utc_now()
            if direct:
                # Its files go straight to the bucket, so nothing is kept on this
                # server's disk: the database remembers where they go.
                self.database.set_upload_target(repo_id, target_storage.id, timestamp)
            else:
                target.mkdir(parents=True, exist_ok=False)
                manifest = {
                    "status": "uploading",
                    "repo_id": repo_id,
                    "owner_id": user["id"],
                    "organization_id": organization["id"] if organization else None,
                    "source": "user-upload",
                    "created_at": timestamp,
                    "storage_target": target_storage.id,
                }
                (target / ".hugginghack.json").write_text(
                    json.dumps(manifest, indent=2), encoding="utf-8"
                )
            try:
                return self.database.create_owned_repository(
                    {
                        "id": uuid.uuid4().hex,
                        "owner_id": user["id"],
                        "repo_id": repo_id,
                        "description": detail,
                        "visibility": visibility,
                        "status": "uploading",
                        "created_at": timestamp,
                        "updated_at": timestamp,
                        "organization_id": organization["id"] if organization else None,
                    }
                )
            except Exception:
                if direct:
                    self.database.delete_upload_target(repo_id)
                else:
                    shutil.rmtree(target, ignore_errors=True)
                raise

    @staticmethod
    def _direct(storage: FilesystemModelStorage | None) -> bool:
        """Whether uploads to this storage go straight from the browser to it."""
        return bool(storage is not None and storage.remote and getattr(storage, "direct_uploads", False))

    def upload_storage(self, repo_id: str) -> FilesystemModelStorage | None:
        """Where an unfinished repository's files go straight to, if they do."""
        target = self.database.get_upload_target(repo_id)
        if not target:
            return None
        try:
            storage = self.storages.get(target)
        except ValueError:
            return None
        return storage if self._direct(storage) else None

    @staticmethod
    def _bucket_has(storage: FilesystemModelStorage, repo_id: str) -> bool:
        if not storage.remote:
            return False
        try:
            return storage.has_objects(repo_id)
        except Exception as error:
            raise StorageUnavailableError(
                "The storage location could not be reached. Try again later."
            ) from error

    def file_status(self, repo_id: str, user_id: str, file_path: str) -> dict[str, Any]:
        self._owned(repo_id, user_id)
        if self.upload_storage(repo_id):
            raise RuntimeError(UPLOADS_TO_STORAGE)
        target, partial, _ = self._upload_target(repo_id, file_path)
        if target.is_file():
            return {"offset": target.stat().st_size, "complete": True}
        if partial.is_file():
            return {"offset": partial.stat().st_size, "complete": False}
        return {"offset": 0, "complete": False}

    def upload_chunk(
        self,
        repo_id: str,
        user_id: str,
        file_path: str,
        offset: int,
        total: int,
        payload: bytes,
    ) -> dict[str, Any]:
        repository = self._owned(repo_id, user_id)
        if repository["status"] != "uploading":
            raise ValueError(
                "Repository is finalized. Upload a change to it from the model page instead."
            )
        if self.upload_storage(repo_id):
            raise RuntimeError(UPLOADS_TO_STORAGE)

        self._check_chunk(offset, total, payload)
        result = self._write_chunk(
            lambda: self._upload_target(repo_id, file_path), offset, total, payload
        )
        self.database.update_owned_repository(repo_id, updated_at=utc_now())
        return result

    # Direct uploads: the browser asks to begin a file, gets signed links for its
    # parts, PUTs them to the bucket, and asks to complete it. What arrived is always
    # read from the bucket (ListParts), so a reload or another server carries on.

    def begin_repository_file(
        self, repo_id: str, user_id: str, file_path: str, size: int
    ) -> dict[str, Any]:
        """Start (or resume) one file of a new repository. {"direct": False} when its
        storage takes uploads through this server instead."""
        self._uploading(repo_id, user_id)
        storage = self.upload_storage(repo_id)
        if storage is None:
            return {"direct": False}
        relative = validate_upload_path(file_path).as_posix()
        state = self._begin_direct(
            storage, repo_id, REPOSITORY_SCOPE, relative, storage.file_key(repo_id, relative), size, user_id
        )
        self.database.update_owned_repository(repo_id, updated_at=utc_now())
        return state

    def repository_part_links(
        self, repo_id: str, user_id: str, file_path: str, numbers: list[int]
    ) -> dict[str, Any]:
        self._uploading(repo_id, user_id)
        storage, record = self._direct_record(self.upload_storage(repo_id), repo_id, REPOSITORY_SCOPE, file_path)
        return self._part_links(storage, record, numbers)

    def complete_repository_file(self, repo_id: str, user_id: str, file_path: str) -> dict[str, Any]:
        self._uploading(repo_id, user_id)
        storage, record = self._direct_record(self.upload_storage(repo_id), repo_id, REPOSITORY_SCOPE, file_path)
        return self._complete_direct(storage, record)

    def _uploading(self, repo_id: str, user_id: str) -> dict[str, Any]:
        repository = self._owned(repo_id, user_id)
        if repository["status"] != "uploading":
            raise ValueError(
                "Repository is finalized. Upload a change to it from the model page instead."
            )
        return repository

    def _check_size(self, size: int) -> None:
        if size < 0:
            raise ValueError("File size is invalid.")
        if size > self.settings.max_upload_size_gb * 1024**3:
            raise ValueError(f"One file cannot exceed {self.settings.max_upload_size_gb} GB.")

    def _begin_direct(
        self,
        storage: FilesystemModelStorage,
        repo_id: str,
        scope: str,
        relative: str,
        key: str,
        size: int,
        user_id: str,
    ) -> dict[str, Any]:
        self._check_size(size)
        with self.database.cluster_lock(f"upload:{repo_id}:{scope}:{relative}"):
            record = self.database.get_direct_upload(repo_id, scope, relative)
            if record and record["size"] != size:
                # The size a file started with stays its size, as with chunk uploads.
                raise RuntimeError(
                    f"This file was started as {record['size']} bytes, not {size}. "
                    "Cancel the upload and start the file again."
                )
            if record is None:
                now = utc_now()
                with self._storage_errors(storage, repo_id, "The storage could not start the upload. Try again."):
                    if size == 0:
                        # Nothing to send: the empty object is written here.
                        storage.put_empty(key)
                        upload_id = None
                    else:
                        upload_id = storage.start_multipart(key)
                record = self.database.add_direct_upload(
                    {
                        "id": uuid.uuid4().hex,
                        "repo_id": repo_id,
                        "scope": scope,
                        "path": relative,
                        "storage_target": storage.id,
                        "object_key": key,
                        "upload_id": upload_id,
                        "size": size,
                        "part_size": part_size_for(size, storage.target.part_size_mb * 1024**2),
                        "complete": size == 0,
                        "user_id": user_id,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            else:
                self.database.update_direct_upload(record["id"], updated_at=utc_now())
        return self._direct_state(storage, record)

    def _direct_record(
        self, storage: FilesystemModelStorage | None, repo_id: str, scope: str, file_path: str
    ) -> tuple[FilesystemModelStorage, dict[str, Any]]:
        relative = validate_upload_path(file_path).as_posix()
        record = self.database.get_direct_upload(repo_id, scope, relative)
        if storage is None or record is None or record["storage_target"] != storage.id:
            raise FileNotFoundError("Start this file's upload first.")
        return storage, record

    def _direct_state(self, storage: FilesystemModelStorage, record: dict[str, Any]) -> dict[str, Any]:
        size, part_size = record["size"], record["part_size"]
        count = part_count(size, part_size)
        state = {
            "direct": True,
            "path": record["path"],
            "size": size,
            "part_size": part_size,
            "part_count": count,
            "complete": record["complete"],
            "done": list(range(1, count + 1)) if record["complete"] else [],
        }
        if not record["complete"]:
            with self._storage_errors(storage, record["repo_id"], "The storage could not be reached. Try again."):
                parts = storage.uploaded_parts(record["object_key"], record["upload_id"])
            state["done"] = sorted(
                part["number"]
                for part in parts
                if 1 <= part["number"] <= count and part["size"] == expected_part_size(size, part_size, part["number"])
            )
        return state

    def _part_links(
        self, storage: FilesystemModelStorage, record: dict[str, Any], numbers: list[int]
    ) -> dict[str, Any]:
        if record["complete"]:
            raise RuntimeError("This file is already uploaded.")
        count = part_count(record["size"], record["part_size"])
        wanted = sorted(set(numbers))
        if not wanted or len(wanted) > MAX_PART_LINKS or wanted[0] < 1 or wanted[-1] > count:
            raise ValueError(f"Ask for 1 to {MAX_PART_LINKS} parts numbered 1 to {count}.")
        self.database.update_direct_upload(record["id"], updated_at=utc_now())
        return {
            "expires_in": storage.target.upload_presign_ttl_seconds,
            "parts": [
                {
                    "number": number,
                    "url": storage.presigned_part(record["object_key"], record["upload_id"], number),
                    "size": expected_part_size(record["size"], record["part_size"], number),
                }
                for number in wanted
            ],
        }

    def _complete_direct(self, storage: FilesystemModelStorage, record: dict[str, Any]) -> dict[str, Any]:
        if record["complete"]:
            return self._direct_state(storage, record)
        size, part_size = record["size"], record["part_size"]
        count = part_count(size, part_size)
        failure = "The storage could not finish the upload. Try again."
        with self.database.cluster_lock(f"upload:{record['repo_id']}:{record['scope']}:{record['path']}"):
            with self._storage_errors(storage, record["repo_id"], failure):
                parts = {part["number"]: part for part in storage.uploaded_parts(record["object_key"], record["upload_id"])}
            for number in range(1, count + 1):
                part = parts.get(number)
                if part is None or part["size"] != expected_part_size(size, part_size, number):
                    raise RuntimeError(
                        f"Part {number} of {record['path']} has not reached the storage yet. "
                        "Retry the upload to send it again."
                    )
            with self._storage_errors(storage, record["repo_id"], failure):
                storage.complete_multipart(record["object_key"], record["upload_id"], [parts[n] for n in range(1, count + 1)])
                stored = storage.object_size(record["object_key"])
            if stored != size:
                raise RuntimeError(f"The storage holds {stored} bytes of {record['path']}, not {size}. Upload it again.")
            self.database.update_direct_upload(record["id"], complete=True, updated_at=utc_now())
        return self._direct_state(storage, {**record, "complete": True})

    def _abort_direct_uploads(self, repo_id: str, scope: str | None = None) -> None:
        for record in self.database.list_direct_uploads(repo_id, scope):
            if record["complete"] or not record["upload_id"]:
                continue
            try:
                self.storages.get(record["storage_target"]).abort_multipart(record["object_key"], record["upload_id"])
            except ValueError:
                continue
        self.database.delete_direct_uploads(repo_id, scope)

    def sweep_stale_uploads(self) -> int:
        """Give up direct uploads nobody touched for a day: their parts stop taking
        space in the bucket, and a change session's upload area is removed. A new
        repository's finished files stay; its unfinished ones start over."""
        cutoff = iso_seconds_ago(STALE_CHANGE_SECONDS)
        swept = 0
        for session in self.database.list_direct_change_sessions(updated_before=cutoff):
            self._discard_direct_session(session)
            swept += 1
        for record in self.database.list_direct_uploads(scope=REPOSITORY_SCOPE, updated_before=cutoff):
            if record["complete"]:
                continue
            try:
                self.storages.get(record["storage_target"]).abort_multipart(record["object_key"], record["upload_id"])
            except ValueError:
                pass
            self.database.delete_direct_uploads(record["repo_id"], REPOSITORY_SCOPE, record["id"])
            swept += 1
        return swept

    def _check_chunk(self, offset: int, total: int, payload: bytes) -> None:
        if offset < 0 or total < 0 or offset + len(payload) > total:
            raise ValueError("Upload offset or total length is invalid.")
        max_bytes = self.settings.max_upload_size_gb * 1024**3
        if total > max_bytes:
            raise ValueError(
                f"One file cannot exceed {self.settings.max_upload_size_gb} GB."
            )
        if len(payload) > self.settings.upload_chunk_mb * 1024**2:
            raise ValueError(
                f"Upload chunks cannot exceed {self.settings.upload_chunk_mb} MB."
            )
        if shutil.disk_usage(self.settings.model_storage).free < len(payload) + 1024**2:
            raise OSError("Not enough free space for this upload chunk.")

    def _write_chunk(self, locate, offset: int, total: int, payload: bytes) -> dict[str, Any]:
        target, partial, relative = locate()
        with self._write_lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            target, partial, relative = locate()
            if target.exists():
                if target.is_file() and target.stat().st_size == total:
                    return {"offset": total, "complete": True, "path": relative.as_posix()}
                raise FileExistsError("A different completed file already exists at this path.")
            current = partial.stat().st_size if partial.exists() else 0
            if current != offset:
                raise RuntimeError(f"Upload offset mismatch. Server has {current} bytes.")
            declared = self._lengths.setdefault(str(partial), total) if current else total
            if declared != total:
                raise RuntimeError(
                    f"This file was started as {declared} bytes, not {total}. "
                    "Cancel the upload and start the file again."
                )
            self._lengths[str(partial)] = total

            with partial.open("ab") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            uploaded = partial.stat().st_size
            complete = uploaded == total
            if complete:
                partial.replace(target)
                self._lengths.pop(str(partial), None)
        return {
            "offset": uploaded,
            "complete": complete,
            "path": relative.as_posix(),
        }

    def finalize(
        self,
        repo_id: str,
        user_id: str,
        message: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        repository = self._owned(repo_id, user_id)
        storage = self.upload_storage(repo_id)
        if storage is not None:
            return self._finalize_direct(repository, storage, user_id, message, description)
        with self.database.cluster_lock(f"repo:{repo_id}"), self._write_lock:
            root = self._repository_root(repo_id)
            partials = [
                path for path in root.rglob(f"*{PART_SUFFIX}") if path.is_file()
            ]
            if partials:
                raise ValueError("Finish all file uploads before finalizing the repository.")
            files = [
                path
                for path in root.rglob("*")
                if path.is_file() and path.name != ".hugginghack.json"
            ]
            if not files:
                raise ValueError("Upload at least one model or metadata file first.")
            # Named the way the file list shows them, without .gitattributes and the like.
            listed = [path for path in files if not hidden_path(path.relative_to(root).as_posix())]
            size, file_count, _ = directory_stats(root)
            completed = utc_now()
            target_storage = self.storages.for_manifest(self._read_manifest(root))
            manifest = {
                "status": "complete",
                "repo_id": repo_id,
                "owner_id": repository["owner_id"],
                "organization_id": repository.get("organization_id"),
                "source": "user-upload",
                "uploaded_at": completed,
                "total_bytes": size,
                "file_count": file_count,
                "storage_target": target_storage.id,
            }
            manifest_path = root / ".hugginghack.json"
            unfinished = manifest_path.read_bytes() if manifest_path.is_file() else None
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            try:
                with self._storage_errors(
                    target_storage, repo_id, "The upload could not be saved to object storage. Finalize it again."
                ):
                    target_storage.sync_repository(repo_id, root)
            except BaseException:
                # Still unfinished, so a rescan does not list it before it is stored.
                if unfinished is not None:
                    manifest_path.write_bytes(unfinished)
                raise
            model = self.indexer.index_path(root)
            updated = self.database.update_owned_repository(
                repo_id, status="ready", updated_at=completed
            )
            if self.history and model:
                self.history.record(
                    model,
                    message or f"Upload {len(listed)} file{'' if len(listed) == 1 else 's'}",
                    author=self.database.get_user(user_id, include_secret=False),
                    description=description,
                )
        return updated or repository

    def _finalize_direct(
        self,
        repository: dict[str, Any],
        storage: FilesystemModelStorage,
        user_id: str,
        message: str,
        description: str,
    ) -> dict[str, Any]:
        """Publish a repository whose files the browser uploaded to the bucket. Until
        this writes its manifest, no scan, listing or pull sees any of its files."""
        repo_id = repository["repo_id"]
        failure = "The upload could not be saved to object storage. Finalize it again."
        with self.database.cluster_lock(f"repo:{repo_id}"):
            if any(not record["complete"] for record in self.database.list_direct_uploads(repo_id, REPOSITORY_SCOPE)):
                raise ValueError("Finish all file uploads before finalizing the repository.")
            with self._storage_errors(storage, repo_id, failure):
                entries = [
                    entry
                    for entry in storage.list_repository_entries(repo_id) or []
                    if entry["path"] != ".hugginghack.json"
                ]
            if not entries:
                raise ValueError("Upload at least one model or metadata file first.")
            listed = [entry for entry in entries if not hidden_path(entry["path"])]
            completed = utc_now()
            total = sum(entry["size"] for entry in entries)
            manifest = {
                "status": "complete",
                "repo_id": repo_id,
                "owner_id": repository["owner_id"],
                "organization_id": repository.get("organization_id"),
                "source": "user-upload",
                "uploaded_at": completed,
                "total_bytes": total,
                "file_count": len(listed),
                "storage_target": storage.id,
            }
            with self._storage_errors(storage, repo_id, failure):
                published = storage.publish_uploaded_repository(repo_id, manifest)
            model = self.indexer.index_remote(
                {
                    **published,
                    "relative_path": repo_id,
                    "size_bytes": total,
                    "file_count": len(listed),
                    "modified_at": completed,
                    "formats": published.get("formats") or model_formats(entry["path"] for entry in listed),
                    "cached": False,
                    "managed": True,
                }
            )
            updated = self.database.update_owned_repository(repo_id, status="ready", updated_at=completed)
            self.database.delete_direct_uploads(repo_id, REPOSITORY_SCOPE)
            self.database.delete_upload_target(repo_id)
            if self.history and model:
                self.history.record(
                    model,
                    message or f"Upload {len(listed)} file{'' if len(listed) == 1 else 's'}",
                    author=self.database.get_user(user_id, include_secret=False),
                    description=description,
                )
        return updated or repository

    def update_repository(
        self,
        repo_id: str,
        user: dict[str, Any],
        description: str,
        visibility: str,
    ) -> dict[str, Any]:
        repository = self._managed(repo_id, user)
        validate_visibility(visibility, bool(repository.get("organization_id")))
        detail = description.strip()
        if len(detail) > 500:
            raise ValueError("Description must be 500 characters or fewer.")
        return self.database.update_owned_repository(
            repo_id,
            description=detail,
            visibility=visibility,
            updated_at=utc_now(),
        )

    def delete_repository(self, repo_id: str, user: dict[str, Any], confirmation: str) -> None:
        repository = self._managed(repo_id, user)
        if confirmation != repo_id:
            raise ValueError("Repository name confirmation does not match.")
        storage = self.upload_storage(repo_id)
        if storage is not None and repository["status"] != "ready":
            # Never published: its files sit in the bucket without a manifest.
            with self.database.cluster_lock(f"repo:{repo_id}"):
                self._abort_direct_uploads(repo_id)
                with self._storage_errors(storage, repo_id, "The upload could not be removed from object storage. Try again."):
                    storage.delete_repository(repo_id)
                self.database.delete_upload_target(repo_id)
                self.database.delete_owned_repository(repo_id)
                self._forget(repo_id)
            return
        with self._write_lock:
            root = self._repository_root(repo_id)
            manifest = self._read_manifest(root)
            if not manifest:
                indexed = self.database.get_local_model(repo_id)
                candidates = (
                    [self.storages.for_model(indexed)] if indexed else self.storages.remotes
                )
                for candidate in candidates:
                    manifest = candidate.repository_manifest(repo_id)
                    if manifest:
                        break
            if not manifest:
                raise ValueError("Repository manifest is missing or unreadable.")
            belongs = (
                manifest.get("organization_id") == repository.get("organization_id")
                if repository.get("organization_id")
                else manifest.get("owner_id") == repository["owner_id"]
            )
            if not belongs or manifest.get("source") != "user-upload":
                raise ValueError("Repository ownership could not be verified.")
            self.storages.get(manifest_target(manifest)).delete_repository(repo_id)
            if root.exists():
                shutil.rmtree(root)
            self.database.delete_owned_repository(repo_id)
            self._forget(repo_id)

    def _forget_mirror(self, repo_id: str) -> None:
        if self.mirror_forget:
            self.mirror_forget(repo_id)
        else:
            shutil.rmtree(self.settings.data_dir / "git-mirrors" / repo_id, ignore_errors=True)

    def _forget(self, repo_id: str) -> None:
        for session in self.database.list_direct_change_sessions(repo_id):
            self._discard_direct_session(session)
        self._abort_direct_uploads(repo_id)
        self.database.delete_upload_target(repo_id)
        self.database.delete_commits(repo_id)
        self.database.delete_file_digests(repo_id)
        self.database.delete_config_revisions(repo_id)
        # A repository created later with the same name must not inherit this
        # one's git history.
        self._forget_mirror(validate_repo_id(repo_id))

    def delete_model(self, repo_id: str, user: dict[str, Any], confirmation: str) -> None:
        """Delete any model from storage: an upload through its owner's checks, a
        downloaded or scanned one only by a server administrator."""
        validated = validate_repo_id(repo_id)
        if self.database.get_owned_repository(validated):
            if not self.can_manage(validated, user):
                raise FileNotFoundError("Repository not found.")
            reason = self._busy(validated)
            if reason and self.database.get_owned_repository(validated)["status"] == "ready":
                raise ValueError(reason)
            self.delete_repository(validated, user, confirmation)
            return
        model = self.database.get_local_model(validated)
        if not model or not can(user, "storage.manage"):
            raise FileNotFoundError("Repository not found.")
        if confirmation != validated:
            raise ValueError("Repository name confirmation does not match.")
        reason = self._busy(validated)
        if reason:
            raise ValueError(reason)
        with self._write_lock:
            storage = self.storages.for_model(model)
            if storage.remote:
                storage.delete_repository(validated)
            root = self._repository_root(validated)
            if root.is_dir() and not root.is_symlink():
                shutil.rmtree(root)
            self._remove_empty_namespace(root.parent)
            self._drop_stale_changes(validated)
            self.database.delete_owned_repository(validated)
            self._forget(validated)

    def _remove_empty_namespace(self, folder: Path) -> None:
        if folder != self.settings.model_storage and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()

    def _transfer_target(
        self, user: dict[str, Any], namespace: str, owned: bool
    ) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
        """Owner name, organization, and account a repository moves to.

        Uploads move to the user themselves or an organization they write to.
        Server administrators may also move to any user or organization, and may
        rename downloaded models into any free namespace, which keeps them unowned.
        """
        admin = can(user, "storage.manage")
        organization = self.database.get_organization(namespace)
        if organization:
            role = self.database.organization_role(organization["id"], user["id"])
            if role not in {"admin", "write"} and not admin:
                raise PermissionError("You cannot move repositories into that organization.")
            return organization["name"], organization, None
        account = self.database.get_user_by_username(namespace)
        if account:
            if account["id"] != user["id"] and not admin:
                raise PermissionError(
                    "Move repositories to yourself or to an organization you write to."
                )
            return account["username"], None, account
        if owned or not admin:
            raise PermissionError("Choose yourself or an organization as the new owner.")
        validate_repo_id(f"{namespace}/placeholder")
        return namespace, None, None

    def _move_folder(self, source: Path, target: Path) -> None:
        """Move a repository folder, including to names that differ only in
        capitalization, which a case-insensitive disk sees as the folder itself."""
        storage = self.settings.model_storage
        source_owner, target_owner = source.parent, target.parent
        if source_owner.name != target_owner.name and target_owner.exists() and (
            target_owner.name not in os.listdir(storage)
        ):
            # The owner folder exists under other capitalization on this disk.
            if source_owner.name.lower() != target_owner.name.lower():
                raise ValueError(
                    f"Another folder differs from {target_owner.name} only in capitalization."
                )
            if any(item.name != source.name for item in source_owner.iterdir()):
                raise ValueError(
                    f"{source_owner.name} holds other models, so its capitalization cannot change."
                )
            interim = storage / f".{source_owner.name}.hugginghack-rename"
            os.rename(source_owner, interim)
            os.rename(interim, target_owner)
            source = target_owner / source.name
        target_owner.mkdir(parents=True, exist_ok=True)
        if source.parent == target_owner and source.name.lower() == target.name.lower():
            interim = source.with_name(f".{source.name}.hugginghack-rename")
            os.rename(source, interim)
            os.rename(interim, target)
        else:
            os.rename(source, target)

    def rename_repository(
        self,
        repo_id: str,
        user: dict[str, Any],
        namespace: str,
        name: str,
        confirmation: str,
    ) -> dict[str, Any]:
        """Rename a repository or move it to another owner, keeping its files,
        history, saves, and hardware tags. Old links and pull names stop working."""
        old = validate_repo_id(repo_id)
        model = self.database.get_local_model(old)
        if not model or not self.can_manage(old, user):
            raise FileNotFoundError("Repository not found.")
        if confirmation != old:
            raise ValueError("Repository name confirmation does not match.")
        storage = self.storages.for_model(model)
        if storage.remote:
            raise ValueError("Models stored in S3 cannot be renamed yet.")
        reason = self._busy(old)
        if reason:
            raise ValueError(reason)
        owned = self.database.get_owned_repository(old)
        owner_name, organization, account = self._transfer_target(user, namespace.strip(), bool(owned))
        new = validate_repo_id(f"{owner_name}/{validate_slug(name)}")
        if new == old:
            raise ValueError("That is already its name.")
        old_root, new_root = self._repository_root(old), self._repository_root(new)
        if (
            self.database.repository_id_taken(new, ignore=old)
            or (new_root.exists() and not os.path.samefile(new_root, old_root))
        ):
            raise FileExistsError(f"{new} already exists.")
        becomes_owned = bool(organization or account)
        if becomes_owned and not can(user, "storage.manage"):
            granted = {
                (item["kind"], item["id"])
                for item in self.database.storage_grants().get(storage.id, [])
            }
            owner = ("organization", organization["id"]) if organization else ("user", account["id"])
            if granted and owner not in granted:
                raise PermissionError(f"{owner_name} cannot store repositories in {storage.name}.")
        visibility = owned["visibility"] if owned else "public"
        if visibility == "organization" and not organization:
            visibility = "private"
        timestamp = utc_now()
        ownership = None
        if becomes_owned:
            ownership = {
                "owner_id": account["id"] if account else user["id"],
                "organization_id": organization["id"] if organization else None,
                "visibility": visibility,
                "updated_at": timestamp,
            }
            if not owned:
                ownership |= {
                    "id": uuid.uuid4().hex,
                    "description": "",
                    "status": "ready",
                    "created_at": timestamp,
                }

        with self.database.cluster_lock(f"repo-name:{new.lower()}"), self._write_lock:
            # Checked again now that no other server can take the name meanwhile.
            if self.database.repository_id_taken(new, ignore=old):
                raise FileExistsError(f"{new} already exists.")
            manifest_path = old_root / ".hugginghack.json"
            original_manifest = manifest_path.read_text(encoding="utf-8") if manifest_path.is_file() else None
            self._move_folder(old_root, new_root)
            try:
                manifest = self._read_manifest(new_root) or {}
                if ownership or manifest:
                    manifest["repo_id"] = new
                if ownership:
                    if manifest.get("source") != "user-upload":
                        manifest["origin"] = manifest.get("source") or "library"
                    manifest |= {
                        "status": "complete",
                        "source": "user-upload",
                        "owner_id": ownership["owner_id"],
                        "organization_id": ownership["organization_id"],
                        "storage_target": storage.id,
                    }
                if manifest:
                    (new_root / ".hugginghack.json").write_text(
                        json.dumps(manifest, indent=2), encoding="utf-8"
                    )
                self.database.rename_repository(old, new, ownership)
            except BaseException:
                if original_manifest is None:
                    (new_root / ".hugginghack.json").unlink(missing_ok=True)
                else:
                    (new_root / ".hugginghack.json").write_text(original_manifest, encoding="utf-8")
                self._move_folder(new_root, old_root)
                raise
            if old_root.parent.name.lower() != new_root.parent.name.lower():
                self._remove_empty_namespace(old_root.parent)
            self._drop_stale_changes(old)
            self._forget_mirror(old)
        return {"repo_id": new, "visibility": visibility if ownership else "public"}

    # Changes to existing repositories are uploaded into a hidden staging area and
    # applied all at once on commit, so pulls never see a half-uploaded change.

    def can_edit(self, repo_id: str, user: dict[str, Any]) -> bool:
        if can(user, "repos.edit_any"):
            return True
        owned = self.database.get_owned_repository(repo_id)
        return (
            bool(owned)
            and self.access(owned, user["id"]) in {"admin", "write"}
            and can(user, "repos.edit_own")
        )

    def _staging_root(self) -> Path:
        return self.settings.model_storage / STAGING_DIRECTORY

    def _session(self, session_id: str, user: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
        """A change session: its staging folder and record, or no folder for one
        whose files go straight to the bucket (kept in the database instead)."""
        if not SESSION_PATTERN.fullmatch(session_id):
            raise FileNotFoundError("Change session not found.")
        direct = self.database.get_direct_change_session(session_id)
        if direct is not None:
            if direct["user_id"] != user["id"]:
                raise FileNotFoundError("Change session not found.")
            return None, direct
        root = self._staging_root() / session_id
        try:
            session = json.loads((root.parent / f"{session_id}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FileNotFoundError("Change session not found.") from error
        if session.get("user_id") != user["id"]:
            raise FileNotFoundError("Change session not found.")
        return root, session

    def start_change(self, repo_id: str, user: dict[str, Any]) -> dict[str, Any]:
        validated = validate_repo_id(repo_id)
        model = self.database.get_local_model(validated)
        owned = self.database.get_owned_repository(validated)
        if not model or (owned and owned["status"] != "ready"):
            raise FileNotFoundError("Repository not found.")
        if not self.can_edit(validated, user):
            raise PermissionError("You cannot change this repository.")
        moving = self.move_guard(validated) if self.move_guard else None
        if moving:
            raise ValueError(moving)
        session_id = uuid.uuid4().hex
        storage = self.storages.for_model(model)
        if self._direct(storage):
            now = utc_now()
            record = self.database.create_direct_change_session(
                {
                    "id": session_id,
                    "repo_id": validated,
                    "user_id": user["id"],
                    "storage_target": storage.id,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            return {key: record[key] for key in ("id", "repo_id", "user_id", "created_at")}
        if self.settings.cluster_mode:
            raise RuntimeError(
                "This model's storage does not take uploads straight from the browser, "
                "which several servers need. Ask an administrator to turn on direct uploads for it."
            )
        staging = self._staging_root()
        (staging / session_id).mkdir(parents=True)
        session = {
            "id": session_id,
            "repo_id": validated,
            "user_id": user["id"],
            "created_at": utc_now(),
        }
        (staging / f"{session_id}.json").write_text(json.dumps(session), encoding="utf-8")
        return session

    def _staged_target(self, root: Path, file_path: str) -> tuple[Path, Path, PurePosixPath]:
        relative = validate_upload_path(file_path)
        target = root.joinpath(*relative.parts)
        resolved_parent = target.parent.resolve()
        if root.resolve() != resolved_parent and root.resolve() not in resolved_parent.parents:
            raise ValueError("Upload path escapes the change session.")
        partial = target.with_name(f".{target.name}{PART_SUFFIX}")
        if target.is_symlink() or partial.is_symlink():
            raise ValueError("Symbolic links are not valid upload targets.")
        return target, partial, relative

    def begin_change_file(
        self, session_id: str, user: dict[str, Any], file_path: str, size: int
    ) -> dict[str, Any]:
        root, session = self._session(session_id, user)
        if root is not None:
            return {"direct": False}
        storage = self._session_storage(session)
        relative = validate_upload_path(file_path).as_posix()
        self.database.touch_direct_change_session(session_id, utc_now())
        return self._begin_direct(
            storage,
            session["repo_id"],
            session_id,
            relative,
            storage.change_key(session["repo_id"], session_id, relative),
            size,
            user["id"],
        )

    def change_part_links(
        self, session_id: str, user: dict[str, Any], file_path: str, numbers: list[int]
    ) -> dict[str, Any]:
        storage, record = self._change_record(session_id, user, file_path)
        return self._part_links(storage, record, numbers)

    def complete_change_file(self, session_id: str, user: dict[str, Any], file_path: str) -> dict[str, Any]:
        storage, record = self._change_record(session_id, user, file_path)
        return self._complete_direct(storage, record)

    def _change_record(
        self, session_id: str, user: dict[str, Any], file_path: str
    ) -> tuple[FilesystemModelStorage, dict[str, Any]]:
        root, session = self._session(session_id, user)
        if root is not None:
            raise FileNotFoundError("Start this file's upload first.")
        self.database.touch_direct_change_session(session_id, utc_now())
        return self._direct_record(self._session_storage(session), session["repo_id"], session_id, file_path)

    def _session_storage(self, session: dict[str, Any]) -> FilesystemModelStorage:
        try:
            storage = self.storages.get(session["storage_target"])
        except ValueError as error:
            raise FileNotFoundError("Change session not found.") from error
        if not self._direct(storage):
            raise RuntimeError("This model's storage no longer takes uploads straight from the browser. Start the change again.")
        return storage

    def _discard_direct_session(self, session: dict[str, Any]) -> None:
        """Remove a change session that uploads to a bucket, with its unfinished
        uploads and its upload area. Best effort in the bucket."""
        self._abort_direct_uploads(session["repo_id"], session["id"])
        try:
            storage = self.storages.get(session["storage_target"])
        except ValueError:
            storage = None
        if storage is not None and storage.remote:
            storage.delete_change_area(session["repo_id"], session["id"])
        self.database.delete_direct_change_session(session["id"])

    def change_file_status(
        self, session_id: str, user: dict[str, Any], file_path: str
    ) -> dict[str, Any]:
        root, _ = self._session(session_id, user)
        if root is None:
            raise RuntimeError(UPLOADS_TO_STORAGE)
        target, partial, _ = self._staged_target(root, file_path)
        if target.is_file():
            return {"offset": target.stat().st_size, "complete": True}
        if partial.is_file():
            return {"offset": partial.stat().st_size, "complete": False}
        return {"offset": 0, "complete": False}

    def change_chunk(
        self,
        session_id: str,
        user: dict[str, Any],
        file_path: str,
        offset: int,
        total: int,
        payload: bytes,
    ) -> dict[str, Any]:
        root, _ = self._session(session_id, user)
        if root is None:
            raise RuntimeError(UPLOADS_TO_STORAGE)
        self._check_chunk(offset, total, payload)
        return self._write_chunk(
            lambda: self._staged_target(root, file_path), offset, total, payload
        )

    def abort_change(self, session_id: str, user: dict[str, Any]) -> None:
        root, session = self._session(session_id, user)
        if root is None:
            self._discard_direct_session(session)
            return
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(root.parent / f"{session_id}{BACKUP_SUFFIX}", ignore_errors=True)
        (root.parent / f"{session_id}.json").unlink(missing_ok=True)

    def commit_change(
        self,
        session_id: str,
        user: dict[str, Any],
        message: str,
        description: str = "",
        deletions: list[str] | None = None,
    ) -> dict[str, Any]:
        root, session = self._session(session_id, user)
        repo_id = session["repo_id"]
        if not self.can_edit(repo_id, user):
            raise PermissionError("You cannot change this repository.")
        model = self.database.get_local_model(repo_id)
        if not model:
            raise FileNotFoundError("Repository not found.")
        moving = self.move_guard(repo_id) if self.move_guard else None
        if moving:
            raise ValueError(moving)
        if root is None:
            return self._commit_direct(session, model, user, message, description, deletions)
        if any(path.name.endswith(PART_SUFFIX) for path in root.rglob("*")):
            raise ValueError("Finish all file uploads before committing the change.")
        staged = {
            path.relative_to(root).as_posix(): path
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        removed = {validate_upload_path(path).as_posix() for path in deletions or []}
        removed -= set(staged)
        if not staged and not removed:
            raise ValueError("Upload or delete at least one file.")
        storage = self.storages.for_model(model)
        repository_root = self._repository_root(repo_id)
        local_copy = bool(model.get("cached")) and repository_root.is_dir()

        with self.database.cluster_lock(f"repo:{repo_id}"), self._write_lock:
            if local_copy:
                self._apply_local_change(root, repo_id, storage, repository_root, staged, removed)
                updated = self.indexer.index_path(repository_root)
            else:
                # The staged files stay where they are, so a failure can be retried.
                # A manifest that cannot be read fails the commit too: writing a
                # new one over it could drop who owns the repository.
                with self._storage_errors(storage, repo_id):
                    manifest = storage.repository_manifest(
                        repo_id, strict=True
                    ) or self._replacement_manifest(repo_id)
                    storage.apply_changes(repo_id, staged, removed, manifest)
                updated = self._reindex_remote(model, storage)
            self.abort_change(session_id, user)
        if self.database.get_owned_repository(repo_id):
            self.database.update_owned_repository(repo_id, updated_at=utc_now())
        commit = None
        if self.history and updated:
            commit = self.history.record(
                updated,
                message,
                author=user,
                description=description,
                touched=set(staged),
            )
        return {"model": updated, "commit": commit}

    def _commit_direct(
        self,
        session: dict[str, Any],
        model: dict[str, Any],
        user: dict[str, Any],
        message: str,
        description: str,
        deletions: list[str] | None,
    ) -> dict[str, Any]:
        """Commit a change the browser uploaded to the bucket's change area: the
        bucket copies the files into place, so no byte passes through this server."""
        repo_id, session_id = session["repo_id"], session["id"]
        storage = self._session_storage(session)
        if storage.id != self.storages.for_model(model).id:
            raise ValueError("The model moved to another storage location during the upload. Start the change again.")
        records = self.database.list_direct_uploads(repo_id, session_id)
        if any(not record["complete"] for record in records):
            raise ValueError("Finish all file uploads before committing the change.")
        uploaded = {record["path"]: record["object_key"] for record in records}
        removed = {validate_upload_path(path).as_posix() for path in deletions or []} - set(uploaded)
        if not uploaded and not removed:
            raise ValueError("Upload or delete at least one file.")
        repository_root = self._repository_root(repo_id)
        local_copy = bool(model.get("cached")) and repository_root.is_dir()
        with self.database.cluster_lock(f"repo:{repo_id}"), self._write_lock:
            with self._storage_errors(storage, repo_id):
                manifest = storage.repository_manifest(repo_id, strict=True) or self._replacement_manifest(repo_id)
                published = storage.apply_uploaded_changes(repo_id, uploaded, removed, manifest)
                if local_copy:
                    # This server's cached copy follows the bucket.
                    storage.download_files(repo_id, uploaded, repository_root)
                elif any(self._describes_model(path) for path in set(uploaded) | removed):
                    # New weights or a new card change what the model is.
                    published = storage.describe_published(repo_id, published)
                    model = {
                        **model,
                        **{key: published.get(key) for key in ("pipeline_tag", "library_name", "license", "tags", "base_model", "base_model_relation", "parameter_count")},
                        "config": published.get("config") or {},
                        "precision": published.get("precision"),
                    }
            if local_copy:
                for relative in removed:
                    target = repository_root.joinpath(*PurePosixPath(relative).parts)
                    if target.is_file() and not target.is_symlink():
                        target.unlink()
                        self._remove_empty_parents(repository_root, PurePosixPath(relative))
                size, file_count, _ = directory_stats(repository_root)
                cached = self._read_manifest(repository_root) or dict(published)
                cached.update(
                    {"change": published.get("change"), "total_bytes": size, "file_count": file_count, "updated_at": utc_now()}
                )
                (repository_root / ".hugginghack.json").write_text(json.dumps(cached, indent=2), encoding="utf-8")
                updated = self.indexer.index_path(repository_root)
            else:
                updated = self._reindex_remote(model, storage)
            self._discard_direct_session(session)
        if self.database.get_owned_repository(repo_id):
            self.database.update_owned_repository(repo_id, updated_at=utc_now())
        commit = None
        if self.history and updated:
            commit = self.history.record(
                updated,
                message,
                author=user,
                description=description,
                touched=set(uploaded),
            )
        return {"model": updated, "commit": commit}

    @staticmethod
    def _describes_model(path: str) -> bool:
        """Whether a file says what the model is: its card, config, or weights."""
        name = path.lower()
        return path in {"README.md", "config.json", "hf_quant_config.json"} or name.endswith((".safetensors", ".gguf"))

    def _replacement_manifest(self, repo_id: str) -> dict[str, Any]:
        """A manifest for a bucket repository that has none, keeping an upload's
        owner and organization so the next scan still lists it for them. Its
        visibility lives in the database and is unchanged."""
        manifest: dict[str, Any] = {"status": "complete", "repo_id": repo_id}
        owned = self.database.get_owned_repository(repo_id)
        if owned:
            manifest |= {
                "source": "user-upload",
                "owner_id": owned["owner_id"],
                "organization_id": owned.get("organization_id"),
            }
        return manifest

    @staticmethod
    @contextmanager
    def _storage_errors(
        storage: FilesystemModelStorage,
        repo_id: str,
        message: str = "The change could not be saved to object storage. Nothing was changed; try again.",
    ) -> Iterator[None]:
        """Report a bucket failure without its details, which can name the bucket,
        endpoint, or account; they go to the server log instead."""
        try:
            yield
        except StorageUnavailableError:
            raise
        except Exception as error:
            # Our own checks raise ValueError with a message meant for people; a
            # few botocore errors are ValueErrors too, and are wrapped like the rest.
            ours = isinstance(error, ValueError) and not type(error).__module__.startswith(("botocore", "boto3"))
            if ours or not storage.remote:
                raise
            logger.error(
                "Storing a change to %s in %s failed: %s",
                repo_id, storage.id, storage.redact(str(error) or error.__class__.__name__),
            )
            raise StorageUnavailableError(message) from error

    def _apply_local_change(
        self,
        session_root: Path,
        repo_id: str,
        storage: FilesystemModelStorage,
        repository_root: Path,
        staged: dict[str, Path],
        removed: set[str],
    ) -> None:
        """Move a change into the repository folder and store it in its bucket.

        Files it replaces or deletes wait in a backup folder until the bucket has
        the change. If anything fails, every file goes back where it was, including
        the staged ones, so the same change session can be committed again.
        """
        backup = session_root.with_name(f"{session_root.name}{BACKUP_SUFFIX}")
        manifest_path = repository_root / ".hugginghack.json"
        original_manifest = manifest_path.read_bytes() if manifest_path.is_file() else None
        moved: list[tuple[Path, Path]] = []

        def move(source: Path, destination: Path) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            moved.append((source, destination))

        try:
            for relative, source in staged.items():
                parts = PurePosixPath(relative).parts
                target = repository_root.joinpath(*parts)
                if target.is_symlink():
                    raise ValueError("Symbolic links cannot be replaced.")
                if target.is_dir():
                    raise ValueError(f"{relative} is a folder in this repository.")
                if target.exists():
                    move(target, backup.joinpath(*parts))
                move(source, target)
            for relative in removed:
                parts = PurePosixPath(relative).parts
                target = repository_root.joinpath(*parts)
                if target.is_file() and not target.is_symlink():
                    move(target, backup.joinpath(*parts))
            manifest = self._read_manifest(repository_root)
            if manifest:
                size, file_count, _ = directory_stats(repository_root)
                manifest.update(
                    {"total_bytes": size, "file_count": file_count, "updated_at": utc_now()}
                )
                manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            if storage.remote:
                with self._storage_errors(storage, repo_id):
                    storage.sync_repository(repo_id, repository_root, set(staged) | removed)
        except BaseException:
            for source, destination in reversed(moved):
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source)
            if original_manifest is None:
                manifest_path.unlink(missing_ok=True)
            else:
                manifest_path.write_bytes(original_manifest)
            # Folders made for new files.
            for relative in staged:
                self._remove_empty_parents(repository_root, PurePosixPath(relative))
            shutil.rmtree(backup, ignore_errors=True)
            raise
        shutil.rmtree(backup, ignore_errors=True)
        for relative in removed:
            self._remove_empty_parents(repository_root, PurePosixPath(relative))

    @staticmethod
    def _remove_empty_parents(root: Path, relative: PurePosixPath) -> None:
        parent = root.joinpath(*relative.parts).parent
        while parent != root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    def _reindex_remote(
        self, model: dict[str, Any], storage: FilesystemModelStorage
    ) -> dict[str, Any] | None:
        entries = storage.list_repository_entries(model["repo_id"]) or []
        visible = [entry for entry in entries if not hidden_path(entry["path"])]
        return self.indexer.index_remote(
            {
                **model,
                "size_bytes": sum(entry["size"] for entry in visible),
                "file_count": len(visible),
                "modified_at": utc_now(),
                "formats": model_formats(entry["path"] for entry in visible),
                "storage_target": storage.id,
                "cached": False,
            }
        )

