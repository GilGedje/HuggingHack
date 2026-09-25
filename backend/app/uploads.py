from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable

from .config import Settings, validate_repo_id
from .database import VISIBILITIES, Database
from .indexer import (
    LocalModelIndexer,
    directory_stats,
    manifest_target,
    model_formats,
    utc_now,
)
from .permissions import can
from .storage import FilesystemModelStorage, StorageRegistry

if TYPE_CHECKING:
    from .history import RepoHistory


SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
RESERVED_FILENAMES = {".hugginghack.json"}
RESERVED_PARTS = {".git", ".cache", "__pycache__"}
PART_SUFFIX = ".hugginghack-part"
STAGING_DIRECTORY = ".hugginghack-staging"
SESSION_PATTERN = re.compile(r"^[0-9a-f]{32}$")
# A change session untouched this long is abandoned and no longer blocks a rename.
STALE_CHANGE_SECONDS = 24 * 60 * 60


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
        or path.name.endswith(PART_SUFFIX)
    ):
        raise ValueError("Upload path is invalid or reserved.")
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
        self._write_lock = threading.RLock()

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
        visibility, and delete. Repository admins may for their uploads; server
        administrators may for every model, including downloaded ones."""
        if can(user, "storage.manage"):
            return True
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
        return None

    def _drop_stale_changes(self, repo_id: str) -> None:
        for path in self._staging_root().glob("*.json"):
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("repo_id") == repo_id:
                    shutil.rmtree(path.with_suffix(""), ignore_errors=True)
                    path.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError):
                continue

    def namespaces(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        """Where this user may create repositories: their own name and writable orgs."""
        choices = [{"name": user["username"], "kind": "user", "display_name": user["display_name"]}]
        for organization in self.database.user_organizations(user["id"]):
            if organization["role"] in {"admin", "write"}:
                choices.append(
                    {
                        "name": organization["name"],
                        "kind": "organization",
                        "display_name": organization["display_name"],
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
        if target.exists() or self.database.get_owned_repository(repo_id):
            raise FileExistsError("That repository already exists.")
        target.mkdir(parents=True, exist_ok=False)
        timestamp = utc_now()
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
            shutil.rmtree(target, ignore_errors=True)
            raise

    def file_status(self, repo_id: str, user_id: str, file_path: str) -> dict[str, Any]:
        self._owned(repo_id, user_id)
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

        self._check_chunk(offset, total, payload)
        result = self._write_chunk(
            lambda: self._upload_target(repo_id, file_path), offset, total, payload
        )
        self.database.update_owned_repository(repo_id, updated_at=utc_now())
        return result

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

            with partial.open("ab") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            uploaded = partial.stat().st_size
            complete = uploaded == total
            if complete:
                partial.replace(target)
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
        with self._write_lock:
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
            (root / ".hugginghack.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
            target_storage.sync_repository(repo_id, root)
            model = self.indexer.index_path(root)
            updated = self.database.update_owned_repository(
                repo_id, status="ready", updated_at=completed
            )
            if self.history and model:
                self.history.record(
                    model,
                    message or f"Upload {len(files)} file{'' if len(files) == 1 else 's'}",
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

    def _forget(self, repo_id: str) -> None:
        self.database.delete_commits(repo_id)
        self.database.delete_file_digests(repo_id)
        self.database.delete_config_revisions(repo_id)
        # A repository created later with the same name must not inherit this
        # one's git history.
        shutil.rmtree(
            self.settings.data_dir / "git-mirrors" / validate_repo_id(repo_id),
            ignore_errors=True,
        )

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
            self.database.get_local_model(new)
            or self.database.get_owned_repository(new)
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

        with self._write_lock:
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
            shutil.rmtree(self.settings.data_dir / "git-mirrors" / old, ignore_errors=True)
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

    def _session(self, session_id: str, user: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        if not SESSION_PATTERN.fullmatch(session_id):
            raise FileNotFoundError("Change session not found.")
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

    def change_file_status(
        self, session_id: str, user: dict[str, Any], file_path: str
    ) -> dict[str, Any]:
        root, _ = self._session(session_id, user)
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
        self._check_chunk(offset, total, payload)
        return self._write_chunk(
            lambda: self._staged_target(root, file_path), offset, total, payload
        )

    def abort_change(self, session_id: str, user: dict[str, Any]) -> None:
        root, _ = self._session(session_id, user)
        shutil.rmtree(root, ignore_errors=True)
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

        with self._write_lock:
            if local_copy:
                for relative, source in staged.items():
                    target = repository_root.joinpath(*PurePosixPath(relative).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.is_symlink():
                        raise ValueError("Symbolic links cannot be replaced.")
                    os.replace(source, target)
                for relative in removed:
                    target = repository_root.joinpath(*PurePosixPath(relative).parts)
                    if target.is_file() and not target.is_symlink():
                        target.unlink()
                        parent = target.parent
                        while parent != repository_root and not any(parent.iterdir()):
                            parent.rmdir()
                            parent = parent.parent
                manifest_path = repository_root / ".hugginghack.json"
                manifest = self._read_manifest(repository_root)
                if manifest:
                    size, file_count, _ = directory_stats(repository_root)
                    manifest.update(
                        {"total_bytes": size, "file_count": file_count, "updated_at": utc_now()}
                    )
                    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                if storage.remote:
                    storage.sync_repository(repo_id, repository_root, set(staged) | removed)
                updated = self.indexer.index_path(repository_root)
            else:
                manifest = storage.repository_manifest(repo_id) or {
                    "status": "complete",
                    "repo_id": repo_id,
                }
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

    def _reindex_remote(
        self, model: dict[str, Any], storage: FilesystemModelStorage
    ) -> dict[str, Any] | None:
        entries = storage.list_repository_entries(model["repo_id"]) or []
        visible = [
            entry for entry in entries if not PurePosixPath(entry["path"]).name.startswith(".")
        ]
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

