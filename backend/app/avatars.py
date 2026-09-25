"""Profile pictures for users and organizations.

The browser crops and re-encodes a picture before sending it; the server still
decides what the bytes are from their signature, never from what the client says,
keeps only PNG, JPEG, and WebP, and serves them so nothing in them can run.
"""

from typing import Any
from urllib.parse import quote

MAX_AVATAR_BYTES = 512 * 1024
AVATAR_KINDS = {"user": "users", "organization": "organizations"}


def image_type(data: bytes) -> str | None:
    """The image type the bytes really are, or None when they are not one we keep."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def avatar_url(namespace: str, version: str | None) -> str | None:
    return f"/api/avatars/{quote(namespace, safe='')}?v={quote(version, safe='')}" if version else None


class AvatarStore:
    """Pictures in the system folder: avatars/users/<id> and avatars/organizations/<id>.
    Files are named by id, so names never reach the file system or the bucket."""

    def __init__(self, store: Any):
        self.store = store

    @staticmethod
    def _key(kind: str, owner_id: str) -> str:
        return f"avatars/{AVATAR_KINDS[kind]}/{owner_id}"

    def save(self, kind: str, owner_id: str, data: bytes) -> str:
        detected = image_type(data)
        if detected is None:
            raise ValueError("Use a PNG, JPEG, or WebP picture.")
        if len(data) > MAX_AVATAR_BYTES:
            raise ValueError("The picture is larger than 512 KB.")
        self.store.put(self._key(kind, owner_id), data, detected)
        return detected

    def read(self, kind: str, owner_id: str) -> tuple[bytes, str] | None:
        data = self.store.get(self._key(kind, owner_id))
        detected = image_type(data) if data else None
        return (data, detected) if data and detected else None

    def delete(self, kind: str, owner_id: str) -> None:
        self.store.delete(self._key(kind, owner_id))
