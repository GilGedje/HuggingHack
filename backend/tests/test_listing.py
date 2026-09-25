import json

import app.main as main
from test_access import server  # noqa: F401  (fixture)
from test_organizations import login, org, upload  # noqa: F401  (fixtures)

README = b"""---
pipeline_tag: text-generation
license: apache-2.0
library_name: transformers
tags:
  - llama
  - fp8
---
# Tiny FP8
"""
CONFIG = {
    "model_type": "llama",
    "architectures": ["LlamaForCausalLM"],
    "torch_dtype": "bfloat16",
    "quantization_config": {"quant_method": "fp8"},
}


def shard(tensors: dict[str, tuple[str, list[int]]]) -> bytes:
    """A SafeTensors file with the given dtype and shape per tensor."""
    sizes = {"F32": 4, "BF16": 2, "F8_E4M3": 1}
    header: dict[str, object] = {"__metadata__": {"format": "pt"}}
    offset = 0
    for name, (dtype, shape) in tensors.items():
        size = sizes[dtype]
        for dimension in shape:
            size *= dimension
        header[name] = {"dtype": dtype, "shape": shape, "data_offsets": [offset, offset + size]}
        offset += size
    encoded = json.dumps(header).encode("utf-8")
    return len(encoded).to_bytes(8, "little") + encoded + b"\0" * offset


FILES = {
    "config.json": json.dumps(CONFIG).encode(),
    "README.md": README,
    "model-00001-of-00002.safetensors": shard({"a.weight": ("F8_E4M3", [16, 16]), "a.weight_scale": ("F32", [1])}),
    "model-00002-of-00002.safetensors": shard({"b.weight": ("BF16", [64])}),
    # The same weights again, which must not be counted twice.
    "consolidated.safetensors": shard({"all": ("BF16", [320])}),
}


def preview_body(repo_id: str) -> dict:
    """What the upload wizard sends: text files and each shard's header, no weights."""
    headers = {}
    for path, payload in FILES.items():
        if path.endswith(".safetensors"):
            length = int.from_bytes(payload[:8], "little")
            headers[path] = payload[8 : 8 + length].decode()
    return {
        "repo_id": repo_id,
        "paths": list(FILES),
        "config": FILES["config.json"].decode(),
        "readme": FILES["README.md"].decode(),
        "headers": headers,
    }


def put_listing(client, repo_id: str, overrides: dict):
    return client.put("/api/repos/listing", params={"repo_id": repo_id}, json={"overrides": overrides})


def test_the_preview_is_what_the_library_lists(org):  # noqa: F811
    writer, _ = login("writer")
    preview = writer.post("/api/uploads/preview", json=preview_body("writer/Tiny-1B-FP8"))
    assert preview.status_code == 200, preview.text
    detected = preview.json()["detected"]
    assert detected["precision"] == "fp8" and detected["parameter_count"] == 320  # scales left out
    assert detected["pipeline_tag"] == "text-generation" and detected["tags"] == ["llama", "fp8"]
    assert preview.json()["listed_task"] == "text-generation"

    assert writer.post("/api/uploads/repositories", json={"slug": "Tiny-1B-FP8", "visibility": "private"}).status_code == 201
    upload(writer, "writer/Tiny-1B-FP8", FILES)
    indexed = main.database.get_local_model("writer/Tiny-1B-FP8")
    for field in ("pipeline_tag", "precision", "parameter_count", "library_name", "license", "tags"):
        assert indexed["detected"][field] == detected[field], field
    assert indexed["formats"] == detected["formats"] == ["safetensors"]

    # Weights are never part of a preview request, and oversize requests are refused.
    too_many = {**preview_body("writer/x"), "paths": [f"f{index}" for index in range(20_001)]}
    assert writer.post("/api/uploads/preview", json=too_many).status_code == 400
    # An unreadable config.json counts as empty, as it does for the indexer.
    broken = writer.post("/api/uploads/preview", json={**preview_body("writer/x"), "config": "{broken"})
    assert broken.status_code == 200 and broken.json()["detected"]["model_type"] is None
    # A missing or unreadable header leaves the size unknown instead of guessing.
    partial = preview_body("writer/x")
    partial["headers"].pop("model-00002-of-00002.safetensors")
    assert writer.post("/api/uploads/preview", json=partial).json()["detected"]["parameter_count"] is None


def test_listing_corrections_follow_the_model(org):  # noqa: F811
    writer, _ = login("writer")
    assert writer.post("/api/uploads/repositories", json={"slug": "tiny", "visibility": "organization", "namespace": "Nvidia"}).status_code == 201
    # The wizard saves corrections as soon as the repository exists, before any file.
    saved = put_listing(writer, "Nvidia/tiny", {"precision": "nvfp4", "parameter_count": 8_000_000_000, "pipeline_tag": "image-text-to-text"})
    assert saved.status_code == 200, saved.text
    upload(writer, "Nvidia/tiny", FILES)

    details = writer.get("/api/library/models/Nvidia/tiny").json()
    assert details["listing"]["detected"]["precision"] == "fp8"
    assert details["listing"]["overrides"] == {
        "precision": "nvfp4", "parameter_count": 8_000_000_000, "pipeline_tag": "image-text-to-text",
    }
    assert details["listing"]["nominal_parameters"] == 8_000_000_000
    # Filters and facet counts use the corrected values.
    found = writer.get("/api/library/models", params={"precision": "nvfp4"}).json()
    assert "Nvidia/tiny" in [item["id"] for item in found["items"]]
    assert "Nvidia/tiny" not in [item["id"] for item in writer.get("/api/library/models", params={"precision": "fp8"}).json()["items"]]
    assert "Nvidia/tiny" in [item["id"] for item in writer.get("/api/library/models", params={"parameters": "min:7B,max:9B"}).json()["items"]]

    # Rescans refresh what the files say without dropping the corrections.
    main.refresh_model_index()
    model = main.database.get_local_model("Nvidia/tiny")
    assert model["config"]["precision"] == "nvfp4" and model["detected"]["precision"] == "fp8"

    # Only people who may change the model correct its listing; values are checked.
    reader, _ = login("reader")
    assert put_listing(reader, "Nvidia/tiny", {"precision": "bf16"}).status_code == 403
    outsider, _ = login("outsider")
    assert put_listing(outsider, "Nvidia/tiny", {"precision": "bf16"}).status_code == 404
    for bad in ({"precision": "fp3"}, {"parameter_count": 0}, {"parameter_count": True}, {"pipeline_tag": "Text Generation"}, {"colour": "red"}, {"tags": "llama"}):
        assert put_listing(writer, "Nvidia/tiny", bad).status_code == 400, bad

    # A rename carries the corrections; clearing one field returns it to the files.
    admin, _ = login("admin")  # renaming and deleting take the organization's admin role
    rename = admin.post("/api/repos/rename", params={"repo_id": "Nvidia/tiny"}, json={"namespace": "Nvidia", "name": "tiny-v2", "confirmation": "Nvidia/tiny"})
    assert rename.status_code == 200, rename.text
    assert main.database.listing_overrides("Nvidia/tiny-v2")["precision"] == "nvfp4"
    kept = put_listing(writer, "Nvidia/tiny-v2", {"pipeline_tag": "image-text-to-text"}).json()
    assert kept["overrides"] == {"pipeline_tag": "image-text-to-text"}
    assert main.database.get_local_model("Nvidia/tiny-v2")["config"]["precision"] == "fp8"

    deleted = admin.request("DELETE", "/api/repos", params={"repo_id": "Nvidia/tiny-v2"}, json={"confirmation": "Nvidia/tiny-v2"})
    assert deleted.status_code == 200, deleted.text
    assert main.database.listing_overrides("Nvidia/tiny-v2") == {}


def test_a_corrected_size_is_the_listed_size(org):  # noqa: F811
    writer, _ = login("writer")
    assert writer.post("/api/uploads/repositories", json={"slug": "Tiny-8B", "visibility": "private"}).status_code == 201
    saved = put_listing(writer, "writer/Tiny-8B", {"parameter_count": 8_200_000_000, "license": "mit"})
    # The name says 8B, but a size set by hand wins over the name.
    assert saved.json()["nominal_parameters"] == 8_200_000_000
    # Resuming an upload gets the saved corrections back with the repository.
    listed = {item["repo_id"]: item for item in writer.get("/api/uploads/repositories").json()["items"]}
    assert listed["writer/Tiny-8B"]["listing_overrides"] == {"parameter_count": 8_200_000_000, "license": "mit"}
    upload(writer, "writer/Tiny-8B", FILES)
    within = writer.get("/api/library/models", params={"parameters": "min:8.1B,max:8.3B"}).json()["items"]
    assert "writer/Tiny-8B" in [item["id"] for item in within]
    below = writer.get("/api/library/models", params={"parameters": "min:4B,max:7.5B"}).json()["items"]
    assert "writer/Tiny-8B" not in [item["id"] for item in below]
