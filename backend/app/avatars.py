"""Profile pictures for users and organizations.

The browser crops and re-encodes a picture before sending it; the server still
decides what the bytes are from their signature, never from what the client says,
keeps only PNG, JPEG, and WebP, and serves them so nothing in them can run.
"""

from pathlib import Path
from urllib.parse import quote

from .config import Settings

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
    def __init__(self, settings: Settings):
        self.root = settings.data_dir / "avatars"

    def _path(self, kind: str, owner_id: str) -> Path:
        # Files are named by id: names never reach the file system.
        return self.root / AVATAR_KINDS[kind] / owner_id

    def save(self, kind: str, owner_id: str, data: bytes) -> str:
        detected = image_type(data)
        if detected is None:
            raise ValueError("Use a PNG, JPEG, or WebP picture.")
        if len(data) > MAX_AVATAR_BYTES:
            raise ValueError("The picture is larger than 512 KB.")
        path = self._path(kind, owner_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        partial.write_bytes(data)
        partial.replace(path)
        return detected

    def read(self, kind: str, owner_id: str) -> tuple[bytes, str] | None:
        try:
            data = self._path(kind, owner_id).read_bytes()
        except OSError:
            return None
        detected = image_type(data)
        return (data, detected) if detected else None

    def delete(self, kind: str, owner_id: str) -> None:
        self._path(kind, owner_id).unlink(missing_ok=True)
