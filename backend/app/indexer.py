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
# Weight number formats. BF16, FP8 and NVFP4 are the ones the model filters offer.
CONFIG_DTYPES = {"bfloat16": "bf16", "float16": "fp16", "float32": "fp32"}
HEADER_DTYPES = {"BF16": "bf16", "F16": "fp16", "F32": "fp32", "F8_E4M3": "fp8", "F8_E5M2": "fp8"}
QUANTIZED_PRECISIONS = {"fp8", "nvfp4", "mxfp4", "int4", "int8"}
PACKED_PRECISIONS = {"nvfp4", "mxfp4"}
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


def safetensors_tensors(path: Path) -> list[tuple[str, str, int]] | None:
    """Name, dtype and element count of every tensor, read from the SafeTensors
    header without touching the weights."""
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
    return header_tensors(header)


def header_tensors(header: Any) -> list[tuple[str, str, int]] | None:
    """The tensors listed in a parsed SafeTensors header."""
    if not isinstance(header, dict):
        return None
    tensors = []
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
        tensors.append((name, str(tensor.get("dtype") or ""), count))
    return tensors


def _quantization_state(name: str) -> bool:
    """Scales and zero points that quantized checkpoints store next to each weight."""
    leaf = name.rsplit(".", 1)[-1]
    return "scale" in leaf or "zero_point" in leaf


def count_parameters(tensors: Iterable[tuple[str, str, int]], precision: str | None = None) -> int:
    """Parameters, not stored elements: 4-bit formats pack two values per byte, and
    their scale tensors are bookkeeping rather than weights."""
    quantized = precision in QUANTIZED_PRECISIONS
    total = 0
    for name, dtype, count in tensors:
        if quantized and _quantization_state(name):
            continue
        total += count * 2 if dtype == "U8" and precision in PACKED_PRECISIONS else count
    return total


def safetensors_parameter_count(path: Path, precision: str | None = None) -> int | None:
    tensors = safetensors_tensors(path)
    return None if tensors is None else count_parameters(tensors, precision)


def _quantization_precision(config: dict[str, Any], quant_file: dict[str, Any]) -> str | None:
    quantization = config.get("quantization_config") or (config.get("text_config") or {}).get(
        "quantization_config"
    )
    quantization = quantization if isinstance(quantization, dict) else {}
    modelopt = quant_file.get("quantization") if isinstance(quant_file.get("quantization"), dict) else {}
    if not quantization and not modelopt:
        return None
    method = str(quantization.get("quant_method") or "").lower()
    layout = str(quantization.get("format") or "").lower()
    if method in {"fp8", "mxfp4"}:
        return method
    algorithms = {
        str(value).upper()
        for value in (quantization.get("quant_algo"), modelopt.get("quant_algo"))
        if value
    }
    # ModelOpt mixed precision lists an algorithm per layer.
    layers = modelopt.get("quantized_layers")
    for layer in (layers.values() if isinstance(layers, dict) else []):
        if isinstance(layer, dict) and layer.get("quant_algo"):
            algorithms.add(str(layer["quant_algo"]).upper())
    groups = quantization.get("config_groups")
    for group in (groups.values() if isinstance(groups, dict) else []):
        if not isinstance(group, dict):
            continue
        weights = group.get("weights") if isinstance(group.get("weights"), dict) else group
        bits, kind = weights.get("num_bits"), weights.get("type")
        if kind == "float" and bits in {4, 8}:
            algorithms.add("NVFP4" if bits == 4 else "FP8")
        elif kind == "int" and bits in {4, 8}:
            algorithms.add(f"INT{bits}")
    if "nvfp4" in layout or any("NVFP4" in name or name == "FP4" for name in algorithms):
        return "nvfp4"
    if "float-quantized" in layout or any(name.startswith("FP8") for name in algorithms):
        return "fp8"
    if method in {"gptq", "awq"}:
        return "int8" if quantization.get("bits") == 8 else "int4"
    if method == "bitsandbytes":
        return "int4" if quantization.get("load_in_4bit") else "int8"
    if any("INT4" in name or "W4A16" in name for name in algorithms):
        return "int4"
    if any("INT8" in name or "W8A8" in name for name in algorithms):
        return "int8"
    return None


def model_precision(
    config: dict[str, Any],
    quant_file: dict[str, Any],
    tensors: Iterable[tuple[str, str, int]] = (),
) -> str | None:
    """The weights' number format. Quantization wins over `torch_dtype`, which in
    FP8 and NVFP4 checkpoints only describes the layers left unquantized."""
    quantized = _quantization_precision(config, quant_file)
    if quantized:
        return quantized
    text_config = config.get("text_config") if isinstance(config.get("text_config"), dict) else {}
    for source in (config, text_config):
        dtype = source.get("torch_dtype") or source.get("dtype")
        if isinstance(dtype, str) and dtype.removeprefix("torch.") in CONFIG_DTYPES:
            return CONFIG_DTYPES[dtype.removeprefix("torch.")]
    elements: dict[str, int] = {}
    for _, dtype, count in tensors:
        elements[dtype] = elements.get(dtype, 0) + count
    if elements:
        return HEADER_DTYPES.get(max(elements, key=elements.__getitem__))
    return None


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


def select_safetensors(paths: Iterable[str]) -> list[str]:
    """The SafeTensors shards that hold the model, as repository-relative paths.
    Root-level Transformers shards win; Mistral-style repos also ship
    consolidated.safetensors with the same weights, which would double count."""
    safetensors = [path for path in paths if path.lower().endswith(".safetensors")]
    selected = [path for path in safetensors if "/" not in path] or safetensors
    standard = [path for path in selected if not path.rsplit("/", 1)[-1].startswith("consolidated")]
    return standard or selected


def _weight_shards(root: Path, files: Iterable[Path]) -> tuple[list[Path], list[Path]]:
    files = list(files)
    relative = {path.relative_to(root).as_posix(): path for path in files}
    ggufs = [path for path in files if path.suffix.lower() == ".gguf"]
    return [relative[path] for path in select_safetensors(relative)], ggufs


def shards_parameter_count(
    shards: list[list[tuple[str, str, int]] | None], precision: str | None = None
) -> int | None:
    """A shard may hold only scales or buffers, so zero is a valid count; only an
    unreadable header makes the total unknown."""
    if not shards or any(tensors is None for tensors in shards):
        return None
    return sum(count_parameters(tensors or [], precision) for tensors in shards) or None


def repository_parameter_count(
    root: Path, files: Iterable[Path], precision: str | None = None
) -> int | None:
    safetensors, ggufs = _weight_shards(root, files)
    if safetensors:
        count = shards_parameter_count([safetensors_tensors(path) for path in safetensors], precision)
        if count is not None:
            return count
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


def repository_facts(root: Path, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    files = _repository_files(root)
    safetensors, ggufs = _weight_shards(root, files)
    gguf_counts = [gguf_parameter_count(path) for path in _gguf_candidates(ggufs)] if ggufs else []
    return classify(
        [path.relative_to(root).as_posix() for path in files],
        parse_json(root / "config.json"),
        parse_json(root / "hf_quant_config.json"),
        readme_metadata(root / "README.md"),
        [safetensors_tensors(path) for path in safetensors],
        manifest,
        sum(gguf_counts) if gguf_counts and all(count is not None for count in gguf_counts) else None,
    )


README_MAX_BYTES = 1_000_000


def readme_metadata(path: Path) -> dict[str, Any]:
    """Read model card YAML frontmatter (pipeline_tag, license, tags) from a local README."""
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > README_MAX_BYTES:
            return {}
        return card_metadata(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def card_metadata(text: str) -> dict[str, Any]:
    """Model card YAML frontmatter (pipeline_tag, library_name, license, tags)."""
    try:
        import yaml
        from huggingface_hub.repocard import REGEX_YAML_BLOCK

        # The same parsing as huggingface_hub's metadata_load, on text.
        match = REGEX_YAML_BLOCK.search(text)
        metadata = yaml.safe_load(match.group(2)) if match else None
    except Exception:
        return {}
    if not isinstance(metadata, dict):
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


def classify(
    paths: Iterable[str],
    config: dict[str, Any],
    quant_file: dict[str, Any],
    card: dict[str, Any],
    shards: list[list[tuple[str, str, int]] | None],
    manifest: dict[str, Any] | None = None,
    gguf_count: int | None = None,
) -> dict[str, Any]:
    """How the library lists a repository, from what its files say. The indexer and
    the upload preview both call this, so a preview is what the listing will be.
    `shards` are the tensors of `select_safetensors(paths)`, in any order."""
    manifest = manifest or {}
    paths = list(paths)
    tensors = [tensor for shard in shards for tensor in (shard or [])]
    precision = model_precision(config, quant_file, tensors)
    parameter_count = shards_parameter_count(shards, precision) if shards else None
    if parameter_count is None:
        parameter_count = gguf_count
    return {
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
        "tags": manifest.get("tags") or card.get("tags") or [],
        "precision": precision,
        "parameter_count": parameter_count,
        "formats": model_formats(paths),
        "architectures": config.get("architectures"),
        "model_type": config.get("model_type"),
        "torch_dtype": config.get("torch_dtype"),
        "vocab_size": config.get("vocab_size"),
    }


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
        facts = repository_facts(resolved, manifest)
        record = {
            "repo_id": repo_id,
            "relative_path": relative,
            "size_bytes": size,
            "file_count": file_count,
            "modified_at": datetime.fromtimestamp(latest, timezone.utc).isoformat(),
            "downloaded_at": manifest.get("downloaded_at"),
            "revision": manifest.get("revision"),
            "sha": manifest.get("sha"),
            "pipeline_tag": facts["pipeline_tag"],
            "library_name": facts["library_name"],
            "license": facts["license"],
            "tags_json": json.dumps(facts["tags"]),
            "config_json": json.dumps(
                {
                    key: facts[key]
                    for key in ("architectures", "model_type", "torch_dtype", "vocab_size", "precision")
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
            "config_json": json.dumps(
                {**(model.get("config") or {}), "precision": model.get("precision")}
            ),
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
