#!/usr/bin/env bash
# Reseed, record, and assemble the upload video: demo/hugginghack-upload.mp4 (gitignored).
# Same needs as render.sh (Google Chrome, Node, ffmpeg, MinIO on 127.0.0.1:9600, the repo .venv),
# plus about 9 GB free: the upload lands in the local MinIO bucket and is pulled once with hf.
#   demo/render-upload.sh                 # headless
#   DEMO_HEADED=1 demo/render-upload.sh   # watch it on screen (keep the Chrome window uncovered)
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
export DEMO_WORK="${DEMO_WORK:-/Users/Shared/hugginghack-demo}"
out="${1:-$repo/demo/hugginghack-upload.mp4}"
pw="${PLAYWRIGHT_CORE:-}"
if [ -z "$pw" ]; then
  cache="${TMPDIR:-/tmp}/hugginghack-demo-playwright"
  mkdir -p "$cache"
  [ -d "$cache/node_modules/playwright-core" ] || (cd "$cache" && npm install -s playwright-core@1 >/dev/null 2>&1)
  pw="$cache/node_modules/playwright-core/index.mjs"
fi

"$repo/demo/setup.sh"

echo "== folders for the upload video"
"$repo/.venv/bin/python" - "$DEMO_WORK/upload-video" <<'PY'
import json, struct, sys
from pathlib import Path
root = Path(sys.argv[1])

def model(folder, count, card):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.json").write_text(json.dumps({"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3", "torch_dtype": "bfloat16"}))
    (folder / "README.md").write_text(card)
    header = json.dumps({"lm_head.weight": {"dtype": "BF16", "shape": [count], "data_offsets": [0, 2 * count]}}).encode()
    header += b" " * (-len(header) % 8)
    with (folder / "model.safetensors").open("wb") as f:
        f.write(struct.pack("<Q", len(header))); f.write(header)
        f.truncate(8 + len(header) + 2 * count)  # sparse: takes no disk until uploaded

card = ("---\nlicense: apache-2.0\npipeline_tag: text-generation\nbase_model: Qwen/Qwen3-0.6B\n"
        "base_model_relation: finetune\ntags: [sql]\n---\n# Qwen3-0.6B SQL\n\nText-to-SQL fine-tune.\n")
# About 4 GB: at local-bucket speed the upload runs long enough to browse away and interrupt it.
model(root / "Qwen3-0.6B-sql", 2_000_000_000, card)
# The team's retrain, carried in on a disk for the "copy the folder and scan" scene.
model(root / "incoming" / "Qwen3-0.6B-sql-v2", 300_000_000, card.replace("# Qwen3-0.6B SQL", "# Qwen3-0.6B SQL v2")
      .replace("Text-to-SQL fine-tune.", "Text-to-SQL fine-tune, retrained on the Q3 query logs."))

changes = root / "changes"
changes.mkdir(parents=True, exist_ok=True)
(changes / "generation_config.json").write_text(json.dumps(
    {"do_sample": True, "temperature": 0.2, "top_p": 0.9, "max_new_tokens": 512}, indent=2) + "\n")
(changes / "README.md").write_text(card + "\n## Prompt format\n\n"
    "Give the schema as `CREATE TABLE` statements, then the question. The model answers with one SQL query.\n\n"
    "## Sampling\n\n`generation_config.json` sets a low temperature, which keeps queries stable between runs.\n")
PY

frames="$DEMO_WORK/frames-upload"
DEMO_HF="$repo/.venv/bin/hf" PLAYWRIGHT_CORE="$pw" node "$repo/demo/record-upload.mjs" "$DEMO_WORK" "$frames"
node "$repo/demo/assemble.mjs" "$frames"
(cd "$frames" && ffmpeg -y -loglevel error -f concat -safe 0 -i list.ffconcat -fps_mode cfr -r 30 \
  -vf "scale=1920:1200:flags=lanczos,format=yuv420p" -c:v libx264 -preset slow -crf 18 \
  -movflags +faststart "$out")
ffprobe -v error -show_entries format=duration:stream=width,height,r_frame_rate -of default=nw=1 "$out"
ls -lh "$out"
