#!/usr/bin/env bash
# Start a throwaway HuggingHack for the demo video, seeded through its API (demo/seed.py).
# Uses copies of the local models folder and two buckets on a local MinIO; never the live
# data/ or models/ themselves. See demo/README.md.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
work="${DEMO_WORK:-${TMPDIR:-/tmp}/hh-demo}"
port="${DEMO_PORT:-7870}"
minio="${MINIO_URL:-http://127.0.0.1:9600}"
export MINIO_KEY="${MINIO_KEY:-spikeadmin}" MINIO_SECRET="${MINIO_SECRET:-spike-secret-123}"
python="$repo/.venv/bin/python"

pkill -f "uvicorn app.main:app --app-dir backend --port $port" 2>/dev/null || true
rm -rf "$work"; mkdir -p "$work/models" "$work/data"

echo "== copies of the models (APFS clones, no extra space)"
for model in Qwen/Qwen3-0.6B RedHat/Qwen3-0.6B-quantized.w4a16 HuggingFaceTB/SmolLM2-135M-Instruct \
             bartowski/SmolLM2-135M-Instruct-GGUF sentence-transformers/all-MiniLM-L6-v2 \
             hf-internal-testing/tiny-random-LlamaForCausalLM; do
  mkdir -p "$work/models/$(dirname "$model")"
  cp -cR "$repo/models/$model" "$work/models/$model" 2>/dev/null || cp -R "$repo/models/$model" "$work/models/$model"
done
# Upload records from the live site would hide the copies from a fresh scan.
find "$work/models" -name .hugginghack.json -delete

echo "== two empty buckets"
"$python" - "$minio" <<'PY'
import os, sys, boto3
from botocore.config import Config
s3 = boto3.client("s3", endpoint_url=sys.argv[1], aws_access_key_id=os.environ["MINIO_KEY"],
                  aws_secret_access_key=os.environ["MINIO_SECRET"], region_name="us-east-1",
                  config=Config(s3={"addressing_style": "path"}))
for bucket in ("demo-primary", "demo-archive"):
    if bucket in [b["Name"] for b in s3.list_buckets()["Buckets"]]:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for item in page.get("Contents", []):
                s3.delete_object(Bucket=bucket, Key=item["Key"])
        for upload in s3.list_multipart_uploads(Bucket=bucket).get("Uploads", []):
            s3.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])
    else:
        s3.create_bucket(Bucket=bucket)
PY

echo "== the server on :$port"
targets="[{\"id\":\"primary\",\"name\":\"NetApp primary\",\"bucket\":\"demo-primary\",\"prefix\":\"models\",\"endpoint_url\":\"$minio\",\"region\":\"us-east-1\",\"addressing_style\":\"path\",\"use_ssl\":false,\"access_key_env\":\"MINIO_KEY\",\"secret_key_env\":\"MINIO_SECRET\",\"direct_downloads\":true,\"direct_uploads\":true,\"part_size_mb\":8},{\"id\":\"archive\",\"name\":\"Archive bucket\",\"bucket\":\"demo-archive\",\"prefix\":\"models\",\"endpoint_url\":\"$minio\",\"region\":\"us-east-1\",\"addressing_style\":\"path\",\"use_ssl\":false,\"access_key_env\":\"MINIO_KEY\",\"secret_key_env\":\"MINIO_SECRET\"}]"
(cd "$repo" && env -u DATABASE_URL PYTHONPATH=backend MODEL_STORAGE="$work/models" DATA_DIR="$work/data" \
  ACCOUNTS_ENABLED=true STORAGE_TARGETS_JSON="$targets" DEFAULT_STORAGE_TARGET=primary \
  PUBLIC_URL=http://127.0.0.1:$port \
  OIDC_ISSUER=http://auth.localhost:59090/application/o/hugginghack/ OIDC_CLIENT_ID=demo OIDC_PROVIDER_NAME=Authentik \
  "$python" -m uvicorn app.main:app --app-dir backend --port "$port" > "$work/server.log" 2>&1 &)
for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:$port/api/health" >/dev/null && break; sleep 1; done

echo "== seed through the API"
DEMO_URL="http://127.0.0.1:$port" DEMO_WORK="$work" "$python" "$repo/demo/seed.py"

echo "== a model folder for the upload scene"
mkdir -p "$work/upload/Qwen3-0.6B-sql"
"$python" - "$work/upload/Qwen3-0.6B-sql" <<'PY'
import json, os, struct, sys
from pathlib import Path
root = Path(sys.argv[1])
(root / "config.json").write_text(json.dumps({"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3", "torch_dtype": "bfloat16"}))
(root / "README.md").write_text("---\nlicense: apache-2.0\npipeline_tag: text-generation\nbase_model: Qwen/Qwen3-0.6B\n"
                                "base_model_relation: finetune\ntags: [sql]\n---\n# Qwen3-0.6B SQL\n\nText-to-SQL fine-tune.\n")
count = 12_000_000
header = json.dumps({"lm_head.weight": {"dtype": "BF16", "shape": [count], "data_offsets": [0, 2 * count]}}).encode()
header += b" " * (-len(header) % 8)
with (root / "model.safetensors").open("wb") as f:
    f.write(struct.pack("<Q", len(header))); f.write(header); f.write(os.urandom(2 * count))
PY

echo "== real client output for the download scene"
{
  echo '$ export HF_ENDPOINT=http://hub.acme.internal'
  echo '$ hf download acme-ai/Qwen3-0.6B-support --local-dir ./support'
  HF_ENDPOINT="http://127.0.0.1:$port" HF_HOME="$work/hf" HF_HUB_DISABLE_TELEMETRY=1 \
    "$repo/.venv/bin/hf" download acme-ai/Qwen3-0.6B-support --local-dir "$work/dl" 2>&1 | tr -d '\r' | grep -v "^$" | sed "s#$work/dl#./support#; s#127.0.0.1:$port#hub.acme.internal#g" | tail -4
  echo '$ git clone http://hub.acme.internal/acme-ai/Qwen3-0.6B-support'
  git clone "http://127.0.0.1:$port/acme-ai/Qwen3-0.6B-support" "$work/clone" 2>&1 | sed "s#$work/clone#Qwen3-0.6B-support#; s#127.0.0.1:$port#hub.acme.internal#g"
  (cd "$work/clone" && echo '$ git log --oneline' && git log --oneline)
} > "$work/terminal.txt"
cat "$work/terminal.txt"
echo "Ready: http://127.0.0.1:$port (gil / ${DEMO_PASSWORD:-demo-password-2026}); work folder $work"
