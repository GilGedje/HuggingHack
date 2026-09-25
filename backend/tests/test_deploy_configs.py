import io
import math
import zipfile

import pytest

import app.main as main
from app.deploy_configs import validate_results
from test_access import server  # noqa: F401  (fixture)
from test_organizations import login, org, upload  # noqa: F401  (fixtures)

SERVE = "vllm serve Nvidia/GLM --tensor-parallel-size 4 --max-model-len 32768\n"
COMPOSE = "services:\n  vllm:\n    image: vllm/vllm-openai:v0.11.0\n"


def test_results_keep_only_known_finite_numbers():
    checked = validate_results(
        {
            "values": {"output_tps": 1840.5, "ttft_ms": 212, "concurrency": 32, "tpot_ms": None},
            "hardware": "b300",
            "vllm_version": " 0.11.0 ",
            "custom": [{"name": "MTP acceptance", "value": 71.2, "unit": "%", "better": "higher"}],
            "notes": "Warm cache",
        }
    )
    assert checked == {
        "values": {"output_tps": 1840.5, "ttft_ms": 212, "concurrency": 32},
        "hardware": "b300",
        "vllm_version": "0.11.0",
        "custom": [{"name": "MTP acceptance", "value": 71.2, "unit": "%", "better": "higher"}],
        "notes": "Warm cache",
    }
    for bad in (
        {"values": {"made_up": 1}},
        {"values": {"ttft_ms": math.nan}},
        {"values": {"ttft_ms": math.inf}},
        {"values": {"ttft_ms": True}},
        {"values": {"ttft_ms": "fast"}},
        {"hardware": "h100-imaginary"},
        {"custom": [{"name": "", "value": 1}]},
        {"custom": [{"name": "x", "value": 1, "better": "sideways"}]},
        {"custom": [{"name": f"m{index}", "value": index} for index in range(21)]},
    ):
        with pytest.raises(ValueError):
            validate_results(bad)


def test_config_revisions_are_linear_snapshots_with_results(org):  # noqa: F811
    writer, _ = login("writer")
    created = writer.post(
        "/api/uploads/repositories", json={"slug": "GLM", "namespace": "Nvidia", "visibility": "organization"}
    )
    assert created.status_code == 201
    upload(writer, "Nvidia/GLM", {"config.json": b"{}"})
    params = {"repo_id": "Nvidia/GLM"}

    def create(client, body):
        return client.post("/api/library/configs", params=params, json=body)

    listing = writer.get("/api/library/configs", params=params).json()
    assert listing["items"] == [] and listing["can_edit"] is True
    assert {metric["id"] for metric in listing["metrics"]} >= {"output_tps", "ttft_ms", "acceptance_rate"}

    first = create(
        writer,
        {
            "message": "TP4 baseline",
            "files": [{"path": "serve.sh", "content": SERVE}, {"path": "compose/docker-compose.yml", "content": COMPOSE}],
            "results": {"values": {"output_tps": 1500, "ttft_ms": 300}, "hardware": "b300"},
        },
    )
    assert first.status_code == 201, first.text
    first = first.json()
    assert first["sequence"] == 1 and first["file_count"] == 2
    assert first["summary"] == {"added": 2, "modified": 0, "deleted": 0}
    assert first["results_updated_by"]

    # A second revision builds on the latest one and must say so.
    change = {
        "message": "Enable MTP",
        "files": [{"path": "serve.sh", "content": SERVE.replace("\n", " --speculative-config mtp\n")}, {"path": "notes.md", "content": "# MTP\n"}],
        "deletions": ["compose/docker-compose.yml"],
    }
    assert create(writer, change).status_code == 409  # no parent: stale
    second = create(writer, {**change, "parent_id": first["id"]})
    assert second.status_code == 201, second.text
    second = second.json()
    assert second["sequence"] == 2 and second["file_count"] == 2
    assert second["summary"] == {"added": 1, "modified": 1, "deleted": 1}
    assert create(writer, {**change, "parent_id": first["id"]}).status_code == 409

    # Nothing changed, binary content, and escaping paths are refused.
    same = {"message": "Again", "parent_id": second["id"], "files": [{"path": "notes.md", "content": "# MTP\n"}]}
    assert create(writer, same).status_code == 400
    assert create(writer, {**same, "files": [{"path": "w.bin", "content": "a\x00b"}]}).status_code == 400
    assert create(writer, {**same, "files": [{"path": "../escape.sh", "content": "x"}]}).status_code == 400
    assert create(writer, {**same, "files": [], "deletions": ["serve.sh", "notes.md"]}).status_code == 400

    detail = writer.get("/api/library/config", params={**params, "revision_id": second["id"]}).json()
    assert [item["path"] for item in detail["files"]] == ["notes.md", "serve.sh"]
    serve = next(item for item in detail["changes"] if item["path"] == "serve.sh")
    assert serve["change"] == "modified" and serve["additions"] == 1 and serve["deletions"] == 1
    assert any("--speculative-config mtp" in line for line in serve["diff"])

    archive = writer.get("/api/library/config/archive", params={**params, "revision_id": first["id"]})
    assert archive.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        assert sorted(bundle.namelist()) == ["compose/docker-compose.yml", "serve.sh"]
        assert bundle.read("serve.sh").decode() == SERVE

    # Results are measured after deploying, so they stay editable.
    results = {"values": {"output_tps": 2100, "ttft_ms": 260, "acceptance_rate": 68.5}, "hardware": "b300"}
    updated = writer.put(
        "/api/library/config/results", params={**params, "revision_id": second["id"]}, json={"results": results}
    )
    assert updated.status_code == 200, updated.text
    items = writer.get("/api/library/configs", params=params).json()["items"]
    assert [item["sequence"] for item in items] == [2, 1]
    assert items[0]["results"]["values"]["acceptance_rate"] == 68.5
    assert writer.get("/api/library/models/Nvidia/GLM").json()["config_count"] == 2

    # Members read; only people who may edit the model add revisions or results.
    reader, _ = login("reader")
    assert reader.get("/api/library/configs", params=params).json()["can_edit"] is False
    assert create(reader, {**same, "files": [{"path": "x.sh", "content": "x"}]}).status_code == 403
    assert reader.put(
        "/api/library/config/results", params={**params, "revision_id": second["id"]}, json={"results": {}}
    ).status_code == 403
    outsider, _ = login("outsider")
    assert outsider.get("/api/library/configs", params=params).status_code == 404
    assert outsider.get("/api/library/config/archive", params={**params, "revision_id": first["id"]}).status_code == 404

    # Configs are never part of the model's files.
    files = [item["path"] for item in writer.get("/api/library/models/Nvidia/GLM").json()["files"]]
    assert "serve.sh" not in files
    assert not (main.settings.model_storage / "Nvidia" / "GLM" / "serve.sh").exists()
