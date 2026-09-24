# Air-gapped setup guide

This guide covers everything needed to run HuggingHack as a private model hub on a
network with no internet access: install it, fill it with models, and let other
machines pull those models with vLLM, `git clone`, the `hf` CLI, or Transformers.

HuggingHack never needs the internet in this mode. The **Models** tab, model cards,
parameter counts, GGUF inspection, and every pull are served from your own storage.

- [1. How it works](#1-how-it-works)
- [2. Move HuggingHack onto the offline network](#2-move-hugginghack-onto-the-offline-network)
- [3. Configure](#3-configure)
- [4. Start, stop, and update](#4-start-stop-and-update)
- [5. First sign-in](#5-first-sign-in)
- [5a. Single sign-on with Authentik](#5a-single-sign-on-with-authentik)
- [6. Add models](#6-add-models)
- [6a. Browse models, history, and changes](#6a-browse-models-history-and-changes)
- [6b. Storage locations and buckets](#6b-storage-locations-and-buckets)
- [7. Use models from other machines](#7-use-models-from-other-machines)
- [8. Network and security](#8-network-and-security)
- [9. Troubleshooting](#9-troubleshooting)
- [10. Limitations](#10-limitations)

## 1. How it works

```text
 connected machine                 │  air-gapped network
                                   │
 hf download / git clone ──► USB ──┼──► models/owner/name/   (plain folders, or S3)
 docker save ──────────────► USB ──┼──► HuggingHack :7860
                                   │        │
                                   │        ├── browser: Models tab, cards, files, GGUF
                                   │        ├── HF_ENDPOINT ◄── vLLM, Transformers, hf CLI
                                   │        └── git clone  ◄── git + git-lfs
```

- Models are ordinary folders: `models/<owner>/<model-name>/config.json, *.safetensors, …`.
- HuggingHack serves the same HTTP API as huggingface.co. Any tool built on
  `huggingface_hub` pulls from it when `HF_ENDPOINT` points at HuggingHack.
- `git clone` works through a read-only mirror. Weights are delivered through Git LFS
  straight from the model folder and are never duplicated on disk.

## 2. Move HuggingHack onto the offline network

Building the image downloads npm and pip packages, so build once on a machine with
internet access, then carry the image across.

On the **connected** machine, from the repository folder:

```bash
docker compose build
docker save hugginghack:local -o hugginghack-image.tar
```

Copy `hugginghack-image.tar` and this repository folder to the offline server, then:

```bash
docker load -i hugginghack-image.tar
```

`docker compose up -d` (without `--build`) uses the loaded image as-is. Only run
`docker compose up --build` on the offline server if it has an internal npm and PyPI
mirror.

## 3. Configure

Create `.env` from the example once:

```bash
cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
```

Settings that matter for an air-gapped install:

| Setting | Value | Why |
| --- | --- | --- |
| `MODEL_STORAGE_PATH` | `./models` or an absolute path such as `/mnt/tank/ai/models` | Folder that holds every model. It must exist before starting. |
| `MODEL_STORAGE_BACKEND` | `filesystem` (default) | Plain folders, no S3 needed. Use `s3` only with an internal MinIO or Ceph (see the README). |
| `STORAGE_TARGETS_JSON` | `[]` | Optional extra S3-compatible buckets. See [6b](#6b-storage-locations-and-buckets). |
| `DEFAULT_STORAGE_TARGET` | empty | Where new uploads go when there are several locations. |
| `PUBLIC_URL` | `http://<server-LAN-IP>:7860` | **Set this.** It is the address shown in every copy-paste command. Without it, commands use the address in your browser, which is wrong when you browse via `localhost`. |
| `HUB_API_ENABLED` | `true` (default) | Lets other machines pull without a token. Set `false` to require a personal API token for every pull. |
| `ACCOUNTS_ENABLED` | `true` (default) | Web UI sign-in, roles, and API tokens. `false` skips sign-in on a single-user trusted network. |
| `HF_TOKEN` | leave empty | Only used to download from the real Hugging Face Hub. |

Find the server's LAN IP with `ipconfig getifaddr en0` (macOS), `hostname -I` (Linux), or
`ipconfig` (Windows). Prefer a fixed IP or a DNS name so commands stay valid.

## 4. Start, stop, and update

```bash
docker compose up -d          # start (Windows: Start HuggingHack.bat)
docker compose down           # stop  (Windows: Stop HuggingHack.bat)
docker compose logs -f        # follow logs
```

Open `http://<server>:7860`. Models, the database, and the git mirrors live in the mounted
folders (`MODEL_STORAGE_PATH` and `./data`) and survive restarts and upgrades.

To upgrade, repeat [section 2](#2-move-hugginghack-onto-the-offline-network) with the new
version, then `docker compose up -d`. Hard-refresh the browser once if the UI looks stale.

## 5. First sign-in, roles, and accounts

On the first visit HuggingHack asks you to create the **owner** account (an administrator,
password of at least 12 characters). Accounts are stored only in the local database.

Administrators open **Admin** in the top bar:

- **Users**: add accounts, pick each one's role, disable or enable them, reset passwords,
  sign people out everywhere, and delete accounts that own no repositories.
- **Roles & permissions**: exactly what each role can do.
  - **Viewer**: browse, save, and pull models; personal API tokens.
  - **Member**: a viewer who can also upload and change their own repositories, rescan
    storage, and download from Hugging Face.
  - **Administrator**: everything, including storage, runtimes, and accounts.
- **Storage**, **Runtimes**, and **Server** (the `.env` configuration, secrets hidden).

Everyone has **Account** (the gear icon, or click your name): profile, password and active
sessions, preferences (theme, default sort, default upload location), and **API tokens**.

## 5a. Single sign-on with Authentik

HuggingHack works with any OpenID Connect provider. With Authentik:

1. In Authentik, open **Applications → Providers → Create** and choose **OAuth2/OpenID Provider**.
   - **Client type:** Confidential. Copy the **Client ID** and **Client Secret**.
   - **Redirect URIs:** `https://hugginghack.example.internal/api/auth/oidc/callback`
     (your `PUBLIC_URL` followed by `/api/auth/oidc/callback`), matching exactly.
   - **Signing Key:** choose a certificate, for example the built-in self-signed one. Tokens
     are then signed with RS256 and verified against Authentik's published keys.
   - **Scopes:** keep `openid`, `email`, and `profile`. Authentik includes `groups` in the
     profile scope.
   - **Grant types** (Authentik 2026 and later): make sure **Authorization Code** is allowed.
2. Open **Applications → Applications → Create**, name it HuggingHack, set the slug to
   `hugginghack`, and select the provider. Bind groups or users there to control access.
3. Add to HuggingHack's `.env`, then run `docker compose up -d`:

   ```dotenv
   PUBLIC_URL=https://hugginghack.example.internal
   OIDC_ISSUER=https://authentik.example.internal/application/o/hugginghack/
   OIDC_CLIENT_ID=paste-the-client-id
   OIDC_CLIENT_SECRET=paste-the-client-secret
   OIDC_PROVIDER_NAME=Authentik
   OIDC_DEFAULT_ROLE=viewer
   # Optional: only these Authentik groups may sign in
   OIDC_ALLOWED_GROUPS=hugginghack-users
   ```

   The issuer is the application's **OpenID Configuration Issuer** shown in Authentik,
   including the trailing slash.
4. If Authentik uses a certificate from an internal CA, copy the CA's PEM file into `./data`
   and set `OIDC_CA_BUNDLE=/data/internal-ca.pem`.

The sign-in page now shows **Sign in with Authentik** above the password form. The first
sign-in creates an account with the default role; promote people under **Admin → Users**.
Usernames come from Authentik's `preferred_username`, cleaned to letters, numbers, and
hyphens, and never change afterwards because repositories live under them.

Other providers use the same settings with their own issuer, for example
`https://keycloak.example.internal/realms/<realm>` for Keycloak or
`https://login.microsoftonline.com/<tenant-id>/v2.0` for Entra ID.

Notes:

- Signing out of HuggingHack does not sign you out of Authentik.
- To block someone, disable their HuggingHack account or remove them from the Authentik
  application. Deleting the account only lasts until their next sign-in.
- If Authentik is unreachable, password sign-in keeps working.

## 6. Add models

### Get models across the air gap

On a connected machine, download each model into an `owner/name` folder:

```bash
pip install -U "huggingface_hub[cli]"
hf download Qwen/Qwen2.5-7B-Instruct --local-dir transfer/Qwen/Qwen2.5-7B-Instruct
# GGUF repositories: take only the quantization you need
hf download bartowski/Qwen2.5-7B-Instruct-GGUF --include "*Q4_K_M.gguf" "*.md" \
  --local-dir transfer/bartowski/Qwen2.5-7B-Instruct-GGUF
```

Alternatively run a second HuggingHack on the connected side, download through its UI,
and copy its `models` folder.

Keep each model's `README.md`. HuggingHack reads its header offline to fill in the task,
license, and tags, and renders it as the model card.

### Put them in the library

1. Copy the folders into `MODEL_STORAGE_PATH`, keeping the `owner/name` layout:
   ```text
   models/
     Qwen/
       Qwen2.5-7B-Instruct/
         config.json
         model-00001-of-00004.safetensors
         README.md
         ...
   ```
2. In the web UI open **Models** and click **Rescan library** (or restart the container,
   which also scans).

The folders are only indexed, never modified. The first scan records an "Initial import"
commit for every model.

The **Uploads** page is an alternative for adding a model folder from a browser: create a
repository, choose a folder, give the upload a commit message, and start it. The upload runs
in a panel at the bottom of the screen, so you can keep browsing. If the page is reloaded,
choose the same folder again in that panel and it resumes where it stopped.

## 6a. Browse models, history, and changes

Click any model to open its page (`#/models/owner/name`):

- **Model card** renders the model's `README.md`, with a side panel for size, parameters,
  storage location, the last commit, **Use this model**, cache actions, and runtime dispatch.
- **Files and versions** lists folders and files with the last commit that touched each,
  and a download button per file.
- **Commits** lists every change: who made it, when, the message, and how many files were
  added, modified, or deleted. Open a commit to see line diffs for text files such as
  `README.md` and `config.json`, and size changes for weights.
- **GGUF** inspects GGUF headers and tensors.

To change a model, its owner or an administrator chooses **Upload changes**: add files or a
folder (optionally into a subfolder), tick existing files to delete, write a commit message,
and commit. Files are staged and applied all at once, so vLLM and `git clone` never see a
half-finished change. Scans also record commits when files change directly on disk or in a
bucket ("Detected changes in storage").

History keeps the text of small text files, but not old weights: only the newest version of
each file can be downloaded or pulled.

## 6b. Storage locations and buckets

Administrators see a **Storage** page (it replaces the old Local library) listing every
location that holds models: the local model folder, the `MODEL_STORAGE_BACKEND=s3` bucket if
set, and any extra buckets. Each shows its connection status, location, capacity, and every
model with its size, file count, parameters, and whether it is on disk or only in S3.

Add internal buckets (MinIO, Ceph, or any S3-compatible service) in `.env`, keeping the JSON
on one line:

```dotenv
STORAGE_TARGETS_JSON=[{"id":"minio-main","name":"MinIO models","bucket":"models","endpoint_url":"http://minio:9000","addressing_style":"path","use_ssl":false,"access_key_env":"MINIO_MAIN_KEY","secret_key_env":"MINIO_MAIN_SECRET"},{"id":"minio-archive","name":"MinIO archive","bucket":"archive","prefix":"","endpoint_url":"http://minio:9000","addressing_style":"path","use_ssl":false,"access_key_env":"MINIO_MAIN_KEY","secret_key_env":"MINIO_MAIN_SECRET"}]
MINIO_MAIN_KEY=replace-me
MINIO_MAIN_SECRET=replace-me
DEFAULT_STORAGE_TARGET=minio-main
```

- `id` is permanent (models reference it): 1-40 lowercase letters, digits, or hyphens.
- `prefix` is the folder inside the bucket (default `models`; `""` for the bucket root).
- Credentials are only named here; their values live in the variables you name, which
  Docker Compose passes into the container from `.env`.
- Every bucket needs `s3:ListBucket`, and `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`
  on its prefix.
- Uploaders choose the location when they create a repository. Everything else uses
  `DEFAULT_STORAGE_TARGET`.
- A repository must exist in only one location. If the same `owner/name` is found in two,
  the earlier one is used and the Storage page shows a warning.

Models in a bucket are pulled directly from the bucket, so they work with vLLM, `git clone`,
and the `hf` CLI without being copied to the server first.

## 7. Use models from other machines

Open a model and click **Use model** (on its card or in its detail panel). The dialog
shows ready-to-copy commands with your server address. Links are shareable:

- `http://<server>:7860/#/models/owner/name?local-app=vllm`
- `http://<server>:7860/#/models/owner/name?clone=true`

In the commands below, replace `SERVER` with your `PUBLIC_URL`, for example
`http://192.168.1.50:7860`.

### Private models and API tokens

Pulls without a token can read every model that all accounts can see. To pull your
**private** repositories, or when the administrator set `HUB_API_ENABLED=false`, create a
token under **Account → API tokens** (read access is enough) and pass it along:

```bash
export HF_TOKEN=hht_your_token          # vLLM, Transformers, and the hf CLI
git clone http://yourname:hht_your_token@SERVER_HOST:7860/owner/private-model
```

The token is shown once; revoke it from the same page when a machine no longer needs it.

### vLLM

vLLM must already be installed on the GPU machine, from your internal PyPI mirror or
container registry.

```bash
export HF_ENDPOINT=SERVER
vllm serve "owner/name"
```

Docker:

```bash
docker run --runtime nvidia --gpus all \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  --env "HF_ENDPOINT=SERVER" \
  -p 8000:8000 --ipc=host \
  vllm/vllm-openai:latest --model owner/name
```

The first start downloads the weights from HuggingHack into the Hugging Face cache on
that machine; later starts reuse the cache.

vLLM needs SafeTensors weights with a `config.json`. GGUF-only models show only the Clone
option; use them with llama.cpp, Ollama, or LM Studio after cloning.

### git clone

Requires `git` and `git-lfs` on the client.

```bash
git lfs install                                   # once per machine
git clone SERVER/owner/name
GIT_LFS_SKIP_SMUDGE=1 git clone SERVER/owner/name  # pointers only, no weights
git pull                                          # picks up changes to the model
```

The first clone of a model computes a SHA-256 of each weight file, so a large model can
take minutes before the clone starts. Hashes are cached, so later clones start immediately.

### hf CLI

```bash
export HF_ENDPOINT=SERVER
hf download owner/name --local-dir ./name
```

### Python (Transformers and anything built on huggingface_hub)

```bash
export HF_ENDPOINT=SERVER
python -c "from transformers import AutoModelForCausalLM; AutoModelForCausalLM.from_pretrained('owner/name')"
```

Set `HF_ENDPOINT` before Python starts; it is read at import time.

## 8. Network and security

- Clients only need TCP port `7860` (or your `HUGGINGHACK_PORT`) on the HuggingHack server.
- Pulling is **read-only**. Without a token, anyone who can reach the port can download
  every model that all accounts can see; private uploads need their owner's API token.
  Restrict the port with a firewall, or set `HUB_API_ENABLED=false` to require a token for
  every pull.
- Roles are enforced by the server for the web interface, API tokens, and pulls. API tokens
  cannot manage accounts or tokens, and read tokens cannot change anything.
- Nothing can be pushed or modified through the pull endpoints or git.
- Model card images inside a model folder are shown; images hosted on the internet are
  skipped, so pages never try to reach outside the network.

## 9. Troubleshooting

| Symptom | Fix |
| --- | --- |
| Commands in the dialog show `localhost` | Set `PUBLIC_URL` in `.env`, then `docker compose up -d`. |
| A copied model does not appear | Check the `owner/name` folder layout, then click **Rescan library**. |
| Model has no task or license | Its `README.md` has no header; copy the original `README.md` from the source repository. |
| Cloned weight files are ~130 bytes of text | `git-lfs` is missing on the client. Install it, run `git lfs install`, then `git lfs pull` in the clone. |
| `git clone` sits silently at first | It is hashing the weights for the first time; wait, and later clones are fast. |
| vLLM tries to reach huggingface.co | `HF_ENDPOINT` was not exported in the shell or container that runs vLLM. |
| `Repository not found` when pulling | The model is private or missing, or `HUB_API_ENABLED=false`. Pass a personal API token as `HF_TOKEN` or as the git password. |
| `The API token is invalid, expired, or revoked` | Create a new token under **Account → API tokens**; disabled accounts' tokens stop working. |
| Uploads or Downloads are missing from the top bar | Your role is **Viewer**. Ask an administrator for the Member role. |
| UI still shows an old version after upgrading | Hard-refresh the browser once. |
| S3-only models | They can be pulled directly (streamed from S3). **Restore to local cache** is only needed for the built-in runtime dispatch. |
| A bucket shows **Offline** on the Storage page | Check its `endpoint_url`, the credential variables it names, and the bucket permissions. Its models stay listed until it reconnects. |
| Storage page warns that a model exists in two locations | Delete one copy; the earlier storage target in the list is the one being served. |
| Upload panel says "Choose the same folder again" | The page was reloaded mid-upload. Pick the same folder; already-sent bytes are skipped. |

## 10. Limitations

- `llama-server -hf …` (llama.cpp) and `ollama run hf.co/…` use different protocols and
  are not supported yet. Clone the GGUF repository and point them at the file instead.
- The **Downloads** page and **Admin → Server → Hugging Face** refer to the real
  Hugging Face Hub and do not work offline. Everything else, including **Saved**, works offline.
- Commit history keeps text files but not old weights; older versions cannot be downloaded.
