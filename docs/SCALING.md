# Scaling plan: direct-to-bucket transfers and multiple replicas

Status: **phase 1 implemented; phases 2 and 3 planned.** Written 2026-09-26 against version 1.2.1. This is the
implementation brief for the production deployment: NetApp StorageGRID S3 behind a private CA,
a 100 GbE network, PostgreSQL, and HuggingHack pods deployed with a Helm chart. Implement the
phases in order; each one is shippable on its own and each later one assumes the earlier ones.

## 1. Why

Today every byte of every pull and upload passes through one Python process:

| Path | Today | Where in the code |
| --- | --- | --- |
| `resolve`, git-LFS objects, UI file reads | Streamed by the pod: `FileResponse` for local files, `iter_bytes` for bucket objects | `backend/app/main.py` `repository_file_response`, `git_lfs_download`; `hub_api.py` `iter_bytes` |
| git-LFS batch | `href` points git back at the pod's own object route | `main.py` `git_lfs_batch` |
| Uploads | Browser sends 8 MB chunks to the pod, which writes them to `MODEL_STORAGE/.hugginghack-staging` on the pod's disk; finalize re-uploads the folder to the bucket with boto3 | `main.py` `upload_file_chunk`, `uploads.py` `upload_chunk`/`finalize`, `storage.py` `sync_repository` |
| Changes to a repository | Same staging, then `apply_changes` copies to the bucket | `uploads.py` `change_chunk`/`commit_change` |

On server-grade hardware with a 100 GbE network and a fast bucket, the pod's NIC and the single
process are the bottleneck, and uploads cross it twice. Running more replicas of the current
code would not help much (bytes still cross a pod) and is unsafe (section 5). The plan removes
the pod from the byte path first, then makes the pod stateless enough to run several copies.

Target shape:

```
      pulls (hf, vLLM, Transformers, git-lfs, browser)      uploads (browser)
                     │                                             │
          1. HEAD/GET resolve ───► HuggingHack pods ◄─── 1. begin upload, ask for part URLs
          2. 302 + signed link     (N replicas, stateless)   2. signed part URLs
                     │                  │    │                     │
                     ▼                  │    │                     ▼
               NetApp S3 ◄──────────────┘    └────────────►  NetApp S3
               (bytes never touch a pod)
                                        │
                                   PostgreSQL
                    (index, accounts, sessions, locks, leases, jobs)
```

Pods keep doing access checks, metadata, and small reads (model cards, configs, GGUF header
ranges). Large bytes move client ↔ bucket directly.

## 2. Prerequisites from the environment

- **Clients can reach the bucket.** Every machine that pulls or uploads must reach the S3
  endpoint named in `public_endpoint_url` (DNS, firewall). Until now they only needed to reach
  HuggingHack.
- **Private CA.** The StorageGRID endpoint serves a certificate from an internal root CA.
  - Pods: mount the root CA (PEM) and point boto3 at it. Today only the boolean `S3_VERIFY_SSL`
    and boto3's `AWS_CA_BUNDLE` exist (`config.py:92`, `storage.py:359`; `SERVE_FROM_S3.md`
    §"TLS"). Phase 1 adds a per-target `ca_bundle` path so several targets can carry different
    CAs, and the Helm chart mounts the CA from a ConfigMap.
  - Pull clients: `huggingface_hub` (so `hf`, vLLM, Transformers) honours `REQUESTS_CA_BUNDLE`
    or `SSL_CERT_FILE`; git-lfs honours `git config http.sslCAInfo`; browsers need the CA in the
    OS trust store. Document these in GUIDE.md next to the `HF_ENDPOINT` instructions.
  - The host in the presigned URL must match a name on that certificate: `public_endpoint_url`
    is what gets signed, so set it to the certificate's name, not an IP.
- **StorageGRID features used:** presigned GET/PUT, multipart upload (`CreateMultipartUpload`,
  `UploadPart`, `ListParts`, `CompleteMultipartUpload`, `AbortMultipartUpload`),
  `CopyObject` and `UploadPartCopy`, bucket CORS. All confirmed available. Verify them in the
  first hour of Phase 1 with a short boto3 script against the real endpoint.
- **PostgreSQL** for anything beyond one replica (ground rule 1 already makes it the production
  database).

## 3. Phase 1: direct downloads (presigned redirects)

**Implemented.** Operator documentation: [SERVE_FROM_S3.md, "Direct downloads"](SERVE_FROM_S3.md#direct-downloads).
Verified end to end against MinIO with `snapshot_download`, `hf_hub_download` (with and without
a token on a private repository), `hf download`, `git clone` + git-lfs (token as password) and a
ranged `curl -L`: hashes match and the server answered every weight request with a redirect.
Deviations from the plan below, both decided after testing real clients:

- `HEAD` on `resolve` answers `200` with the metadata instead of `302`. A bucket refuses `HEAD`
  on a link signed for `GET`, which broke `curl -IL`-style probes; `huggingface_hub` and `hf`
  work with `HEAD` 200 + `GET` 302. The redirect carries `X-Linked-Size`/`X-Linked-Etag`.
- `direct_downloads` defaults to `false` (opt-in per target), so an existing S3 installation
  does not suddenly send clients to a bucket they may not reach.
- A move out of a direct bucket keeps the old copy for `presign_ttl_seconds` after the switch
  (not TTL + a maximum download time): a download that started before expiry continues, and a
  resume asks HuggingHack for a fresh link to the new location.

### Behaviour

- For a model whose storage target is a bucket, `GET`/`HEAD /{owner}/{name}/resolve/{revision}/{path}`
  answers `302 Found` with a presigned S3 GET URL instead of streaming. The access check
  (visibility, token, `HUB_API_ENABLED`, revision check) runs first, exactly as now; a link is
  signed only after it passes.
- The redirect carries the headers Hugging Face clients read on a redirect:
  `Location`, `X-Linked-Etag`, `X-Linked-Size`, `ETag`, `X-Repo-Commit`, `Accept-Ranges: bytes`.
  `huggingface_hub` does a HEAD with redirects disabled, takes size and ETag from
  `X-Linked-Size`/`X-Linked-Etag`, then GETs `Location` directly, with Range requests for
  resume. This is the shape the real Hub uses for its CDN, so `hf download`, vLLM and
  Transformers need no changes. `ETag` stays HuggingHack's own value (the file digest), never the
  bucket's ETag.
- git-LFS batch: each object's `actions.download.href` becomes the presigned URL,
  `authenticated: false`, no `header`, `expires_in` = the link TTL. git-lfs then pulls weights
  straight from the bucket. `info/refs` and packed objects stay on the pod (small).
- UI reads through `/api/library/file` redirect when the file is larger than 8 MB; smaller files
  (model cards, configs, `.gitattributes`) stay proxied so the page's CSP needs no change for
  viewing. `/api/library/gguf-range` and `/api/library/asset` stay proxied (bounded, small).
- Local-disk models keep streaming from the pod; nothing else can serve them.
- Multi-range `Range` requests keep answering 416 before any redirect (M1 fix).

### Configuration

`STORAGE_TARGETS_JSON` entries and the legacy `S3_*` target gain:

| Key | Default | Meaning |
| --- | --- | --- |
| `public_endpoint_url` | the target's `endpoint_url` | Endpoint clients can reach; used only for signing. Must match a name on the endpoint's certificate. |
| `direct_downloads` | `true` when the target has an endpoint | Redirect instead of streaming. |
| `presign_ttl_seconds` | `900` | Lifetime of a download link. |
| `ca_bundle` | unset | PEM file the pod uses to verify this endpoint (`verify=` in boto3). |

Legacy env names: `S3_PUBLIC_ENDPOINT_URL`, `S3_DIRECT_DOWNLOADS`, `S3_PRESIGN_TTL_SECONDS`,
`S3_CA_BUNDLE`. Add them to `.env.example`, `backend/CLAUDE.md`'s settings table and
`SERVE_FROM_S3.md`.

### Code

- `storage.py` `S3ModelStorage`: `presigned_get(repo_id, relative_path, ttl)` using
  `generate_presigned_url("get_object")` on a client built with `endpoint_url=public_endpoint_url`
  (a second client, since signing bakes the host in). `redact()` must also strip the query
  string of presigned URLs so they never reach logs or error texts.
- `main.py` `repository_file_response`: when the snapshot is bucket-backed and the target has
  `direct_downloads`, build the redirect; keep the streaming path otherwise.
- `main.py` `git_lfs_batch`: use the presigned URL for objects in bucket-backed repos.
- `main.py` `library_file`: size threshold, then the same redirect.
- Storage-move interplay: a link signed for a repository that then moves to another bucket
  keeps working until it expires (the old copy is removed after readers finish; extend the
  "readers" notion with the link TTL, see Phase 3).

### Security

- A presigned link is a bearer capability for its lifetime. Keep the TTL short (15 min default).
  A download in progress continues past expiry; a resume asks HuggingHack for a fresh link.
- Revoking a token or making a repository private stops new links immediately; an already
  issued link lives out its TTL. State this in GUIDE.md and AIRGAPPED.md's security section.
- Signing uses the pod's bucket key; the key never leaves the pod. Links carry no bucket
  credentials.
- Bucket policy: no anonymous listing or reads; only the pod's key has access. Presigned links
  are the only way in for clients.

### Tests (both databases)

- HEAD and GET `resolve` on a bucket-backed public model → 302 with all five headers, and the
  `Location` host is `public_endpoint_url`, not the pod's endpoint.
- Private model, anonymous or wrong token → `401 RepoNotFound` with no `Location` (no link is
  ever signed before the check).
- `HUB_API_ENABLED=false` → 401 anonymous, 302 with a token.
- LFS batch → `href` is presigned, `authenticated` false; a local-disk repo still gets the pod
  route.
- Local-disk model → unchanged streaming, byte-exact.
- Multi-range → 416.
- `redact()` removes the signature from an error containing a presigned URL.
- A real end-to-end run against MinIO (`docker run minio`, path-style) with `huggingface_hub`
  `snapshot_download`, `hf download`, and `git clone` + `git lfs pull`, hashes compared.
- Once against the real StorageGRID endpoint with the CA mounted.

### Done when

`hf download`, `snapshot_download`, `git clone` and the browser's Download button all fetch
bytes from the bucket, the pod's transfer volume for those pulls is only headers, and the suite
is green on PostgreSQL with 0 skipped and on SQLite.

## 4. Phase 2: direct uploads (multipart to the bucket)

### Flow

1. `POST /api/uploads/repositories/files/begin` `{repo_id, path, size}` → the pod creates an
   S3 multipart upload and records it in a new `upload_parts` table
   (`repo_id, path, upload_id, part_size, size, parts_received, created_at, updated_at`).
   Reuses the existing `Uploader` dependency and `uploads.access` checks.
2. `GET /api/uploads/repositories/files/parts?repo_id&path&from=N&count=16` → presigned
   `UploadPart` URLs for parts N…N+15 (TTL 1 h). Part size 64–128 MB, chosen from the file size
   so the count stays under S3's 10,000-part limit (128 MB × 10,000 = 1.25 TB, above
   `MAX_UPLOAD_SIZE_GB`).
3. The browser PUTs parts straight to the bucket, 4–8 in parallel, and reports each part's
   ETag with `POST …/files/parts/done`.
4. `POST …/files/complete` → the pod calls `CompleteMultipartUpload` with the ETags it recorded.
5. Resume after a reload or crash: the pod answers `…/files/status` from `ListParts`, so nothing
   depends on what the browser remembers. Abandoned uploads are aborted
   (`AbortMultipartUpload`) after `STALE_CHANGE_SECONDS`, same rule as staging today.

The same four routes exist for change sessions under `/api/repos/changes/{session_id}/files/…`.

### Where objects land

- **New repository (first upload):** parts go to the final keys under the repository prefix,
  protected by the pending record introduced by the interrupted-commit fix
  (`.hugginghack-pending.json`; `storage.py` `_begin_change`/`_unpublished`). Scans, listings,
  restores and moves ignore those keys until the manifest names the change. Finalize publishes
  the manifest. No copy, no staging.
- **Changes to an existing repository:** new and replaced files upload to
  `<repo prefix>/.hugginghack-changes/<change id>/<path>`, so the published bytes stay untouched
  while the change is in flight. Commit does server-side `CopyObject` (`UploadPartCopy` above
  5 GB) to the final keys, publishes the manifest, then deletes the change area. The copy runs
  inside the storage system; no pod bandwidth. Deletions are recorded in the manifest as now.
  `hidden_path` already hides any dotted folder, so `.hugginghack-changes` needs no new rule;
  add a test that says so.
- git-LFS pointers need each weight's SHA-256. The mirror builder already reads bucket-backed
  files from the bucket (`git_mirror.py` `_build` via `iter_bytes`), so nothing changes; the
  read happens once per file, on the pod, over the internal network.

### Frontend

- `frontend/src/api.ts`: `uploadResumable` grows a second engine, `uploadDirect`, chosen from
  the repository's storage target (`direct_uploads` in the `/api/uploads/namespaces` or
  repository payload; mirror the field in `types.ts`).
- `frontend/src/uploads.tsx`: parallel part workers (4–8), per-file progress from bytes
  acknowledged by the bucket, resume from `…/files/status`, the same interrupted-upload and
  lease behaviour as today. The upload dock and review step do not change.
- The chunk-to-pod path stays as the fallback for local-disk targets and small installs.

### Bucket and headers

- Bucket CORS must allow `PUT` from the `PUBLIC_URL` origin with `ETag` in `ExposeHeaders`.
  Ship a ready-made CORS policy in `SERVE_FROM_S3.md`.
- The pod adds each direct-upload target's public origin to `connect-src` in the
  Content-Security-Policy, only when `direct_uploads` is on. This is a deliberate change to a
  load-bearing header (ground rule 4): the test in `test_hardening.py` that asserts the CSP must
  cover both states.

### Configuration

Per target: `direct_uploads` (default `false` until the CORS policy is in place; the Storage
page shows "Direct uploads off: set CORS then enable"), `part_size_mb` (default `64`),
`upload_presign_ttl_seconds` (default `3600`).

### Tests

- Begin → parts → done → complete for a 3-part file against MinIO; `ListParts`-based resume
  after dropping one part; abort after the stale window.
- A change session that replaces a file: the published object is byte-identical until commit,
  then equals the new bytes; the change area is gone after commit; an aborted session leaves
  the repository untouched and the change area is removed.
- Interrupting the bucket mid-commit (pause MinIO, as the review did) still yields the
  documented 502 and an unchanged repository.
- CSP header with and without a direct-upload target.
- Frontend: node tests for part planning (sizes, counts, resume set); a Playwright run of the
  wizard and Upload changes against MinIO at 1280 and 390, light and dark.

### Done when

A multi-GB upload from a browser reaches the bucket at the client's NIC speed, survives a page
reload, and commits atomically; the pod's transfer volume for it is only control messages.

## 5. Phase 3: multiple replicas

**Status: 3a done (cluster infrastructure); 3b waits for phase 2.** Everything below except
the upload paths is implemented and tested (`backend/app/cluster.py`,
`backend/tests/test_cluster.py`, and a run of two real servers on one PostgreSQL database and
one bucket). Uploads and change sessions still stage files on the server that receives them, so
until phase 2 lands a cluster must not take uploads through more than one server
(`uploads.py` and the upload paths of `storage.py` get `Database.cluster_lock` there, and
cluster mode will then require `direct_uploads`).

### Running several replicas

Set on every server: `CLUSTER_MODE=true`, the same `DATABASE_URL` (PostgreSQL),
`DEFAULT_STORAGE_TARGET` and `SYSTEM_STORAGE_TARGET` naming buckets, and a distinct
`INSTANCE_ID` (the host name by default, which is the pod name under Kubernetes). A server
that cannot share the library refuses to start and logs one sentence per reason:

- `DATABASE_URL` is SQLite.
- New models would go to a server's own disk (`DEFAULT_STORAGE_TARGET` is `local`).
- Profile pictures and git history would stay on one server (`SYSTEM_STORAGE_TARGET` is `local`).
- A model is indexed on a server's own disk: move it to a bucket first (Admin → Storage).
- `HF_DOWNLOADS_ENABLED=true`: downloads are written to one server's disk.

Every server answers every request. One server, the **leader**, also does the work that must
happen once: it recovers interrupted moves when it is elected, scans the library at startup,
runs storage moves, and fails runtime jobs and takes over moves whose server stopped sending
heartbeats (30 s). Leadership is a session-level advisory lock on a connection of its own; when
the leader dies or loses the database, another server takes over within seconds (measured:
under a second after `kill -9`). `/api/health` for `settings.view` reports
`cluster: {enabled, instance_id, leader}`.

What each server does on its own, every 2 s: it sends a heartbeat for its runtime jobs and
moves (every 10 s), and stops the ones someone cancelled through another server. A cancel
marks the job cancelled at once, so every server shows it; the running server's progress
never overwrites it.

Differences from a single server:
- The local model cache is off: restore and evict answer 409 with a sentence and the buttons
  are hidden (the capability is turned off). Loading a model into a runtime needs that cache,
  so it is refused in cluster mode until runtimes read from the bucket.
- Moves go between buckets only. The old copy is kept for 24 h after the switch, because
  downloads other servers are sending cannot be counted from the leader.
- The sign-in throttle counts failures in the `login_attempts` table, so the limit is the same
  however many servers there are.
- The last scan's errors and conflicts, and whether a scan finished, live in `cluster_state`.
- Each server keeps git mirrors under its `DATA_DIR` as a cache and refreshes them from the
  system folder in the bucket before building, so every server serves the same commits. Give
  `/data` an `emptyDir`.

### Cluster mode

`CLUSTER_MODE=true` (Helm sets it when `replicaCount > 1`). Startup refuses, with one clear
sentence, unless: `DATABASE_URL` is PostgreSQL, every storage target is a bucket,
`SYSTEM_STORAGE_TARGET` is a bucket (git mirrors and avatars already persist there and restore
on demand: `git_mirror.py` `_persist`/`_restore`, `system.py`), and the local model cache is
off. A local-disk target cannot be shared between pods and is refused. Each pod gets an
`INSTANCE_ID` (default: hostname).

### What leaves process memory

| Today (one process) | Cluster mode |
| --- | --- |
| `database._write_lock` (SQLite) and the per-repo locks in `uploads`, `history`, `git_mirror`, `storage`, `moves` guarding invariants (last org admin, unique names, commit order, one mirror build at a time) | PostgreSQL advisory locks: `pg_advisory_xact_lock(hashtext('repo:' || repo_id))` inside the transaction that does the write; `SELECT … FOR UPDATE` where a row is the protected thing (organization on member changes, upload repository on finalize). SQLite keeps the in-process locks (single replica only). |
| Startup fails every unfinished runtime job and recovers every move (`lifespan`) | Runtime jobs and moves carry `worker_id` and `heartbeat_at`. A pod recovers only its own; the leader reclaims work whose heartbeat is older than 3 intervals. |
| Background work runs in every pod: startup scan, moves, runtime jobs, HF downloads | One **leader**, elected by holding a session-level advisory lock (`pg_try_advisory_lock`) on a dedicated connection; the others serve requests only. Loss of the connection releases the lock, so failover takes seconds. `/api/health` for `settings.view` reports `leader: true/false`. |
| `reads.ReadTracker` leases so a move never deletes a copy being read | Bucket→bucket moves delete the old copy after a grace period stored in the DB: `presign_ttl_seconds` + the longest download you allow (default 24 h, matching `STALE_LEASE_SECONDS`). No in-memory leases in cluster mode. |
| Login throttle in `auth._attempts` | `login_attempts` table (key, attempted_at), pruned on write; the limit is global. |
| Local model cache (`local_models.cached`, restore/evict routes, `.hugginghack-staging`) | Off in cluster mode; everything reads the bucket. Restore/Evict are hidden on the Storage page and answer 409 with a sentence. |
| First-owner setup lock `auth._setup_lock` | Advisory lock plus the existing unique index on `LOWER(username)`. |
| `uploads` in-memory declared upload lengths (L5 fix) | Column on `upload_parts`. |

Already safe across replicas: sessions, CSRF tokens, OIDC state (`oidc_states`), API tokens,
the frontend (static files), the startup `setup_required` cache (only ever flips to false).

### Deployment (Helm chart, built later with the user)

Values: `replicaCount`, image, `env` and `envFrom` secrets, a ConfigMap-mounted CA bundle wired
to `ca_bundle`/`OIDC_CA_BUNDLE`, `PUBLIC_URL`, ingress with the four forwarded headers and
`FORWARDED_ALLOW_IPS` set to the ingress's pod CIDR, readiness probe on `/api/health`,
`terminationGracePeriodSeconds: 60` (the compose `stop_grace_period`),
`PodDisruptionBudget` (`minAvailable: 1`), rolling update `maxUnavailable: 0`, `runAsUser: 1000`,
`readOnlyRootFilesystem` with an `emptyDir` at `/tmp` (`HF_HOME`). No persistent volume: in
cluster mode the pod writes nothing it needs to keep.

### Tests

- A fixture that starts two `app` instances against one PostgreSQL database (distinct
  `INSTANCE_ID`s) and runs the invariant tests concurrently across them: two demotions of the
  last admin → `[200, 409]`; two commits on one repository → two ordered commits; two mirror
  builds → one mirror; leader lock held by exactly one; leader failover when its connection is
  closed; a runtime job's heartbeat expiring → reclaimed once.
- Cluster-mode startup refusals, one per prerequisite.
- The whole existing suite still passes with `CLUSTER_MODE=false` on both databases.

### Done when

Two pods behind one Service pass the concurrent invariant tests, a rolling update of a 3-pod
deployment keeps `/api/health` answering throughout, and killing the leader hands background
work to another pod within a minute.

## 6. What each phase buys

| | Pull speed | Upload speed | Availability | Depends on |
| --- | --- | --- | --- | --- |
| Phase 1 | Bucket speed, bounded by the client NIC | unchanged | unchanged | CA and endpoint reachability |
| Phase 2 | — | Bucket speed, bounded by the client NIC | unchanged | Phase 1 config, bucket CORS |
| Phase 3 | Metadata and API scale with pods | — | Rolling updates, pod loss survived | Phases 1–2 (otherwise replicas still carry bytes) |

## 7. Out of scope

- `git push` (repositories stay read-only over git).
- A CDN or cache layer in front of the bucket.
- Object-storage moves between two different S3 systems at bucket speed (they stream through
  the leader pod; an admin operation, rare).
- Replacing uvicorn with multiple workers per pod: it has the same shared-state problems as
  replicas without the availability gain, so replicas are the answer.

## 8. Rules that still apply

Every ground rule in the root `CLAUDE.md`: PostgreSQL with 0 skipped and SQLite green, no
outbound request the operator did not configure (presigned links are issued, never fetched, by
the pod), model files are data, the security pieces change only deliberately and with tests,
backend and frontend shapes stay in sync (`types.ts`), and every touched screen works at 390 px
and 1280 px, light and dark, reduced motion.
