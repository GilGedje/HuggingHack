"""Several HuggingHack servers sharing one PostgreSQL database and the buckets
(CLUSTER_MODE; docs/SCALING.md, phase 3).

Every server answers requests. One of them, the leader, also does the work that
must happen once: recovering interrupted moves, the startup library scan, running
storage moves, and taking over jobs whose server went quiet. The leader is whoever
holds a session-level PostgreSQL advisory lock on a connection of its own; if that
server dies or loses the database, PostgreSQL releases the lock and another server
takes it within a few seconds.

Every server also ticks every few seconds: it says it is still running its own
jobs and moves (a heartbeat), and stops the ones someone cancelled through another
server.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .config import Settings
from .database import Database
from .storage import LOCAL_TARGET_ID, StorageRegistry

logger = logging.getLogger("hugginghack.cluster")

LEADER_LOCK = "hugginghack:leader"
TICK_SECONDS = 2.0
HEARTBEAT_SECONDS = 10.0
# Work whose server has not sent a heartbeat for three intervals is taken over.
STALE_SECONDS = 3 * HEARTBEAT_SECONDS


def utc_iso(seconds_ago: float = 0.0) -> str:
    return datetime.fromtimestamp(time.time() - seconds_ago, timezone.utc).isoformat()


def startup_problems(
    settings: Settings, database: Database, storages: StorageRegistry, system_remote: bool
) -> list[str]:
    """Why this server cannot share the library with others, one sentence each.
    Anything on a server's own disk is invisible to the others, so everything
    lasting must live in PostgreSQL or a bucket."""
    if not settings.cluster_mode:
        return []
    problems = []
    if database.backend != "postgresql":
        problems.append(
            "CLUSTER_MODE needs PostgreSQL: set DATABASE_URL to a postgresql:// address that every server shares."
        )
    if not storages.default.remote:
        problems.append(
            "CLUSTER_MODE needs new models to go to a bucket: set DEFAULT_STORAGE_TARGET to an S3 target id."
        )
    if not system_remote:
        problems.append(
            "CLUSTER_MODE needs SYSTEM_STORAGE_TARGET set to an S3 target id, so every server shares "
            "profile pictures and git history."
        )
    staging = [storage.id for storage in storages.remotes if not getattr(storage, "direct_uploads", False)]
    if staging:
        problems.append(
            f"CLUSTER_MODE needs direct uploads on every bucket, so no upload waits on one server's disk: "
            f"set direct_uploads for {', '.join(staging)} (and its bucket CORS policy, see docs/SERVE_FROM_S3.md)."
        )
    if settings.hf_downloads_enabled:
        problems.append(
            "CLUSTER_MODE does not support HF_DOWNLOADS_ENABLED yet: downloads are written to one "
            "server's disk. Turn it off, or download on a single server and move the model to a bucket."
        )
    if database.backend == "postgresql":
        local = [
            model["repo_id"]
            for model in database.list_local_models()
            if model.get("storage_target") == LOCAL_TARGET_ID
        ]
        if local:
            problems.append(
                f"{len(local)} model{'s are' if len(local) != 1 else ' is'} stored on a server's own disk "
                f"(first: {local[0]}). Move {'them' if len(local) != 1 else 'it'} to a bucket from "
                "Admin → Storage before turning on CLUSTER_MODE."
            )
    return problems


class Cluster:
    """Leader election and the per-server tick. With CLUSTER_MODE off this server
    is always the leader and nothing runs in the background."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        tick_seconds: float = TICK_SECONDS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ):
        self.settings = settings
        self.database = database
        self.enabled = settings.cluster_mode
        self.instance_id = settings.instance_id
        self.tick_seconds = tick_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._leader = not self.enabled
        self._session: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._elected: list[Callable[[], None]] = []
        self._tasks: list[Callable[[bool], None]] = []
        self._last_heartbeat = 0.0

    def is_leader(self) -> bool:
        return self._leader

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "instance_id": self.instance_id, "leader": self._leader}

    def when_elected(self, callback: Callable[[], None]) -> None:
        """Run `callback` (on a thread of its own) each time this server becomes leader."""
        self._elected.append(callback)

    def every_tick(self, callback: Callable[[bool], None]) -> None:
        """Run `callback(is_leader)` on every tick."""
        self._tasks.append(callback)

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="cluster", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop ticking and give up leadership at once, so another server takes over."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.tick_seconds + 5)
            self._thread = None
        self._close()
        if self.enabled:
            self._leader = False

    def _run(self) -> None:
        while not self._stop.is_set():
            self.step()
            self._stop.wait(self.tick_seconds)

    def step(self) -> None:
        """One tick: keep or seek the leader lock, send the heartbeat, run the tasks."""
        was_leader = self._leader
        self._leader = self._hold_leader_lock()
        if self._leader and not was_leader:
            logger.info("This server (%s) is now the leader.", self.instance_id)
            for callback in self._elected:
                threading.Thread(target=self._guarded, args=(callback,), daemon=True).start()
        elif was_leader and not self._leader:
            logger.warning("This server (%s) is no longer the leader.", self.instance_id)
        now = time.monotonic()
        if now - self._last_heartbeat >= self.heartbeat_seconds:
            if self._guarded(lambda: self.database.heartbeat(self.instance_id, utc_iso())):
                self._last_heartbeat = now
        for task in self._tasks:
            self._guarded(lambda: task(self._leader))

    @staticmethod
    def _guarded(callback: Callable[[], Any]) -> bool:
        try:
            callback()
            return True
        except Exception:  # noqa: BLE001 - one failing task must not stop the tick
            logger.exception("Cluster task failed")
            return False

    def _hold_leader_lock(self) -> bool:
        """Whether this server holds the leader lock after this tick. A lost
        connection loses the lock with it, so leadership is given up at once."""
        try:
            if self._session is None or self._session.closed:
                self._session = self.database.open_session()
            elif self._leader:
                self._session.execute("SELECT 1")
                return True
            row = self._session.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (LEADER_LOCK,)
            ).fetchone()
            return bool(row and row[0])
        except Exception as error:  # noqa: BLE001 - an unreachable database is a lost lock
            logger.warning("Lost the cluster connection: %s", error.__class__.__name__)
            self._close()
            return False

    def _close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
