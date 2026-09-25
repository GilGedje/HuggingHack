<p align="center">
  <img src="frontend/public/hugginghack-mark.svg" width="88" alt="HuggingHack terminal-face mark">
</p>

<h1 align="center">HuggingHack</h1>

<p align="center">
  <strong>Your own Hugging Face Hub, on a network with no internet.</strong><br>
  Bring models in once, browse them like the Hub, and pull them with vLLM, git, or the <code>hf</code> CLI from any machine on the LAN.
</p>

<p align="center">
  <a href="https://github.com/GilGedje/HuggingHack/actions/workflows/ci.yml"><img src="https://github.com/GilGedje/HuggingHack/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <img src="https://img.shields.io/badge/FastAPI-0.116-009688?logo=fastapi&logoColor=white" alt="FastAPI 0.116">
  <img src="https://img.shields.io/badge/React-18-20232A?logo=react&logoColor=61DAFB" alt="React 18">
  <img src="https://img.shields.io/badge/PostgreSQL-17-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL 17">
  <img src="https://img.shields.io/badge/S3-compatible-569A31?logo=amazons3&logoColor=white" alt="S3-compatible storage">
  <img src="https://img.shields.io/badge/air--gapped-ready-F59E0B" alt="Air-gapped ready">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="MIT license">
</p>

<p align="center">
  <a href="#a-tour">Tour</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#pull-a-model">Pull a model</a> ·
  <a href="#deploy-it-properly">Deploy</a> ·
  <a href="docs/GUIDE.md">Guide</a> ·
  <a href="#security">Security</a> ·
  <a href="#development">Development</a>
</p>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/explore-dark.webp">
  <img src="docs/images/explore.webp" alt="HuggingHack Explore page: a grid of model cards with task, precision, size, and hardware, and filters for parameters, tasks, precision, and hardware">
</picture>

> [!NOTE]
> HuggingHack is an unofficial, independent project. It is not affiliated with or endorsed by
> Hugging Face.

## Why HuggingHack

GPU clusters on closed networks still need models, and the tools that run them (vLLM,
Transformers, `hf`, `git`) all expect a Hugging Face Hub. HuggingHack is that Hub, running on
your own storage: carry models across the air gap once, and every machine on the network pulls
them the same way it would from huggingface.co.

<table>
  <tr>
    <td width="33%" valign="top"><strong>Serve</strong><br>Speaks the Hub protocol, so <code>HF_ENDPOINT</code>, <code>vllm serve</code>, <code>hf download</code>, and <code>git clone</code> with LFS work unchanged. Files stream from disk or straight from S3.</td>
    <td width="33%" valign="top"><strong>Organize</strong><br>Model cards, file browser, commit history with diffs, deployment configs with benchmark results, collections, organizations, and roles.</td>
    <td width="33%" valign="top"><strong>Own it</strong><br>Models in a plain folder or S3-compatible buckets, metadata in PostgreSQL or SQLite. No calls home, no CDN, no cloud dashboard in the middle.</td>
  </tr>
</table>

## A tour

### Every model gets a Hub-style page

The rendered model card, tags read from the files, the model tree (base model, quantizations,
fine-tunes), tested hardware, and one click to the commands that pull it.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/model-dark.webp">
  <img src="docs/images/model.webp" alt="A model page for Qwen/Qwen3-0.6B with its model card, details, model tree, and hardware">
</picture>

### Files, versions, and commit history

Every upload, change, and rescan is a commit with its author and a line diff for text files.
Changes are staged and applied all at once, so nobody pulls a half-uploaded model.

<table>
  <tr>
    <td width="50%" valign="top"><img src="docs/images/files.webp" alt="Files and versions tab listing a repository's files with sizes and per-file downloads"></td>
    <td width="50%" valign="top"><img src="docs/images/commit.webp" alt="A commit showing the files it added with a line diff"></td>
  </tr>
</table>

### Deployment configs next to what they achieved

Keep the scripts you serve a model with as numbered revisions, record throughput and latency
for each, and compare them side by side. The best value in each row is highlighted.

<table>
  <tr>
    <td width="50%" valign="top"><img src="docs/images/configs.webp" alt="Config tab comparing two revisions' benchmark results"></td>
    <td width="50%" valign="top"><img src="docs/images/config-revision.webp" alt="One config revision with its test setup and measured results"></td>
  </tr>
</table>

### Pull it from anywhere on the network

**Use this model** gives copy-paste commands for vLLM and `git clone`, pointed at your server.

<img src="docs/images/use-model.webp" alt="Use this model dialog with the pull endpoint and vLLM commands">

### Upload, save, and share

A four-step upload checks the folder, previews how the model will be listed, and resumes where
it stopped if the connection drops. Save models into collections with private notes, and give
teams their own organization namespace.

<table>
  <tr>
    <td width="50%" valign="top"><img src="docs/images/upload.webp" alt="Upload a model: name, access, files, and review steps"></td>
    <td width="50%" valign="top"><img src="docs/images/saved.webp" alt="Saved models with collections and private notes"></td>
  </tr>
  <tr>
    <td width="50%" valign="top"><img src="docs/images/org.webp" alt="An organization page with its models, members, and settings"></td>
    <td width="50%" valign="top"><img src="docs/images/gguf-dark.webp" alt="GGUF tab showing metadata and tensors of a quantized file, in dark mode"></td>
  </tr>
</table>

### Administration

Accounts and roles, every storage location with its capacity, moves between locations, runtimes,
and the server's configuration, all enforced the same way for the web, API tokens, and pulls.

<table>
  <tr>
    <td width="50%" valign="top"><img src="docs/images/storage.webp" alt="Admin Storage page with the local disk, its capacity, and its models"></td>
    <td width="50%" valign="top"><img src="docs/images/users-dark.webp" alt="Admin Users page in dark mode with roles and account actions"></td>
  </tr>
  <tr>
    <td width="50%" valign="top"><img src="docs/images/roles.webp" alt="Roles and permissions matrix for Viewer, Member, and Administrator"></td>
    <td width="50%" valign="top"><img src="docs/images/server.webp" alt="Server settings page showing accounts, database, storage, and runtimes configuration without secrets"></td>
  </tr>
</table>

### On a phone, in light or dark

<img src="docs/images/phones.webp" alt="Three phone screenshots: Explore in light mode, a model page in dark mode, and Saved models in light mode">

## Quick start

You need Docker. On Windows, install Docker Desktop, double-click **Start HuggingHack.bat**, and
open [http://localhost:7860](http://localhost:7860). Anywhere else:

```bash
cp .env.example .env
docker compose up --build -d
```

On the first visit HuggingHack asks you to create the owner account (a password of at least 12
characters). Put models in the library by uploading them on **Uploads**, or copy model folders
into `./models` and choose **Rescan library**. Stop it with `docker compose down` (or
**Stop HuggingHack.bat**); models and data stay.

To use it as a single person on a trusted LAN without sign-in, set `ACCOUNTS_ENABLED=false`.

## Pull a model

```bash
export HF_ENDPOINT=http://your-server:7860

vllm serve Qwen/Qwen3-0.6B            # vLLM
hf download Qwen/Qwen3-0.6B           # the hf CLI
python -c "from transformers import AutoModel; AutoModel.from_pretrained('Qwen/Qwen3-0.6B')"

git lfs install
git clone http://your-server:7860/Qwen/Qwen3-0.6B
```

Models every account can see need no token. For private models, create a token under
**Account → API tokens** and use it as `HF_TOKEN` or as the git password. Set `PUBLIC_URL` so the
commands the site shows use the address other machines reach.

## Deploy it properly

| You want to | Read |
| --- | --- |
| Install on a network with no internet access | [Air-gapped setup guide](docs/AIRGAPPED.md) |
| Keep models in S3 and metadata in PostgreSQL, with no lasting data on the server | [Serve from S3 and PostgreSQL](docs/SERVE_FROM_S3.md) |
| Switch from SQLite to PostgreSQL, or move an existing install | [Guide → PostgreSQL](docs/GUIDE.md#postgresql) |
| Run it on a Synology, TrueNAS, or QNAP | [Guide → Run it on a NAS](docs/GUIDE.md#run-it-on-a-nas) |
| Sign in with Authentik, Keycloak, Entra ID, or another OIDC provider | [Guide → Single sign-on](docs/GUIDE.md#single-sign-on-openid-connect) |
| Load models into Ollama or a vLLM rig from the web page | [Guide → Send models to Ollama or vLLM](docs/GUIDE.md#send-models-to-ollama-or-vllm) |
| Understand roles, organizations, visibility, uploads, and configs | [Guide](docs/GUIDE.md) |

Every setting, with its default, is in [`.env.example`](.env.example).

## Security

- **Model files are data.** HuggingHack never imports, unpickles, or runs model code.
- **Accounts:** scrypt passwords, hashed sessions and API tokens, per-session CSRF tokens, and
  sign-in throttling. Changing a password revokes the account's API tokens.
- **Roles on every path:** Viewer, Member, and Administrator are enforced alike for the web, API
  tokens, `git clone`, and Hub pulls. Private models stay private through all of them.
- **A locked-down page:** strict Content-Security-Policy, sanitized model cards, no outside
  images, and cross-site writes refused, even with accounts turned off.
- **Quiet by default:** anonymous health checks reveal nothing about storage, API docs are off,
  and `ALLOWED_HOSTS` blocks DNS rebinding.

Put it behind an HTTPS reverse proxy before exposing it beyond a trusted network. Details are in
[Guide → Security](docs/GUIDE.md#security).

## Development

```bash
# Backend (Python 3.12)
python3.12 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
MODEL_STORAGE=$PWD/models DATA_DIR=$PWD/data \
  .venv/bin/uvicorn app.main:app --app-dir backend --reload --port 7860

# Frontend (Node 22): the dev server proxies /api to port 7860
cd frontend && npm ci && npm run dev
```

Tests run on SQLite and on PostgreSQL, and every change must pass on both, because PostgreSQL is
the production database:

```bash
docker run -d --name hh-pg-test -p 55432:5432 -e POSTGRES_DB=hugginghack_test \
  -e POSTGRES_USER=hugginghack -e POSTGRES_PASSWORD=test-only-password postgres:17-alpine
PYTHONPATH=backend \
  TEST_POSTGRES_URL=postgresql://hugginghack:test-only-password@127.0.0.1:55432/hugginghack_test \
  .venv/bin/python -m pytest backend/tests -q

cd frontend && npm test && npm run build
```

Working on the code, by hand or with Claude Code? Start with [`CLAUDE.md`](CLAUDE.md), then
[`backend/CLAUDE.md`](backend/CLAUDE.md) and [`frontend/CLAUDE.md`](frontend/CLAUDE.md): they
describe the architecture, the rules that keep it secure and air-gapped, and how to verify a
change without internet access.

```text
backend/app/     FastAPI server: API, Hub protocol, git, storage, accounts
backend/tests/   pytest suite (SQLite, plus PostgreSQL when TEST_POSTGRES_URL is set)
frontend/src/    React + TypeScript single-page app
docs/            Guide, air-gapped setup, S3 and PostgreSQL
```

## License

[MIT](LICENSE) © 2026 Gil Gedje. Hugging Face is a trademark of Hugging Face, Inc.; HuggingHack
is not affiliated with it.
