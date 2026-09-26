# Demo video

A recorded walk through HuggingHack, told as two people using it. About two and a half
minutes, 1920×1200 at 30 fps, captions instead of a voice-over.

1. **The engineer** signs in (the SSO button is shown), searches for a model, opens its model
   tree to pick a quantization, reads the commit that added serving notes and its diff, compares
   deployment-config revisions to see which one gave the best results, and copies the vLLM and
   `hf` commands from "Use this model" (the terminal card is real `hf download` output).
2. **The admin** creates an organization, uploads a model into it straight to a bucket while
   the upload panel shows live progress, and moves the model to another bucket from Storage.
   The move waits for the old bucket's download links to expire; the video cuts that wait out
   and says so.

The recording is real: a Chrome driven by Playwright against a throwaway HuggingHack seeded
through its own API, with copies of the local models and two buckets on a local MinIO. Nothing
touches the live `data/` or `models/`. The MP4 is not committed (`.gitignore`); re-record it:

```bash
# MinIO on 127.0.0.1:9600 (backend/CLAUDE.md shows how the test one is started), then:
demo/render.sh                     # reseeds, records, writes demo/hugginghack-demo.mp4
DEMO_HEADED=1 demo/render.sh       # the same, watching the browser on screen
demo/setup.sh                      # only the seeded instance on :7870, to click around
```

Every take reseeds, because the video creates the organization and moves the upload. The
throwaway instance lives in `DEMO_WORK` (default `/Users/Shared/hugginghack-demo`, a path that
reads well on screen). With `DEMO_HEADED=1`, keep the Chrome window uncovered: a hidden window
stops sending frames.

| File | What |
| --- | --- |
| `setup.sh` | Copies the models, empties/creates the buckets, starts the server, runs `seed.py`, prepares the upload folder and the terminal transcript |
| `seed.py` | Accounts, an organization, moves into the buckets, a fine-tune with a second commit, the quantization's "serving notes" commit, three config revisions with results |
| `record.mjs` | The scenes. A cursor, eased scrolling, captions and fades are injected into the page; every step is a real click or keystroke. Frames come from Chrome's screencast with their timestamps (`frames.json`) |
| `assemble.mjs` | Resamples the timestamped frames to a constant 30 fps list for ffmpeg |
| `render.sh` | Runs `setup.sh`, `record.mjs`, `assemble.mjs`, then ffmpeg to an H.264 MP4 |

Needs Google Chrome, Node, ffmpeg and the repo `.venv`. `playwright-core` is installed once into
`$TMPDIR/hugginghack-demo-playwright` (or set `PLAYWRIGHT_CORE`). The seeded numbers in the
config results are illustrative, not measurements.
