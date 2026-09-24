from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from .config import Settings, validate_repo_id
from .database import Database
from .indexer import (
    LocalModelIndexer,
    directory_stats,
    manifest_target,
    model_formats,
    utc_now,
)
from .storage import FilesystemModelStorage, StorageRegistry

if TYPE_CHECKING:
    from .history import RepoHistory


SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
RESERVED_FILENAMES = {".hugginghack.json"}
RESERVED_PARTS = {".git", ".cache", "__pycache__"}
PART_SUFFIX = ".hugginghack-part"
STAGING_DIRECTORY = ".hugginghack-staging"
SESSION_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def validate_slug(value: str) -> str:
    slug = value.strip()
    if not SLUG_PATTERN.fullmatch(slug):
        raise ValueError(
            "Repository name must be 1-96 letters, numbers, dots, underscores, or hyphens."
        )
    return slug


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

    def _owned(self, repo_id: str, user_id: str) -> dict[str, Any]:
        repository = self.database.get_owned_repository(repo_id, user_id)
        if not repository:
            raise FileNotFoundError("Uploaded repository not found.")
        return repository

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
    ) -> dict[str, Any]:
        name = validate_slug(slug)
        target_storage = self.storages.get(storage_target)
        if visibility not in {"private", "shared"}:
            raise ValueError("Visibility must be private or shared.")
        detail = description.strip()
        if len(detail) > 500:
            raise ValueError("Description must be 500 characters or fewer.")
        repo_id = validate_repo_id(f"{user['username']}/{name}")
        target = self._repository_root(repo_id)
        if target.exists() or self.database.get_owned_repository(repo_id):
            raise FileExistsError("That repository already exists.")
        target.mkdir(parents=True, exist_ok=False)
        timestamp = utc_now()
        manifest = {
            "status": "uploading",
            "repo_id": repo_id,
            "owner_id": user["id"],
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
        self.database.update_owned_repository(
            repo_id, user_id, updated_at=utc_now()
        )
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
                "owner_id": user_id,
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
                repo_id, user_id, status="ready", updated_at=completed
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
        user_id: str,
        description: str,
        visibility: str,
    ) -> dict[str, Any]:
        self._owned(repo_id, user_id)
        if visibility not in {"private", "shared"}:
            raise ValueError("Visibility must be private or shared.")
        detail = description.strip()
        if len(detail) > 500:
            raise ValueError("Description must be 500 characters or fewer.")
        return self.database.update_owned_repository(
            repo_id,
            user_id,
            description=detail,
            visibility=visibility,
            updated_at=utc_now(),
        )

    def delete_repository(self, repo_id: str, user_id: str, confirmation: str) -> None:
        self._owned(repo_id, user_id)
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
            if (
                manifest.get("owner_id") != user_id
                or manifest.get("source") != "user-upload"
            ):
                raise ValueError("Repository ownership could not be verified.")
            self.storages.get(manifest_target(manifest)).delete_repository(repo_id)
            if root.exists():
                shutil.rmtree(root)
            self.database.delete_owned_repository(repo_id, user_id)
            self.database.delete_commits(repo_id)
            self.database.delete_file_digests(repo_id)
            # A repository created later with the same name must not inherit this
            # one's git history.
            shutil.rmtree(
                self.settings.data_dir / "git-mirrors" / validate_repo_id(repo_id),
                ignore_errors=True,
            )

    # Changes to existing repositories are uploaded into a hidden staging area and
    # applied all at once on commit, so pulls never see a half-uploaded change.

    def can_edit(self, repo_id: str, user: dict[str, Any]) -> bool:
        owned = self.database.get_owned_repository(repo_id)
        if owned:
            return owned["owner_id"] == user["id"] or user["role"] == "admin"
        return user["role"] == "admin"

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
            self.database.update_owned_repository(
                repo_id, self.database.get_owned_repository(repo_id)["owner_id"], updated_at=utc_now()
            )
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

