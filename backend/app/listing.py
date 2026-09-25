"""How a model is listed: previewed from an upload's files before they are sent,
and corrected by the people who own it."""

import json
import re
from typing import Any

from .indexer import (
    CONFIG_DTYPES,
    HEADER_DTYPES,
    QUANTIZED_PRECISIONS,
    card_metadata,
    classify,
    header_tensors,
    select_safetensors,
)

# Every number format the indexer can report, so a detected value can always be kept.
PRECISIONS = sorted(set(CONFIG_DTYPES.values()) | set(HEADER_DTYPES.values()) | QUANTIZED_PRECISIONS)
TASK_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
MAX_PARAMETERS = 100_000_000_000_000
MAX_TAGS = 50

# Limits for a preview request: the text files that describe a model and the
# headers of its weight shards, never the weights themselves.
PREVIEW_MAX_PATHS = 20_000
PREVIEW_MAX_SHARDS = 1_000
PREVIEW_MAX_HEADER_CHARS = 16_000_000
PREVIEW_MAX_HEADERS_TOTAL = 64_000_000
PREVIEW_MAX_TEXT_CHARS = 1_000_000


def _text(value: Any, field: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text.")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{field} is longer than {limit} characters.")
    return value or None


def validate_overrides(raw: Any) -> dict[str, Any]:
    """Corrections to a listing. A field left out or set to null keeps what the
    files say."""
    if not isinstance(raw, dict):
        raise ValueError("Listing corrections must be an object.")
    unknown = sorted(set(raw) - {"pipeline_tag", "precision", "parameter_count", "library_name", "license", "tags"})
    if unknown:
        raise ValueError(f"Unknown listing fields: {', '.join(unknown)}.")
    result: dict[str, Any] = {}
    task = _text(raw.get("pipeline_tag"), "Task", 60)
    if task is not None:
        if not TASK_PATTERN.fullmatch(task):
            raise ValueError("Task uses lowercase letters, digits, and dashes, like text-generation.")
        result["pipeline_tag"] = task
    precision = raw.get("precision")
    if precision is not None:
        if precision not in PRECISIONS:
            raise ValueError(f"Precision is one of {', '.join(PRECISIONS)}.")
        result["precision"] = precision
    count = raw.get("parameter_count")
    if count is not None:
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_PARAMETERS:
            raise ValueError("Parameters must be a whole number above zero.")
        result["parameter_count"] = count
    for field, label, limit in (("library_name", "Library", 60), ("license", "License", 100)):
        value = _text(raw.get(field), label, limit)
        if value is not None:
            result[field] = value
    tags = raw.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or len(tags) > MAX_TAGS:
            raise ValueError(f"Tags are a list of at most {MAX_TAGS}.")
        cleaned: list[str] = []
        for tag in tags:
            value = _text(tag, "A tag", 60)
            if value and value not in cleaned:
                cleaned.append(value)
        result["tags"] = cleaned
    return result


def _json_object(text: str | None) -> dict[str, Any]:
    """Like the indexer's parse_json: unreadable JSON counts as empty."""
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def preview(
    paths: list[str],
    config: str | None,
    quant_config: str | None,
    readme: str | None,
    headers: dict[str, str],
) -> dict[str, Any]:
    """The listing an upload will get, from the same classification the indexer
    runs after it arrives. `headers` maps each SafeTensors path to its header JSON."""
    if len(paths) > PREVIEW_MAX_PATHS:
        raise ValueError("Too many files to preview.")
    if len(headers) > PREVIEW_MAX_SHARDS:
        raise ValueError("Too many weight shards to preview.")
    if sum(len(text) for text in headers.values()) > PREVIEW_MAX_HEADERS_TOTAL:
        raise ValueError("The weight headers are too large to preview.")
    for text, name in ((config, "config.json"), (quant_config, "hf_quant_config.json"), (readme, "README.md")):
        if text and len(text) > PREVIEW_MAX_TEXT_CHARS:
            raise ValueError(f"{name} is too large to preview.")
    shards = []
    for path in select_safetensors(paths):
        header = headers.get(path)
        if header is None or len(header) > PREVIEW_MAX_HEADER_CHARS:
            shards.append(None)
            continue
        try:
            shards.append(header_tensors(json.loads(header)))
        except json.JSONDecodeError:
            shards.append(None)
    return classify(
        paths,
        _json_object(config),
        _json_object(quant_config),
        card_metadata(readme) if readme else {},
        shards,
    )
