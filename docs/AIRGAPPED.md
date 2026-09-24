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
- [6. Add models](#6-add-models)
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
| `PUBLIC_URL` | `http://<server-LAN-IP>:7860` | **Set this.** It is the address shown in every copy-paste command. Without it, commands use the address in your browser, which is wrong when you browse via `localhost`. |
| `HUB_API_ENABLED` | `true` (default) | Lets other machines pull models. Set `false` to disable pulling entirely. |
| `ACCOUNTS_ENABLED` | `true` (default) | Web UI sign-in. `false` skips sign-in on a single-user trusted network. Pulling is anonymous either way. |
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

## 5. First sign-in

On the first visit HuggingHack asks you to create the **owner** account (password of at
least 12 characters). The owner can add member accounts from **Settings**. Accounts are
stored only in the local database.

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

The folders are only indexed, never modified. The **Uploads** page is an alternative for
adding a model folder from a browser.

## 7. Use models from other machines

Open a model and click **Use model** (on its card or in its detail panel). The dialog
shows ready-to-copy commands with your server address. Links are shareable:

- `http://<server>:7860/#/models?model=owner/name&local-app=vllm`
- `http://<server>:7860/#/models?model=owner/name&clone=true`

In the commands below, replace `SERVER` with your `PUBLIC_URL`, for example
`http://192.168.1.50:7860`.

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
- Pulling is **anonymous and read-only**: anyone who can reach the port can download every
  model that all accounts can see. Private uploads are never served. Restrict the port
  with a firewall, or set `HUB_API_ENABLED=false` to turn pulling off.
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
| `Repository not found` when pulling | The model is a private upload, or it is missing from the library, or `HUB_API_ENABLED=false`. |
| UI still shows an old version after upgrading | Hard-refresh the browser once. |
| S3-only models | They can be pulled directly (streamed from S3). **Restore to local cache** is only needed for the built-in runtime dispatch. |

## 10. Limitations

- `llama-server -hf …` (llama.cpp) and `ollama run hf.co/…` use different protocols and
  are not supported yet. Clone the GGUF repository and point them at the file instead.
- Pull access has no tokens: it is all-or-nothing per server via `HUB_API_ENABLED`.
- The **Saved** page and **Downloads** page still contact the real Hugging Face Hub and do
  not work offline. The **Models** tab, **Local library**, **Uploads**, and pulling do.
