#!/usr/bin/env bash
# Record the demo and turn it into an MP4: demo/setup.sh must have run (the seeded instance
# on :7870). Needs Google Chrome, Node, ffmpeg, and playwright-core (installed into a temp
# folder when PLAYWRIGHT_CORE is unset). Output: demo/hugginghack-demo.mp4 (gitignored).
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
work="${DEMO_WORK:-${TMPDIR:-/tmp}/hh-demo}"
out="${1:-$repo/demo/hugginghack-demo.mp4}"
pw="${PLAYWRIGHT_CORE:-}"
if [ -z "$pw" ]; then
  mkdir -p "$work/pw" && (cd "$work/pw" && [ -d node_modules/playwright-core ] || npm install -s playwright-core@1 >/dev/null 2>&1)
  pw="$work/pw/node_modules/playwright-core/index.mjs"
fi
mkdir -p "$work/video"
rm -f "$work/video"/*.webm
PLAYWRIGHT_CORE="$pw" node "$repo/demo/record.mjs" "$work" "$work/video/demo.webm"
# H.264 at 30 fps, even dimensions, plays everywhere.
ffmpeg -y -loglevel error -i "$work/video/demo.webm" -c:v libx264 -preset slow -crf 19 -r 30 -pix_fmt yuv420p \
  -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" -movflags +faststart "$out"
ffprobe -v error -show_entries format=duration:stream=width,height,codec_name -of default=nw=1 "$out"
ls -lh "$out"
