"""Read-only Hugging Face Hub protocol served from the local library.

Clients such as vLLM, Transformers, and the `hf` CLI use `huggingface_hub`, which
talks to whatever `HF_ENDPOINT` points at. Serving the same model-info, tree, and
resolve endpoints lets those tools pull models from HuggingHack on an air-gapped
network with no code changes. Access is anonymous by design, so only models that
every account can already see are served; private uploads are never exposed.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .config import Settings, validate_repo_id
from .database import Database
from .storage import PART_SUFFIXES, FilesystemModelStorage


STREAM_CHUNK_BYTES = 1024 * 1024


class HubError(Exception):
    """An error reported to Hub clients through the X-Error-Code header."""

    def __init__(
        self, code: str, message: str, status_code: int = 404, commit: str | None = None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.commit = commit


@dataclass(frozen=True)
class RepoEntry:
    path: str
    size: int
    version: str

    @property
    def oid(self) -> str:
        return hashlib.sha1(f"{self.path}\0{self.version}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RepoSnapshot:
    model: dict[str, Any]
    entries: tuple[RepoEntry, ...]
    local_root: Path | None

    @property
    def repo_id(self) -> str:
        return self.model["repo_id"]

    @property
    def sha(self) -> str:
        """A stable 40-hex revision that changes whenever any file changes."""
        digest = hashlib.sha1(self.repo_id.encode("utf-8"))
        for entry in self.entries:
            digest.update(f"\n{entry.path}\0{entry.version}".encode("utf-8"))
        return digest.hexdigest()

    def entry(self, path: str) -> RepoEntry | None:
        return next((entry for entry in self.entries if entry.path == path), None)


def hub_date(value: Any) -> str | None:
    """Format a timestamp the way the Hub does: UTC with a trailing Z."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _hidden(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    return (
        not parts
        or any(part.startswith(".") or part == "__pycache__" for part in parts)
        or any(parts[-1].endswith(suffix) for suffix in PART_SUFFIXES)
    )


class HubRepositories:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        model_storage: FilesystemModelStorage,
    ):
        self.settings = settings
        self.database = database
        self.model_storage = model_storage

    def model(self, repo_id: str) -> dict[str, Any]:
        if not self.settings.hub_api_enabled:
            raise HubError("RepoNotFound", "Repository not found.")
        try:
            validated = validate_repo_id(repo_id)
        except ValueError as error:
            raise HubError("RepoNotFound", "Repository not found.") from error
        model = self.database.get_public_local_model(validated)
        if not model:
            raise HubError("RepoNotFound", "Repository not found.")
        return model

    def _local_root(self, model: dict[str, Any]) -> Path | None:
        if not model.get("cached"):
            return None
        storage = self.settings.model_storage
        root = (storage / model["relative_path"]).resolve()
        if storage != root and storage not in root.parents:
            return None
        return root if root.is_dir() else None

    def snapshot(self, repo_id: str) -> RepoSnapshot:
        model = self.model(repo_id)
        root = self._local_root(model)
        entries: list[RepoEntry] = []
        if root is not None:
            for current, directories, names in os.walk(root):
                directories[:] = sorted(
                    name
                    for name in directories
                    if not name.startswith(".") and name != "__pycache__"
                )
                for name in names:
                    path = Path(current) / name
                    relative = path.relative_to(root).as_posix()
                    if _hidden(relative) or path.is_symlink():
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    entries.append(
                        RepoEntry(relative, stat.st_size, f"{stat.st_size}-{stat.st_mtime_ns}")
                    )
        elif model.get("storage_backend") == "s3":
            for item in self.model_storage.list_repository_entries(model["repo_id"]) or []:
                if not _hidden(item["path"]):
                    entries.append(
                        RepoEntry(item["path"], item["size"], f"{item['size']}-{item['version']}")
                    )
        if not entries:
            raise HubError("RepoNotFound", "Repository files were not found.")
        entries.sort(key=lambda entry: entry.path)
        return RepoSnapshot(model=model, entries=tuple(entries), local_root=root)

    def check_revision(self, snapshot: RepoSnapshot, revision: str) -> None:
        if revision not in {"main", "HEAD", "refs/heads/main", snapshot.sha}:
            raise HubError("RevisionNotFound", f"Revision {revision} was not found.")

    def model_info(
        self, repo_id: str, revision: str = "main", blobs: bool = False
    ) -> dict[str, Any]:
        snapshot = self.snapshot(repo_id)
        self.check_revision(snapshot, revision)
        model = snapshot.model
        owner = snapshot.repo_id.split("/", 1)[0]
        siblings = []
        for entry in snapshot.entries:
            sibling: dict[str, Any] = {"rfilename": entry.path}
            if blobs:
                sibling.update({"size": entry.size, "blobId": entry.oid})
            siblings.append(sibling)
        card_data: dict[str, Any] = {}
        if model.get("license"):
            card_data["license"] = model["license"]
        if model.get("pipeline_tag"):
            card_data["pipeline_tag"] = model["pipeline_tag"]
        return {
            "_id": snapshot.sha[:24],
            "id": snapshot.repo_id,
            "modelId": snapshot.repo_id,
            "author": owner,
            "sha": snapshot.sha,
            "lastModified": hub_date(model.get("modified_at")),
            "createdAt": hub_date(model.get("downloaded_at") or model.get("modified_at")),
            "private": False,
            "gated": False,
            "disabled": False,
            "downloads": 0,
            "likes": 0,
            "library_name": model.get("library_name"),
            "pipeline_tag": model.get("pipeline_tag"),
            "tags": model.get("tags") or [],
            "cardData": card_data,
            "siblings": siblings,
            "usedStorage": sum(entry.size for entry in snapshot.entries),
        }

    def tree(
        self, repo_id: str, revision: str, path: str = "", recursive: bool = False
    ) -> list[dict[str, Any]]:
        snapshot = self.snapshot(repo_id)
        self.check_revision(snapshot, revision)
        prefix = path.strip("/")
        if prefix and _hidden(prefix):
            raise HubError(
                "EntryNotFound", f"{prefix} does not exist on {revision}.", commit=snapshot.sha
            )
        base = f"{prefix}/" if prefix else ""
        files: list[dict[str, Any]] = []
        directories: set[str] = set()
        for entry in snapshot.entries:
            if not entry.path.startswith(base):
                continue
            remainder = entry.path[len(base) :]
            parts = remainder.split("/")
            for depth in range(1, len(parts)):
                if recursive or depth == 1:
                    directories.add(base + "/".join(parts[:depth]))
            if recursive or len(parts) == 1:
                files.append(
                    {"type": "file", "oid": entry.oid, "size": entry.size, "path": entry.path}
                )
        if prefix and not files and not directories:
            raise HubError(
                "EntryNotFound", f"{prefix} does not exist on {revision}.", commit=snapshot.sha
            )
        folders = [
            {
                "type": "directory",
                "oid": hashlib.sha1(f"{snapshot.sha}\0{directory}".encode("utf-8")).hexdigest(),
                "size": 0,
                "path": directory,
            }
            for directory in sorted(directories)
        ]
        return folders + files

    def resolve(self, repo_id: str, revision: str, path: str) -> tuple[RepoSnapshot, RepoEntry]:
        snapshot = self.snapshot(repo_id)
        self.check_revision(snapshot, revision)
        entry = None if _hidden(path) else snapshot.entry(path)
        if entry is None:
            raise HubError(
                "EntryNotFound", f"{path} does not exist on {revision}.", commit=snapshot.sha
            )
        return snapshot, entry

    def local_file(self, snapshot: RepoSnapshot, entry: RepoEntry) -> Path | None:
        if snapshot.local_root is None:
            return None
        target = snapshot.local_root.joinpath(*PurePosixPath(entry.path).parts)
        resolved = target.resolve()
        if target.is_symlink() or snapshot.local_root not in resolved.parents:
            raise HubError("EntryNotFound", f"{entry.path} is not available.")
        return resolved

    def iter_bytes(
        self, snapshot: RepoSnapshot, entry: RepoEntry, start: int = 0, end: int | None = None
    ) -> Iterator[bytes]:
        """Stream an inclusive byte range from the local cache or S3."""
        last = entry.size - 1 if end is None else min(end, entry.size - 1)
        if last < start:
            return
        local = self.local_file(snapshot, entry)
        if local is not None:
            with local.open("rb") as handle:
                handle.seek(start)
                remaining = last - start + 1
                while remaining > 0:
                    chunk = handle.read(min(STREAM_CHUNK_BYTES, remaining))
                    if not chunk:
                        return
                    remaining -= len(chunk)
                    yield chunk
            return
        yield from self.model_storage.iter_repository_file(
            snapshot.repo_id, entry.path, start, last
        )

    def read_all(self, snapshot: RepoSnapshot, entry: RepoEntry) -> bytes:
        return b"".join(self.iter_bytes(snapshot, entry))

    def sha256(self, snapshot: RepoSnapshot, entry: RepoEntry) -> str:
        cached = self.database.get_file_digest(snapshot.repo_id, entry.path, entry.version)
        if cached:
            return cached
        digest = hashlib.sha256()
        for chunk in self.iter_bytes(snapshot, entry):
            digest.update(chunk)
        value = digest.hexdigest()
        self.database.set_file_digest(snapshot.repo_id, entry.path, entry.version, value)
        return value


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single `bytes=` range. Returns None for a full-body response."""
    if not header:
        return None
    value = header.strip()
    if not value.startswith("bytes=") or "," in value:
        raise HubError("RangeNotSatisfiable", "Only one byte range is supported.", 416)
    start_text, _, end_text = value[6:].partition("-")
    try:
        if start_text == "":
            length = int(end_text)
            if length <= 0:
                raise ValueError
            start, end = max(0, size - length), size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError as error:
        raise HubError("RangeNotSatisfiable", "The byte range is invalid.", 416) from error
    if start >= size or end < start:
        raise HubError("RangeNotSatisfiable", "The byte range is not satisfiable.", 416)
    return start, min(end, size - 1)

