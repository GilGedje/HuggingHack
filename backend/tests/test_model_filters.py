import json
import struct
from pathlib import Path

from app.indexer import model_precision, repository_facts
from test_access import login, server  # noqa: F401  (fixture)


def write_shard(path: Path, tensors: dict[str, tuple[str, list[int]]]) -> None:
    """A SafeTensors file with only its header, which is all the indexer reads."""
    header = json.dumps({name: {"dtype": dtype, "shape": shape, "data_offsets": [0, 0]} for name, (dtype, shape) in tensors.items()}).encode()
    path.write_bytes(struct.pack("<Q", len(header)) + header)


def repo(tmp_path: Path, name: str, config: dict, shards: list[dict], quant_file: dict | None = None) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "config.json").write_text(json.dumps(config))
    if quant_file:
        (root / "hf_quant_config.json").write_text(json.dumps(quant_file))
    for index, tensors in enumerate(shards, 1):
        write_shard(root / f"model-{index:05d}-of-{len(shards):05d}.safetensors", tensors)
    return root


# Layouts copied from real checkpoints: Qwen/Qwen3-8B-FP8, RedHatAI/Qwen3-8B-NVFP4 and
# nvidia/Qwen3.8-27B-NVFP4, with the tensors shrunk.
def test_precision_and_parameters_follow_the_quantization(tmp_path: Path):
    fp8 = repo(tmp_path, "fp8", {
        "torch_dtype": "bfloat16",
        "quantization_config": {"quant_method": "fp8", "fmt": "e4m3", "weight_block_size": [128, 128]},
    }, [{
        "model.embed_tokens.weight": ("BF16", [100, 10]),
        "model.layers.0.mlp.up_proj.weight": ("F8_E4M3", [40, 10]),
        "model.layers.0.mlp.up_proj.weight_scale_inv": ("F32", [1, 1]),
    }])
    assert repository_facts(fp8)["precision"] == "fp8"
    assert repository_facts(fp8)["parameter_count"] == 1_400  # scales are not parameters

    # NVFP4 packs two 4-bit values per byte and keeps FP8 scales next to them; an
    # "any F8 tensor means FP8" rule would get this wrong.
    nvfp4 = repo(tmp_path, "nvfp4", {
        "torch_dtype": "bfloat16",
        "quantization_config": {
            "quant_method": "compressed-tensors",
            "format": "nvfp4-pack-quantized",
            "config_groups": {"group_0": {"weights": {"num_bits": 4, "type": "float", "group_size": 16}}},
        },
    }, [{
        "model.embed_tokens.weight": ("BF16", [100, 10]),
        "model.layers.0.mlp.up_proj.weight_packed": ("U8", [40, 8]),
        "model.layers.0.mlp.up_proj.weight_scale": ("F8_E4M3", [40, 1]),
        "model.layers.0.mlp.up_proj.weight_global_scale": ("F32", [1]),
    }, {}])  # a trailing shard with no tensors must not make the count unknown
    facts = repository_facts(nvfp4)
    assert facts["precision"] == "nvfp4"
    assert facts["parameter_count"] == 1_000 + 40 * 16

    mixed = repo(tmp_path, "mixed", {
        "dtype": "bfloat16",
        "quantization_config": {"quant_method": "modelopt", "quant_algo": "MIXED_PRECISION"},
    }, [{"model.layers.0.weight": ("U8", [10, 10])}], quant_file={
        "quantization": {"quant_algo": "MIXED_PRECISION", "quantized_layers": {
            "model.layers.0": {"quant_algo": "NVFP4"}, "model.layers.1": {"quant_algo": "FP8"},
        }},
    })
    assert repository_facts(mixed)["precision"] == "nvfp4"

    assert model_precision({}, {"quantization": {"quant_algo": "FP8"}}) == "fp8"
    assert model_precision({"quantization_config": {"quant_method": "compressed-tensors", "format": "float-quantized"}}, {}) == "fp8"
    # Vision-language configs keep the dtype under text_config, newer ones as "dtype".
    assert model_precision({"text_config": {"dtype": "bfloat16"}}, {}) == "bf16"
    assert model_precision({}, {}, [("a", "F16", 10), ("b", "F32", 2)]) == "fp16"
    assert model_precision({"quantization_config": {"quant_method": "gptq", "bits": 4}}, {}) == "int4"
    assert model_precision({}, {}) is None


def test_hardware_tags_are_edited_by_repository_editors_only(server):  # noqa: F811
    member, _ = login("member")
    viewer, _ = login("viewer")
    admin, _ = login("admin")

    def put(client, repo_id, hardware):
        return client.put("/api/library/hardware", params={"repo_id": repo_id}, json={"hardware": hardware})

    assert put(member, "member/secret", ["b300", "l40"]).json()["hardware"] == ["l40", "b300"]
    assert put(member, "acme/open", ["l40"]).status_code == 403  # not theirs
    assert put(viewer, "acme/open", ["l40"]).status_code == 403  # viewers never edit
    assert put(viewer, "member/secret", ["l40"]).status_code == 403
    assert put(admin, "acme/open", ["h100"]).status_code == 400
    assert put(admin, "acme/open", ["l40"]).status_code == 200

    assert member.get("/api/library/models/member/secret").json()["hardware"] == ["l40", "b300"]
    tagged = member.get("/api/library/models", params={"hardware": "b300"}).json()
    assert [item["id"] for item in tagged["items"]] == ["member/secret"]
    assert {item["id"] for item in member.get("/api/library/models", params={"hardware": "l40,b300"}).json()["items"]} == {
        "member/secret", "acme/open",
    }

    # Counts only cover models the viewer can see, so the private repo stays hidden.
    counts = {key: count for key, _, count in viewer.get("/api/library/models").json()["facets"]["hardware"]}
    assert counts == {"l40": 1, "a100": 0, "rtx-pro-6000": 0, "b300": 0}
    assert viewer.get("/api/library/models", params={"precision": "fp16"}).status_code == 400

    deleted = member.request(
        "DELETE", "/api/uploads/repositories", params={"repo_id": "member/secret"}, json={"confirmation": "member/secret"}
    )
    assert deleted.status_code == 200
    assert "member/secret" not in server["database"].model_hardware()


def test_the_precision_filter_groups_formats_by_width():
    from app.catalog import search_catalog

    def model(repo_id: str, precision: str) -> dict:
        return {
            "repo_id": repo_id, "config": {"precision": precision}, "size_bytes": 1, "file_count": 1,
            "modified_at": "2026-09-25T00:00:00+00:00", "storage_target": "local", "cached": True,
        }

    models = [
        model("a/bf16", "bf16"), model("a/fp8", "fp8"), model("a/int8", "int8"),
        model("a/nvfp4", "nvfp4"), model("a/mxfp4", "mxfp4"), model("a/w4a16", "int4"), model("a/fp16", "fp16"),
    ]

    def ids(precision: str) -> list[str]:
        return sorted(item["id"] for item in search_catalog(models, set(), precision=precision, sort="name")["items"])

    assert ids("fp8") == ["a/fp8", "a/int8"]
    assert ids("fp4") == ["a/mxfp4", "a/nvfp4", "a/w4a16"]
    assert ids("nvfp4") == ids("fp4")  # older links
    assert ids("bf16,fp8") == ["a/bf16", "a/fp8", "a/int8"]
    facets = search_catalog(models, set())["facets"]["precision"]
    assert facets == {"bf16": 1, "fp8": 2, "fp4": 3}
    # Each model still reports its own precision.
    assert {item["id"]: item["precision"] for item in search_catalog(models, set(), precision="fp4")["items"]}["a/w4a16"] == "int4"
