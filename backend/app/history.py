"""Commit history for every repository in the library.

A commit records who changed a repository, when, why, and which files were added,
modified, or deleted. File versions come from the repository's durable storage:
the model folder for local repositories and the bucket listing for S3 targets, so
restoring or evicting a local cache never looks like a change. Small text files
keep their content (deduplicated by SHA-256) so commits can show line diffs;
weights only record their size.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from .database import Database
from .hub_api import HubRepositories, RepoEntry, local_entries


TEXT_EXTENSIONS = {
    ".cfg", ".csv", ".html", ".ini", ".jinja", ".json", ".md", ".py", ".sh",
    ".toml", ".tsv", ".txt", ".xml", ".yaml", ".yml",
}
TEXT_NAMES = {"LICENSE", "NOTICE", "README", "USE_POLICY", "MODEL_CARD"}
TEXT_MAX_BYTES = 512 * 1024
DIFF_MAX_LINES = 400
SYSTEM_AUTHOR = "HuggingHack"


def is_text_path(path: str, size: int) -> bool:
    name = PurePosixPath(path)
    return size <= TEXT_MAX_BYTES and (
        name.suffix.lower() in TEXT_EXTENSIONS or name.stem.upper() in TEXT_NAMES
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RepoHistory:
    def __init__(self, database: Database, repositories: HubRepositories):
        self.database = database
        self.repositories = repositories
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, repo_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(repo_id, threading.Lock())

    def durable_entries(self, model: dict[str, Any]) -> list[RepoEntry]:
        """Files as stored in the repository's own target, not its local cache."""
        storage = self.repositories.storages.for_model(model)
        if storage.remote:
            return self.repositories.remote_entries(model)
        root = self.repositories._local_root(model)
        return local_entries(root) if root is not None else []

    def _read_text(self, model: dict[str, Any], entry: RepoEntry) -> str | None:
        try:
            root = self.repositories._local_root(model)
            if root is not None and (root / entry.path).is_file():
                payload = (root / entry.path).read_bytes()[: TEXT_MAX_BYTES + 1]
            else:
                storage = self.repositories.storages.for_model(model)
                result = storage.read_repository_file(
                    model["repo_id"], entry.path, 0, TEXT_MAX_BYTES - 1, TEXT_MAX_BYTES
                )
                if result is None:
                    return None
                payload = result[0]
            if len(payload) > TEXT_MAX_BYTES or b"\0" in payload:
                return None
            return payload.decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            return None

    def _store_text(self, content: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.database.put_text_blob(digest, content)
        return digest

    def record(
        self,
        model: dict[str, Any],
        message: str,
        author: dict[str, Any] | None = None,
        description: str = "",
        entries: list[RepoEntry] | None = None,
        touched: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """Record a commit when the repository differs from its latest commit.

        `touched` marks files that were rewritten even if their size and
        timestamp look unchanged. Returns None when nothing changed.
        """
        repo_id = model["repo_id"]
        with self._lock(repo_id):
            current = entries if entries is not None else self.durable_entries(model)
            latest = self.database.latest_commit(repo_id)
            previous = {item["path"]: item for item in (latest or {}).get("snapshot", [])}
            snapshot: list[dict[str, Any]] = []
            changes: list[dict[str, Any]] = []
            for entry in sorted(current, key=lambda item: item.path):
                old = previous.pop(entry.path, None)
                text = None
                if is_text_path(entry.path, entry.size):
                    if old and old.get("version") == entry.version and entry.path not in (touched or set()):
                        text = old.get("text")
                    else:
                        content = self._read_text(model, entry)
                        text = self._store_text(content) if content is not None else None
                snapshot.append(
                    {"path": entry.path, "size": entry.size, "version": entry.version, "text": text}
                )
                if old is None:
                    change = "added"
                elif text is not None or old.get("text") is not None:
                    change = "modified" if text != old.get("text") else None
                elif old.get("version") != entry.version or old.get("size") != entry.size:
                    change = "modified"
                elif entry.path in (touched or set()):
                    change = "modified"
                else:
                    change = None
                if change:
                    changes.append(
                        {
                            "path": entry.path,
                            "change": change,
                            "old_size": old.get("size") if old else None,
                            "new_size": entry.size,
                            "old_text": old.get("text") if old else None,
                            "new_text": text,
                        }
                    )
            for path, old in sorted(previous.items()):
                changes.append(
                    {
                        "path": path,
                        "change": "deleted",
                        "old_size": old.get("size"),
                        "new_size": None,
                        "old_text": old.get("text"),
                        "new_text": None,
                    }
                )
            if latest and not changes:
                return None
            if not latest and not snapshot:
                return None
            created_at = _now()
            parent = latest["id"] if latest else None
            sequence = (latest["sequence"] + 1) if latest else 1
            commit_id = hashlib.sha1(
                json.dumps(
                    [repo_id, parent, sequence, created_at, message, snapshot], sort_keys=True
                ).encode("utf-8")
            ).hexdigest()
            return self.database.create_commit(
                {
                    "id": commit_id,
                    "repo_id": repo_id,
                    "sequence": sequence,
                    "parent_id": parent,
                    "author_id": author["id"] if author else None,
                    "author_name": (author or {}).get("display_name")
                    or (author or {}).get("username")
                    or SYSTEM_AUTHOR,
                    "message": message.strip()[:200] or "Update files",
                    "description": description.strip()[:5000],
                    "created_at": created_at,
                    "snapshot_json": json.dumps(snapshot),
                    "changes_json": json.dumps(changes),
                }
            )

    def record_scan(
        self, model: dict[str, Any], entries: list[RepoEntry] | None = None
    ) -> dict[str, Any] | None:
        first = self.database.latest_commit(model["repo_id"]) is None
        return self.record(
            model,
            "Initial import" if first else "Detected changes in storage",
            entries=entries,
        )

    def diff(self, change: dict[str, Any]) -> dict[str, Any]:
        """A change with a unified diff for text files, capped for the browser."""
        result = {key: value for key, value in change.items() if key not in {"old_text", "new_text"}}
        old_text = self.database.get_text_blob(change["old_text"]) if change.get("old_text") else None
        new_text = self.database.get_text_blob(change["new_text"]) if change.get("new_text") else None
        if old_text is None and new_text is None:
            result["diff"] = None
            result["binary"] = True
            return result
        lines = list(
            difflib.unified_diff(
                (old_text or "").splitlines(),
                (new_text or "").splitlines(),
                fromfile=f"a/{change['path']}" if old_text is not None else "/dev/null",
                tofile=f"b/{change['path']}" if new_text is not None else "/dev/null",
                lineterm="",
                n=3,
            )
        )
        result["binary"] = False
        result["truncated"] = len(lines) > DIFF_MAX_LINES
        result["diff"] = lines[:DIFF_MAX_LINES]
        result["additions"] = sum(
            1 for line in lines if line.startswith("+") and not line.startswith("+++")
        )
        result["deletions"] = sum(
            1 for line in lines if line.startswith("-") and not line.startswith("---")
        )
        return result

    def last_commits(self, repo_id: str, paths: list[str]) -> dict[str, dict[str, Any]]:
        """The latest commit that touched each path, like the Hub's file list."""
        remaining = set(paths)
        found: dict[str, dict[str, Any]] = {}
        offset = 0
        while remaining:
            commits = self.database.list_commits(repo_id, limit=100, offset=offset, full=True)
            if not commits:
                break
            for commit in commits:
                summary = {
                    "id": commit["id"],
                    "message": commit["message"],
                    "created_at": commit["created_at"],
                }
                for change in commit["changes"]:
                    if change["path"] in remaining and change["change"] != "deleted":
                        found[change["path"]] = summary
                        remaining.discard(change["path"])
            offset += len(commits)
        return found


def public_commit(commit: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in commit.items() if key not in {"snapshot", "changes"}}

