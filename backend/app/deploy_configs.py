"""Deployment configs for a model, kept as a linear history of revisions.

A revision is a snapshot of text files (launch scripts, compose files, vLLM
arguments) that never changes once made, like a commit. Its results, the numbers
measured while that config ran, stay editable, because a config is deployed
first and measured afterwards. Configs live apart from the model's files, so
pulling the model never pulls them.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any

from .catalog import HARDWARE
from .database import Database
from .history import RepoHistory
from .uploads import validate_upload_path

MAX_FILES = 50
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_CUSTOM_METRICS = 20
MAX_NUMBER = 1e15

# Each measured value, its unit, and which way is better. Context values describe
# the test itself and have no better direction; they tell apart runs that are
# not comparable.
METRICS: list[dict[str, Any]] = [
    {"id": "output_tps", "label": "Output throughput", "unit": "tok/s", "better": "higher", "group": "speed"},
    {"id": "per_user_tps", "label": "Per-user speed", "unit": "tok/s", "better": "higher", "group": "speed"},
    {"id": "request_throughput", "label": "Request throughput", "unit": "req/s", "better": "higher", "group": "speed"},
    {"id": "ttft_ms", "label": "TTFT mean", "unit": "ms", "better": "lower", "group": "latency"},
    {"id": "ttft_p99_ms", "label": "TTFT p99", "unit": "ms", "better": "lower", "group": "latency"},
    {"id": "tpot_ms", "label": "TPOT mean", "unit": "ms", "better": "lower", "group": "latency"},
    {"id": "itl_ms", "label": "ITL mean", "unit": "ms", "better": "lower", "group": "latency"},
    {"id": "kv_cache_tokens", "label": "KV cache", "unit": "tokens", "better": "higher", "group": "capacity"},
    {"id": "kv_cache_gib", "label": "KV cache memory", "unit": "GiB", "better": "higher", "group": "capacity"},
    {"id": "max_concurrency", "label": "Max concurrency (reported)", "unit": "×", "better": "higher", "group": "capacity"},
    {"id": "acceptance_rate", "label": "Draft acceptance rate", "unit": "%", "better": "higher", "group": "speculative"},
    {"id": "acceptance_length", "label": "Mean acceptance length", "unit": "tokens", "better": "higher", "group": "speculative"},
    {"id": "concurrency", "label": "Concurrent users tested", "unit": "users", "better": None, "group": "context"},
    {"id": "input_tokens", "label": "Input length", "unit": "tokens", "better": None, "group": "context"},
    {"id": "output_tokens", "label": "Output length", "unit": "tokens", "better": None, "group": "context"},
    {"id": "gpu_count", "label": "GPUs", "unit": "", "better": None, "group": "context"},
    {"id": "tp_size", "label": "Tensor parallel", "unit": "", "better": None, "group": "context"},
]
METRIC_IDS = {metric["id"] for metric in METRICS}


def _number(value: Any, label: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number.")
    if not math.isfinite(value) or abs(value) > MAX_NUMBER:
        raise ValueError(f"{label} is out of range.")
    return int(value) if float(value).is_integer() else float(value)


def _text(value: Any, label: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    text = value.strip()
    if len(text) > limit:
        raise ValueError(f"{label} must be {limit} characters or fewer.")
    return text


def validate_results(raw: Any) -> dict[str, Any]:
    """Results as stored: known values, test context, custom metrics, and notes."""
    raw = raw if isinstance(raw, dict) else {}
    values: dict[str, float | int] = {}
    for key, value in (raw.get("values") or {}).items():
        if key not in METRIC_IDS:
            raise ValueError(f"Unknown metric {key!r}.")
        if value is not None:
            label = next(metric["label"] for metric in METRICS if metric["id"] == key)
            values[key] = _number(value, label)
    hardware = raw.get("hardware") or None
    if hardware is not None and hardware not in HARDWARE:
        raise ValueError("Unknown hardware.")
    custom = []
    items = raw.get("custom") or []
    if not isinstance(items, list) or len(items) > MAX_CUSTOM_METRICS:
        raise ValueError(f"Add at most {MAX_CUSTOM_METRICS} custom metrics.")
    for item in items:
        item = item if isinstance(item, dict) else {}
        name = _text(item.get("name"), "Metric name", 60)
        if not name:
            raise ValueError("Every custom metric needs a name.")
        better = item.get("better")
        if better not in {"higher", "lower", None}:
            raise ValueError("Better must be higher, lower, or empty.")
        custom.append(
            {
                "name": name,
                "value": _number(item.get("value"), name),
                "unit": _text(item.get("unit"), "Unit", 16),
                "better": better,
            }
        )
    return {
        "values": values,
        "hardware": hardware,
        "vllm_version": _text(raw.get("vllm_version"), "vLLM version", 40) or None,
        "custom": custom,
        "notes": _text(raw.get("notes"), "Notes", 2000),
    }


def has_results(results: dict[str, Any]) -> bool:
    return bool(results.get("values") or results.get("custom") or results.get("notes"))


class ConfigRevisions:
    def __init__(self, database: Database, history: RepoHistory):
        self.database = database
        self.history = history

    def _store(self, content: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.database.put_text_blob(digest, content)
        return digest

    @staticmethod
    def summary(revision: dict[str, Any]) -> dict[str, Any]:
        changes = revision["changes"]
        return {
            "id": revision["id"],
            "sequence": revision["sequence"],
            "parent_id": revision["parent_id"],
            "message": revision["message"],
            "description": revision["description"],
            "author_name": revision["author_name"],
            "created_at": revision["created_at"],
            "file_count": len(revision["files"]),
            "summary": {
                kind: sum(1 for change in changes if change["change"] == kind)
                for kind in ("added", "modified", "deleted")
            },
            "results": revision["results"],
            "results_updated_at": revision["results_updated_at"],
            "results_updated_by": revision["results_updated_by"],
        }

    def list(self, repo_id: str) -> list[dict[str, Any]]:
        return [self.summary(item) for item in self.database.list_config_revisions(repo_id)]

    def detail(self, repo_id: str, revision_id: str) -> dict[str, Any]:
        revision = self.database.get_config_revision(repo_id, revision_id)
        if not revision:
            raise FileNotFoundError("Config revision not found.")
        result = self.summary(revision)
        result["files"] = [
            {
                "path": item["path"],
                "size": item["size"],
                "content": self.database.get_text_blob(item["sha256"]) or "",
            }
            for item in revision["files"]
        ]
        result["changes"] = [self.history.diff(change) for change in revision["changes"]]
        return result

    def archive(self, repo_id: str, revision_id: str) -> tuple[str, bytes]:
        revision = self.database.get_config_revision(repo_id, revision_id)
        if not revision:
            raise FileNotFoundError("Config revision not found.")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in revision["files"]:
                archive.writestr(item["path"], self.database.get_text_blob(item["sha256"]) or "")
        name = f"{repo_id.replace('/', '--')}-config-{revision['sequence']}.zip"
        return name, buffer.getvalue()

    def create(
        self,
        repo_id: str,
        user: dict[str, Any],
        parent_id: str | None,
        files: list[dict[str, Any]],
        deletions: list[str],
        message: str,
        description: str = "",
        results: Any = None,
    ) -> dict[str, Any]:
        """Record a new revision: the latest one's files, with these added or
        replaced and those deleted. `parent_id` must be the latest revision, so
        two people editing at once cannot silently undo each other."""
        title = _text(message, "Message", 200)
        if not title:
            raise ValueError("Describe what changed in this revision.")
        detail = _text(description, "Description", 5000)
        checked = validate_results(results) if results is not None else validate_results({})
        latest = self.database.latest_config_revision(repo_id)
        if (latest["id"] if latest else None) != (parent_id or None):
            raise RuntimeError("Someone added a revision since you started. Reload and try again.")
        current = {item["path"]: item for item in (latest["files"] if latest else [])}
        snapshot = dict(current)
        changes: list[dict[str, Any]] = []
        if len(files) > MAX_FILES:
            raise ValueError(f"A config can have at most {MAX_FILES} files.")
        seen: set[str] = set()
        for item in files:
            path = validate_upload_path(str(item.get("path") or "")).as_posix()
            if path in seen:
                raise ValueError(f"{path} is listed twice.")
            seen.add(path)
            content = item.get("content")
            if not isinstance(content, str) or "\x00" in content:
                raise ValueError(f"{path} is not a text file.")
            size = len(content.encode("utf-8"))
            if size > MAX_FILE_BYTES:
                raise ValueError(f"{path} is larger than {MAX_FILE_BYTES // 1024} KB.")
            digest = self._store(content)
            previous = current.get(path)
            if previous and previous["sha256"] == digest:
                continue
            snapshot[path] = {"path": path, "sha256": digest, "size": size}
            changes.append(
                {
                    "path": path,
                    "change": "modified" if previous else "added",
                    "old_text": previous["sha256"] if previous else None,
                    "new_text": digest,
                    "size": size,
                }
            )
        for raw_path in deletions:
            path = validate_upload_path(str(raw_path)).as_posix()
            if path in seen:
                continue
            previous = snapshot.pop(path, None)
            if previous:
                changes.append(
                    {"path": path, "change": "deleted", "old_text": previous["sha256"], "new_text": None, "size": 0}
                )
        if not changes:
            raise ValueError("Add, change, or delete at least one file.")
        if not snapshot:
            raise ValueError("A config needs at least one file.")
        if len(snapshot) > MAX_FILES:
            raise ValueError(f"A config can have at most {MAX_FILES} files.")
        if sum(item["size"] for item in snapshot.values()) > MAX_TOTAL_BYTES:
            raise ValueError(f"A config can hold at most {MAX_TOTAL_BYTES // (1024 * 1024)} MB.")
        timestamp = _now()
        revision = self.database.create_config_revision(
            {
                "id": uuid.uuid4().hex,
                "repo_id": repo_id,
                "parent_id": latest["id"] if latest else None,
                "message": title,
                "description": detail,
                "author_id": user["id"],
                "author_name": user.get("display_name") or user.get("username") or "",
                "files_json": json.dumps(sorted(snapshot.values(), key=lambda item: item["path"])),
                "changes_json": json.dumps(sorted(changes, key=lambda item: item["path"])),
                "results_json": json.dumps(checked),
                "results_updated_at": timestamp if has_results(checked) else None,
                "results_updated_by": (user.get("display_name") or user.get("username")) if has_results(checked) else None,
                "created_at": timestamp,
            }
        )
        return self.summary(revision)

    def set_results(
        self, repo_id: str, revision_id: str, user: dict[str, Any], results: Any
    ) -> dict[str, Any]:
        checked = validate_results(results)
        revision = self.database.update_config_results(
            repo_id,
            revision_id,
            json.dumps(checked),
            _now(),
            user.get("display_name") or user.get("username") or "",
        )
        if not revision:
            raise FileNotFoundError("Config revision not found.")
        return self.summary(revision)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
