<p align="center">
  <img src="frontend/public/hugginghack-mark.svg" width="92" alt="HuggingHack terminal-face mark">
</p>

<h1 align="center">HuggingHack</h1>

<p align="center">
  <strong>Bring the Hugging Face Hub home.</strong><br>
  Browse live models, choose exactly which files to keep, and build a clean local library on your PC or NAS.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/React-18-20232A?logo=react&logoColor=61DAFB" alt="React 18">
  <img src="https://img.shields.io/badge/TypeScript-5.7-3178C6?logo=typescript&logoColor=white" alt="TypeScript 5.7">
  <img src="https://img.shields.io/badge/FastAPI-0.116-009688?logo=fastapi&logoColor=white" alt="FastAPI 0.116">
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <img src="https://img.shields.io/badge/self--hosted-NAS%20ready-F59E0B" alt="Self-hosted and NAS ready">
  <a href="https://github.com/tyedalwaves/HuggingHack/actions/workflows/ci.yml"><img src="https://github.com/tyedalwaves/HuggingHack/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
</p>

<p align="center">
  <a href="#features">Features</a> ·
  <a href="#screenshots">Screenshots</a> ·
  <a href="#quick-start-on-this-pc">Quick start</a> ·
  <a href="#move-it-to-the-nas">NAS setup</a> ·
  <a href="#security">Security</a>
</p>

![HuggingHack model catalog showing live model cards, filters, search, and download actions](docs/images/models-catalog.png)

<p align="center"><sub>Live Hub discovery with practical metadata, storage-aware downloads, and no cloud dashboard in the middle.</sub></p>

> [!NOTE]
> HuggingHack is an unofficial, local-first project. It is not affiliated with or endorsed by Hugging Face.

> [!TIP]
> Running without internet access? Follow the [air-gapped setup guide](docs/AIRGAPPED.md)
> to install HuggingHack offline, load models, and pull them with vLLM, `git clone`, or the `hf` CLI.

<table>
  <tr>
    <td width="33%" valign="top"><strong>🔎 Discover</strong><br>Search the live model catalog and narrow it by task, format, local app, parameter count, or popularity.</td>
    <td width="33%" valign="top"><strong>🎯 Download precisely</strong><br>Keep a full repository, SafeTensors, one GGUF, metadata only, or your own include and exclude patterns.</td>
    <td width="33%" valign="top"><strong>🏠 Own the library</strong><br>Store models in a plain folder on your disk or NAS and index files you copied there yourself.</td>
  </tr>
</table>

## Features

- Familiar Hub-style model catalog with visual, metadata-driven model cards plus task, format, local-app, parameter, and sort filters
- Richly rendered model cards, repository file lists, and commit history from your own library
- On-demand GGUF metadata and tensor inspection with shard position, names, shapes, data types, and parameter totals
- Optional server-side Hugging Face downloads (API only), off by default so an air-gapped server never tries to reach the internet; see [File filtering](#file-filtering)
- Automatic local-library indexing with model size, file count, config metadata, and unsafe serialization warnings
- Built-in local accounts with a first-run owner, HTTP-only sessions, and administrator-created member accounts
- Per-account saved models, private notes, and project or rig collections
- Private or locally shared user repositories with resumable, chunked model-folder uploads
- Optional S3-compatible durable storage with a local working cache, remote browsing, restore, and cache eviction
- Network runtime jobs: transfer models to Ollama or switch a remote vLLM rig through an authenticated manager
- Offline Hub protocol: point vLLM, Transformers, or the `hf` CLI at `HF_ENDPOINT`, or `git clone` with Git LFS, straight from the library
- Ownership-verified repository deletion with exact-name confirmation
- Optional read-only `HF_TOKEN` support for private and gated models
- Light/dark themes and responsive desktop/mobile layouts
- One Docker Compose service with persistent model and application-data mounts

## Screenshots

The catalog above is the main workspace. Open any model to inspect its repository, estimate
storage, and choose the exact download mode without leaving the app. Local accounts add
private shortlists, notes, collections, and repositories without turning HuggingHack into a
hosted service.

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="docs/images/account-setup.png" alt="HuggingHack first-run owner account setup">
    </td>
    <td width="50%" valign="top">
      <img src="docs/images/saved-library.png" alt="HuggingHack saved model library with collections and private notes">
    </td>
  </tr>
  <tr>
    <td align="center"><sub>One-time local owner setup with no hosted identity service</sub></td>
    <td align="center"><sub>Per-account model shortlists, collections, and private notes</sub></td>
  </tr>
</table>

![HuggingHack account repositories with private and shared model uploads](docs/images/account-uploads.png)

<p align="center"><sub>Resumable model-folder uploads into private or locally shared repositories on the mounted drive</sub></p>

<table>
  <tr>
    <td width="68%" valign="top">
      <img src="docs/images/model-details-dark.png" alt="HuggingHack dark-theme model details and download options">
    </td>
    <td width="32%" valign="top">
      <img src="docs/images/mobile-catalog.png" alt="HuggingHack responsive mobile model catalog">
    </td>
  </tr>
  <tr>
    <td align="center"><sub>Repository details and file-aware download controls in dark mode</sub></td>
    <td align="center"><sub>The same live catalog on mobile</sub></td>
  </tr>
</table>

## Quick start on this PC

1. Install and start Docker Desktop.
2. Double-click **Start HuggingHack.bat**.
3. Open [http://localhost:7860](http://localhost:7860).

The first launch builds the container. Later launches reuse the image unless the project changes.
On the first browser visit, HuggingHack asks you to create the owner account. Use a unique
password of at least 12 characters. The owner can add accounts and choose their roles from **Admin → Users**.

Command-line equivalent:

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

Stop it with **Stop HuggingHack.bat** or:

```powershell
docker compose down
```

Models and the default SQLite database are persistent and are not removed by
`docker compose down`.

## Use PostgreSQL

SQLite remains the zero-configuration default. For a multi-user deployment or an external
database service, set `DATABASE_URL` to a PostgreSQL connection URL:

```dotenv
DATABASE_URL=postgresql://hugginghack:password@database-host:5432/hugginghack
```

HuggingHack creates and upgrades its tables at startup. PostgreSQL credentials stay on the
server and are not returned by the API.

An optional Compose overlay runs PostgreSQL 17 beside HuggingHack. Add a long URL-safe
password to `.env`, then start both services:

```dotenv
POSTGRES_PASSWORD=replace-with-a-long-random-password
```

```powershell
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up --build -d
```

The overlay stores PostgreSQL data in the `postgres-data` named volume and waits for the
database health check before starting HuggingHack. Back it up separately from `./data`.

### Move an existing SQLite installation to PostgreSQL

`python -m app.migrate_sqlite` copies accounts, sessions, tokens, saved models, repositories,
commit history, and everything else into an empty PostgreSQL database. It works on a copy,
so the SQLite file is never changed, and it refuses a target that already has accounts.

```bash
docker compose down
cp data/hugginghack.sqlite3 data/hugginghack.sqlite3.backup
# Add POSTGRES_PASSWORD to .env, then start only the database:
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d postgres
# Copy the data before HuggingHack starts on PostgreSQL:
docker compose -f docker-compose.yml -f docker-compose.postgres.yml run --rm hugginghack \
  python -m app.migrate_sqlite
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

Run the copy before anyone opens the web interface on PostgreSQL; otherwise the first-run
setup creates an owner account and the copy refuses to overwrite it.

## Accounts, saved models, and uploads

Accounts are local to this HuggingHack installation—there is no hosted identity service and
no account data leaves the server. Each account gets a separate saved-model library, private
notes, collections, and download history.

Every account has one of three roles:

| Role | Can |
| --- | --- |
| **Viewer** | Browse, save, and pull models, and create personal API tokens. |
| **Member** | Everything a viewer can, plus upload repositories, change their own repositories, rescan storage, and manage the S3 cache. |
| **Administrator** | Everything, including other people's repositories, storage, runtimes, server settings, and accounts. |

The server enforces these roles for the web interface, API tokens, and pulls alike; the full
matrix is under **Admin → Roles & permissions**.

**Account** (the gear icon or your name) holds your profile, password and active sessions
(sign out other browsers), preferences that follow you across browsers (theme, default sort,
default upload location), and **API tokens**. A token acts as you from scripts and tools:
use it as `HF_TOKEN` for vLLM, Transformers, and the `hf` CLI, as the git password, or as
`Authorization: Bearer hht_…` for the REST API. Read tokens can only browse and pull; write
tokens can also upload. Tokens can never manage accounts or other tokens.

**Admin** (administrators only) lists every account with its role, status, last sign-in,
sessions, tokens, and repositories. Change roles, disable or enable accounts (which signs
them out immediately), reset passwords, sign people out everywhere, or delete accounts that
own no repositories. The last active administrator can never be demoted, disabled, or
deleted. The **Server** tab shows the configuration from `.env` without revealing secrets.

Use the heart on a Hub model to save it without downloading. The **Saved** workspace can
organize those models into multiple collections, such as a project shortlist or a target rig.

The **Uploads** workspace creates repositories under the signed-in owner name:

```text
models/
  your-username/
    your-repository/
      .hugginghack.json
      config.json
      model.safetensors
      ...
```

Choose a model folder in the browser and HuggingHack sends each file in bounded chunks.
Interrupted uploads keep their progress and resume from the server's confirmed offset.
Uploaded repositories are private by default; their owner can share them with every local
account. Model files stay in the model mount rather than in the metadata database.

To preserve the original trusted-LAN behavior, set `ACCOUNTS_ENABLED=false`. This creates a
single local compatibility identity and skips sign-in. Do not use that mode on an untrusted
network.

## Choose the model folder

Edit `.env` and set `MODEL_STORAGE_PATH` to the host folder that should contain models:

```dotenv
MODEL_STORAGE_PATH=./models
```

The container sees this folder as `/models`. Managed repositories are stored in a plain hierarchy:

```text
models/
  organization/
    repository/
      .hugginghack.json
      config.json
      model.safetensors
      ...
```

That layout is portable and works with vLLM, llama.cpp, Ollama import workflows, Transformers, Diffusers, and other tools that accept a local repository path.

## Use S3-compatible model storage

Set `MODEL_STORAGE_BACKEND=s3` to keep complete managed repositories in AWS S3 or an
S3-compatible service such as MinIO or Ceph. `/models` remains a local working cache because
vLLM, llama.cpp, and similar runtimes require filesystem paths.

```dotenv
MODEL_STORAGE_BACKEND=s3
MODEL_STORAGE_PATH=./models
S3_BUCKET=my-model-bucket
S3_PREFIX=models
S3_REGION=us-east-1
AWS_ACCESS_KEY_ID=replace-me
AWS_SECRET_ACCESS_KEY=replace-me
```

For MinIO or another custom endpoint:

```dotenv
S3_ENDPOINT_URL=http://minio:9000
S3_ADDRESSING_STYLE=path
S3_USE_SSL=false
```

HuggingHack also supports boto3's normal credential chain, including attached IAM roles, so
static keys are optional on AWS. Credentials stay server-side and are never returned by the API.
Downloads and finalized browser uploads sync automatically. The manifest is published last, so
partially transferred repositories are not indexed as complete. From a model's page you can
remove a local cache copy while keeping its durable S3 copy, then restore it when an inference
runtime needs the files.

The bucket identity needs `s3:ListBucket` on the bucket and `s3:GetObject`,
`s3:PutObject`, and `s3:DeleteObject` on the configured prefix.
Keep the metadata database backed up too: private upload manifests fail closed unless their
matching ownership metadata is present.

## Send models to Ollama or vLLM

HuggingHack can dispatch a cached model to another inference device on the same network.
Destinations are configured server-side so endpoints and credentials never have to be entered
in the browser. The owner can then open a model and choose **Send to runtime**, while
automation can use the same API.

Add one or both target types to `.env` on a single line:

```dotenv
RUNTIME_TARGETS_JSON=[{"id":"ollama-rig","name":"Ollama GPU","kind":"ollama","base_url":"http://192.168.0.36:11434","keep_alive":"15m"},{"id":"vllm-rig","name":"vLLM GPU","kind":"vllm","base_url":"http://192.168.0.35:8090","remote_model_root":"/mnt/nas/models","token_env":"VLLM_AGENT_TOKEN"}]
RUNTIME_WORKERS=2
VLLM_AGENT_TOKEN=replace-with-the-same-long-random-secret-used-on-the-agent
```

The two adapters deliberately handle storage differently:

- **Ollama** uses its native blob and create APIs. HuggingHack hashes each required file, skips
  blobs the remote server already has, transfers missing data over HTTP, creates the Ollama
  model, and preloads it for the configured `keep_alive`. A repository needs one selected GGUF
  or a root-level SafeTensors model supported by Ollama.
- **vLLM** reads the existing NAS files instead of copying them. Mount the HuggingHack model
  folder on the vLLM device, then set `remote_model_root` to that device's mount path. vLLM
  fixes its base model at process startup, so the authenticated agent stops the process it
  manages and starts `vllm serve` with the selected model. Active inference requests will be
  interrupted during a switch.

S3-only models must be restored to the local cache before either adapter can use them.

### Run the vLLM agent

On the vLLM device, mount the same model share and run the small manager included in this
repository. The token is mandatory and must match the environment variable forwarded to the
HuggingHack container:

```bash
export VLLM_AGENT_TOKEN='replace-with-a-long-random-secret'
export VLLM_AGENT_MODEL_ROOT=/mnt/nas/models
export VLLM_AGENT_VLLM_PORT=8000
export VLLM_AGENT_EXTRA_ARGS_JSON='["--gpu-memory-utilization","0.9"]'
python -m uvicorn app.vllm_agent:app \
  --app-dir backend \
  --host 0.0.0.0 \
  --port 8090
```

The agent never accepts a shell command or arbitrary model path. It only starts `vllm serve`
for a directory inside `VLLM_AGENT_MODEL_ROOT`, with additional vLLM arguments fixed by the
agent administrator through `VLLM_AGENT_EXTRA_ARGS_JSON`. Do not run a separate vLLM server on
the configured vLLM port; the agent owns that process.

### Runtime API

Set `RUNTIME_API_TOKEN` to enable bearer-token automation scoped to runtime targets, loads, and
job history:

```dotenv
RUNTIME_API_TOKEN=replace-with-another-long-random-secret
```

Queue a load:

```bash
curl -X POST http://NAS-IP:7860/api/runtimes/ollama-rig/load \
  -H "Authorization: Bearer $RUNTIME_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"repo_id":"bartowski/Qwen2.5-7B-Instruct-GGUF","runtime_model_name":"qwen-local","source_file":"Qwen2.5-7B-Instruct-Q4_K_M.gguf"}'
```

The response is a persistent asynchronous job. Read it at
`GET /api/runtime-jobs/{job_id}`, list history at `GET /api/runtime-jobs`, and discover
configured destinations at `GET /api/runtimes`. The same endpoints also accept the owner's
normal browser session and CSRF token. Interactive API documentation is available at
`http://NAS-IP:7860/api/docs`.

## Move it to the NAS

Copy the entire `HuggingHack` directory to your NAS, then change only `MODEL_STORAGE_PATH` in `.env`.

The host model folder and the project's `data` folder must exist before the container starts. Synology Container Manager does not always create missing bind-mount sources. For a project stored at `/volume1/docker/HuggingHack`, create them in File Station or over SSH:

```bash
mkdir -p /volume1/docker/HuggingHack/models
mkdir -p /volume1/docker/HuggingHack/data
```

Then set `MODEL_STORAGE_PATH=/volume1/docker/HuggingHack/models`. If you choose another model location, create that exact path first.

Common examples:

```dotenv
# Synology
MODEL_STORAGE_PATH=/volume1/AI/models

# TrueNAS
MODEL_STORAGE_PATH=/mnt/tank/ai/models

# QNAP
MODEL_STORAGE_PATH=/share/Container/models
```

If your NAS enforces Unix ownership, set its user and group IDs:

```dotenv
PUID=1026
PGID=100
```

Find them over SSH with `id your-nas-user`. Then launch from the project directory:

```bash
docker compose up --build -d
```

Open `http://NAS-IP:7860` from another computer on the LAN.

## Gated and private models

1. Sign in at Hugging Face and accept the repository's license or access terms in your browser.
2. Create a read-only user access token.
3. Put it in `.env`:

```dotenv
HF_TOKEN=hf_your_read_token
```

4. Restart the service:

```bash
docker compose up -d
```

The token is read only by the backend container. It is never returned by the API or sent to the browser.

## File filtering

Server-side downloads from Hugging Face are off by default (`HF_DOWNLOADS_ENABLED=false`),
because an air-gapped server cannot reach Hugging Face; models arrive by upload instead. On a
server with internet access, set `HF_DOWNLOADS_ENABLED=true` and start downloads with
`POST /api/downloads` (members and admins, or a write-scope API token). The `mode` field takes:

- **Full repository** downloads every file in the selected revision.
- **SafeTensors** selects safe weights plus configuration and tokenizer files.
- **One GGUF** lets you choose a specific quantization from the repository file list.
- **Metadata only** fetches configuration, tokenizer, and documentation files without weights.
- **Custom** accepts comma-separated include and exclude patterns.

Custom pattern examples:

- Include only SafeTensors and config files: `*.safetensors, *.json, tokenizer*`
- Download one GGUF quantization: `*Q4_K_M.gguf, *.json, tokenizer*`
- Exclude legacy PyTorch weights: `*.bin, *.pt, *.pth`

Patterns use Hugging Face's official `snapshot_download` filtering.

## GGUF metadata and tensors

Repositories containing GGUF files get a **GGUF** tab on the model page. Select a file or
shard to inspect its metadata, tensor names, shapes, data types, quantization breakdown, and
parameter count without downloading the model weights.

HuggingHack reads only bounded byte ranges from the selected file, caches the result for the
browser session, and leaves every other shard untouched until you select it. Private and gated
repositories use the backend's `HF_TOKEN`; the token is never exposed to the browser.

## Cancel and resume

Active downloads have a **Cancel download** action. Cancellation stops the isolated download worker, keeps already transferred files and Hugging Face local-directory metadata, and marks the job as cancelled in history. Starting the same repository again can reuse those partial files instead of discarding the completed work.

## Organizations

Organizations are shared namespaces for teams and companies, so a model can live at
`Nvidia/GLM-5.3-NVFP4` without an `Nvidia` user account. Administrators create them under
**Admin → Organizations**; each organization then has its own members:

| Organization role | Can |
| --- | --- |
| **Read** | See and pull the organization's private repositories |
| **Write** | Also create repositories in the organization and upload changes |
| **Admin** | Also manage members and settings, change visibility, and delete repositories |

Organization roles add to the server role: a server **Viewer** with organization **Write**
access can read but still cannot upload. Private organization repositories are visible only to
members, including through API tokens and `git clone`. Organization names and usernames share
one namespace (case-insensitive), so a user cannot take an organization's name or the reverse.
Repositories stay with the organization when the account that created them leaves or is deleted.
Browse every organization at `#/orgs`; pick the owner when creating a repository on **Uploads**.

## Single sign-on (OpenID Connect)

HuggingHack signs people in through any OpenID Connect provider (Authentik, Keycloak,
Microsoft Entra ID, Okta, Dex, and others) using the Authorization Code flow with PKCE.
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
  under **Admin → Users**. Accounts are matched by the provider's subject id, never by
  username or email, so single sign-on cannot take over a local account.
- `OIDC_ALLOWED_GROUPS` limits who may sign in. To block one person, disable their account;
  deleting it only lasts until their next sign-in.
- The owner account is always created with a password on first run.
- The step-by-step Authentik setup is in the [air-gapped setup guide](docs/AIRGAPPED.md#5a-single-sign-on-with-authentik).

## Model pages and commit history

Every model has a full page at `#/models/owner/name` with its rendered model card,
a **Files and versions** browser with per-file downloads, a **Commits** history, and
GGUF inspection. Each change is recorded as a commit with its author, message, and the
files that were added, modified, or deleted; text files such as `README.md` and
`config.json` show line diffs.

Commits are created when you upload or change a repository, when a Hub download finishes,
and when a scan finds that files changed on disk or in a bucket. The repository owner, or
any administrator, can choose **Upload changes** to add, replace, or delete files with a
commit message. Changed files are staged and applied all at once, so nobody pulling the
model sees a half-uploaded change. Weights of older commits are not kept; only the newest
version of each file can be downloaded.

Uploads continue in a panel at the bottom of the screen while you browse. Reloading the page
stops them, but the server keeps what was sent: choose the same folder again to resume.

## Storage page and multiple buckets

Administrators get a **Storage** page listing every location that holds models, with its
connection status, capacity, and each model's size. Besides the local model folder and the
optional `MODEL_STORAGE_BACKEND=s3` bucket, add as many S3-compatible buckets as you need:

```dotenv
STORAGE_TARGETS_JSON=[{"id":"minio-main","name":"MinIO models","bucket":"models","endpoint_url":"http://minio:9000","addressing_style":"path","use_ssl":false,"access_key_env":"MINIO_MAIN_KEY","secret_key_env":"MINIO_MAIN_SECRET"}]
MINIO_MAIN_KEY=replace-me
MINIO_MAIN_SECRET=replace-me
DEFAULT_STORAGE_TARGET=minio-main
```

Target ids are permanent because models reference them. Credentials are read from the
named environment variables and never returned by the API. Uploaders pick a target when
creating a repository; new uploads and downloads otherwise use `DEFAULT_STORAGE_TARGET`.
If the same repository exists in two locations, the earlier target wins and the Storage page
reports the conflict.

## Pull models with vLLM, git, or the hf CLI

HuggingHack speaks the Hugging Face Hub protocol, so any machine on the network can
pull a model from the library without internet access. Open a model and choose
**Use model**, then **Deploy with vLLM** or **Clone repository**, for copy-paste
commands. Links use the same form as the Hub: `#/models/owner/name?local-app=vllm`
and `#/models/owner/name?clone=true`.

```bash
# vLLM, Transformers, and the hf CLI all honor HF_ENDPOINT
export HF_ENDPOINT=http://NAS-IP:7860
vllm serve owner/model-name
hf download owner/model-name

# git clone with Git LFS for the weights
git lfs install
git clone http://NAS-IP:7860/owner/model-name
```

- Files stream from the model folder, or directly from S3 for S3-only models, with
  byte-range support for resumed and parallel downloads.
- `git clone` is served from a read-only mirror in `data/git-mirrors`. Weights become
  Git LFS pointers whose SHA-256 is computed once per file and cached, so the first
  clone of a large model waits while it is hashed. Weights are never copied into the mirror.
- A model that changes gets a new commit on top of the previous one, so `git pull`
  picks up the update.
- Pulls are read-only. Without a token they can read every model visible to all accounts;
  a personal API token also reaches its owner's private uploads. Set `HUB_API_ENABLED=false`
  to require a token for every pull.
- Set `PUBLIC_URL=http://NAS-IP:7860` when the address in your browser (for example
  `localhost`) is not the one other machines use.

## Manually added models

Copy a model folder anywhere within the first few directory levels of the mounted model folder, then choose **Models → Rescan library** (or **Storage → Scan storage**). HuggingHack recognizes common configs and weight extensions such as:

- `config.json`, `model_index.json`, `tokenizer.json`
- `.safetensors`, `.gguf`, `.onnx`, `.bin`, `.pt`, `.pth`, and `.ckpt`

Manually copied models are indexed but never modified.

## Security

- HuggingHack downloads files but does not execute repository code, import model modules, or deserialize weights.
- Model cards are rendered as sanitized Markdown with safe HTML, readable code, tables, lists, and math; embedded scripts, forms, and frames are discarded.
- Pickle-compatible formats can execute code when loaded by other applications. Prefer SafeTensors or GGUF and only load models from publishers you trust.
- Passwords are salted and hashed with `scrypt`; sessions use hashed random tokens in HTTP-only, SameSite cookies and state-changing requests require a per-session CSRF token.
- Built-in accounts protect application data, but public exposure still requires HTTPS. Put HuggingHack behind a TLS reverse proxy such as Caddy, Traefik, or Nginx Proxy Manager and set `SECURE_COOKIES=true`.
- Upload paths are confined to repositories owned by the signed-in account. Repository deletion verifies ownership and requires the exact repository name.
- Runtime dispatch is administrator-only in the UI. Optional bearer access is limited to runtime endpoints; use long random tokens and firewall Ollama and the vLLM agent to trusted LAN clients.
- The vLLM agent rejects paths outside its configured model root and launches a fixed argument vector without a shell.
- Use a read-only Hugging Face token.
- The Hub-protocol pull endpoints are read-only and allow anonymous reads of models every account can see. Keep HuggingHack on a trusted network, or set `HUB_API_ENABLED=false` to require personal API tokens.
- API tokens are stored only as SHA-256 hashes and shown once. Revoke them from **Account → API tokens**; disabling an account stops its tokens immediately.

## Development

Backend:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\pip install -r backend\requirements.txt
$env:MODEL_STORAGE="$PWD\models"
$env:DATA_DIR="$PWD\data"
.venv\Scripts\uvicorn app.main:app --app-dir backend --reload --port 7860
```

Python 3.12 or 3.13 is recommended for local development. The Docker image uses Python 3.12, so Python is not required on the NAS.

Frontend:

```powershell
Set-Location frontend
npm install
npm run dev
```

The Vite development server proxies `/api` to port 7860.
Frontend development and production builds require Node.js 22 or newer. The lockfile is maintained with npm 10.9.8.

Tests and build:

```powershell
$env:PYTHONPATH="$PWD\backend"
pytest backend\tests
Set-Location frontend
npm test
npm run build
```

## Data ownership and backups

- Models: the host path configured by `MODEL_STORAGE_PATH`
- S3 mode: durable model objects in `S3_BUCKET` and working copies in `MODEL_STORAGE_PATH`
- Accounts, sessions, saved collections, repository ownership, download history, and local
  index: `./data/hugginghack.sqlite3` by default, or the database named by `DATABASE_URL`

Back up the models folder and metadata database together. Keep backing up `data` for SQLite
deployments. The model index can be rebuilt from model files, but the database preserves accounts, saved-model organization, ownership, and download history.
Store backups securely because it contains password hashes and active session hashes.
