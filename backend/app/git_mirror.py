"""Read-only git mirrors so `git clone` works against the local library.

Each mirror is a bare repository with a single commit, written in pure Python and
served over git's "dumb" HTTP protocol (plain static files). Large files become
Git LFS pointers; the LFS batch API then streams the real bytes from the library,
so a clone never duplicates model weights on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .hub_api import HubRepositories, RepoEntry, RepoSnapshot
from .indexer import UNSAFE_EXTENSIONS, WEIGHT_EXTENSIONS


LFS_THRESHOLD_BYTES = 10 * 1024 * 1024
LFS_EXTENSIONS = WEIGHT_EXTENSIONS | UNSAFE_EXTENSIONS | {
    ".7z", ".arrow", ".bz2", ".gz", ".npy", ".npz", ".ot", ".parquet",
    ".tar", ".tflite", ".tgz", ".xz", ".zip", ".zst",
}
MIRROR_FORMAT = 1
METADATA_NAME = "hugginghack-mirror.json"
OBJECT_PATTERN = re.compile(r"^objects/[0-9a-f]{2}/[0-9a-f]{38}$")
OID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMITTER = "HuggingHack <hugginghack@localhost>"


@dataclass(frozen=True)
class Mirror:
    root: Path
    commit: str
    lfs: dict[str, str]


def is_lfs(entry: RepoEntry) -> bool:
    return (
        entry.size > LFS_THRESHOLD_BYTES
        or PurePosixPath(entry.path).suffix.lower() in LFS_EXTENSIONS
    )


def lfs_pointer(oid: str, size: int) -> bytes:
    return (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        f"size {size}\n"
    ).encode("ascii")


def gitattributes_pattern(path: str) -> str:
    """Match exactly one repository path in .gitattributes."""
    escaped = re.sub(r"([\\*?\[\]])", r"\\\1", f"/{path}")
    if any(character in escaped for character in ' "\t') or not escaped.isprintable():
        quoted = escaped.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{quoted}"'
    return escaped


class _ObjectWriter:
    def __init__(self, root: Path):
        self.root = root

    def write(self, kind: str, content: bytes) -> str:
        payload = f"{kind} {len(content)}\0".encode("ascii") + content
        sha = hashlib.sha1(payload).hexdigest()
        target = self.root / "objects" / sha[:2] / sha[2:]
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, zlib.compress(payload))
        return sha

    def tree(self, node: dict[str, Any]) -> str:
        entries: list[tuple[str, bytes]] = []
        for name, value in node.items():
            if isinstance(value, dict):
                entries.append((f"{name}/", b"40000 " + name.encode("utf-8") + b"\0" + bytes.fromhex(self.tree(value))))
            else:
                entries.append((name, b"100644 " + name.encode("utf-8") + b"\0" + bytes.fromhex(value)))
        # Git orders tree entries by name, comparing directories as if they end in "/".
        entries.sort(key=lambda item: item[0].encode("utf-8"))
        return self.write("tree", b"".join(content for _, content in entries))


class GitMirrors:
    def __init__(self, repositories: HubRepositories):
        self.repositories = repositories
        self.base = repositories.settings.data_dir / "git-mirrors"
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, repo_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(repo_id, threading.Lock())

    def _path(self, repo_id: str) -> Path:
        owner, name = repo_id.split("/", 1)
        return self.base / owner / name

    @staticmethod
    def _load(root: Path) -> Mirror | None:
        try:
            metadata = json.loads((root / METADATA_NAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if metadata.get("format") != MIRROR_FORMAT:
            return None
        return Mirror(root=root, commit=metadata["commit"], lfs=metadata.get("lfs") or {})

    def _metadata_sha(self, root: Path) -> str | None:
        try:
            return json.loads((root / METADATA_NAME).read_text(encoding="utf-8")).get("snapshot")
        except (OSError, json.JSONDecodeError):
            return None

    def existing(self, repo_id: str) -> Mirror | None:
        self.repositories.model(repo_id)
        return self._load(self._path(repo_id))

    def ensure(self, repo_id: str) -> Mirror:
        """Return an up-to-date mirror, rebuilding it when any file changed."""
        snapshot = self.repositories.snapshot(repo_id)
        root = self._path(snapshot.repo_id)
        with self._lock(snapshot.repo_id):
            if self._metadata_sha(root) == snapshot.sha:
                mirror = self._load(root)
                if mirror:
                    return mirror
            return self._build(snapshot, root)

    def _build(self, snapshot: RepoSnapshot, root: Path) -> Mirror:
        """Write a new snapshot commit on top of the previous one.

        Objects are content addressed and written atomically, and refs are replaced
        last, so a concurrent clone always sees either the old or the new commit.
        Chaining commits keeps `git pull` working after the model changes.
        """
        previous = self._load(root)
        writer = _ObjectWriter(root)
        tree: dict[str, Any] = {}
        lfs: dict[str, str] = {}
        attributes: list[str] = []
        for entry in snapshot.entries:
            if is_lfs(entry):
                oid = self.repositories.sha256(snapshot, entry)
                blob = writer.write("blob", lfs_pointer(oid, entry.size))
                lfs[oid] = entry.path
                attributes.append(
                    f"{gitattributes_pattern(entry.path)} filter=lfs diff=lfs merge=lfs -text"
                )
            else:
                blob = writer.write("blob", self.repositories.read_all(snapshot, entry))
            node = tree
            *folders, name = entry.path.split("/")
            for folder in folders:
                node = node.setdefault(folder, {})
            node[name] = blob
        if attributes:
            tree[".gitattributes"] = writer.write(
                "blob", ("\n".join(attributes) + "\n").encode("utf-8")
            )
        tree_sha = writer.tree(tree)
        parent = f"parent {previous.commit}\n" if previous else ""
        # Reuse the repository's latest recorded commit so `git log` matches the
        # Commits page; weights of older commits are not kept, so only the newest
        # state becomes a git commit.
        latest = self.repositories.database.latest_commit(snapshot.repo_id)
        if latest:
            timestamp = _timestamp(latest["created_at"])
            author_name = re.sub(r"[<>\n]", "", latest["author_name"]) or "HuggingHack"
            author = f"{author_name} <hugginghack@localhost>"
            message = latest["message"]
            if latest.get("description"):
                message += f"\n\n{latest['description']}"
            message += f"\n\nHuggingHack-Commit: {latest['id']}"
        else:
            timestamp = _timestamp(snapshot.model.get("modified_at"))
            author = COMMITTER
            message = f"Snapshot of {snapshot.repo_id} from HuggingHack"
        commit = writer.write(
            "commit",
            (
                f"tree {tree_sha}\n"
                f"{parent}"
                f"author {author} {timestamp} +0000\n"
                f"committer {COMMITTER} {timestamp} +0000\n"
                "\n"
                f"{message}\n"
            ).encode("utf-8"),
        )
        _atomic_write(root / "objects" / "info" / "packs", b"")
        _atomic_write(root / "HEAD", b"ref: refs/heads/main\n")
        _atomic_write(root / "refs" / "heads" / "main", f"{commit}\n".encode("ascii"))
        _atomic_write(root / "info" / "refs", f"{commit}\trefs/heads/main\n".encode("ascii"))
        _atomic_write(
            root / METADATA_NAME,
            json.dumps(
                {"format": MIRROR_FORMAT, "snapshot": snapshot.sha, "commit": commit, "lfs": lfs}
            ).encode("utf-8"),
        )
        return Mirror(root=root, commit=commit, lfs=lfs)

    def read_file(self, repo_id: str, relative: str) -> bytes | None:
        """Read one dumb-protocol file from an already built mirror."""
        if relative not in {"HEAD", "info/refs", "objects/info/packs"} and not OBJECT_PATTERN.fullmatch(relative):
            return None
        mirror = self.existing(repo_id)
        if mirror is None:
            return None
        try:
            return (mirror.root / relative).read_bytes()
        except OSError:
            return None

    def lfs_entry(self, repo_id: str, oid: str) -> tuple[RepoSnapshot, RepoEntry] | None:
        if not OID_PATTERN.fullmatch(oid):
            return None
        mirror = self.ensure(repo_id)
        path = mirror.lfs.get(oid)
        if path is None:
            return None
        snapshot = self.repositories.snapshot(repo_id)
        entry = snapshot.entry(path)
        return (snapshot, entry) if entry else None


def _atomic_write(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _timestamp(value: Any) -> int:
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp())
        except ValueError:
            pass
    return int(datetime.now(timezone.utc).timestamp())
