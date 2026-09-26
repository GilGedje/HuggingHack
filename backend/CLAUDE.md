# backend/ — maintainer guide

HuggingHack is a self-hosted Hugging Face–style model hub for air-gapped networks. This
folder is the FastAPI app (`backend/app`). It stores metadata in PostgreSQL (the production
target) or SQLite (kept for a small test deployment). It serves models from a folder or S3
buckets, and speaks enough of the Hub protocol for vLLM, transformers, `hf` and `git clone`
to pull from it. See the root `CLAUDE.md` for the whole picture and `frontend/CLAUDE.md` for
the UI.

## Commands (run from the repo root)

```bash
# requirements-dev.txt adds pytest to the runtime requirements.txt (the only file the image installs).
python3.12 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt   # Docker uses Python 3.12
# If `python3.12 -m venv` fails on ensurepip, `uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -r backend/requirements-dev.txt` works.

# Dev server. The defaults /models and /data are absolute paths, so override them locally.
# The download worker child process finds `app` on its own (downloads.PACKAGE_ROOT); PYTHONPATH=backend is harmless.
PYTHONPATH=backend MODEL_STORAGE=$PWD/models DATA_DIR=$PWD/data ACCOUNTS_ENABLED=true \
  .venv/bin/uvicorn app.main:app --app-dir backend --reload --port 7860
# The UI comes from backend/static or frontend/dist when index.html exists; the Vite dev server proxies /api to :7860.

# Tests on SQLite only: PostgreSQL tests skip
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests -q

# Tests on PostgreSQL 17, the production database
docker run -d --name hh-pg-test -e POSTGRES_DB=hugginghack_test -e POSTGRES_USER=hugginghack \
  -e POSTGRES_PASSWORD=test-only-password -p 55432:5432 postgres:17-alpine
PYTHONPATH=backend TEST_POSTGRES_URL=postgresql://hugginghack:test-only-password@127.0.0.1:55432/hugginghack_test \
  .venv/bin/python -m pytest backend/tests -q -rs

# One test, or a keyword
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_access.py::test_every_write_route_declares_who_may_call_it -q
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests -q -k content_security

# Copy SQLite into an empty PostgreSQL database (never modifies the source; target must have no users)
PYTHONPATH=backend DATABASE_URL=postgresql://... .venv/bin/python -m app.migrate_sqlite [--source FILE] [--target URL]
# In Docker: see the docstring at the top of app/migrate_sqlite.py
```

**Definition of done:** run the suite with `TEST_POSTGRES_URL` set, and with `git` and
`git-lfs` on PATH, until it reports **0 skipped**. Without Postgres, 41 tests skip. Without
git-lfs, the clone tests skip. SQLite passing alone is not enough. GitHub Actions does not run for this repository, so this local run is the gate.

## Module map (`backend/app`)

| File | Owns |
| --- | --- |
| `main.py` | The app. It builds module-level singletons at import (`database`, `auth`, `uploads`, `storages`, …) and holds middleware, every route, `requires()`/`personal()` auth dependencies and the startup `lifespan`. |
| `config.py` | `Settings` (every env var), `settings`, `validate_repo_id`, `validate_namespace`, `RESERVED_NAMESPACES`, `repository_path` (path-escape guard) |
| `database.py` | `Database`: all SQL for both backends, schema creation and migrations in `initialize()`, `VISIBLE_TO_USER`, `ORG_ROLE_ACTS`, `BIG_NUMBER_COLUMNS` |
| `permissions.py` | `CAPABILITIES`, `ROLE_CAPABILITIES` (viewer ⊂ member ⊂ admin), `can()`, `permission_matrix()` |
| `auth.py` | `AuthService`: scrypt passwords, sessions and CSRF tokens, `hht_` API tokens (stored as SHA-256), login throttle (in memory, or the `login_attempts` table in cluster mode), OIDC user provisioning |
| `cluster.py` | `Cluster`: leader election (session advisory lock on its own connection) and the per-server tick (heartbeats, cancels from other servers); `startup_problems` for `CLUSTER_MODE` |
| `oidc.py` | `OidcClient`: Authorization Code flow with PKCE. It fetches discovery lazily on the first SSO sign-in and validates ID tokens with PyJWT. |
| `hub_api.py` | Hub protocol: `HubRepositories` (`model`, `model_info`, `tree`, `resolve`), `HubError`, `local_entries`/`remote_entries`, `parse_range` |
| `git_mirror.py` | `GitMirrors`: pure-Python bare repos served over git's dumb HTTP. Weights become LFS pointers and are streamed from the library. Mirrors persist in the system folder; with several servers, `read_file` refreshes this server's mirror from there when git asks it for an object another server's `info/refs` named. |
| `storage.py` | `FilesystemModelStorage`, `S3ModelStorage` (boto3), `StorageRegistry` (local plus `STORAGE_TARGETS_JSON` targets), `StorageUnavailableError` |
| `uploads.py` | `UploadManager`: repo create/rename/delete, resumable chunk uploads, `finalize`, change sessions (`start_change` → `change_chunk` → `commit_change`), direct uploads to a bucket (`begin_*_file` → `*_part_links` → `complete_*_file`, `sweep_stale_uploads`), `can_manage`/`can_edit` |
| `indexer.py` | `LocalModelIndexer.scan`/`index_path`/`index_remote`, `hidden_path`, safetensors/GGUF header parsing (bytes only), `upload_is_registered` |
| `history.py` | `RepoHistory`: commit records per repository, with text blobs deduplicated by SHA-256 for diffs |
| `catalog.py` | Offline Models-tab search, facets, model card, GGUF ranges and assets (callers pass pre-filtered models) |
| `listing.py` | Upload listing preview, and validation of owner corrections to a listing (`model_listing` table) |
| `moves.py` | `MoveManager`: copy → verify hashes → switch → remove old copy after readers finish. Runs `recover()` at startup. |
| `reads.py` | `ReadTracker` read leases (`reads`), `LeasedResponse`, so moves never delete a copy that is being read |
| `system.py` | System folder `_system/` (avatars, git mirrors) on local disk or S3 (`SYSTEM_STORAGE_TARGET`) |
| `avatars.py` | Profile pictures. The type is sniffed from the magic bytes; only PNG, JPEG and WebP are kept. |
| `deploy_configs.py` | `ConfigRevisions`: immutable deployment-config revisions with editable results |
| `runtimes.py` | `RuntimeManager`: pushes blobs to Ollama, or calls the vLLM agent (`RUNTIME_TARGETS_JSON`) |
| `vllm_agent.py` | A **separate** FastAPI app that runs on the GPU host (`uvicorn app.vllm_agent:app`) and reads `VLLM_AGENT_*` env |
| `downloads.py` / `download_worker.py` / `hub_service.py` | Optional HF downloads (`HF_DOWNLOADS_ENABLED`). A thread pool runs `snapshot_download` in a child process so that cancel can kill it. |
| `migrate_sqlite.py` | One-shot SQLite → PostgreSQL copy (`TABLES` in foreign-key order) |

**Where things are in `main.py`**, in file order: bootstrap and `refresh_model_index`,
`lifespan`, then middleware (`reject_cross_site_writes`, `security_headers`, `AllowedHosts`),
request models, then the auth dependencies (`resolve_principal`, `require_user`,
`require_write_user`, `requires`, and aliases such as `Browser`, `Uploader`, `Editor`,
`StorageManager`, `UserAdmin`). The route groups follow:
- `/api/health`, then `/api/auth/*` (setup, login, OIDC)
- `/api/account/*` and `/api/admin/*`
- `/api/library/*` (details, files, commits, configs), `/api/repos/*` (rename, delete, change sessions)
- `/api/downloads`, `/api/local-models/*`, `/api/storage/*`
- `/api/runtimes`, `/api/collections`, `/api/saved-models`, `/api/organizations/*`, `/api/uploads/*`

Last come the **Hub routes** (`/api/models/{owner}/{name}…`, `/{owner}/{name}/resolve/…`), the
**git and LFS routes** (`/{owner}/{name}/info/refs`, `/objects/…`, `/info/lfs/…`) and the static mount at `/`.

## Invariants — do not break

**SQL must run on both SQLite and PostgreSQL**
- Write SQL with `?`, or with `:name` plus a dict. `_postgres_query` turns every `?` into
  `%s` and every `:name` into `%(name)s`, and psycopg always parses placeholders. Never put a
  literal `?`, `%`, or `:word` in SQL text. Pass them as parameters, e.g. LIKE patterns (see `search_users`).
- `executescript` on Postgres splits on `;`. Never put a `;` inside a DDL literal.
- Booleans are `INTEGER` 0/1 (`disabled = 0`). Timestamps are ISO-8601 UTC `TEXT`. JSON goes
  in `*_json` TEXT columns, which `_decode_row` decodes.
- Alias every aggregate (`COUNT(*) AS count`), because rows are read by key (`sqlite3.Row` / psycopg `dict_row`).
- Postgres `LIKE` is case-sensitive, so use `LOWER(col) LIKE LOWER(?)`. NULL sort order
  differs between the backends, so make it explicit when it matters. Upserts use `ON CONFLICT … DO UPDATE`.
- **Byte counts and anything that can exceed 2^31 must be `BIGINT`.** Postgres `INTEGER` is
  32-bit, so a 5 GB model fails with `integer out of range`. Every such column is listed in
  `BIG_NUMBER_COLUMNS`; `_widen_big_number_columns` upgrades older Postgres databases in
  `initialize()`. A new byte, offset, or parameter-count column goes in that list and gets a
  large-value round-trip test (see `test_accounts_db.py`).
- Schema changes happen in code, in `Database.initialize()`: `CREATE TABLE IF NOT EXISTS`,
  then `ALTER TABLE … ADD COLUMN` guarded by `_column_names()`. There is no migration tool. A
  CHECK-constraint change needs a table rebuild on SQLite and DROP/ADD CONSTRAINT on Postgres (see `_migrate_users`, `_migrate_visibility`).
- Postgres uses a `psycopg_pool.ConnectionPool` (`DATABASE_POOL_SIZE`, default 10, with a
  health check). SQLite opens a WAL connection per `connect()`. Leaving `with database.connect()` commits, or rolls back on an exception.
- **Several processes (CLUSTER_MODE).** A check-then-write rule (last admin, last acting org
  admin, names shared by users and organizations, the first owner) runs inside
  `Database._guarded()`: one transaction, the in-process `_write_lock`, and on PostgreSQL a
  `pg_advisory_xact_lock` on the same connection, so it holds across processes and needs no
  extra connection. Work that spans several transactions (a repository's commits in
  `history.py`, a git-mirror build, the library scan, schema setup) takes
  `Database.cluster_lock(key)`, a session advisory lock on a separate small pool. Never check
  in one `connect()` block and write in another without one of these. Per-process state that
  other servers must see goes in the database: the sign-in throttle (`login_attempts`, in
  cluster mode), the scan state (`cluster_state`, via `main.scan_state()`), jobs' and moves'
  `worker_id`/`heartbeat_at`/`cancel_requested`. The leader (`cluster.Cluster`, a session
  advisory lock on its own connection) runs moves, the startup scan and recovery; everything
  else runs on the server that got the request. `cluster.startup_problems` lists what cluster
  mode refuses. The Dockerfile runs one uvicorn process per pod; scale with replicas, not
  `--workers`. docs/SCALING.md, "Running several replicas", has the operator's view.

**Visibility**
- `database.VISIBLE_TO_USER` (takes the user id 3×) is the only definition. A user sees
  non-upload models, `public` uploads, their own personal uploads, org repos (all of an org's
  repos when the member has an `admin`/`write` row, otherwise only `organization`-visibility
  ones), and everything when they are an enabled server admin. An org `admin`/`write` row only
  counts when `ORG_ROLE_ACTS` holds (the account is enabled and its server role can create
  repositories), so a demoted Viewer keeps only what `read` gives. It is used by
  `list_visible_local_models` and `get_visible_local_model`.
- Anonymous pulls only get `get_public_local_model`: models that aren't uploads, plus
  `public` uploads. They get nothing when `HUB_API_ENABLED=false`.
- The Hub API and git share one gate, `HubRepositories.model()`. `GitMirrors.existing`/`ensure`
  call it, and `pull_user` in `main.py` resolves Bearer or Basic `hht_` tokens and sessions.
  A non-`hht_` token (such as a stale HF token) pulls anonymously. An unauthenticated miss
  answers 401 (git needs the challenge) and never reveals whether the repo exists.

**Permissions**
- Only use `requires("<capability>", write=…, session_only=…)` or `personal()` as route
  dependencies. `test_every_write_route_declares_who_may_call_it` fails on an unguarded
  write. `test_no_read_route_asks_for_the_write_security_token` fails when a GET depends on
  `require_write_user`.
- `main.effective_org_role`: a server Viewer's org role acts as `read`, and
  `check_org_role_fits` refuses to grant them more. `UploadManager.can_manage` covers
  rename, transfer, visibility and delete: `storage.manage`, or a repo admin who also has
  `repos.create`. `can_edit` needs `repos.edit_any`, or an admin/write role plus `repos.edit_own`.
- `main.model_for` strips `STORAGE_LOCATION_FIELDS` unless the user has `storage.view`.
  Use it for every model row returned to a client.
- `/api/health` has three tiers: anonymous callers get only status, app and version; signed-in
  users get UI limits; `settings.view` adds disk, database (`Database.ping`), S3 and HF details.
  The anonymous tier never touches the database once an account exists (`AuthService.setup_required`
  remembers it), so it answers while PostgreSQL is down.
- Last admin: `Database.update_user_guarded` and `delete_user` keep at least one enabled
  admin. `set_organization_member` and `remove_organization_member` keep one acting org
  admin (`ORG_ROLE_ACTS`: enabled and able to create repos) unless `force=True`.
- `AuthService.change_password` signs out other sessions and **revokes all API tokens**.
  Admin password reset (`admin_reset_password`) revokes them too.

**Hidden files:** `indexer.hidden_path` means dotted path parts, `__pycache__`, or
`*.hugginghack-part` / `*.hugginghack-s3-part`. It is the single rule for file counts
(`directory_stats`, `_reindex_remote`), listings (`main.listing_for`), pulls, and commit
records (`RepoHistory.durable_entries` → `hub_api.local_entries`/`remote_entries`). It is
mirrored in `frontend/src/uploadPlan.ts` `isRecorded`, so change both together.

**Storage and uploads**
- `S3ModelStorage.sync_repository` order: upload the objects, then the manifest
  (`.hugginghack.json`), then delete stale keys (the delete is best-effort). The manifest is
  never removed first, so a failed sync keeps the old version listed. `apply_changes` has the same order.
  Before uploading, both write `.hugginghack-pending.json` naming the new objects (`_begin_change`), and the
  manifest carries that change's id. Until a manifest with the same id is published, discovery, listings,
  restores and moves leave those objects out (`_repository_objects`), so a failed change is never adopted as
  a "Detected changes in storage" commit; the next change that succeeds deletes them (`_finish_change`).
  Objects added to a bucket by hand still count, as before.
- `UploadManager._apply_local_change` moves replaced or deleted files into
  `<session>.backup` and restores everything (manifest included) on any failure, so the
  same session can be committed again. `finalize` restores the unfinished manifest if the sync fails.
- `UploadManager._storage_errors` logs the redacted S3 error and raises
  `StorageUnavailableError` with a generic message. The routes (`commit_repository_change`,
  `create_upload_repository`, `finalize_upload_repository`) turn it into **502 with that
  message**. Never put raw boto errors in a response. Use `storage.redact()` in logs, and
  `S3ModelStorage.describe_error()` for a sentence people see (health, Storage page).
- Anywhere else, `StorageUnavailableError` and any botocore error become **503** with a fixed
  sentence (app exception handlers), and an unreachable PostgreSQL (`database_unreachable`:
  pool timeout, connection errors) becomes **503** "The database is not reachable."
- `S3ModelStorage` has three clients: `client` (patient retries: transfers, streaming GETs,
  manifests, commits), `read_client` (2 attempts, 5 s reads: listings, HEAD, small reads and the
  system folder, where someone is waiting) and `health_client`. The PostgreSQL pool waits
  `POOL_TIMEOUT_SECONDS` for a connection and its `check_connection` has a deadline, so a
  frozen database answers in seconds.
- Upload paths go through `validate_upload_path`, and repo paths through
  `config.repository_path`, which refuses to escape `MODEL_STORAGE`.
- **Direct downloads** (`direct_downloads` on a target): `main.repository_file_response`
  answers a `GET` for a file that exists only in such a bucket with `302` to
  `HubRepositories.direct_link` (`S3ModelStorage.presigned_get`, signed offline by
  `signing_client` for `public_endpoint_url`), plus `X-Linked-Size`/`X-Linked-Etag`. `HEAD`
  always answers `200` itself (a bucket refuses `HEAD` on a GET-signed link). A local copy
  wins and streams. The access check always runs first: never sign a link before it.
  `git_lfs_batch` hands out signed hrefs (`direct_lfs_links`, `authenticated: false`);
  `/api/library/file` redirects only above `DIRECT_LINK_MIN_BYTES` (8 MB), with the filename in
  the signed `response-content-disposition`. A move out of such a bucket keeps the old copy
  until `presign_ttl_seconds` after the switch (`MoveManager._links_outstanding`, from the
  `switched_at` column, so it survives restarts). Signed links never go into logs or
  responses other than the redirect: `redact()` strips `SIGNED_QUERY`.
- **Direct uploads** (`direct_uploads` on a target): one S3 multipart upload per file; the
  browser PUTs parts to `S3ModelStorage.presigned_part` links. `begin` answers `{"direct": false}`
  for other storage (the chunk path stays the fallback) and otherwise the parts the bucket has,
  read with `uploaded_parts` (`ListParts`); `complete` checks every part's size from the bucket,
  never from the browser. In-flight state is in the database only: `upload_targets` (an
  unfinished repository's bucket; no local folder is made for it), `direct_change_sessions`,
  `direct_uploads` (declared size is fixed per file). A new repository's files land at their
  final keys and exist only once `_finalize_direct` publishes the manifest
  (`publish_uploaded_repository`, which describes the model from a `_sketch`: small metadata
  files whole, weight headers by range). A change lands in `CHANGES_DIRECTORY`
  (`<repo>/.hugginghack-changes/<session>/`) and `apply_uploaded_changes` copies it into place
  with the same pending-record order as `apply_changes`. **`in_change_area` keys are never part
  of a repository**: `_begin_change`, `sync_repository`'s cleanup, `_repository_objects` (so
  listings, restores, `object_files` for moves) and `discover_repositories` all skip them; keep
  it that way in anything new that lists a prefix. `sweep_stale_uploads` (run by the library
  scan, and hourly by `main.sweep_abandoned_uploads_forever` on the leader or a single server, on
  its own thread so it never delays heartbeats) aborts uploads untouched for `STALE_CHANGE_SECONDS`. `main.CONTENT_SECURITY_POLICY`
  adds each direct-upload bucket's public origin to `connect-src` (`direct_upload_origins`).

**HTTP security (main.py)**
- `reject_cross_site_writes`: a POST/PUT/PATCH/DELETE carrying a foreign `Origin` gets 403.
  Allowed origins are the Host, `PUBLIC_URL`, `CORS_ORIGINS`, and `X-Forwarded-Host` only when
  `from_trusted_proxy` holds: the peer is in `FORWARDED_ALLOW_IPS` (`TRUSTED_PROXIES`, parsed as
  uvicorn does), or uvicorn already swapped the client for its `X-Forwarded-For` (port 0).
  Clients that send no Origin (git, hf, curl) pass.
- `security_headers` sets `CONTENT_SECURITY_POLICY`: `script-src 'self'`,
  `connect-src 'self'` plus only the public origins of buckets with `direct_uploads`
  (`content_security_policy(direct_upload_origins(storages))`), no other external origins
  (styles may be inline). The built UI must have no
  inline scripts and no CDN or external URLs. `nosniff`, `X-Frame-Options: DENY`, and `no-cache` for HTML.
- `AllowedHosts` enforces `ALLOWED_HOSTS` (loopback is always allowed). `/api/docs` and
  `/openapi.json` exist only with `API_DOCS_ENABLED` (Swagger comes from a CDN, so it won't render offline).
- `header_value` percent-encodes non-ASCII text in response headers. Use it for any
  user-derived header, such as `X-Error-Message` or `X-Repo-Commit`.
- Cookie sessions need `X-CSRF-Token` on writes (`require_write_user`). A Bearer token never
  uses the cookie, and a `read`-scope token cannot write. Cookies are `HttpOnly` and
  `SameSite=Lax`, with `Secure` from `secure_cookies()` (`SECURE_COOKIES`, or auto from the scheme and `X-Forwarded-Proto`).
- Login throttle: `AuthService.authenticate` allows 8 failures per account+IP and 30 per IP within 5 minutes. It never locks an account by name.

**Model files are data.** Never import, unpickle or execute anything from a model repo
(no `torch.load`, no `trust_remote_code`, no `yaml.load`). The indexer reads JSON and the
safetensors/GGUF headers with bounded sizes. Pickle-family files are only flagged (`UNSAFE_EXTENSIONS`).

## Air-gapped constraints

- At runtime, network calls go only to configured endpoints: S3 targets, the OIDC issuer
  (lazily), runtime targets, and huggingface.co or a mirror, and the last one only when
  `HF_DOWNLOADS_ENABLED=true` (`HubService`, `download_worker`). `HfApi` does no I/O at
  construction. Don't add telemetry, update checks, font or CDN fetches, or calls made at import time.
- The code does not set `HF_HUB_OFFLINE`. Compose sets `HF_HUB_DISABLE_TELEMETRY=1`.
  Clients pulling from HuggingHack must **not** set `HF_HUB_OFFLINE`, because it blocks fetching.
- The image is built on a connected machine and carried across (`docs/AIRGAPPED.md` §2), so
  every dependency must be installable from PyPI wheels at build time. Add to
  `requirements.txt` only when essential, pin it exactly, and prefer the stdlib. Test-only tools go in
  `requirements-dev.txt`. psycopg and boto3
  are imported lazily, so SQLite and filesystem installs keep working without them.
- A dead S3 bucket or IdP must never block startup or password login. The startup scan runs
  in the background, and a failing target is recorded in `storage_errors`. One exception is on purpose:
  an unknown `SYSTEM_STORAGE_TARGET` stops startup (`prepare_system_folder`).

## Recipes

**New endpoint**
1. Add a pydantic request model (with `Field` limits) and the route in `main.py` next to its
   group. Declare it **before** the `/{owner}/{name}/…` catch-alls and the static mount, and
   keep it under `/api/`. A new top-level segment also needs to go into `RESERVED_NAMESPACES`.
   Put `{repo_id:path}` routes with suffixes (`…/restore`) before the bare `{repo_id:path}` one.
2. Guard it with an existing alias (`Browser`, `Editor`, …) or `requires(...)`. Use `write=True` for mutations.
3. Filter data through `get_visible_local_model`/`visible_model` and `model_for`. Map domain
   errors with `change_errors` (403/404/409/400), and map `StorageUnavailableError` to 502.
4. Tests: copy the `server` fixture pattern in `tests/test_access.py`, which builds `Settings(...)`
   explicitly and monkeypatches `main.*`. Test each role and anonymous callers.
   Add frontend types in `frontend/src/types.ts` when the UI uses it.

**New column or table**
1. Add the column to the `CREATE TABLE` in `initialize()`, and also add a
   `_column_names`-guarded `ALTER TABLE … ADD COLUMN` there for existing databases
   (`parameter_count` is the example). Use `BIGINT` for large numbers.
2. Update the `Database` methods (insert/update column lists, `USER_FIELDS`/`*_FIELDS`
   allow-lists, `_decode_row` for `*_json`). For a new table, add it to
   `migrate_sqlite.TABLES` in foreign-key order.
3. Add a `test_postgres.py` case (skipped without `TEST_POSTGRES_URL`), and run the full suite on both backends.

**New permission**
1. Add it to `CAPABILITIES` and the right role set in `permissions.py`. Admin gets every capability automatically.
2. Create an alias with `requires("x.y", …)` in `main.py`. If a feature flag disables it, extend `disabled_capabilities()`.
3. The UI reads capabilities from `/api/auth/status`. Update `ADMIN_CAPABILITIES` in
   `frontend/src/access.tsx` if it is admin-only. Add role tests like `test_roles_follow_the_capability_table`.

## Settings (`config.py`; compose passes `.env` through `env_file`)

| Env var | Default | Purpose |
| --- | --- | --- |
| `MODEL_STORAGE` / `DATA_DIR` | `/models` / `/data` | Model folder (also the S3 cache and upload staging) / SQLite, git mirrors, avatars. Compose maps host `MODEL_STORAGE_PATH` → `/models`. |
| `MODEL_STORAGE_BACKEND` | `filesystem` | `s3` enables the legacy single bucket (`S3_*`, target id `s3`) |
| `DATABASE_URL` / `DATABASE_POOL_SIZE` | empty (SQLite `DATA_DIR/hugginghack.sqlite3`) / `10` (1–100) | `postgresql://…` selects Postgres |
| `ACCOUNTS_ENABLED` | `true` | `false` makes a single implicit admin `local`, with no CSRF or tokens |
| `SECURE_COOKIES` / `SESSION_TTL_HOURS` | auto / `720` | Cookie `Secure` flag / session lifetime |
| `HUB_API_ENABLED` | `true` | Anonymous Hub and git pulls of public models |
| `PUBLIC_URL` / `CORS_ORIGINS` / `ALLOWED_HOSTS` | empty | UI copy-paste base and allowed Origin / extra credentialed origins / Host allow-list |
| `API_DOCS_ENABLED` | `false` | `/api/docs`, `/openapi.json` |
| `HF_DOWNLOADS_ENABLED` / `HF_ENDPOINT` / `HF_TOKEN` | `false` / huggingface.co / empty | Server-side Hub downloads, off when air-gapped |
| `MAX_CONCURRENT_DOWNLOADS` / `DOWNLOAD_WORKERS_PER_JOB` | `2` / `4` | Download pool sizes |
| `UPLOAD_CHUNK_MB` / `MAX_UPLOAD_SIZE_GB` | `8` / `1024` | Upload chunking and cap |
| `S3_BUCKET` `S3_PREFIX`(`models`) `S3_ENDPOINT_URL` `S3_REGION` `S3_ACCESS_KEY_ID` `S3_SECRET_ACCESS_KEY` `S3_SESSION_TOKEN` `S3_USE_SSL`(`true`) `S3_VERIFY_SSL`(`true`) `S3_ADDRESSING_STYLE`(`auto`) `S3_STORAGE_CLASS` | — | Legacy single bucket. Keys can come from boto3's chain (`AWS_*`) instead. |
| `S3_MAX_CONCURRENCY` / `S3_MULTIPART_CHUNK_MB` | `4` / `64` | boto3 transfer tuning (applies to all targets) |
| `S3_CA_BUNDLE` `S3_DIRECT_DOWNLOADS`(`false`) `S3_PUBLIC_ENDPOINT_URL` `S3_PRESIGN_TTL_SECONDS`(`900`) | — | Private CA for the endpoint; direct downloads through signed links (per target: `ca_bundle`, `direct_downloads`, `public_endpoint_url`, `presign_ttl_seconds`) |
| `S3_DIRECT_UPLOADS`(`false`) `S3_PART_SIZE_MB`(`64`) `S3_UPLOAD_PRESIGN_TTL_SECONDS`(`3600`) | — | Direct uploads from the browser (per target: `direct_uploads`, `part_size_mb`, `upload_presign_ttl_seconds`); the bucket's CORS must allow `PUT` from `PUBLIC_URL` |
| `CLUSTER_MODE` / `INSTANCE_ID` | `false` / hostname | Several processes sharing one database (docs/SCALING.md phase 3) |
| `STORAGE_TARGETS_JSON` / `DEFAULT_STORAGE_TARGET` | `[]` / auto | Extra buckets (`parse_storage_targets`; credentials named via `access_key_env`/`secret_key_env`) / where new repos go |
| `SYSTEM_STORAGE_TARGET` / `SYSTEM_STORAGE_PREFIX` | `local` / `<prefix>/_system` | Where avatars and git mirrors live |
| `OIDC_ISSUER` `OIDC_CLIENT_ID` `OIDC_CLIENT_SECRET` | empty | SSO is on when accounts are enabled and issuer + client id are set |
| `OIDC_SCOPES` `OIDC_PROVIDER_NAME` `OIDC_DEFAULT_ROLE`(`viewer`) `OIDC_ALLOWED_GROUPS` `OIDC_GROUPS_CLAIM`(`groups`) `OIDC_USERNAME_CLAIM`(`preferred_username`) `OIDC_REDIRECT_URL` `OIDC_CA_BUNDLE` `OIDC_VERIFY_SSL`(`true`) | see config | SSO details |
| `RUNTIME_TARGETS_JSON` / `RUNTIME_WORKERS` / `RUNTIME_API_TOKEN` | `[]` / `2` / empty | Ollama and vLLM-agent targets / pool / bearer for the runtime endpoints |
| `APP_NAME` / `APP_VERSION` | HuggingHack / `1.3.0` | Shown in health and the UI |

Not in `config.py`: `FORWARDED_ALLOW_IPS` (read by uvicorn `--proxy-headers`, set it to the proxy IP, never `*`), `AWS_*`/`AWS_CA_BUNDLE` (boto3), and `VLLM_AGENT_*` (`vllm_agent.py`).

## Gotchas

- `Settings` defaults are evaluated **at import**, and `main.py` builds its singletons at
  import. Setting env vars after `import app.main` does nothing. Tests construct
  `Settings(...)` and monkeypatch `main.database`, `main.uploads` and so on.
- Postgres tests share one database, so use unique ids (`uuid4().hex`) and clean up. They don't get a fresh schema.
- Staging for change sessions is `MODEL_STORAGE/.hugginghack-staging`. The dotted name keeps it out of scans. A session younger than 24 h (`STALE_CHANGE_SECONDS`) blocks rename and delete. Direct change sessions have no staging folder (`_session` returns `None` for the root); they block the same way through `direct_change_sessions.updated_at`.
- `_storage_errors` turns any non-`ValueError` raised inside it into `StorageUnavailableError` (502). Raise your own `RuntimeError` (409) outside the block, as `_complete_direct` does.
- `add_direct_upload` stores `complete` as 0/1: PostgreSQL rejects a Python `bool` for an `INTEGER` column (SQLite does not), so the direct-upload tests run on both databases.
- Threads: downloads (`ThreadPoolExecutor` plus a subprocess), runtime jobs, the move worker, S3 transfers (`use_threads=True`) and sync routes in Starlette's threadpool. Shared state needs locks, and file reads must take a `reads` lease (`main.leased`, `reads.hold`).
- `/api/health` checks storage (an S3 `ListObjects`) only for callers with `settings.view`; anonymous calls, including the container health check, never touch S3.
- `ACCOUNTS_ENABLED=false` makes `verify_csrf` always true. The Origin check is then the only CSRF defence.
- User and org names are the repo namespace. `_system` and the `RESERVED_NAMESPACES` can never be names; new organizations also can't take `RESERVED_ORGANIZATION_NAMES` (adds `admin`, which accounts may keep).

## Pointers

`docs/AIRGAPPED.md` (install and operate offline), `docs/SERVE_FROM_S3.md` (S3 + PostgreSQL
deployment and migration), `docs/SCALING.md`, root `CLAUDE.md`, `frontend/CLAUDE.md`,
`helm/CLAUDE.md` (the Helm chart: the probes rely on `AllowedHosts` admitting loopback Host
headers, and settings reach pods only through `envFrom`), `.env.example`.
