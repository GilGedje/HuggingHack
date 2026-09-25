import json
import sqlite3

import app.main as main
from app.database import Database
from app.indexer import card_metadata, classify
from test_access import server  # noqa: F401  (fixture)
from test_listing import FILES, put_listing
from test_organizations import login, org, upload  # noqa: F401  (fixtures)


def card(front: str) -> dict:
    return card_metadata(f"---\n{front}\n---\n# Card\n")


def relation(front: str, paths=("config.json",), config=None) -> tuple:
    facts = classify(list(paths), config or {}, {}, card(front), [])
    return facts["base_model"], facts["base_model_relation"]


def test_the_base_model_and_relation_are_read_like_the_hub():
    fp8 = {"quantization_config": {"quant_method": "fp8"}}
    gptq = {"quantization_config": {"quant_method": "gptq", "bits": 4}}
    assert relation("base_model: Qwen/Qwen3-0.6B", config=fp8) == ("Qwen/Qwen3-0.6B", "quantized")
    assert relation("base_model:\n- Qwen/Qwen3-0.6B", config=gptq) == ("Qwen/Qwen3-0.6B", "quantized")
    assert relation("base_model: Qwen/Qwen3-0.6B-Base") == ("Qwen/Qwen3-0.6B-Base", "finetune")
    assert relation("base_model: meta/llama", paths=("adapter_config.json",)) == ("meta/llama", "adapter")
    assert relation("base_model:\n- a/one\n- b/two") == ("a/one", "merge")
    assert relation("base_model: a/one\nbase_model_relation: Adapter", config=fp8) == ("a/one", "adapter")
    assert relation("base_model: not a repo id") == (None, None)
    assert relation("license: mit") == (None, None)


def test_a_model_tree_links_quantizations_to_their_base_as_far_as_you_may_see(org):  # noqa: F811
    writer, _ = login("writer")
    admin, _ = login("admin")
    assert writer.post("/api/uploads/repositories", json={"slug": "base", "namespace": "Nvidia", "visibility": "public"}).status_code == 201
    upload(writer, "Nvidia/base", {"config.json": b'{"torch_dtype": "bfloat16"}', "README.md": b"# Base\n"})
    assert writer.post("/api/uploads/repositories", json={"slug": "base-fp8", "visibility": "public"}).status_code == 201
    upload(writer, "writer/base-fp8", {
        "config.json": json.dumps({"quantization_config": {"quant_method": "fp8"}}).encode(),
        "README.md": b"---\nbase_model: nvidia/Base\n---\n# FP8\n",
    })

    child = writer.get("/api/library/models/writer/base-fp8").json()
    assert (child["base_model"], child["base_model_relation"]) == ("nvidia/Base", "quantized")
    base = child["model_tree"]["base"]
    # Matched whatever the case, and named as the library spells it.
    assert base == {"id": "Nvidia/base", "relation": "quantized", "in_library": True,
                    "organization": {"name": "Nvidia", "display_name": "NVIDIA"}}
    tree = writer.get("/api/library/models/Nvidia/base").json()["model_tree"]
    assert tree["base"] is None
    assert [item["id"] for item in tree["children"]["quantized"]] == ["writer/base-fp8"]

    # The organization page lists what others built on its models, not its own.
    built = writer.get("/api/library/models", params={"built_on": "nvidia"}).json()["items"]
    assert [item["id"] for item in built] == ["writer/base-fp8"]

    # A base only its owner can see reads like a model that is not here at all.
    assert writer.post("/api/uploads/repositories", json={"slug": "secret-base", "visibility": "private"}).status_code == 201
    upload(writer, "writer/secret-base", {"config.json": b"{}"})
    assert put_listing(writer, "writer/base-fp8", {"base_model": "writer/secret-base"}).status_code == 200
    reader, _ = login("reader")
    hidden = reader.get("/api/library/models/writer/base-fp8").json()["model_tree"]["base"]
    assert hidden == {"id": "writer/secret-base", "relation": "quantized", "in_library": False, "organization": None}
    assert writer.get("/api/library/models/writer/base-fp8").json()["model_tree"]["base"]["in_library"] is True

    # Corrections are checked; the relation is one of the Hub's four.
    assert put_listing(writer, "writer/base-fp8", {"base_model": "no slash"}).status_code == 400
    assert put_listing(writer, "writer/base-fp8", {"base_model_relation": "distilled"}).status_code == 400
    assert put_listing(writer, "writer/base-fp8", {"base_model": "Nvidia/base", "base_model_relation": "finetune"}).json()["overrides"] == {
        "base_model": "Nvidia/base", "base_model_relation": "finetune",
    }
    assert [item["id"] for item in admin.get("/api/library/models/Nvidia/base").json()["model_tree"]["children"]["finetune"]] == ["writer/base-fp8"]


def test_the_preview_reads_the_base_model_like_the_index(org):  # noqa: F811
    writer, _ = login("writer")
    files = {**FILES, "README.md": FILES["README.md"].replace(b"tags:", b"base_model: Qwen/Qwen3-0.6B\ntags:")}
    from test_listing import preview_body

    body = preview_body("writer/tiny")
    body["readme"] = files["README.md"].decode()
    detected = writer.post("/api/uploads/preview", json=body).json()["detected"]
    assert (detected["base_model"], detected["base_model_relation"]) == ("Qwen/Qwen3-0.6B", "quantized")
    assert writer.post("/api/uploads/repositories", json={"slug": "tiny", "visibility": "private"}).status_code == 201
    upload(writer, "writer/tiny", files)
    indexed = main.database.get_local_model("writer/tiny")
    assert (indexed["base_model"], indexed["base_model_relation"]) == ("Qwen/Qwen3-0.6B", "quantized")


def test_older_databases_gain_the_base_model_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE local_models DROP COLUMN base_model")
        connection.execute("ALTER TABLE local_models DROP COLUMN base_model_relation")
    database = Database(path)
    database.initialize()
    with database.connect() as connection:
        assert {"base_model", "base_model_relation"} <= database._column_names(connection, "local_models")
