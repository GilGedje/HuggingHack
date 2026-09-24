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
from .hub_service import parse_gguf_range, validate_gguf_filename
from .indexer import UNSAFE_EXTENSIONS
from .storage import FilesystemModelStorage


MODEL_CARD_MAX_BYTES = 120_000
ASSET_MAX_BYTES = 10_000_000
ASSET_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
SORT_OPTIONS = ("updated", "name", "size", "parameters")
FORMAT_LABELS = {
    "safetensors": "SafeTensors",
    "gguf": "GGUF",
    "pytorch": "PyTorch (pickle)",
    "onnx": "ONNX",
    "tensorflow": "TensorFlow",
    "flax": "Flax",
}
LIBRARY_LABELS = {
    "transformers": "Transformers",
    "diffusers": "Diffusers",
    "sentence-transformers": "Sentence Transformers",
    "peft": "PEFT",
    "timm": "timm",
    "mlx": "MLX",
}
APP_LABELS = {
    "vllm": "vLLM",
    "llama.cpp": "llama.cpp",
    "ollama": "Ollama",
    "lm-studio": "LM Studio",
}
PARAMETER_PATTERN = re.compile(r"^(min|max):(\d+(?:\.\d+)?)([KMBT]?)$", re.IGNORECASE)
PARAMETER_UNITS = {"": 1, "K": 10**3, "M": 10**6, "B": 10**9, "T": 10**12}


def _label(value: str) -> str:
    return " ".join(part.capitalize() for part in value.replace("_", "-").split("-"))


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


def catalog_item(model: dict[str, Any], saved_ids: set[str]) -> dict[str, Any]:
    repo_id = model["repo_id"]
    return {
        "id": repo_id,
        "author": repo_id.split("/", 1)[0] if "/" in repo_id else None,
        "pipeline_tag": model_task(model),
        "library_name": model.get("library_name"),
        "tags": model.get("tags") or [],
        "license": model.get("license"),
        "parameter_count": model.get("parameter_count"),
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


def _matches(
    item: dict[str, Any],
    search: str,
    task: str,
    library: str,
    app: str,
    parameter_range: tuple[int | None, int | None],
) -> bool:
    if search:
        haystack = " ".join(
            [item["id"], item.get("pipeline_tag") or "", item.get("library_name") or ""]
            + list(item.get("tags") or [])
        ).lower()
        if not all(term in haystack for term in search.lower().split()):
            return False
    if task and item.get("pipeline_tag") != task:
        return False
    if library and library not in item["formats"] and item.get("library_name") != library:
        return False
    if app and app not in item["apps"]:
        return False
    minimum, maximum = parameter_range
    if minimum is not None or maximum is not None:
        count = item.get("parameter_count")
        if not count:
            return False
        if minimum is not None and count < minimum:
            return False
        if maximum is not None and count >= maximum:
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


def catalog_facets(items: list[dict[str, Any]]) -> dict[str, list[list[str]]]:
    tasks = sorted({item["pipeline_tag"] for item in items if item.get("pipeline_tag")})
    formats = sorted({value for item in items for value in item["formats"]})
    libraries = sorted(
        {
            item["library_name"]
            for item in items
            if item.get("library_name") and item["library_name"] not in formats
        }
    )
    apps = [app for app in APP_LABELS if any(app in item["apps"] for item in items)]
    return {
        "tasks": [[task, _label(task)] for task in tasks],
        "libraries": [[value, FORMAT_LABELS.get(value, _label(value))] for value in formats]
        + [[value, LIBRARY_LABELS.get(value, _label(value))] for value in libraries],
        "apps": [[app, APP_LABELS[app]] for app in apps],
    }


def search_catalog(
    models: list[dict[str, Any]],
    saved_ids: set[str],
    search: str = "",
    sort: str = "updated",
    task: str = "",
    library: str = "",
    app: str = "",
    parameters: str = "",
) -> dict[str, Any]:
    parameter_range = parse_parameter_range(parameters)
    items = [catalog_item(model, saved_ids) for model in models]
    matched = [
        item
        for item in items
        if _matches(item, search.strip(), task, library, app, parameter_range)
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
    def __init__(self, settings: Settings, model_storage: FilesystemModelStorage):
        self.settings = settings
        self.model_storage = model_storage

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
            result = self.model_storage.read_repository_file(
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

