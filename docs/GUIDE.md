# HuggingHack guide

Everything HuggingHack does, in one place. The [README](../README.md) is the overview; the
[air-gapped setup guide](AIRGAPPED.md) and [Serve from S3 and PostgreSQL](SERVE_FROM_S3.md)
are step-by-step installs. Every setting named here is also listed, with its default, in
[`.env.example`](../.env.example).

- [Accounts and roles](#accounts-and-roles)
- [Uploads and repositories](#uploads-and-repositories)
- [Organizations](#organizations)
- [Model pages, commits, and configs](#model-pages-commits-and-configs)
- [Filtering the library](#filtering-the-library)
- [Storage](#storage)
- [PostgreSQL](#postgresql)
- [Pull models with vLLM, git, or the hf CLI](#pull-models-with-vllm-git-or-the-hf-cli)
- [Send models to Ollama or vLLM](#send-models-to-ollama-or-vllm)
- [Single sign-on (OpenID Connect)](#single-sign-on-openid-connect)
- [Hugging Face downloads on a connected server](#hugging-face-downloads-on-a-connected-server)
- [Run it on a NAS](#run-it-on-a-nas)
- [Security](#security)
- [Data ownership and backups](#data-ownership-and-backups)

## Accounts and roles

Accounts are local to this HuggingHack installation. There is no hosted identity service and
no account data leaves the server. Each account gets its own saved models, private notes,
collections, and API tokens. On the first visit HuggingHack asks you to create the owner
account; use a unique password of at least 12 characters.

Every account has one of three roles:

| Role | Can |
| --- | --- |
| **Viewer** | Browse, save, and pull models, and create personal read API tokens. |
| **Member** | Everything a viewer can, plus upload repositories, change their own repositories, rescan storage, and manage the S3 cache. |
| **Administrator** | Everything, including every repository (private ones too), storage, runtimes, server settings, and accounts. |

The server enforces these roles for the web interface, API tokens, and pulls alike; the full
matrix is under **Admin → Roles & permissions**.

**Account** (the gear icon or your name) holds:

- **Profile**: name, email, picture, and a password change for local accounts. Changing your
  password signs out your other sessions and revokes your API tokens; create new tokens where
  you still need them.
- **Security**: active sessions, each with its browser and device. Sign one out, or all
  others at once (both ask first).
- **API tokens**: a token acts as you from scripts and tools. Use it as `HF_TOKEN` for vLLM,
  Transformers, and the `hf` CLI, as the git password, or as `Authorization: Bearer hht_…`
  for the REST API. Read tokens can only browse and pull; write tokens can also upload, and
  only roles that can upload may create them. Tokens can never manage accounts or other
  tokens. A new token is shown once.
- **Preferences** that follow you across browsers: theme (light, dark, or match this device),
  default sort, and default upload location.

**Admin** (administrators only) lists every account with its role, status, last sign-in,
sessions, tokens, and repositories. Change roles, disable or enable accounts (disabling signs
them out immediately and stops their tokens), sign people out everywhere, or delete accounts
that own no repositories. Role changes, disabling, and deletion ask for confirmation first.
Click a name to open the account: its organizations and repositories, its sessions, and its
API tokens, each shown by its first characters (the full token is never stored) with a
button to revoke it. Local accounts also get a form to set a new password, which signs them
out everywhere and revokes their API tokens; administrators change their own password from
their profile instead. The last active administrator can never be demoted, disabled, or
deleted. The **Server** tab shows the configuration from `.env` without revealing secrets.

Use the heart on any model to save it. The **Saved** workspace organizes saved models into
collections, such as a project shortlist or a target rig, each with a private note per model.

To run as a single user on a trusted LAN, set `ACCOUNTS_ENABLED=false`. This creates one local
identity with administrator rights and skips sign-in. Do not use that mode on an untrusted
network.

## Uploads and repositories

The **Uploads** workspace creates repositories under your name or an organization you write to:

```text
models/
  your-username/
    your-repository/
      .hugginghack.json
      config.json
      model.safetensors
      ...
```

An upload has four steps: name the model, choose who can see it and where it is stored, drop
or pick the model folder, then review and upload.

- The folder check flags a missing `config.json`, tokenizer, safetensors weights, or model
  card, shows the detected precision, and skips `.git`, `.cache`, `__pycache__`, and system
  clutter, so a folder cloned from Hugging Face uploads as is.
- The review step shows how the library will list the model (task, precision, parameter
  count, library, license, and tags), read from `config.json`, the model card, and the weight
  headers by the same code the indexer runs after the upload. Correct any field that does not
  fit; corrections are saved as soon as the repository exists. Only those small files and the
  headers are read for this, never the weights.
- The repository is created only when the upload starts, and never over an existing model of
  the same name.
- Files travel in bounded chunks and continue in a panel at the bottom of the screen while you
  browse. An interrupted upload keeps its progress: choose the same folder again under
  **Unfinished uploads** to resume from the server's confirmed offset. Two tabs never take
  over each other's running uploads.
- Model files stay in model storage, never in the metadata database.

| Visibility | Who can see it |
| --- | --- |
| **Private** (default) | You, or for an organization repository its admins and writers, plus server administrators |
| **Organization** | Every member of the owning organization, plus server administrators |
| **Public** | Every account, plus anonymous pulls through the Hub protocol and `git clone` |

Changing visibility asks for confirmation and says who will be able to see the model.

Every model page has a **Settings** tab for the repository's admins, and for server
administrators on every model, including ones copied in by hand:

- **Visibility** and **description** of owned repositories.
- **Listing**: correct the task, precision, parameter count, library, license, or tags the
  library shows and filters by. Each correction sits next to what the files say and can be
  reset; rescans keep corrections and keep refreshing the detected values underneath.
- **Rename or transfer**: change the name or move the model to yourself or an organization you
  write to (administrators: any organization). Files, commit history, saves, and hardware tags
  move with it; old links, `vllm serve` names, and git remotes stop working, with no redirect.
  Giving an unowned model an owner registers it like an upload and keeps it **Public**. Models
  stored in S3 cannot be renamed yet.
- **Delete**, confirmed by typing the repository name.

## Organizations

Organizations are shared namespaces for teams and companies, so a model can live at
`Nvidia/GLM-5.3-NVFP4` without an `Nvidia` user account. Administrators create them under
**Admin → Organizations**; each organization then has its own members:

| Organization role | Can |
| --- | --- |
| **Read** | See and pull the organization's repositories with **Organization** visibility |
| **Write** | Also see **Private** ones, create repositories in the organization, and upload changes |
| **Admin** | Also manage members and settings, change visibility, and delete repositories |

- Organization roles add to the server role, and a server **Viewer** can only be given
  organization **Read**. A Viewer who kept **Write** or **Admin** from earlier can read but not
  upload, change settings, rename, or delete. Changing a repository's settings (rename,
  transfer, visibility, delete) also needs a server role that can create repositories.
- An organization always keeps an admin who can act: its last one cannot be demoted, removed,
  or leave, and cannot be deleted as an account. An admin role held by a disabled account or a
  Viewer does not count, and a server administrator can always appoint a new admin.
- Private and organization repositories stay inside the organization, including through API
  tokens and `git clone`.
- Organization names and usernames share one namespace (case-insensitive), so a user cannot
  take an organization's name or the reverse. `api`, `assets`, `static`, `orgs`, `models`,
  and `account` are reserved, and no organization can be called `admin`. Names taken before
  1.2.1 keep working.
- Repositories stay with the organization when the account that created them leaves or is
  deleted.

Browse every organization at `#/orgs`; pick the owner when creating a repository on **Uploads**.

## Model pages, commits, and configs

Every model has a page at `#/models/owner/name` with its rendered model card, a **Files and
versions** browser with per-file downloads, **Commits**, **Config**, and, for repositories
with GGUF files, a **GGUF** tab.

**Commits.** Each change is recorded as a commit with its author, message, and the files that
were added, modified, or deleted; text files such as `README.md` and `config.json` show line
diffs. Commits are created when you upload or change a repository, when a Hub download
finishes, and when a scan finds that files changed on disk or in a bucket. Hidden files such
as `.gitattributes` are stored but left out of file lists, counts, and commit records.
Anyone who may write to the repository can choose **Upload changes** to add, replace, or
delete files with a commit message. Changed files are staged and applied all at once, so
nobody pulling the model sees a half-uploaded change; if storage fails midway, the change is
rolled back and can be committed again. Weights of older commits are not kept; only the
newest version of each file can be downloaded.

**Config.** The files you deploy a model with (launch scripts, compose files, vLLM arguments)
are kept as numbered revisions, next to what each one achieved. A revision starts from the
latest files: upload or drop files and folders, edit or write files in place, remove some,
and describe the change. Its files never change afterwards, and each revision shows a diff
against the one before. Results stay editable, since you deploy first and measure second:
the test setup (hardware, GPUs, tensor parallel, vLLM version, concurrency, input and output
length), throughput, TTFT, TPOT and ITL, KV cache and reported max concurrency,
speculative-decoding acceptance rate and length, your own metrics, and notes. **Compare
results** lines up every measured revision and highlights the best value in each row.
**Download .zip** fetches one revision's files for the GPU host. Configs are stored in the
metadata database, never in the model's files, so pulling a model never pulls them. Anyone
who can see the model can read them, so keep tokens out; anyone who may upload changes to it
can add revisions and results.

**GGUF.** Select a file or shard to inspect its metadata, tensor names, shapes, data types,
quantization breakdown, and parameter count. Only bounded byte ranges of the selected file are
read, and every other shard stays untouched until you select it.

**Hardware.** Anyone who can upload changes to a repository can tag the GPUs it runs on from
the **Hardware** card on its page.

## Filtering the library

The **Models** page filters by:

- **Parameters:** a range slider. A model counts at the size in its name when the name gives
  one and roughly agrees with its weights (`Qwen3-8B-FP8` is 8B, even though it holds 8.19B
  parameters), and at its counted parameters otherwise. Both ends are inclusive.
- **Tasks:** Hugging Face task names (Text Generation, Image-Text-to-Text, Any-to-Any,
  Feature Extraction, Sentence Similarity, Text Ranking), followed by any other task your
  library has. The task comes from `pipeline_tag` in the model card.
- **Precision:** BF16, FP8 / INT8, or FP4 / INT4 (NVFP4, MXFP4, and INT4 together); formats
  of the same width share a filter, and each model keeps its exact label. It is read at scan time from `quantization_config` in
  `config.json`, from ModelOpt's `hf_quant_config.json`, and otherwise from the dtype. Packed
  4-bit weights count as two parameters per byte, so NVFP4 models show their real size.
- **Hardware:** L40, A100, RTX PRO 6000, and B300 tags.

Filters and sort live in the address, so a reload, the back button, or a shared link keeps
them. The model tree on a model page links to every quantization, fine-tune, adapter, and
merge of it in the library. Precision and sizes come from the index, which the server rebuilds
at startup; **Rescan library** (or **Admin → Storage → Scan storage**) refreshes it on demand.

## Storage

### The model folder

Set `MODEL_STORAGE_PATH` in `.env` to the host folder that should hold models. The container
sees it as `/models`, and managed repositories use a plain, portable hierarchy that works with
vLLM, llama.cpp, Ollama import, Transformers, and Diffusers:

```text
models/
  organization/
    repository/
      .hugginghack.json
      config.json
      model.safetensors
      ...
```

To add a model by hand, copy its folder anywhere within the first few directory levels of the
model folder, then choose **Rescan library**. HuggingHack recognizes common configs
(`config.json`, `model_index.json`, `tokenizer.json`) and weights (`.safetensors`, `.gguf`,
`.onnx`, `.bin`, `.pt`, `.pth`, `.ckpt`). Manually copied models are indexed but never modified.

### S3-compatible model storage

Set `MODEL_STORAGE_BACKEND=s3` to keep complete repositories in AWS S3 or an S3-compatible
service such as MinIO or Ceph. `/models` remains a local working cache because vLLM,
llama.cpp, and similar runtimes need filesystem paths.

```dotenv
MODEL_STORAGE_BACKEND=s3
MODEL_STORAGE_PATH=./models
S3_BUCKET=my-model-bucket
S3_PREFIX=models
S3_REGION=us-east-1
AWS_ACCESS_KEY_ID=replace-me
AWS_SECRET_ACCESS_KEY=replace-me

# MinIO or another custom endpoint:
S3_ENDPOINT_URL=http://minio:9000
S3_ADDRESSING_STYLE=path
S3_USE_SSL=false
```

- boto3's normal credential chain works too, including attached IAM roles, so static keys are
  optional on AWS. Credentials stay on the server and are never returned by the API.
- Syncs upload new files first, then publish the manifest, then remove stale objects, so a
  half-transferred repository is never indexed as complete. Bucket errors reach people as a
  short message; the details go to the server log.
- From a model's page you can remove a local cache copy while keeping the S3 copy, then
  restore it when a runtime needs the files.
- The bucket identity needs `s3:ListBucket` on the bucket and `s3:GetObject`,
  `s3:PutObject`, and `s3:DeleteObject` on the prefix.
- Back up the metadata database too: private upload manifests fail closed unless their
  ownership metadata is present.

### The Storage page and multiple buckets

Administrators get **Admin → Storage**, listing every location that holds models with its
connection status, capacity, and each model's size. Besides the local folder and the optional
`MODEL_STORAGE_BACKEND=s3` bucket, add as many buckets as you need:

```dotenv
STORAGE_TARGETS_JSON=[{"id":"minio-main","name":"MinIO models","bucket":"models","endpoint_url":"http://minio:9000","addressing_style":"path","use_ssl":false,"access_key_env":"MINIO_MAIN_KEY","secret_key_env":"MINIO_MAIN_SECRET"}]
MINIO_MAIN_KEY=replace-me
MINIO_MAIN_SECRET=replace-me
DEFAULT_STORAGE_TARGET=minio-main
```

- Target ids are permanent because models reference them. Credentials are read from the named
  environment variables and never returned by the API.
- Uploaders pick a target when creating a repository; otherwise `DEFAULT_STORAGE_TARGET` is used.
- Every location is open to every uploader until an administrator reserves it under **Who can
  upload → Reserve**. A user grant covers that user's personal repositories and an
  organization grant covers the organization's, so a bucket can be dedicated to one person or
  team. A reserved location is preselected for its owners and hidden from everyone else.
  Administrators can always use every location.
- If the same repository exists in two locations, the earlier target wins and the Storage page
  reports the conflict.

### Site data in S3

The site's own files, profile pictures and the git history behind `git clone` and `git pull`,
live in one system folder. `SYSTEM_STORAGE_TARGET=local` (the default) keeps it in `DATA_DIR`;
set it to an S3 target id to keep it in that bucket as `<prefix>/_system/`
(`SYSTEM_STORAGE_PREFIX` overrides the path). With PostgreSQL for the database and every model
in a bucket, the server keeps no lasting data of its own.

```text
_system/
  README.txt
  avatars/users/<user id>
  avatars/organizations/<organization id>
  git-mirrors/<owner>/<name>/
```

An unknown target id stops the server at start. Pictures already on local disk are copied into
the folder on the next start and removed locally only once the copy reads back. While a bucket
is unreachable, pictures fall back to initials and uploads of new ones are refused with a
message. The step-by-step setup is in [Serve from S3 and PostgreSQL](SERVE_FROM_S3.md).

### Moving a model to another location

Each model on the Storage page has a **Move** button. Choose the new location and type the
model's name to confirm; the move runs in the background and its row shows each step:

1. **Copy**: every file is copied while its SHA-256 is computed, into a hidden staging folder
   or, in S3, without a manifest, so no scan picks up a half-copied model.
2. **Verify**: every copied file is read back and must match its hash and size, or the move
   stops, removes the copy, and leaves the model where it was.
3. **Switch**: the model points at the new location in one step.
4. **Clean up**: the old copy is removed once every download that started from it has finished.

Pulls keep working throughout. A client that fixed the revision before the switch (as
`snapshot_download` and `vllm serve` do) keeps getting the same files, commit history records no
change, and git mirrors keep their commit. While a model moves it cannot be changed, renamed,
deleted, or loaded into a runtime, and one move runs at a time. A move can be cancelled until
it switches. After a restart, moves that had not switched are undone and those that had are
finished. Moving from local disk to a bucket removes the local copy unless **Keep a copy on
this server's disk** is ticked.

## PostgreSQL

SQLite is the zero-configuration default for a single machine. For a multi-user deployment,
set `DATABASE_URL`:

```dotenv
DATABASE_URL=postgresql://hugginghack:password@database-host:5432/hugginghack
DATABASE_POOL_SIZE=10
```

HuggingHack creates and upgrades its tables at startup and keeps a small connection pool. An
optional Compose overlay runs PostgreSQL 17 beside it; add `POSTGRES_PASSWORD` to `.env`, then:

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up --build -d
```

The overlay stores data in the `postgres-data` named volume. Back it up separately from `./data`.

**Move an existing SQLite installation.** `python -m app.migrate_sqlite` copies accounts,
sessions, tokens, saved models, repositories, commit history, and everything else into an
empty PostgreSQL database. It works on a copy, never changes the SQLite file, and refuses a
target that already has accounts, so run it before anyone opens the web interface.
When it finishes it keeps a copy of the source beside it, `hugginghack.sqlite3.migrated`;
like any backup it holds password and session hashes, so store it securely or delete it once
PostgreSQL works:

```bash
docker compose down
cp data/hugginghack.sqlite3 data/hugginghack.sqlite3.backup
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d postgres
docker compose -f docker-compose.yml -f docker-compose.postgres.yml run --rm hugginghack \
  python -m app.migrate_sqlite
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

## Pull models with vLLM, git, or the hf CLI

HuggingHack speaks the Hugging Face Hub protocol, so any machine on the network can pull a
model without internet access. Open a model and choose **Use this model** for copy-paste
commands. Links use the same form as the Hub: `#/models/owner/name?local-app=vllm` and
`#/models/owner/name?clone=true`.

```bash
# vLLM, Transformers, and the hf CLI all honor HF_ENDPOINT
export HF_ENDPOINT=http://NAS-IP:7860
vllm serve owner/model-name
hf download owner/model-name

# git clone with Git LFS for the weights
git lfs install
git clone http://NAS-IP:7860/owner/model-name
```

- Files stream from the model folder, or directly from S3 for S3-only models, with byte-range
  support for resumed and parallel downloads.
- `git clone` is served from a read-only mirror. Weights become Git LFS pointers whose SHA-256
  is computed once per file and cached, so the first clone of a large model waits while it is
  hashed. Weights are never copied into the mirror.
- A model that changes gets a new commit on top of the previous one, so `git pull` picks up
  the update.
- Pulls are read-only. Without a token they can read every model visible to all accounts; a
  personal API token also reaches the models its owner can see. Set `HUB_API_ENABLED=false` to
  require a token for every pull.
- The Hub API covers downloading: `snapshot_download`, `hf_hub_download`, `model_info`,
  `list_repo_files`, `list_repo_tree`, `repo_exists`/`file_exists`/`revision_exists`, and
  `hf download`. `HfApi.list_models()`, `list_repo_refs()`, and `list_repo_commits()` answer
  404; browse the library and its history in the web UI.
- Set `PUBLIC_URL=http://NAS-IP:7860` when the address in your browser (for example
  `localhost`) is not the one other machines use.

## Send models to Ollama or vLLM

HuggingHack can hand a cached model to another inference machine on the network. Destinations
are configured on the server, so endpoints and credentials are never typed in a browser.
Administrators open a model and choose **Send to runtime**; automation can use the same API.

```dotenv
RUNTIME_TARGETS_JSON=[{"id":"ollama-rig","name":"Ollama GPU","kind":"ollama","base_url":"http://192.168.0.36:11434","keep_alive":"15m"},{"id":"vllm-rig","name":"vLLM GPU","kind":"vllm","base_url":"http://192.168.0.35:8090","remote_model_root":"/mnt/nas/models","token_env":"VLLM_AGENT_TOKEN"}]
RUNTIME_WORKERS=2
VLLM_AGENT_TOKEN=replace-with-the-same-long-random-secret-used-on-the-agent
```

- **Ollama** uses its native blob and create APIs. HuggingHack hashes each required file, skips
  blobs the server already has, transfers the rest over HTTP, creates the model, and preloads
  it for `keep_alive`. A repository needs one selected GGUF or a root-level SafeTensors model
  that Ollama supports.
- **vLLM** reads the existing NAS files instead of copying them. Mount the model folder on the
  vLLM machine and set `remote_model_root` to that mount path. vLLM fixes its model at startup,
  so the agent stops the process it manages and starts `vllm serve` with the selected model;
  requests in flight are interrupted during a switch.
- S3-only models must be restored to the local cache first.

The job's progress shows on the model page and on **Admin → Runtimes**.

**The vLLM agent.** On the vLLM machine, mount the same model share and run the small manager
from this repository. The token is mandatory and must match the one forwarded to HuggingHack:

```bash
export VLLM_AGENT_TOKEN='replace-with-a-long-random-secret'
export VLLM_AGENT_MODEL_ROOT=/mnt/nas/models
export VLLM_AGENT_VLLM_PORT=8000
export VLLM_AGENT_EXTRA_ARGS_JSON='["--gpu-memory-utilization","0.9"]'
python -m uvicorn app.vllm_agent:app --app-dir backend --host 0.0.0.0 --port 8090
```

The agent never accepts a shell command or an arbitrary path. It only starts `vllm serve` for a
directory inside `VLLM_AGENT_MODEL_ROOT`, with extra arguments fixed by
`VLLM_AGENT_EXTRA_ARGS_JSON`. Don't run another vLLM server on the agent's vLLM port.

**Runtime API.** Set `RUNTIME_API_TOKEN` to allow bearer-token automation scoped to runtime
targets, loads, and job history:

```bash
curl -X POST http://NAS-IP:7860/api/runtimes/ollama-rig/load \
  -H "Authorization: Bearer $RUNTIME_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"repo_id":"bartowski/Qwen2.5-7B-Instruct-GGUF","runtime_model_name":"qwen-local","source_file":"Qwen2.5-7B-Instruct-Q4_K_M.gguf"}'
```

The response is a persistent job: read it at `GET /api/runtime-jobs/{job_id}`, list history at
`GET /api/runtime-jobs`, and discover destinations at `GET /api/runtimes`. The interactive API
reference at `/api/docs` is off by default; set `API_DOCS_ENABLED=true` to turn it on (it
loads Swagger UI from a CDN, so it needs internet access in the browser).

## Single sign-on (OpenID Connect)

HuggingHack signs people in through any OpenID Connect provider (Authentik, Keycloak,
Microsoft Entra ID, Okta, Dex, and others) with the Authorization Code flow and PKCE.
Passwords keep working alongside it, so a local administrator can always get in.

```dotenv
PUBLIC_URL=https://hugginghack.example.internal
OIDC_ISSUER=https://authentik.example.internal/application/o/hugginghack/
OIDC_CLIENT_ID=from-your-provider
OIDC_CLIENT_SECRET=from-your-provider
OIDC_PROVIDER_NAME=Authentik
OIDC_DEFAULT_ROLE=viewer
```

- Register `PUBLIC_URL` + `/api/auth/oidc/callback` as the redirect URI with the provider.
- The first sign-in creates an account with `OIDC_DEFAULT_ROLE`; administrators change roles
  under **Admin → Users**. Accounts are matched by the provider's subject id, never by username
  or email, so single sign-on cannot take over a local account.
- `OIDC_ALLOWED_GROUPS` limits who may sign in. To block one person, disable their account;
  deleting it only lasts until their next sign-in.
- The provider owns the name, email, and password of these accounts; they update at each
  sign-in. Roles and disabling stay in HuggingHack.
- The owner account is always created with a password on first run.
- The step-by-step Authentik setup is in the
  [air-gapped setup guide](AIRGAPPED.md#5a-single-sign-on-with-authentik).

## Hugging Face downloads on a connected server

Air-gapped servers never reach Hugging Face: `HF_DOWNLOADS_ENABLED=false` is the default and
models arrive by upload. On a server with internet access, set `HF_DOWNLOADS_ENABLED=true` and
start downloads with `POST /api/downloads` (members and administrators, or a write API token).
The `mode` field takes:

- **Full repository**: every file in the selected revision.
- **SafeTensors**: safe weights plus configuration and tokenizer files.
- **One GGUF**: a single quantization from the repository file list.
- **Metadata only**: configuration, tokenizer, and documentation files without weights.
- **Custom**: comma-separated include and exclude patterns, using Hugging Face's
  `snapshot_download` filtering, for example `*.safetensors, *.json, tokenizer*` or
  `*Q4_K_M.gguf, *.json, tokenizer*`.

Cancelling stops the isolated download worker but keeps the files already transferred, so
starting the same repository again reuses them.

**Gated and private Hugging Face models.** Accept the repository's terms on Hugging Face, create
a read-only access token, and set `HF_TOKEN` in `.env`. The token is read only by the backend
and never sent to the browser. A downloaded model has no owner, so only administrators may
download repositories that are private or gated on Hugging Face.

## Run it on a NAS

Copy the whole `HuggingHack` folder to the NAS, then change only `MODEL_STORAGE_PATH` in `.env`.
The model folder and the project's `data` folder must exist before the container starts;
Synology Container Manager does not always create missing bind-mount sources:

```bash
mkdir -p /volume1/docker/HuggingHack/models /volume1/docker/HuggingHack/data
```

```dotenv
# Synology
MODEL_STORAGE_PATH=/volume1/AI/models
# TrueNAS
MODEL_STORAGE_PATH=/mnt/tank/ai/models
# QNAP
MODEL_STORAGE_PATH=/share/Container/models

# Run as the NAS user that owns the folders (find the ids with `id your-nas-user`):
PUID=1026
PGID=100
```

With `PUID`/`PGID` set, that user must own the model folder and `data`
(`sudo chown -R 1026:100 /volume1/AI/models /volume1/docker/HuggingHack/data`), or the server
cannot write to them. See [the air-gapped guide](AIRGAPPED.md#3-configure).

Then run `docker compose up --build -d` from the project folder and open `http://NAS-IP:7860`
from another computer on the LAN.

## Security

**Model files are data.** HuggingHack never executes repository code, imports model modules,
or deserializes weights. Pickle-compatible formats can execute code when other applications
load them; prefer SafeTensors or GGUF and load models only from publishers you trust.

**Accounts and sessions**

- Passwords are salted and hashed with `scrypt`. Sessions are hashed random tokens in
  HTTP-only, SameSite cookies, and state-changing requests carry a per-session CSRF token.
- Failed sign-ins are throttled per account and per address. Behind a reverse proxy, set
  `FORWARDED_ALLOW_IPS` to the proxy's address (never `*`) so the real client address is used.
- API tokens are stored only as SHA-256 hashes and shown once. Changing or resetting a
  password revokes them, and disabling an account stops them immediately.

**The web interface**

- Every page is served with a strict Content-Security-Policy: its own scripts and styles only,
  no outside images or connections. Model cards are rendered as sanitized Markdown; scripts,
  forms, frames, and outside images are dropped.
- Writes whose `Origin` names another site are refused, so other web pages cannot act on the
  server, even with `ACCOUNTS_ENABLED=false`. Tools such as git, `hf`, and curl send no
  `Origin` and are unaffected. `CORS_ORIGINS` (empty by default) lists extra origins allowed
  to call the API with cookies.
- Set `ALLOWED_HOSTS` to the names people use to reach the server (for example
  `hugginghack.lan,10.0.0.5`) to block DNS-rebinding attacks. Loopback names always pass.
- `/api/health` tells anonymous callers only whether the server is up; storage paths, buckets,
  and other server details need **View server configuration**. `/api/docs` is off unless
  `API_DOCS_ENABLED=true`.

**The network**

- Public exposure needs HTTPS. Put HuggingHack behind a TLS reverse proxy such as Caddy,
  Traefik, or Nginx Proxy Manager. With `SECURE_COOKIES=auto` (the default), cookies are marked
  Secure whenever the request arrives over HTTPS, directly or with `X-Forwarded-Proto: https`.
- Hub-protocol pulls are read-only and allow anonymous reads of models every account can see.
  Keep HuggingHack on a trusted network, or set `HUB_API_ENABLED=false` to require tokens.
- Runtime dispatch is for administrators. Optional bearer access is limited to runtime
  endpoints; use long random tokens and firewall Ollama and the vLLM agent to trusted clients.
- Upload paths are confined to repositories the account may write to. Deleting a repository
  requires its exact name.

The full list of security settings is in the
[air-gapped setup guide](AIRGAPPED.md#security-settings).

## Data ownership and backups

- **Models:** the host path in `MODEL_STORAGE_PATH`, and in S3 mode the objects in the bucket.
- **Metadata:** accounts, sessions, saved models, collections, repository ownership, commit
  history, configs, and the index, in `./data/hugginghack.sqlite3` or the database named by
  `DATABASE_URL`.
- **Site data:** profile pictures and git mirrors in `./data`, or in the bucket with
  `SYSTEM_STORAGE_TARGET`.

Back up models and the metadata database together: step by step for folders and SQLite in the
[air-gapped setup guide](AIRGAPPED.md#back-up-and-restore), and for PostgreSQL and buckets in
[Serve from S3 and PostgreSQL](SERVE_FROM_S3.md#10-back-up-restore-and-upgrade). The index can be rebuilt from the model
files, but the database holds accounts, ownership, saved models, configs, and history. Store
backups securely: they contain password hashes and session hashes.
