"""Read-only git mirrors so `git clone` works against the local library.

Each mirror is a bare repository with a single commit, written in pure Python and
served over git's "dumb" HTTP protocol (plain static files). Large files become
Git LFS pointers; the LFS batch API then streams the real bytes from the library,
so a clone never duplicates model weights on disk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .hub_api import HubRepositories, RepoEntry, RepoSnapshot
from .indexer import UNSAFE_EXTENSIONS, WEIGHT_EXTENSIONS
from .system import SystemStoreError

logger = logging.getLogger("hugginghack.git")


LFS_THRESHOLD_BYTES = 10 * 1024 * 1024
LFS_EXTENSIONS = WEIGHT_EXTENSIONS | UNSAFE_EXTENSIONS | {
    ".7z", ".arrow", ".bz2", ".gz", ".npy", ".npz", ".ot", ".parquet",
    ".tar", ".tflite", ".tgz", ".xz", ".zip", ".zst",
}
MIRROR_FORMAT = 1
METADATA_NAME = "hugginghack-mirror.json"
OBJECT_PATTERN = re.compile(r"^objects/[0-9a-f]{2}/[0-9a-f]{38}$")
OID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EMPTY_OID = hashlib.sha256(b"").hexdigest()
COMMITTER = "HuggingHack <hugginghack@localhost>"


@dataclass(frozen=True)
class Mirror:
    root: Path
    commit: str
    lfs: dict[str, str]


def is_lfs(entry: RepoEntry) -> bool:
    # git-lfs never stores an empty file as a pointer, so a checkout of one would
    # always look modified.
    return entry.size > 0 and (
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
    def __init__(self, repositories: HubRepositories, system: Callable[[], Any] | None = None):
        self.repositories = repositories
        self.base = repositories.settings.data_dir / "git-mirrors"
        # With the system folder in S3, each mirror is kept there too: a new commit
        # builds on the previous one, and `git pull` needs that chain to survive the
        # server's disk being replaced.
        self.system = system
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

    def existing(self, repo_id: str, user: dict[str, Any] | None = None) -> Mirror | None:
        self.repositories.model(repo_id, user)
        return self._load(self._path(repo_id))

    def ensure(self, repo_id: str, user: dict[str, Any] | None = None) -> Mirror:
        """Return an up-to-date mirror, rebuilding it when any file changed."""
        snapshot = self.repositories.snapshot(repo_id, user)
        root = self._path(snapshot.repo_id)
        with self._lock(snapshot.repo_id):
            if not (root / METADATA_NAME).exists():
                self._restore(snapshot.repo_id, root)
            if self._metadata_sha(root) == snapshot.sha:
                mirror = self._load(root)
                # Mirrors built before empty files stayed out of LFS are rebuilt
                # on top of themselves, so existing clones still fast-forward.
                if mirror and EMPTY_OID not in mirror.lfs:
                    return mirror
            mirror = self._build(snapshot, root)
            self._persist(snapshot.repo_id, root)
            return mirror

    # ---- keeping mirrors in the system folder -------------------------------------

    def _remote(self) -> Any:
        store = self.system() if self.system else None
        return store if store is not None and store.remote else None

    @staticmethod
    def _key(repo_id: str) -> str:
        return f"git-mirrors/{repo_id}"

    def _restore(self, repo_id: str, root: Path) -> None:
        store = self._remote()
        if store is None:
            return
        prefix = self._key(repo_id)
        try:
            for key in store.keys(prefix):
                data = store.get(key)
                if data is not None:
                    _atomic_write(root.joinpath(*key[len(prefix) + 1 :].split("/")), data)
        except SystemStoreError:
            logger.warning("Could not restore the git mirror of %s; it is rebuilt.", repo_id)

    def _persist(self, repo_id: str, root: Path) -> None:
        store = self._remote()
        if store is None or not root.is_dir():
            return
        prefix = self._key(repo_id)
        try:
            stored = store.keys(prefix)
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.name.startswith("."):
                    continue
                relative = path.relative_to(root).as_posix()
                key = f"{prefix}/{relative}"
                # Objects are named by their content, so one already there is the same.
                if relative.startswith("objects/") and key in stored:
                    continue
                store.put(key, path.read_bytes())
        except SystemStoreError:
            logger.warning("Could not keep the git mirror of %s in the system folder.", repo_id)

    def forget(self, repo_id: str) -> None:
        """Remove a repository's mirror everywhere, when it is deleted or renamed."""
        shutil.rmtree(self._path(repo_id), ignore_errors=True)
        store = self._remote()
        if store is not None:
            try:
                store.delete_prefix(self._key(repo_id))
            except SystemStoreError:
                logger.warning("Could not remove the git mirror of %s from the system folder.", repo_id)

    def relabel(self, repo_id: str, old_sha: str, new_sha: str) -> None:
        """The same files now carry another snapshot id (a storage move changed their
        timestamps); keep the mirror instead of writing an identical commit."""
        root = self._path(repo_id)
        with self._lock(repo_id):
            try:
                metadata = json.loads((root / METADATA_NAME).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            if metadata.get("snapshot") == old_sha:
                metadata["snapshot"] = new_sha
                _atomic_write(root / METADATA_NAME, json.dumps(metadata).encode("utf-8"))
                self._persist(repo_id, root)

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

    def read_file(
        self, repo_id: str, relative: str, user: dict[str, Any] | None = None
    ) -> bytes | None:
        """Read one dumb-protocol file from an already built mirror."""
        if relative not in {"HEAD", "info/refs", "objects/info/packs"} and not OBJECT_PATTERN.fullmatch(relative):
            return None
        mirror = self.existing(repo_id, user)
        if mirror is None:
            return None
        try:
            return (mirror.root / relative).read_bytes()
        except OSError:
            return None

    def lfs_entry(
        self, repo_id: str, oid: str, user: dict[str, Any] | None = None
    ) -> tuple[RepoSnapshot, RepoEntry] | None:
        if not OID_PATTERN.fullmatch(oid):
            return None
        mirror = self.ensure(repo_id, user)
        path = mirror.lfs.get(oid)
        if path is None:
            return None
        snapshot = self.repositories.snapshot(repo_id, user)
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
