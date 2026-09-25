"""Offline model catalog built from the local library index.

The Models tab reads everything here instead of the Hugging Face Hub, so the
application keeps working on an air-gapped network. Callers must pass models that
were already filtered for the requesting user's visibility.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from .config import Settings
from .indexer import UNSAFE_EXTENSIONS
from .reads import reads
from .storage import FilesystemModelStorage, StorageRegistry


MODEL_CARD_MAX_BYTES = 120_000
GGUF_RANGE_PATTERN = re.compile(r"^bytes=(\d+)-(\d+)$")
GGUF_MAX_HEADER_BYTES = 50_000_000
GGUF_MAX_RANGE_BYTES = 2_100_000
ASSET_MAX_BYTES = 10_000_000
ASSET_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# GPUs a model can be tagged as tested on. Ids are stored; labels are shown.
HARDWARE = {
    "l40": "L40",
    "a100": "A100",
    "rtx-pro-6000": "RTX PRO 6000",
    "b300": "B300",
}
PRECISIONS = ("bf16", "fp8", "nvfp4")
TASK_PATTERN = re.compile(r"^[a-z0-9-]{1,60}$")
PARAMETER_PATTERN = re.compile(r"^(min|max):(\d+(?:\.\d+)?)([KMBT]?)$", re.IGNORECASE)
PARAMETER_UNITS = {"": 1, "K": 10**3, "M": 10**6, "B": 10**9, "T": 10**12}
# A size in the repository name, as in Qwen3-8B-FP8 or Nemotron-3-Nano-30B-A3B. The
# letter guard skips active-parameter counts (A3B) and expert shapes (8x7B).
NAME_SIZE_PATTERN = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)([MBT])(?![A-Za-z0-9])", re.IGNORECASE)


def validate_gguf_filename(filename: str) -> str:
    value = filename.strip().replace("\\", "/")
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 500
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix.lower() != ".gguf"
    ):
        raise ValueError("GGUF filename must be a safe repository-relative .gguf path.")
    return path.as_posix()


def parse_gguf_range(value: str | None) -> tuple[int, int]:
    match = GGUF_RANGE_PATTERN.fullmatch((value or "").strip())
    if not match:
        raise ValueError("A single bounded byte range is required.")
    start, end = (int(part) for part in match.groups())
    if end < start:
        raise ValueError("The GGUF byte range is invalid.")
    if end - start + 1 > GGUF_MAX_RANGE_BYTES:
        raise ValueError("GGUF range requests are limited to 2.1 MB.")
    if end >= GGUF_MAX_HEADER_BYTES:
        raise ValueError("GGUF inspection is limited to the first 50 MB of a file.")
    return start, end


def model_task(model: dict[str, Any]) -> str | None:
    """Return the pipeline task, ignoring the indexer's config.model_type fallback."""
    task = model.get("pipeline_tag")
    config = model.get("config") or {}
    if not task or task == config.get("model_type"):
        return None
    return task


def compatible_apps(model: dict[str, Any]) -> list[str]:
    formats = set(model.get("formats") or [])
    config = model.get("config") or {}
    apps: list[str] = []
    if "safetensors" in formats and (config.get("architectures") or config.get("model_type")):
        apps.append("vllm")
    if "gguf" in formats:
        apps.extend(["llama.cpp", "ollama", "lm-studio"])
    return apps


def catalog_item(
    model: dict[str, Any], saved_ids: set[str], hardware: list[str] | None = None
) -> dict[str, Any]:
    repo_id = model["repo_id"]
    return {
        "id": repo_id,
        "author": repo_id.split("/", 1)[0] if "/" in repo_id else None,
        "pipeline_tag": model_task(model),
        "library_name": model.get("library_name"),
        "tags": model.get("tags") or [],
        "license": model.get("license"),
        "parameter_count": model.get("parameter_count"),
        "parameters_corrected": "parameter_count" in (model.get("listing_overrides") or {}),
        "precision": (model.get("config") or {}).get("precision"),
        "hardware": [item for item in HARDWARE if item in (hardware or [])],
        "formats": model.get("formats") or [],
        "apps": compatible_apps(model),
        "size_bytes": int(model.get("size_bytes") or 0),
        "file_count": int(model.get("file_count") or 0),
        "last_modified": model.get("modified_at"),
        "downloaded_at": model.get("downloaded_at"),
        "revision": model.get("revision"),
        "sha": model.get("sha"),
        "managed": bool(model.get("managed")),
        "storage_backend": model.get("storage_backend") or "filesystem",
        "storage_target": model.get("storage_target") or "local",
        "cached": bool(model.get("cached")),
        "saved": repo_id in saved_ids,
    }


def parse_parameter_range(value: str) -> tuple[int | None, int | None]:
    minimum: int | None = None
    maximum: int | None = None
    for part in (item.strip() for item in value.split(",") if item.strip()):
        match = PARAMETER_PATTERN.fullmatch(part)
        if not match:
            raise ValueError("Parameter filters use the form min:7B,max:32B.")
        bound, number, unit = match.groups()
        amount = int(float(number) * PARAMETER_UNITS[unit.upper()])
        if bound.lower() == "min":
            minimum = amount
        else:
            maximum = amount
    return minimum, maximum


def parse_choices(value: str, allowed: Any, name: str) -> set[str]:
    """A comma-separated filter; any one of the values matches."""
    chosen = {item.strip() for item in value.split(",") if item.strip()}
    invalid = sorted(item for item in chosen if not (allowed(item) if callable(allowed) else item in allowed))
    if invalid:
        raise ValueError(f"Unknown {name}: {', '.join(invalid)}.")
    return chosen


def nominal_parameters(item: dict[str, Any]) -> int | None:
    """The size a model is known by. Counted parameters run a little over the name
    (a "7B" model has 7.6B, a "32B" one 32.8B), so the name decides when it agrees
    with the count; otherwise the count does."""
    count = item.get("parameter_count")
    if item.get("parameters_corrected"):
        # Someone set the size by hand; that is the size, whatever the name says.
        return count
    match = NAME_SIZE_PATTERN.search(item["id"].rsplit("/", 1)[-1])
    if match:
        named = int(float(match.group(1)) * PARAMETER_UNITS[match.group(2).upper()])
        if not count or 0.5 <= named / count <= 2:
            return named
    return count


def _matches(
    item: dict[str, Any],
    search: str,
    tasks: set[str],
    precisions: set[str],
    hardware: set[str],
    parameter_range: tuple[int | None, int | None],
    owner: str = "",
) -> bool:
    if owner and (item.get("author") or "").lower() != owner.lower():
        return False
    if search:
        haystack = " ".join(
            [item["id"], item.get("pipeline_tag") or "", item.get("library_name") or ""]
            + list(item.get("tags") or [])
        ).lower()
        if not all(term in haystack for term in search.lower().split()):
            return False
    if tasks and item.get("pipeline_tag") not in tasks:
        return False
    if precisions and item.get("precision") not in precisions:
        return False
    if hardware and not hardware.intersection(item["hardware"]):
        return False
    minimum, maximum = parameter_range
    if minimum is not None or maximum is not None:
        size = nominal_parameters(item)
        if not size:
            return False
        # Both ends are inclusive: an 8B model is in "up to 8B" and in "8B and up".
        if minimum is not None and size < minimum * 0.95:
            return False
        if maximum is not None and size > maximum * 1.05:
            return False
    return True


def _sort_key(sort: str):
    if sort == "name":
        return lambda item: item["id"].lower()
    if sort == "size":
        return lambda item: -item["size_bytes"]
    if sort == "parameters":
        return lambda item: -(item.get("parameter_count") or 0)
    return lambda item: item.get("last_modified") or ""


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        if value:
            counts[value] = counts.get(value, 0) + 1
    return counts


def catalog_facets(items: list[dict[str, Any]]) -> dict[str, Any]:
    """How many models each filter value would match, across the whole visible library."""
    hardware = _counts(tag for item in items for tag in item["hardware"])
    return {
        "tasks": _counts(item.get("pipeline_tag") for item in items),
        "precision": _counts(item.get("precision") for item in items),
        "hardware": [[key, label, hardware.get(key, 0)] for key, label in HARDWARE.items()],
    }


def search_catalog(
    models: list[dict[str, Any]],
    saved_ids: set[str],
    *,
    search: str = "",
    sort: str = "updated",
    task: str = "",
    precision: str = "",
    hardware: str = "",
    parameters: str = "",
    owner: str = "",
    hardware_tags: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    parameter_range = parse_parameter_range(parameters)
    tasks = parse_choices(task, TASK_PATTERN.fullmatch, "task")
    precisions = parse_choices(precision, PRECISIONS, "precision")
    chosen_hardware = parse_choices(hardware, HARDWARE, "hardware")
    tags = hardware_tags or {}
    items = [catalog_item(model, saved_ids, tags.get(model["repo_id"])) for model in models]
    matched = [
        item
        for item in items
        if _matches(item, search.strip(), tasks, precisions, chosen_hardware, parameter_range, owner)
    ]
    matched.sort(key=_sort_key(sort), reverse=sort == "updated")
    return {
        "items": matched,
        "count": len(matched),
        "total": len(items),
        "total_bytes": sum(item["size_bytes"] for item in items),
        "facets": catalog_facets(items),
    }


class LocalCatalog:
    def __init__(
        self, settings: Settings, model_storage: FilesystemModelStorage | StorageRegistry
    ):
        self.settings = settings
        self.storages = StorageRegistry.wrap(model_storage, settings)

    def _local_root(self, model: dict[str, Any]) -> Path | None:
        if not model.get("cached"):
            return None
        storage = self.settings.model_storage
        root = (storage / model["relative_path"]).resolve()
        if storage != root and storage not in root.parents:
            raise ValueError("Indexed path escapes configured storage.")
        return root if root.is_dir() else None

    def _local_file(self, model: dict[str, Any], relative: PurePosixPath) -> Path | None:
        root = self._local_root(model)
        if root is None:
            return None
        target = root.joinpath(*relative.parts)
        if target.is_symlink() or not target.is_file():
            raise FileNotFoundError("File not found in this model.")
        resolved = target.resolve()
        if root not in resolved.parents:
            raise ValueError("File path escapes the model repository.")
        return resolved

    def read_bytes(
        self,
        model: dict[str, Any],
        relative_path: str,
        start: int = 0,
        end: int | None = None,
        max_bytes: int = 1_000_000,
    ) -> tuple[bytes, int]:
        """Read a bounded range of a model file from the local cache or S3."""
        with reads.hold(model["repo_id"]):
            return self._read_bytes(model, relative_path, start, end, max_bytes)

    def _read_bytes(
        self,
        model: dict[str, Any],
        relative_path: str,
        start: int,
        end: int | None,
        max_bytes: int,
    ) -> tuple[bytes, int]:
        relative = _safe_relative(relative_path)
        local = self._local_file(model, relative)
        if local is not None:
            total = local.stat().st_size
            if start >= total:
                return b"", total
            last = min(total - 1, end if end is not None else total - 1)
            length = last - start + 1
            if length > max_bytes:
                raise ValueError("The requested file is too large to read.")
            with local.open("rb") as handle:
                handle.seek(start)
                return handle.read(length), total
        if model.get("storage_backend") == "s3":
            result = self.storages.for_model(model).read_repository_file(
                model["repo_id"], relative.as_posix(), start, end, max_bytes
            )
            if result is not None:
                return result
        raise FileNotFoundError("File not found in this model.")

    def model_card(self, model: dict[str, Any], files: list[dict[str, Any]]) -> str | None:
        readme = next((file for file in files if file["path"].lower() == "readme.md"), None)
        if not readme:
            return None
        try:
            payload, _ = self.read_bytes(
                model, readme["path"], 0, MODEL_CARD_MAX_BYTES - 1, MODEL_CARD_MAX_BYTES
            )
        except (FileNotFoundError, ValueError, OSError):
            return None
        return payload.decode("utf-8", errors="replace")

    def details(
        self,
        model: dict[str, Any],
        listing: dict[str, Any],
        saved_ids: set[str],
    ) -> dict[str, Any]:
        files = [
            {"path": file["path"], "size": file["size"]}
            for file in listing.get("files") or []
            if not any(part.startswith(".") for part in PurePosixPath(file["path"]).parts)
        ]
        files.sort(key=lambda file: file["path"])
        result = catalog_item(model, saved_ids)
        result.update(
            {
                "files": files,
                "total_bytes": sum(file["size"] for file in files),
                "truncated": bool(listing.get("truncated")),
                "unsafe_file_count": sum(
                    1
                    for file in files
                    if PurePosixPath(file["path"]).suffix.lower() in UNSAFE_EXTENSIONS
                ),
                "local_path": f"{self.settings.model_storage.as_posix()}/{model['relative_path']}",
                "remote_uri": model.get("remote_uri"),
                "source_url": model.get("source_url"),
                "model_card": self.model_card(model, files),
            }
        )
        return result

    def gguf_range(
        self, model: dict[str, Any], filename: str, range_header: str | None
    ) -> dict[str, Any]:
        path = validate_gguf_filename(filename)
        start, end = parse_gguf_range(range_header)
        content, total = self.read_bytes(model, path, start, end, end - start + 1)
        if start >= total:
            raise ValueError("The requested GGUF header range is unavailable.")
        last = start + len(content) - 1
        return {
            "content": content,
            "status_code": 206,
            "headers": {
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{last}/{total}",
                "Cache-Control": "private, max-age=3600",
                "Vary": "Range",
            },
        }

    def asset(self, model: dict[str, Any], relative_path: str) -> tuple[bytes, str]:
        """Serve a raster image referenced by a model card.

        Only fixed raster types are served, with a server-chosen content type, so
        uploaded SVG or HTML files can never execute script on this origin.
        """
        relative = _safe_relative(relative_path)
        content_type = ASSET_CONTENT_TYPES.get(relative.suffix.lower())
        if not content_type:
            raise PermissionError("Only PNG, JPEG, GIF, and WebP model card images are served.")
        content, total = self.read_bytes(model, relative.as_posix(), 0, None, ASSET_MAX_BYTES)
        if len(content) != total:
            raise ValueError("The model card image could not be read completely.")
        return content, content_type


def _safe_relative(value: str) -> PurePosixPath:
    cleaned = value.strip().replace("\\", "/")
    path = PurePosixPath(cleaned)
    if (
        not cleaned
        or len(cleaned) > 500
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(part.startswith(".") for part in path.parts)
    ):
        raise ValueError("File path must be a safe repository-relative path.")
    return path

