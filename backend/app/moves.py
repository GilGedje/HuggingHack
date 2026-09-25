"""Moving a model between storage locations without disturbing anyone pulling it.

A move copies every file to the new location while hashing it, reads the copy
back to check the hashes match, and only then switches the model over. Pulls that
started before the switch keep reading the old copy; the old copy is removed once
the last of them finishes. The revision a client pinned before the switch keeps
working afterwards, because the files behind it are the same.
"""

import hashlib
import io
import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .config import Settings, repository_path, validate_repo_id
from .database import Database
from .reads import ReadTracker
from .storage import LOCAL_TARGET_ID, MANIFEST_NAME, StorageRegistry, repository_files

logger = logging.getLogger("hugginghack.moves")

MOVES_DIRECTORY = ".hugginghack-moves"
CHUNK_BYTES = 8 * 1024 * 1024
PRE_SWITCH = ("queued", "copying", "verifying", "switching")
POST_SWITCH = ("draining", "cleaning")
PROGRESS_SECONDS = 0.5
DRAIN_POLL_SECONDS = 1.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MoveCancelled(Exception):
    pass


class _Reader(io.RawIOBase):
    """A file-like view of a chunk iterator that hashes what passes through, for
    uploads that want something to read from."""

    def __init__(self, chunks: Iterator[bytes], on_bytes: Callable[[int], None]):
        self._chunks = chunks
        self._buffer = b""
        self._on_bytes = on_bytes
        self.digest = hashlib.sha256()
        self.size = 0

    def readable(self) -> bool:
        return True

    def readinto(self, target: Any) -> int:
        while not self._buffer:
            try:
                self._buffer = next(self._chunks)
            except StopIteration:
                return 0
            self.digest.update(self._buffer)
            self.size += len(self._buffer)
            self._on_bytes(len(self._buffer))
        count = min(len(target), len(self._buffer))
        target[:count] = self._buffer[:count]
        self._buffer = self._buffer[count:]
        return count


class MoveManager:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        storages: StorageRegistry,
        repositories: Any,
        history: Any,
        mirrors: Any,
        tracker: ReadTracker,
        busy: Callable[[str], str | None],
    ):
        self.settings = settings
        self.database = database
        self.storages = storages
        self.repositories = repositories
        self.history = history
        self.mirrors = mirrors
        self.tracker = tracker
        self.busy = busy
        self._wake = threading.Event()
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # File hashes of a switched move, kept for relabeling after the cleanup.
        self._sums: dict[str, dict[str, str]] = {}
        # When each move switched (time.monotonic()). After a restart no read of an
        # old copy can still be running, so the manager's start time stands in.
        self._switched: dict[str, float] = {}
        self._started = time.monotonic()

    # ---- public -----------------------------------------------------------------

    def moving(self, repo_id: str) -> str | None:
        """Why a repository cannot change right now because it is being moved."""
        for move in self.database.unfinished_moves():
            if move["repo_id"] == repo_id:
                return "This model is moving to another storage location. Try again when the move finishes."
        return None

    def overview(self) -> list[dict[str, Any]]:
        moves = self.database.list_moves()
        for move in moves:
            if move["status"] in POST_SWITCH:
                move["active_reads"] = self.tracker.active(move["repo_id"])
        return moves

    def start(
        self, user: dict[str, Any], repo_id: str, destination: str, confirmation: str, keep_local: bool
    ) -> dict[str, Any]:
        validated = validate_repo_id(repo_id)
        if confirmation != validated:
            raise ValueError("Type the model name exactly to confirm the move.")
        model = self.database.get_local_model(validated)
        if not model:
            raise FileNotFoundError("Model not found.")
        if model.get("relative_path") != validated:
            raise ValueError("Only models stored as owner/name folders can be moved.")
        source = self.storages.for_model(model)
        target = self.storages.get(destination)
        if target.id == source.id:
            raise ValueError(f"{validated} is already stored in {target.name}.")
        if not target.health().get("connected"):
            raise ValueError(f"{target.name} is not reachable right now.")
        reason = self.moving(validated) or self.busy(validated)
        if reason:
            raise ValueError(reason)
        if source.remote:
            manifest = source.repository_manifest(validated)
            if not manifest or manifest.get("status") != "complete":
                raise ValueError(f"{source.name} does not hold a complete copy of {validated}.")
        if target.remote and target.has_objects(validated):
            raise ValueError(f"{target.name} already has files under {validated}. Remove them before moving here.")
        if not target.remote:
            need = int(model.get("size_bytes") or 0)
            free = shutil.disk_usage(self.settings.model_storage).free
            if need * 1.05 + 256 * 1024**2 > free:
                raise ValueError(f"{target.name} needs about {need / 1024**3:.1f} GB free for this model.")
        timestamp = _now()
        move = self.database.create_move(
            {
                "id": uuid.uuid4().hex,
                "repo_id": validated,
                "source_target": source.id,
                "destination_target": target.id,
                "keep_local": int(bool(keep_local) and target.remote and not source.remote),
                "status": "queued",
                "message": "Waiting to start",
                "created_by": user["id"],
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        self._ensure_worker()
        self._wake.set()
        return move

    def cancel(self, move_id: str) -> dict[str, Any]:
        move = self.database.get_move(move_id)
        if not move:
            raise FileNotFoundError("Move not found.")
        if move["status"] == "queued":
            return self.database.update_move(
                move_id, status="cancelled", message="Cancelled before it started", updated_at=_now(), finished_at=_now()
            )
        if move["status"] in ("copying", "verifying"):
            with self._lock:
                self._cancelled.add(move_id)
            return move
        raise ValueError("This move is past the point where it can be cancelled.")

    def recover(self) -> None:
        """After a restart: undo moves that had not switched, finish those that had."""
        for move in self.database.unfinished_moves():
            if move["status"] in ("copying", "verifying", "switching"):
                model = self.database.get_local_model(move["repo_id"]) or {}
                if move["status"] == "switching" and model.get("storage_target") == move["destination_target"]:
                    self.database.update_move(move["id"], status="draining", message="Resuming after a restart", updated_at=_now())
                    continue
                self._discard_copy(move)
                self.database.update_move(
                    move["id"], status="failed", error="HuggingHack restarted during the move; the model stayed where it was.",
                    message="Interrupted", updated_at=_now(), finished_at=_now(),
                )
        if self.database.unfinished_moves():
            self._ensure_worker()
            self._wake.set()

    # ---- worker -----------------------------------------------------------------

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="storage-moves", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            pending = self.database.unfinished_moves()
            # Switched moves wait for their old copy's downloads without holding up
            # the next move; each pass tries to finish them.
            for move in pending:
                if move["status"] in POST_SWITCH:
                    self._guarded(self._finish, move)
            queued = next((item for item in pending if item["status"] == "queued"), None)
            if queued is not None:
                self._guarded(self._run, queued)
                continue
            waiting = any(item["status"] in POST_SWITCH for item in pending)
            self._wake.wait(timeout=DRAIN_POLL_SECONDS if waiting else 30)
            self._wake.clear()

    def _guarded(self, step: Callable[[dict[str, Any]], Any], move: dict[str, Any]) -> None:
        try:
            step(move)
        except Exception:  # noqa: BLE001 - one bad move must not stop the worker
            logger.exception("Storage move %s failed unexpectedly", move["id"])
            self.database.update_move(
                move["id"], status="failed", error="The move stopped unexpectedly; see the server log.",
                updated_at=_now(), finished_at=_now(),
            )

    # ---- one move ---------------------------------------------------------------

    def _paths(self, move: dict[str, Any]) -> tuple[Path, Path, Path]:
        base = self.settings.model_storage / MOVES_DIRECTORY
        return base / move["id"], base / f"{move['id']}-previous", repository_path(move["repo_id"], self.settings.model_storage)

    def _check(self, move_id: str) -> None:
        with self._lock:
            if move_id in self._cancelled:
                raise MoveCancelled()

    def _progress(self, move: dict[str, Any], field: str) -> Callable[[int], None]:
        state = {"done": 0, "saved": 0.0}

        def advance(count: int) -> None:
            self._check(move["id"])
            state["done"] += count
            now = time.monotonic()
            if now - state["saved"] >= PROGRESS_SECONDS:
                state["saved"] = now
                self.database.update_move(move["id"], **{field: state["done"], "updated_at": _now()})

        return advance

    def _source_files(self, move: dict[str, Any], source: Any, root: Path) -> list[tuple[str, int]]:
        if source.remote:
            return sorted(source.object_files(move["repo_id"]))
        return sorted(
            (relative, path.stat().st_size) for path, relative in repository_files(root) if relative != MANIFEST_NAME
        )

    def _read_source(self, source: Any, repo_id: str, root: Path, relative: str, size: int) -> Iterator[bytes]:
        if source.remote:
            if size:
                yield from source.iter_repository_file(repo_id, relative, 0, size - 1)
            return
        with (root / relative).open("rb") as handle:
            while chunk := handle.read(CHUNK_BYTES):
                yield chunk

    def _read_destination(self, target: Any, repo_id: str, staging: Path, relative: str, size: int) -> Iterable[bytes]:
        if target.remote:
            return target.iter_repository_file(repo_id, relative, 0, size - 1) if size else []
        return self._read_file(staging / relative)

    @staticmethod
    def _read_file(path: Path) -> Iterator[bytes]:
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK_BYTES):
                yield chunk

    def _source_manifest(self, source: Any, repo_id: str, root: Path) -> dict[str, Any]:
        if source.remote:
            return dict(source.repository_manifest(repo_id) or {})
        try:
            value = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _run(self, move: dict[str, Any]) -> None:
        repo_id = move["repo_id"]
        source = self.storages.get(move["source_target"])
        target = self.storages.get(move["destination_target"])
        staging, _, root = self._paths(move)
        try:
            files = self._source_files(move, source, root)
            total = sum(size for _, size in files)
            self.database.update_move(
                move["id"], status="copying", message=f"Copying to {target.name}",
                total_bytes=total, file_count=len(files), updated_at=_now(),
            )
            copied = self._progress(move, "copied_bytes")
            sums: dict[str, str] = {}
            for relative, size in files:
                chunks = self._read_source(source, repo_id, root, relative, size)
                if target.remote:
                    reader = _Reader(chunks, copied)
                    target.write_object(repo_id, relative, io.BufferedReader(reader, CHUNK_BYTES))
                    written, digest = reader.size, reader.digest.hexdigest()
                else:
                    path = staging / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    hasher, written = hashlib.sha256(), 0
                    with path.open("wb") as handle:
                        for chunk in chunks:
                            copied(len(chunk))
                            hasher.update(chunk)
                            handle.write(chunk)
                            written += len(chunk)
                    digest = hasher.hexdigest()
                if written != size:
                    raise ValueError(f"{relative} changed size while it was being copied.")
                sums[relative] = digest
            self.database.update_move(
                move["id"], status="verifying", message="Checking every file against its hash",
                copied_bytes=total, updated_at=_now(),
            )
            verified = self._progress(move, "verified_bytes")
            for relative, size in files:
                hasher, read = hashlib.sha256(), 0
                for chunk in self._read_destination(target, repo_id, staging, relative, size):
                    verified(len(chunk))
                    hasher.update(chunk)
                    read += len(chunk)
                if read != size or hasher.hexdigest() != sums[relative]:
                    raise ValueError(f"The copy of {relative} does not match the original.")
            self.database.update_move(
                move["id"], status="switching", message="Switching to the new copy", verified_bytes=total, updated_at=_now()
            )
            self._switch(move, source, target, staging, root, sums)
        except Exception as error:  # noqa: BLE001
            with self._lock:
                cancelled = move["id"] in self._cancelled
            if not cancelled:
                logger.exception("Storage move of %s failed", repo_id)
                self._discard_copy(move)
                self.database.update_move(
                    move["id"], status="failed", error=str(error)[:500] or error.__class__.__name__,
                    message="The model stayed where it was", updated_at=_now(), finished_at=_now(),
                )
                return
            # A cancel can surface as any error from inside an upload thread.
            self._discard_copy(move)
            self.database.update_move(
                move["id"], status="cancelled", message="Cancelled; the model stayed where it was",
                updated_at=_now(), finished_at=_now(),
            )
            return
        finally:
            with self._lock:
                self._cancelled.discard(move["id"])
        self._finish(self.database.get_move(move["id"]) or move)

    def _switch(self, move: dict[str, Any], source: Any, target: Any, staging: Path, root: Path, sums: dict[str, str]) -> None:
        repo_id = move["repo_id"]
        old_sha = self._sha(repo_id)
        base = {**self._source_manifest(source, repo_id, root), "status": "complete", "repo_id": repo_id}
        if target.remote:
            manifest = {**base, "storage_backend": "s3", "storage_target": target.id, "remote_uri": target.remote_uri(repo_id)}
            target.publish_manifest(repo_id, manifest)
            model = self.database.get_local_model(repo_id) or {}
            cached = bool(model.get("cached")) and root.is_dir()
            if cached:
                # The local folder is now the new location's cache.
                (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            self.database.set_local_model_location(repo_id, "s3", target.id, target.remote_uri(repo_id), cached)
        else:
            manifest = {**base, "storage_backend": "filesystem", "storage_target": LOCAL_TARGET_ID}
            manifest.pop("remote_uri", None)
            (staging / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            _, previous, _ = self._paths(move)
            if root.exists():
                # An old cache of the source; reads that have it open keep reading it.
                os.replace(root, previous)
            root.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, root)
            self.database.set_local_model_location(repo_id, "filesystem", LOCAL_TARGET_ID, None, True)
        if source.remote:
            # Rescans stop seeing the old copy; reads already under way still finish.
            source.delete_manifest(repo_id)
        self._relabel(repo_id, sums, old_sha)
        self.database.update_move(
            move["id"], status="draining", message="Waiting for downloads of the old copy to finish",
            switched_at=_now(), updated_at=_now(),
        )
        self._sums[move["id"]] = sums
        self._switched[move["id"]] = time.monotonic()

    def _finish(self, move: dict[str, Any]) -> bool:
        """Remove the old copy once nothing can be reading it. Returns False while
        it still has to wait; the worker tries again on its next pass."""
        repo_id = move["repo_id"]
        source = self.storages.get(move["source_target"])
        staging, previous, root = self._paths(move)
        removes_files = source.remote or not move["keep_local"]
        # After the switch no read goes to the source bucket, so only reads that
        # began before it matter; a local cache is read until it is gone.
        started_before = self._switched.get(move["id"], self._started) if source.remote else None
        if removes_files and not self.tracker.try_begin_removal(repo_id, started_before):
            active = self.tracker.active(repo_id, started_before)
            if active != move.get("active_reads") or move["status"] != "draining":
                self.database.update_move(
                    move["id"], status="draining", active_reads=active, updated_at=_now(),
                    message=f"Waiting for {active} download{'s' if active != 1 else ''} of the old copy to finish",
                )
            return False
        try:
            self.database.update_move(move["id"], status="cleaning", active_reads=0, message="Removing the old copy", updated_at=_now())
            # New reads are held back now, so the files cannot change under this.
            old_sha = self._sha(repo_id)
            if source.remote:
                source.delete_repository(repo_id)
            elif not move["keep_local"] and root.is_dir():
                shutil.rmtree(root)
                model = self.database.get_local_model(repo_id) or {}
                self.database.set_local_model_location(
                    repo_id, model.get("storage_backend") or "s3", move["destination_target"],
                    model.get("remote_uri"), False,
                )
            shutil.rmtree(previous, ignore_errors=True)
            shutil.rmtree(staging, ignore_errors=True)
            self._relabel(repo_id, self._sums.pop(move["id"], {}), old_sha)
        except Exception as error:  # noqa: BLE001
            logger.exception("Could not remove the old copy of %s", repo_id)
            self.database.update_move(
                move["id"], status="failed", message="Moved, but the old copy was not removed",
                error=str(error)[:500], updated_at=_now(), finished_at=_now(),
            )
            return True
        finally:
            if removes_files:
                self.tracker.end_removal(repo_id)
        self._switched.pop(move["id"], None)
        self.database.update_move(move["id"], status="done", message="Moved", updated_at=_now(), finished_at=_now())
        return True

    def _discard_copy(self, move: dict[str, Any]) -> None:
        """Remove whatever a move that did not switch left at its destination."""
        staging, _, _ = self._paths(move)
        shutil.rmtree(staging, ignore_errors=True)
        try:
            target = self.storages.get(move["destination_target"])
        except ValueError:
            return
        model = self.database.get_local_model(move["repo_id"]) or {}
        if target.remote and model.get("storage_target") != target.id:
            try:
                target.delete_repository(move["repo_id"])
            except Exception:  # noqa: BLE001
                logger.exception("Could not remove the partial copy of %s", move["repo_id"])

    # ---- keeping pulls working across the switch ------------------------------------

    def _sha(self, repo_id: str) -> str | None:
        model = self.database.get_local_model(repo_id)
        try:
            return self.repositories.snapshot_for_model(model).sha if model else None
        except Exception:  # noqa: BLE001 - no files yet is not an error here
            return None

    def _relabel(self, repo_id: str, sums: dict[str, str], old_sha: str | None) -> None:
        """The files are the same but their timestamps are not: carry the history,
        the file hashes, the git mirror, and the old revision over to the new ones."""
        model = self.database.get_local_model(repo_id)
        if not model:
            return
        durable = self.history.durable_entries(model)
        self.database.relabel_latest_commit(repo_id, {entry.path: (entry.size, entry.version) for entry in durable})
        try:
            snapshot = self.repositories.snapshot_for_model(model)
        except Exception:  # noqa: BLE001
            return
        for entry in snapshot.entries:
            if entry.path in sums:
                self.database.set_file_digest(repo_id, entry.path, entry.version, sums[entry.path])
        if old_sha and snapshot.sha != old_sha:
            self.database.add_revision_alias(repo_id, old_sha, snapshot.sha, _now())
            self.mirrors.relabel(repo_id, old_sha, snapshot.sha)
