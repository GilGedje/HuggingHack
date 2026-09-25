"""The site's own files, apart from the database and the models.

Everything lives under one folder with a fixed layout, on local disk or in an S3
bucket (SYSTEM_STORAGE_TARGET), so a deployment can keep all of its lasting data in
S3 and the database:

    _system/
      README.txt
      avatars/users/<user id>
      avatars/organizations/<organization id>
      git-mirrors/<owner>/<name>/...        the git history clones pull

The leading underscore can never be a user or organization name, so the folder is
never mistaken for a model.
"""

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from .config import Settings

logger = logging.getLogger("hugginghack.system")

SYSTEM_FOLDER = "_system"
README = """This folder holds HuggingHack's own data. Do not edit it by hand.

avatars/users/<id>           profile pictures of accounts
avatars/organizations/<id>   profile pictures of organizations
git-mirrors/<owner>/<name>/  git history served to `git clone` (weights are LFS pointers)
"""
KEY_PART = re.compile(r"^[A-Za-z0-9._-]+$")


class SystemStoreError(RuntimeError):
    """The system folder could not be reached."""


def check_key(key: str) -> str:
    parts = key.split("/")
    if not parts or any(not KEY_PART.fullmatch(part) or part in {".", ".."} for part in parts):
        raise ValueError(f"Invalid system key {key!r}.")
    return key


class LocalSystemStore:
    remote = False

    def __init__(self, settings: Settings):
        self.root = settings.data_dir / "system"
        self.name = "This server's disk"

    def location(self) -> str:
        return str(self.root)

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*check_key(key).split("/"))

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".{path.name}.part")
        partial.write_bytes(data)
        partial.replace(path)

    def get(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except OSError:
            return None

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        shutil.rmtree(self._path(prefix), ignore_errors=True)

    def keys(self, prefix: str) -> dict[str, int]:
        base = self._path(prefix)
        if not base.is_dir():
            return {}
        return {
            f"{prefix}/{path.relative_to(base).as_posix()}": path.stat().st_size
            for path in base.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        }

    def probe(self) -> dict[str, Any]:
        return {"ok": True, "error": None}


class S3SystemStore:
    remote = True

    def __init__(self, storage: Any, prefix: str):
        self.storage = storage
        self.prefix = prefix.strip("/")
        self.name = storage.name

    def location(self) -> str:
        return f"s3://{self.storage.bucket}/{self.prefix}/"

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{check_key(key)}"

    def _call(self, action: str, operation: Any) -> Any:
        try:
            return operation()
        except Exception as error:  # noqa: BLE001 - any S3 failure means "unreachable"
            logger.warning("System storage %s failed: %s", action, error.__class__.__name__)
            raise SystemStoreError(f"{self.name} could not {action}.") from error

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self._call("save the file", lambda: self.storage.client.put_object(
            Bucket=self.storage.bucket, Key=self._key(key), Body=data, **extra
        ))

    def get(self, key: str) -> bytes | None:
        def read() -> bytes | None:
            try:
                response = self.storage.client.get_object(Bucket=self.storage.bucket, Key=self._key(key))
            except Exception as error:  # noqa: BLE001
                code = getattr(error, "response", {}).get("Error", {}).get("Code") if hasattr(error, "response") else None
                if isinstance(error, KeyError) or code in {"NoSuchKey", "404", "NotFound"}:
                    return None
                raise
            body = response["Body"]
            try:
                return body.read()
            finally:
                close = getattr(body, "close", None)
                if close:
                    close()

        return self._call("read the file", read)

    def delete(self, key: str) -> None:
        self._call("delete the file", lambda: self.storage._delete_keys([self._key(key)]))

    def delete_prefix(self, prefix: str) -> None:
        base = f"{self._key(prefix)}/"
        self._call("delete the folder", lambda: self.storage._delete_keys(
            [item["Key"] for item in self.storage._objects(base)]
        ))

    def keys(self, prefix: str) -> dict[str, int]:
        base = f"{self._key(prefix)}/"
        items = self._call("list the folder", lambda: list(self.storage._objects(base)))
        return {f"{prefix}/{item['Key'][len(base):]}": int(item.get("Size") or 0) for item in items}

    def probe(self) -> dict[str, Any]:
        """Write, read back, and delete a small file."""
        try:
            self.put("probe", b"ok", "text/plain")
            if self.get("probe") != b"ok":
                raise SystemStoreError(f"{self.name} returned something else than was written.")
            self.delete("probe")
        except SystemStoreError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "error": None}


def create_system_store(settings: Settings, storages: Any) -> LocalSystemStore | S3SystemStore:
    target_id = settings.system_storage_target
    if target_id == "local":
        return LocalSystemStore(settings)
    storage = storages.get(target_id)  # raises ValueError for an unknown id
    if not storage.remote:
        return LocalSystemStore(settings)
    prefix = settings.system_storage_prefix or "/".join(
        part for part in (storage.prefix, SYSTEM_FOLDER) if part
    )
    return S3SystemStore(storage, prefix)


def migrate_local_data(settings: Settings, store: Any) -> dict[str, int]:
    """Bring files from before the system folder, or from local disk when the site
    moved to S3, into the store. A local file is removed only once its copy reads back
    whole; anything that fails stays and is tried again on the next start."""
    moved = {"copied": 0, "kept": 0}
    sources: list[tuple[Path, str]] = [(settings.data_dir / "avatars", "avatars")]
    if store.remote:
        sources.append((settings.data_dir / "system", ""))
    for root, key_prefix in sources:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name.startswith(".") or path.name == "README.txt":
                continue
            relative = path.relative_to(root).as_posix()
            key = f"{key_prefix}/{relative}" if key_prefix else relative
            if not store.remote and path == settings.data_dir / "system" / key:
                continue
            try:
                data = path.read_bytes()
                store.put(key, data)
                if store.get(key) != data:
                    raise SystemStoreError("The copy did not read back the same.")
                path.unlink()
                moved["copied"] += 1
            except (OSError, ValueError, SystemStoreError) as error:
                logger.warning("Kept %s on local disk: %s", path, error)
                moved["kept"] += 1
    return moved


def ensure_readme(store: Any) -> None:
    try:
        if store.get("README.txt") is None:
            store.put("README.txt", README.encode("utf-8"), "text/plain")
    except SystemStoreError:
        logger.warning("Could not write the system folder's README.")
