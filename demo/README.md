# Demo video

A recorded walk through HuggingHack: sign-in with SSO, Explore, a model page and "Use this
model", the model tree (quantizations and fine-tunes), commits and diffs, deployment configs
with compared results, a browser upload straight to a bucket, administration (users, roles,
organizations, storage with three locations and a live move), pulling with `hf` and `git`,
and dark mode. About two minutes, 1440×900, captions instead of a voice-over.

The recording is real: a Chrome driven by Playwright against a throwaway HuggingHack seeded
through its own API, with copies of the local models and two buckets on a local MinIO. Nothing
touches the live `data/` or `models/`. The MP4 is not committed (`.gitignore`); re-record it:

```bash
# MinIO on 127.0.0.1:9600 (backend/CLAUDE.md shows how the test one is started), then:
demo/setup.sh                      # throwaway instance on :7870, seeded (about 90 s)
demo/render.sh                     # records and writes demo/hugginghack-demo.mp4
DEMO_HEADED=1 demo/render.sh       # the same, watching the browser on screen
```

| File | What |
| --- | --- |
| `setup.sh` | Copies the models, empties/creates the buckets, starts the server, runs `seed.py`, prepares the upload folder and the terminal transcript |
| `seed.py` | Accounts, an organization, moves into the buckets, hardware tags, a collection, a fine-tune uploaded to the bucket with a second commit, three config revisions with results |
| `record.mjs` | The scenes: a cursor and a caption bar are injected into the page; every step is a real click or keystroke. Writes `<work>/video/demo.webm` and a `.marks.json` with scene times |
| `render.sh` | Runs `record.mjs`, then ffmpeg → H.264 MP4 |

Needs Google Chrome, Node, ffmpeg, the repo `.venv`, and `playwright-core` plus its ffmpeg
(`node node_modules/playwright-core/cli.js install ffmpeg`, installed into the work folder on
first run). The seeded numbers in the config results are illustrative, not measurements.
