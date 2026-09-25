"""Who is reading a repository's files right now.

A storage move copies a model, switches it to the new location, and only then
removes the old copy. Every file read takes a lease here first, so the old copy
is removed only once nobody is reading it, and a read that starts while it is
being removed waits and then reads from the new location.
"""

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from starlette.responses import Response
from starlette.types import Receive, Scope, Send

# A lease this old belongs to a reader that went away without saying so; it no
# longer holds back the removal of an old copy.
STALE_LEASE_SECONDS = 24 * 60 * 60


class ReadTracker:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._leases: dict[int, tuple[str, float]] = {}
        self._removing: set[str] = set()
        self._next = 0

    def acquire(self, repo_id: str) -> int:
        """Start a read; waits while the repository's old copy is being removed."""
        repo_id = repo_id.lower()  # URLs may spell a repository in any case
        with self._condition:
            while repo_id in self._removing:
                self._condition.wait(timeout=1)
            self._next += 1
            self._leases[self._next] = (repo_id, time.monotonic())
            return self._next

    def release(self, lease: int | None) -> None:
        if lease is None:
            return
        with self._condition:
            self._leases.pop(lease, None)
            self._condition.notify_all()

    @contextmanager
    def hold(self, repo_id: str) -> Iterator[None]:
        lease = self.acquire(repo_id)
        try:
            yield
        finally:
            self.release(lease)

    def active(self, repo_id: str) -> int:
        repo_id = repo_id.lower()
        cutoff = time.monotonic() - STALE_LEASE_SECONDS
        with self._condition:
            return sum(1 for name, started in self._leases.values() if name == repo_id and started > cutoff)

    @contextmanager
    def removing(self, repo_id: str) -> Iterator[None]:
        """Hold new reads of `repo_id` back while its old copy is removed. Call it
        once `active(repo_id)` is zero."""
        repo_id = repo_id.lower()
        with self._condition:
            self._removing.add(repo_id)
        try:
            yield
        finally:
            with self._condition:
                self._removing.discard(repo_id)
                self._condition.notify_all()


class LeasedResponse(Response):
    """Wraps a response so its read lease ends when sending ends, however it ends:
    finished, failed, or the client hung up mid-download. It is a Response so the
    framework sends it as it is."""

    def __init__(self, inner: Response, tracker: ReadTracker, lease: int):
        self.inner = inner
        self.tracker = tracker
        self.lease = lease
        self.status_code = inner.status_code
        self.raw_headers = inner.raw_headers

    @property
    def background(self) -> Any:  # type: ignore[override]
        return self.inner.background

    @background.setter
    def background(self, value: Any) -> None:
        self.inner.background = value

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.inner(scope, receive, send)
        finally:
            self.tracker.release(self.lease)


reads = ReadTracker()
