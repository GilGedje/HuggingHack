from __future__ import annotations

import json
import os
import re
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .database import Database


WEIGHT_EXTENSIONS = {
    ".safetensors",
    ".gguf",
    ".bin",
    ".pt",
    ".pth",
    ".ckpt",
    ".onnx",
    ".h5",
    ".msgpack",
}
UNSAFE_EXTENSIONS = {".bin", ".pt", ".pth", ".pkl", ".pickle", ".ckpt"}
CONFIG_FILES = {"config.json", "model_index.json", "tokenizer.json", "params.json"}
LOCAL_TARGET_ID = "local"
LEGACY_S3_TARGET_ID = "s3"
FORMAT_EXTENSIONS = {
    ".safetensors": "safetensors",
    ".gguf": "gguf",
    ".bin": "pytorch",
    ".pt": "pytorch",
    ".pth": "pytorch",
    ".ckpt": "pytorch",
    ".onnx": "onnx",
    ".h5": "tensorflow",
    ".msgpack": "flax",
}
GGUF_SHARD_PATTERN = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$", re.IGNORECASE)
SAFETENSORS_MAX_HEADER_BYTES = 100_000_000
GGUF_MAX_STRING_BYTES = 16_000_000
GGUF_MAX_ITEMS = 50_000_000
# GGUF scalar value types mapped to their struct format.
GGUF_SCALARS = {
    0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i",
    6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def directory_stats(path: Path, include_cache: bool = False) -> tuple[int, int, float]:
    size = 0
    count = 0
    latest = path.stat().st_mtime if path.exists() else 0
    for root, directories, files in os.walk(path):
        directories[:] = [
            name for name in directories if include_cache or name not in {".cache", "__pycache__"}
        ]
        for name in files:
            file_path = Path(root) / name
            try:
                stat = file_path.stat()
            except (FileNotFoundError, PermissionError, OSError):
                continue
            size += stat.st_size
            count += 1
            latest = max(latest, stat.st_mtime)
    return size, count, latest


def manifest_target(manifest: dict[str, Any]) -> str:
    """Storage target of a repository manifest, including pre-target manifests."""
    target = manifest.get("storage_target")
    if isinstance(target, str) and target:
        return target
    return LEGACY_S3_TARGET_ID if manifest.get("storage_backend") == "s3" else LOCAL_TARGET_ID


def upload_is_registered(database: Database, repo_id: str, manifest: dict[str, Any]) -> bool:
    """A user upload is indexed only when the database knows who owns it.

    Organization repositories are checked against their organization, since the
    account that created one can leave or be deleted.
    """
    repository = database.get_owned_repository(repo_id)
    if not repository:
        return False
    if repository.get("organization_id") or manifest.get("organization_id"):
        return manifest.get("organization_id") == repository.get("organization_id")
    return bool(manifest.get("owner_id")) and manifest.get("owner_id") == repository["owner_id"]


def model_formats(relative_paths: Iterable[str]) -> list[str]:
    formats = {
        FORMAT_EXTENSIONS[suffix]
        for suffix in (Path(value).suffix.lower() for value in relative_paths)
        if suffix in FORMAT_EXTENSIONS
    }
    return sorted(formats)


def safetensors_parameter_count(path: Path) -> int | None:
    """Sum tensor element counts from a SafeTensors header without reading weights."""
    try:
        with path.open("rb") as handle:
            raw_length = handle.read(8)
            if len(raw_length) != 8:
                return None
            (length,) = struct.unpack("<Q", raw_length)
            if length <= 0 or length > SAFETENSORS_MAX_HEADER_BYTES:
                return None
            header = json.loads(handle.read(length))
    except (OSError, ValueError):
        return None
    if not isinstance(header, dict):
        return None
    total = 0
    for name, tensor in header.items():
        if name == "__metadata__" or not isinstance(tensor, dict):
            continue
        shape = tensor.get("shape")
        if not isinstance(shape, list):
            continue
        count = 1
        for dimension in shape:
            if not isinstance(dimension, int) or dimension < 0:
                return None
            count *= dimension
        total += count
    return total or None


class _GgufReader:
    def __init__(self, handle: Any):
        self.handle = handle

    def read(self, size: int) -> bytes:
        data = self.handle.read(size)
        if len(data) != size:
            raise ValueError("Unexpected end of GGUF header.")
        return data

    def unpack(self, fmt: str) -> Any:
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))[0]

    def skip_string(self) -> None:
        length = self.unpack("<Q")
        if length > GGUF_MAX_STRING_BYTES:
            raise ValueError("GGUF string is too large.")
        self.handle.seek(length, os.SEEK_CUR)

    def string(self) -> str:
        length = self.unpack("<Q")
        if length > GGUF_MAX_STRING_BYTES:
            raise ValueError("GGUF string is too large.")
        return self.read(length).decode("utf-8", errors="replace")

    def skip_value(self, value_type: int) -> None:
        if value_type in GGUF_SCALARS:
            self.handle.seek(struct.calcsize(GGUF_SCALARS[value_type]), os.SEEK_CUR)
        elif value_type == 8:
            self.skip_string()
        elif value_type == 9:
            item_type = self.unpack("<I")
            count = self.unpack("<Q")
            if count > GGUF_MAX_ITEMS:
                raise ValueError("GGUF array is too large.")
            if item_type in GGUF_SCALARS:
                size = struct.calcsize(GGUF_SCALARS[item_type]) * count
                self.handle.seek(size, os.SEEK_CUR)
            else:
                for _ in range(count):
                    self.skip_value(item_type)
        else:
            raise ValueError(f"Unknown GGUF value type {value_type}.")


def gguf_parameter_count(path: Path) -> int | None:
    """Sum tensor element counts from a GGUF header without reading weights."""
    try:
        with path.open("rb") as handle:
            reader = _GgufReader(handle)
            if reader.read(4) != b"GGUF":
                return None
            version = reader.unpack("<I")
            count_format = "<I" if version == 1 else "<Q"
            tensor_count = reader.unpack(count_format)
            kv_count = reader.unpack(count_format)
            if tensor_count > GGUF_MAX_ITEMS or kv_count > GGUF_MAX_ITEMS:
                return None
            for _ in range(kv_count):
                reader.skip_string()
                reader.skip_value(reader.unpack("<I"))
            total = 0
            for _ in range(tensor_count):
                reader.skip_string()
                dimensions = reader.unpack("<I")
                if dimensions > 8:
                    return None
                count = 1
                for _ in range(dimensions):
                    count *= reader.unpack("<Q")
                reader.unpack("<I")
                reader.unpack("<Q")
                total += count
            return total or None
    except (OSError, ValueError, struct.error):
        return None


def _gguf_candidates(ggufs: list[Path]) -> list[Path]:
    """Pick one quantization (all of its shards) to represent the model's size."""
    weights = [path for path in ggufs if "mmproj" not in path.name.lower()] or ggufs
    first = sorted(weights, key=lambda path: path.as_posix())[0]
    shard = GGUF_SHARD_PATTERN.match(first.name)
    if not shard:
        return [first]
    prefix = shard.group(1)
    return [
        path
        for path in weights
        if path.parent == first.parent
        and (match := GGUF_SHARD_PATTERN.match(path.name))
        and match.group(1) == prefix
    ]


def repository_parameter_count(root: Path, files: Iterable[Path]) -> int | None:
    safetensors: list[Path] = []
    ggufs: list[Path] = []
    for path in files:
        suffix = path.suffix.lower()
        if suffix == ".safetensors":
            safetensors.append(path)
        elif suffix == ".gguf":
            ggufs.append(path)
    if safetensors:
        # Prefer root-level Transformers shards; Mistral-style repos also ship
        # consolidated.safetensors with the same weights, which would double count.
        selected = [path for path in safetensors if path.parent == root] or safetensors
        standard = [path for path in selected if not path.name.startswith("consolidated")]
        selected = standard or selected
        counts = [safetensors_parameter_count(path) for path in selected]
        if counts and all(count is not None for count in counts):
            return sum(counts)
    if ggufs:
        counts = [gguf_parameter_count(path) for path in _gguf_candidates(ggufs)]
        if counts and all(count is not None for count in counts):
            return sum(counts)
    return None


def _repository_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current, directories, names in os.walk(root):
        directories[:] = [
            name
            for name in directories
            if name not in {".cache", "__pycache__"} and not name.startswith(".")
        ]
        for name in names:
            path = Path(current) / name
            if not path.is_symlink():
                files.append(path)
    return files


def repository_facts(root: Path) -> dict[str, Any]:
    files = _repository_files(root)
    return {
        "parameter_count": repository_parameter_count(root, files),
        "formats": model_formats(path.name for path in files),
    }


def readme_metadata(path: Path) -> dict[str, Any]:
    """Read model card YAML frontmatter (pipeline_tag, license, tags) from a local README."""
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 1_000_000:
            return {}
        from huggingface_hub import metadata_load

        metadata = metadata_load(path) or {}
    except Exception:
        return {}
    result: dict[str, Any] = {}
    for key in ("pipeline_tag", "library_name", "license"):
        value = metadata.get(key)
        if isinstance(value, list):
            value = next((item for item in value if isinstance(item, str)), None)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()[:100]
    tags = metadata.get("tags")
    if isinstance(tags, list):
        result["tags"] = [tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()][:50]
    return result


def parse_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


class LocalModelIndexer:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database

    def _candidates(self) -> Iterable[Path]:
        storage = self.settings.model_storage
        if not storage.exists():
            return
        for root, directories, files in os.walk(storage):
            path = Path(root)
            relative = path.relative_to(storage)
            directories[:] = [
                name
                for name in directories
                if name not in {".cache", "__pycache__"} and not name.startswith(".")
            ]
            if len(relative.parts) > 3:
                directories[:] = []
                continue
            file_set = set(files)
            has_manifest = ".hugginghack.json" in file_set
            has_config = bool(file_set & CONFIG_FILES)
            has_weight = any(Path(name).suffix.lower() in WEIGHT_EXTENSIONS for name in files)
            if has_manifest or has_config or has_weight:
                if has_manifest:
                    manifest = parse_json(path / ".hugginghack.json")
                    if manifest.get("status") not in {None, "complete"}:
                        directories[:] = []
                        continue
                yield path
                directories[:] = []

    def index_path(self, path: Path) -> dict[str, Any]:
        resolved = path.resolve()
        if self.settings.model_storage != resolved and self.settings.model_storage not in resolved.parents:
            raise ValueError("Model path escapes configured storage")
        relative = resolved.relative_to(self.settings.model_storage).as_posix()
        manifest = parse_json(resolved / ".hugginghack.json")
        config = parse_json(resolved / "config.json")
        repo_id = manifest.get("repo_id") or (
            relative if "/" in relative else f"local/{relative}"
        )
        if manifest.get("source") == "user-upload" and not upload_is_registered(
            self.database, repo_id, manifest
        ):
            raise PermissionError(
                "User-uploaded repositories require matching ownership metadata."
            )
        size, file_count, latest = directory_stats(resolved)
        facts = repository_facts(resolved)
        card = readme_metadata(resolved / "README.md")
        tags = manifest.get("tags") or card.get("tags") or []
        record = {
            "repo_id": repo_id,
            "relative_path": relative,
            "size_bytes": size,
            "file_count": file_count,
            "modified_at": datetime.fromtimestamp(latest, timezone.utc).isoformat(),
            "downloaded_at": manifest.get("downloaded_at"),
            "revision": manifest.get("revision"),
            "sha": manifest.get("sha"),
            # S3 syncs used to store config.model_type as the task; a real task from
            # the model card wins over that fallback.
            "pipeline_tag": (
                (
                    manifest.get("pipeline_tag")
                    if manifest.get("pipeline_tag") not in {None, "", config.get("model_type")}
                    else None
                )
                or card.get("pipeline_tag")
                or manifest.get("pipeline_tag")
                or config.get("model_type")
            ),
            "library_name": manifest.get("library_name") or card.get("library_name"),
            "license": manifest.get("license") or card.get("license"),
            "tags_json": json.dumps(tags),
            "config_json": json.dumps(
                {
                    "architectures": config.get("architectures"),
                    "model_type": config.get("model_type"),
                    "torch_dtype": config.get("torch_dtype"),
                    "vocab_size": config.get("vocab_size"),
                }
            ),
            "source_url": manifest.get("source_url"),
            "managed": 1 if manifest else 0,
            "storage_backend": manifest.get("storage_backend") or "filesystem",
            "storage_target": manifest_target(manifest),
            "cached": 1,
            "remote_uri": manifest.get("remote_uri"),
            "parameter_count": facts["parameter_count"],
            "formats_json": json.dumps(facts["formats"]),
        }
        self.database.upsert_local_model(record)
        return self.database.get_local_model(repo_id)

    def index_remote(self, model: dict[str, Any]) -> dict[str, Any] | None:
        record = {
            "repo_id": model["repo_id"],
            "relative_path": model.get("relative_path") or model["repo_id"],
            "size_bytes": int(model.get("size_bytes") or 0),
            "file_count": int(model.get("file_count") or 0),
            "modified_at": model.get("modified_at") or utc_now(),
            "downloaded_at": model.get("downloaded_at"),
            "revision": model.get("revision"),
            "sha": model.get("sha"),
            "pipeline_tag": model.get("pipeline_tag"),
            "library_name": model.get("library_name"),
            "license": model.get("license"),
            "tags_json": json.dumps(model.get("tags") or []),
            "config_json": json.dumps(model.get("config") or {}),
            "source_url": model.get("source_url"),
            "managed": int(bool(model.get("managed", True))),
            "storage_backend": "s3",
            "storage_target": model.get("storage_target") or LEGACY_S3_TARGET_ID,
            "cached": int(bool(model.get("cached"))),
            "remote_uri": model.get("remote_uri"),
            "parameter_count": model.get("parameter_count"),
            "formats_json": json.dumps(model.get("formats") or []),
        }
        self.database.upsert_local_model(record)
        return self.database.get_local_model(model["repo_id"])

    def scan(self) -> dict[str, Any]:
        indexed = []
        paths: set[str] = set()
        for candidate in self._candidates() or []:
            try:
                model = self.index_path(candidate)
            except (OSError, ValueError):
                continue
            if model:
                indexed.append(model)
                paths.add(model["relative_path"])
        self.database.prune_local_models(paths)
        return {"count": len(indexed), "models": indexed, "scanned_at": utc_now()}

    def files_for_model(self, repo_id: str, limit: int = 500) -> dict[str, Any] | None:
        model = self.database.get_local_model(repo_id)
        if not model:
            return None
        root = (self.settings.model_storage / model["relative_path"]).resolve()
        if self.settings.model_storage != root and self.settings.model_storage not in root.parents:
            raise ValueError("Indexed path escapes configured storage")
        files = []
        unsafe_count = 0
        for current, directories, names in os.walk(root):
            directories[:] = [name for name in directories if name != ".cache"]
            for name in names:
                path = Path(current) / name
                relative = path.relative_to(root).as_posix()
                try:
                    stat = path.stat()
                except (OSError, PermissionError):
                    continue
                suffix = path.suffix.lower()
                unsafe = suffix in UNSAFE_EXTENSIONS
                unsafe_count += int(unsafe)
                files.append(
                    {
                        "path": relative,
                        "size": stat.st_size,
                        "modified_at": datetime.fromtimestamp(
                            stat.st_mtime, timezone.utc
                        ).isoformat(),
                        "unsafe_serialization": unsafe,
                    }
                )
                if len(files) >= limit:
                    break
            if len(files) >= limit:
                break
        files.sort(key=lambda item: (-item["size"], item["path"]))
        return {
            "model": model,
            "files": files,
            "unsafe_file_count": unsafe_count,
            "truncated": len(files) >= limit,
        }
