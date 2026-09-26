# HuggingHack

A self-hosted, air-gapped Hugging Face–style model hub. People bring models across the air gap
once (upload, or copy into the model folder and rescan); every machine on the network then pulls
them through the Hub protocol (`HF_ENDPOINT` for vLLM, Transformers, `hf`) or `git clone` with LFS.

- `backend/`: FastAPI server (Python 3.12). API, Hub protocol, git mirror, storage (folder +
  S3-compatible buckets), accounts, organizations, roles, uploads, commits, configs, runtimes.
  Read [`backend/CLAUDE.md`](backend/CLAUDE.md) before changing it.
- `frontend/`: React 18 + Vite + TypeScript single-page app (HashRouter), served by the backend
  from `frontend/dist` (or `backend/static` in the image). Read
  [`frontend/CLAUDE.md`](frontend/CLAUDE.md) before changing it.
- `docs/`: [GUIDE.md](docs/GUIDE.md) (every feature), [AIRGAPPED.md](docs/AIRGAPPED.md) (offline
  install, security settings, reverse proxy), [SERVE_FROM_S3.md](docs/SERVE_FROM_S3.md)
  (S3 + PostgreSQL deployment), [SCALING.md](docs/SCALING.md) (the plan for direct-to-bucket
  transfers and multiple replicas; not implemented yet). `README.md` is the public overview with
  screenshots in `docs/images`.
- `.env.example` lists every setting with its default; `docker-compose.yml` runs one service,
  `docker-compose.postgres.yml` adds PostgreSQL 17.

## Ground rules

1. **PostgreSQL is the production database.** Every change must pass the test suite on
   PostgreSQL (`TEST_POSTGRES_URL` set, 0 skipped) as well as SQLite. SQLite stays supported for
   small and test deployments.
2. **Air-gapped at runtime.** No request may reach the internet unless an operator configured it
   (S3 endpoints, OIDC provider, runtimes, optional `HF_DOWNLOADS_ENABLED`). No CDN links, no
   Google Fonts, no telemetry. Dependencies are installed from the lockfile / requirements on a
   connected build machine and carried over as a Docker image, so add them only when essential.
3. **Model files are data.** Never import, unpickle, or execute anything from a model repository.
4. **Security is load-bearing.** Visibility (`VISIBLE_TO_USER`), roles and permissions, the
   cross-site write check, the Content-Security-Policy, CSRF tokens, and the hidden-file rule are
   relied on everywhere. The sub-directory CLAUDE.md files say where each lives; change them only
   deliberately, with tests.
5. **Backend and frontend share a few rules; keep them in sync.** The hidden-file rule
   (`backend/app/indexer.py` `hidden_path` ↔ `frontend/src/uploadPlan.ts` `isRecorded`), role and
   permission names, and API response shapes (`frontend/src/types.ts`).
6. **The UI is part of the product.** Risky actions ask first, cancelling restores the previous
   value, lists show skeletons while loading and a retryable error on failure, and every page works
   at 390px and 1280px, in light and dark mode, and with reduced motion.

## Verify a change

```bash
# Backend: SQLite and PostgreSQL (see backend/CLAUDE.md for the venv and the test database container;
# test tools are in backend/requirements-dev.txt, requirements.txt is what the image installs)
PYTHONPATH=backend TEST_POSTGRES_URL=postgresql://hugginghack:test-only-password@127.0.0.1:55432/hugginghack_test \
  .venv/bin/python -m pytest backend/tests -q -rs
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests -q

# Frontend: type-check + build, and unit tests
cd frontend && npm run build && npm test
```

A change is done when the backend suite reports **0 skipped** with `TEST_POSTGRES_URL` set and
`git` plus `git-lfs` on PATH (without them the PostgreSQL and clone tests skip).

This local run is the gate. `.github/workflows/ci.yml` describes the same steps but GitHub
Actions does not run for this repository, so never treat a missing CI run as a check.

For UI changes, also run the built app against a **copy** of the data (never the live `data/` or
`models/`), for example with `ACCOUNTS_ENABLED=false` on another port, and check the pages you
touched in a browser at both widths and both themes.

## Deploy

`data/` and `models/` are the live installation's data: never point tests or experiments at them.
Before a deploy that changes the schema or how files are stored, back up `data/` outside the repo
while the site runs, with SQLite's own copy so the `-wal` file is included:

```bash
B=~/HuggingHack-backups/data-$(date +%Y%m%d-%H%M) && mkdir -p $B
sqlite3 data/hugginghack.sqlite3 ".backup $B/hugginghack.sqlite3" && cp -Rp data/git-mirrors data/system data/avatars $B/
```

`docker compose up -d --build` rebuilds and restarts the service on port 7860. The container runs
as `PUID:PGID` from `.env` (default and live: `1000:1000`), which must be able to write `data/`
and the model folder. Afterwards, check that `curl -s localhost:7860/api/health` answers with the
new version, and that the served bundle (`curl -s localhost:7860/ | grep -o 'assets/index-[^"]*\.js'`)
matches `frontend/dist/assets`. The version lives in `backend/app/config.py` (`APP_VERSION`) and
`frontend/package.json`; bump both together.

## Conventions

- Commit messages: a short imperative subject that says what changed for people, then a body
  with the details. Push to `main`; there are no long-lived branches.
- Write code like the surrounding code: its naming, comment density, and idioms. Comments explain
  why, not what.
- User-facing text is plain and specific: say what happened and what to do next.
