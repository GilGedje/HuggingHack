#!/usr/bin/env bash
# Reseed, record, and assemble the demo video: demo/hugginghack-demo.mp4 (gitignored).
# Needs Google Chrome, Node, ffmpeg, MinIO on 127.0.0.1:9600, the repo .venv; playwright-core
# is installed once into a cache folder outside the demo's work folder.
#   demo/render.sh                 # headless
#   DEMO_HEADED=1 demo/render.sh   # watch it on screen (keep the Chrome window uncovered:
#                                  # a hidden window stops sending frames)
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
export DEMO_WORK="${DEMO_WORK:-/Users/Shared/hugginghack-demo}"
out="${1:-$repo/demo/hugginghack-demo.mp4}"
pw="${PLAYWRIGHT_CORE:-}"
if [ -z "$pw" ]; then
  cache="${TMPDIR:-/tmp}/hugginghack-demo-playwright"
  mkdir -p "$cache"
  [ -d "$cache/node_modules/playwright-core" ] || (cd "$cache" && npm install -s playwright-core@1 >/dev/null 2>&1)
  pw="$cache/node_modules/playwright-core/index.mjs"
fi

# Every take starts from the same seeded state: the video creates an organization,
# uploads into it and moves the upload.
"$repo/demo/setup.sh"

frames="$DEMO_WORK/frames"
PLAYWRIGHT_CORE="$pw" node "$repo/demo/record.mjs" "$DEMO_WORK" "$frames"
node "$repo/demo/assemble.mjs" "$frames"
(cd "$frames" && ffmpeg -y -loglevel error -f concat -safe 0 -i list.ffconcat -fps_mode cfr -r 30 \
  -vf "scale=1920:1200:flags=lanczos,format=yuv420p" -c:v libx264 -preset slow -crf 18 \
  -movflags +faststart "$out")
ffprobe -v error -show_entries format=duration:stream=width,height,r_frame_rate -of default=nw=1 "$out"
ls -lh "$out"
