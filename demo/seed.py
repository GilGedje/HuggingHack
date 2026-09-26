"""Seed a throwaway HuggingHack instance for the demo video (demo/README.md).

Run by demo/setup.sh against a fresh server. Everything goes through the public API, the
way a user would: accounts, an organization, models moved into two buckets, hardware tags,
saved models, a fine-tune uploaded straight to a bucket with a follow-up commit, and three
deployment-config revisions with benchmark results to compare.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("DEMO_URL", "http://127.0.0.1:7870")
PASSWORD = os.environ.get("DEMO_PASSWORD", "demo-password-2026")
WORK = Path(os.environ["DEMO_WORK"])

client = httpx.Client(base_url=BASE, headers={"Origin": BASE}, timeout=300)


def ok(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        sys.exit(f"{response.request.method} {response.request.url} -> {response.status_code}: {response.text[:300]}")
    return response.json() if response.content else {}


def safetensors(path: Path, parameters: int) -> None:
    """A small but valid safetensors file (BF16), so the library counts its parameters."""
    header = json.dumps(
        {"model.embed_tokens.weight": {"dtype": "BF16", "shape": [parameters], "data_offsets": [0, 2 * parameters]}}
    ).encode()
    header += b" " * (-len(header) % 8)
    with path.open("wb") as handle:
        handle.write(struct.pack("<Q", len(header)))
        handle.write(header)
        handle.write(os.urandom(2 * parameters))


# ---- the owner and the team ---------------------------------------------------------------
setup = ok(client.post("/api/auth/setup", json={"username": "gil", "display_name": "Gil", "password": PASSWORD}))
client.headers["X-CSRF-Token"] = setup["csrf_token"]
ok(client.post("/api/organizations", json={
    "name": "acme-ai", "display_name": "Acme AI",
    "description": "Serving team. Our fine-tunes and the configs we run them with.",
}))
for username, name, role, org_role in (
    ("maya", "Maya Levi", "member", "write"),
    ("noam", "Noam Bar", "viewer", "read"),
    ("dana", "Dana Katz", "admin", "admin"),
):
    ok(client.post("/api/users", json={
        "username": username, "display_name": name, "password": PASSWORD, "role": role,
        "organizations": [{"organization": "acme-ai", "role": org_role}],
    }))

# ---- models into the buckets --------------------------------------------------------------
ok(client.post("/api/local-models/scan"))
for repo_id, destination in (
    ("sentence-transformers/all-MiniLM-L6-v2", "primary"),
    ("HuggingFaceTB/SmolLM2-135M-Instruct", "primary"),
    ("hf-internal-testing/tiny-random-LlamaForCausalLM", "archive"),
):
    ok(client.post("/api/storage/moves", json={"repo_id": repo_id, "destination": destination, "confirmation": repo_id}))
    for _ in range(300):
        moves = ok(client.get("/api/storage/moves"))["items"]
        move = next(item for item in moves if item["repo_id"] == repo_id)
        if move["status"] in {"done", "failed", "cancelled"}:
            break
        time.sleep(1)
    assert move["status"] == "done", move

for repo_id, hardware in (
    ("Qwen/Qwen3-0.6B", ["a100", "l40"]),
    ("RedHat/Qwen3-0.6B-quantized.w4a16", ["l40", "rtx-pro-6000"]),
    ("bartowski/SmolLM2-135M-Instruct-GGUF", ["rtx-pro-6000"]),
    ("HuggingFaceTB/SmolLM2-135M-Instruct", ["l40"]),
):
    ok(client.put("/api/library/hardware", params={"repo_id": repo_id}, json={"hardware": hardware}))

collection = ok(client.post("/api/collections", json={"name": "Serving candidates", "description": "Small models for the edge rig"}))
for repo_id, note in (
    ("Qwen/Qwen3-0.6B", "Baseline for the support bot"),
    ("RedHat/Qwen3-0.6B-quantized.w4a16", "W4A16: fits the L40 with room for KV cache"),
):
    ok(client.post("/api/saved-models", json={"repo_id": repo_id, "note": note, "collection_ids": [collection["id"]], "metadata": {}}))

# ---- a fine-tune, uploaded straight to the bucket, then changed ---------------------------
def direct_upload(base: str, params: dict, files: dict[str, Path]) -> None:
    for path, source in files.items():
        size = source.stat().st_size
        state = ok(client.post(f"{base}/begin", params=params, json={"path": path, "size": size}))
        if not state.get("direct"):
            # A repository on local disk takes the file in chunks through the server.
            data = source.read_bytes()
            ok(client.put(base, params={**params, "path": path}, content=data, headers={
                "Upload-Offset": "0", "Upload-Length": str(len(data)), "Content-Type": "application/octet-stream"}))
            continue
        if state["part_count"]:
            links = ok(client.post(f"{base}/parts", params=params, json={"path": path, "parts": list(range(1, state["part_count"] + 1))}))
            data = source.read_bytes()
            for part in links["parts"]:
                start = (part["number"] - 1) * state["part_size"]
                assert httpx.put(part["url"], content=data[start:start + part["size"]]).status_code == 200
        ok(client.post(f"{base}/complete", params=params, json={"path": path}))


finetune = WORK / "seed" / "Qwen3-0.6B-support"
finetune.mkdir(parents=True, exist_ok=True)
(finetune / "config.json").write_text(json.dumps({"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3", "torch_dtype": "bfloat16"}))
(finetune / "README.md").write_text(
    "---\nlicense: apache-2.0\npipeline_tag: text-generation\nlibrary_name: transformers\n"
    "base_model: Qwen/Qwen3-0.6B\nbase_model_relation: finetune\ntags: [support, chat]\n---\n"
    "# Qwen3-0.6B support\n\nQwen3-0.6B fine-tuned on 40k support conversations.\n"
)
safetensors(finetune / "model.safetensors", 3_000_000)
repo = ok(client.post("/api/uploads/repositories", json={"slug": "Qwen3-0.6B-support", "namespace": "acme-ai", "visibility": "public"}))["repo_id"]
direct_upload("/api/uploads/repositories/files", {"repo_id": repo}, {p.name: p for p in finetune.iterdir()})
ok(client.post("/api/uploads/repositories/finalize", params={"repo_id": repo}, json={"message": "First fine-tune: 40k support conversations"}))
(finetune / "README.md").write_text(
    (finetune / "README.md").read_text()
    + "\n## Evaluation\n\n| Set | Accuracy |\n| --- | --- |\n| Support intents | 91.4% |\n| Escalation | 87.9% |\n"
)
session = ok(client.post("/api/repos/changes", json={"repo_id": repo}))["id"]
direct_upload(f"/api/repos/changes/{session}/files", {}, {"README.md": finetune / "README.md"})
ok(client.post(f"/api/repos/changes/{session}/commit", json={"message": "Add evaluation results", "description": "Held-out intent and escalation sets."}))

# ---- a commit on the quant: serving notes near the top of its card ------------------------
card = WORK / "models" / "RedHat" / "Qwen3-0.6B-quantized.w4a16" / "README.md"
text = card.read_text()
notes = ("\n> **Serving notes (Acme AI):** runs on one L40 with vLLM 0.11, 8k context. "
         "See the Config tab for the flags we use and the measured throughput.\n")
edited = WORK / "seed" / "quant-README.md"
edited.parent.mkdir(parents=True, exist_ok=True)
edited.write_text(text.replace("# Qwen3-0.6B-quantized.w4a16\n", "# Qwen3-0.6B-quantized.w4a16\n" + notes, 1))
session = ok(client.post("/api/repos/changes", json={"repo_id": "RedHat/Qwen3-0.6B-quantized.w4a16"}))["id"]
direct_upload(f"/api/repos/changes/{session}/files", {}, {"README.md": edited})
ok(client.post(f"/api/repos/changes/{session}/commit", json={
    "message": "Add serving notes for the L40", "description": "What we run it with, and where the numbers are."}))

# ---- deployment configs with results ------------------------------------------------------
serve = """#!/bin/sh
vllm serve RedHat/Qwen3-0.6B-quantized.w4a16 \\
  --max-model-len 8192 \\
  --gpu-memory-utilization 0.90{extra}
"""
parent = None
for message, extra, values in (
    ("Baseline on one L40", "", {"output_tps": 1840, "per_user_tps": 57.5, "ttft_ms": 212, "ttft_p99_ms": 480, "tpot_ms": 17.4, "kv_cache_tokens": 310000}),
    ("More sequences and prefix caching", " \\\n  --max-num-seqs 256 \\\n  --enable-prefix-caching",
     {"output_tps": 2480, "per_user_tps": 77.5, "ttft_ms": 164, "ttft_p99_ms": 390, "tpot_ms": 13.9, "kv_cache_tokens": 310000}),
    ("FP8 KV cache", " \\\n  --max-num-seqs 256 \\\n  --enable-prefix-caching \\\n  --kv-cache-dtype fp8",
     {"output_tps": 2710, "per_user_tps": 84.7, "ttft_ms": 191, "ttft_p99_ms": 455, "tpot_ms": 12.6, "kv_cache_tokens": 620000}),
):
    revision = ok(client.post("/api/library/configs", params={"repo_id": "RedHat/Qwen3-0.6B-quantized.w4a16"}, json={
        "parent_id": parent,
        "message": message,
        "files": [{"path": "serve.sh", "content": serve.format(extra=extra)}],
        "deletions": [],
        "results": {
            "values": {**values, "concurrency": 32, "input_tokens": 1024, "output_tokens": 256, "gpu_count": 1, "tp_size": 1},
            "hardware": "l40", "vllm_version": "0.11.0", "custom": [], "notes": "",
        },
    }))
    parent = revision["id"]
print(f"seeded: {repo}, 3 users, org acme-ai, 3 config revisions, models in 3 locations")
