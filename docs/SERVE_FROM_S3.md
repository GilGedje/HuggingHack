# Serve HuggingHack from S3 and PostgreSQL on an air-gapped network

This runbook sets up HuggingHack so that everything it must keep lives in two places you
already back up: an S3-compatible bucket (MinIO, Ceph, or similar) and a PostgreSQL
database. The server itself holds only caches, so it can be replaced, moved, or rebuilt
without losing anything.

Follow the steps in order. Each step ends with a check; do not go on until it passes.
For everything else about running HuggingHack offline (accounts, organizations, SSO,
pulling models), see the [air-gapped setup guide](AIRGAPPED.md).

- [1. What lives where](#1-what-lives-where)
- [2. Before you start](#2-before-you-start)
- [3. Carry the images across](#3-carry-the-images-across)
- [4. Prepare the bucket](#4-prepare-the-bucket)
- [5. Prepare PostgreSQL](#5-prepare-postgresql)
- [6. Configure .env](#6-configure-env)
- [7. Start and verify](#7-start-and-verify)
- [8. Bring an existing installation over](#8-bring-an-existing-installation-over)
- [9. Serve a model](#9-serve-a-model)
- [10. Back up, restore, and upgrade](#10-back-up-restore-and-upgrade)
- [11. Troubleshooting](#11-troubleshooting)
- [12. Checklist](#12-checklist)

## 1. What lives where

| Data | Where | Lost if the server is replaced? |
| --- | --- | --- |
| Accounts, organizations, permissions, API tokens, commit history, listings, deployment configs, storage moves | PostgreSQL | No |
| Model files | The bucket, under its prefix: `models/<owner>/<name>/…` | No |
| Profile pictures | The bucket: `models/_system/avatars/…` | No |
| Git history served to `git clone` / `git pull` | The bucket: `models/_system/git-mirrors/…` | No |
| Local copies of models (a cache for faster pulls and for runtimes) | The server's `/models` volume | Yes, and that is fine: models are pulled from the bucket until they are cached again |
| Uploads and storage moves in progress, the Hugging Face download cache | The server's `/models` and `/data` volumes | Yes: an upload in progress must be started again |

The bucket layout HuggingHack creates:

```text
<bucket>/
  models/                               the target's prefix
    <owner>/<name>/                     one folder per model
      .hugginghack.json                 the model's manifest (written last)
      config.json, *.safetensors, …
    _system/                            the site's own files
      README.txt
      avatars/users/<user id>
      avatars/organizations/<organization id>
      git-mirrors/<owner>/<name>/
```

`_system` starts with an underscore, which no user or organization name can, so it is never
mistaken for a model. Do not edit anything in the bucket by hand.

## 2. Before you start

You need, on the offline network:

- A Linux host with Docker Engine and Docker Compose v2 for HuggingHack.
- An S3-compatible service reachable from that host, and an access key for it.
- PostgreSQL 15 or newer, or permission to run the `postgres:17-alpine` container.
- Local disk on the HuggingHack host for caches: at least the size of the largest model
  you will upload, plus room for any models you want cached (see
  [Local disk](#local-disk)).

And on a machine with internet access: Docker, and this repository.

## 3. Carry the images across

Building the image downloads packages, so build on the connected machine:

```bash
docker compose build
docker pull postgres:17-alpine            # only if you will run PostgreSQL in a container
docker save hugginghack:local postgres:17-alpine -o hugginghack-images.tar
```

Copy `hugginghack-images.tar` and this repository folder to the offline host, then:

```bash
docker load -i hugginghack-images.tar
```

**Check:** `docker images` lists `hugginghack:local` (and `postgres:17-alpine`).

## 4. Prepare the bucket

Create a bucket, for example `models`, and an access key that may use the `models/` prefix
in it. HuggingHack needs to list the bucket and to read, write, and delete objects under the
prefix; the `_system` folder sits inside the prefix, so no extra permission is needed.

A policy for MinIO or any S3-compatible service (replace `models` with your bucket):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::models"],
      "Condition": {"StringLike": {"s3:prefix": ["models/*", "models"]}}
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
      "Resource": ["arn:aws:s3:::models/models/*"]
    }
  ]
}
```

With MinIO's client:

```bash
mc alias set lab http://minio:9000 ADMIN_KEY ADMIN_SECRET
mc mb lab/models
mc admin policy create lab hugginghack policy.json
mc admin user add lab hugginghack LONG_RANDOM_SECRET
mc admin policy attach lab hugginghack --user hugginghack
```

Turn on bucket versioning if your service supports it: it lets you recover a deleted model
or picture.

**Check:** from the HuggingHack host, the key can write and list:

```bash
mc alias set hh http://minio:9000 hugginghack LONG_RANDOM_SECRET
echo ok | mc pipe hh/models/models/_probe && mc ls hh/models/models/ && mc rm hh/models/models/_probe
```

## 5. Prepare PostgreSQL

Pick one:

- **An existing PostgreSQL server.** Create an empty database and a user that owns it:

  ```sql
  CREATE USER hugginghack WITH PASSWORD 'long-random-password';
  CREATE DATABASE hugginghack OWNER hugginghack;
  ```

  You will set `DATABASE_URL` in `.env`.

- **PostgreSQL next to HuggingHack**, with the Compose overlay
  `docker-compose.postgres.yml`. It keeps its data in the `postgres-data` volume; you will
  set `POSTGRES_PASSWORD` in `.env`.

HuggingHack creates and upgrades its tables on every start; do not create them yourself.

**Check (existing server):** `psql "postgresql://hugginghack:…@db-host:5432/hugginghack" -c 'select 1'`
works from the HuggingHack host.

## 6. Configure .env

```bash
cp .env.example .env
```

Set these values (JSON on one line; keep `.env` readable only by administrators):

```dotenv
# Where people reach this server; used in every copy-paste command.
PUBLIC_URL=http://192.168.1.50:7860

# The bucket. Credentials are named here and set below; the API never returns them.
STORAGE_TARGETS_JSON=[{"id":"minio-main","name":"MinIO models","bucket":"models","prefix":"models","endpoint_url":"http://minio:9000","region":"us-east-1","addressing_style":"path","use_ssl":false,"access_key_env":"MINIO_MAIN_KEY","secret_key_env":"MINIO_MAIN_SECRET"}]
MINIO_MAIN_KEY=hugginghack
MINIO_MAIN_SECRET=LONG_RANDOM_SECRET

# New uploads go to the bucket, and the site keeps its own files there too.
DEFAULT_STORAGE_TARGET=minio-main
SYSTEM_STORAGE_TARGET=minio-main
# SYSTEM_STORAGE_PREFIX=models/_system     # the default; change only if you must

# The database: either an existing server...
DATABASE_URL=postgresql://hugginghack:long-random-password@db-host:5432/hugginghack
# ...or the Compose overlay (then leave DATABASE_URL empty):
# POSTGRES_PASSWORD=long-random-password

# Offline: never reach huggingface.co.
HF_DOWNLOADS_ENABLED=false
```

Notes:

- `id` (`minio-main`) is permanent: models and the site's files refer to it. Choose it once.
- `SYSTEM_STORAGE_TARGET` must name a target in `STORAGE_TARGETS_JSON`. An unknown id
  stops the server at start, on purpose, so pictures and git history never land somewhere
  unexpected.
- With `use_ssl: true` and a private certificate authority, copy the CA's PEM file to
  `./data/internal-ca.pem` and set `AWS_CA_BUNDLE=/data/internal-ca.pem`.

## 7. Start and verify

With an existing PostgreSQL server:

```bash
docker compose up -d
```

With the PostgreSQL overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

Then open `PUBLIC_URL` and create the **owner** account (the first visit asks for it).

**Checks:**

1. `curl -s http://localhost:7860/api/health` answers with `"status": "ok"`.
2. **Admin → Server** shows **Engine: PostgreSQL** under Database, and **Site data:
   s3://models/models/_system/** under Storage.
3. **Admin → Storage** shows **MinIO models** as **Connected**, and the **Site data** box
   says **In S3**. The box writes, reads back, and deletes a small file each time the page
   loads, so a green **In S3** means the folder really works.
4. The bucket has `models/_system/README.txt`:

   ```bash
   mc cat hh/models/models/_system/README.txt
   ```

## 8. Bring an existing installation over

Skip this section for a new installation.

### The database

Copy SQLite into the empty PostgreSQL database **before** anyone opens the web interface on
PostgreSQL (the copy refuses a database that already has accounts):

```bash
docker compose down
cp data/hugginghack.sqlite3 data/hugginghack.sqlite3.backup
# Existing server: set DATABASE_URL in .env, then:
docker compose run --rm hugginghack python -m app.migrate_sqlite
# Or with the overlay: start the database, then copy:
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d postgres
docker compose -f docker-compose.yml -f docker-compose.postgres.yml run --rm hugginghack \
  python -m app.migrate_sqlite
```

### Profile pictures and git history

Nothing to do. On the first start with `SYSTEM_STORAGE_TARGET` set, pictures from the old
data folder are copied into `_system/`, each copy is read back, and only then is the local
file removed. If the bucket cannot be reached, the files stay and the copy is tried again on
the next start. Git history is saved to the bucket the next time each model is cloned.

**Check:** the old folders are empty and the bucket has the pictures:

```bash
ls data/avatars data/system 2>/dev/null
mc ls --recursive hh/models/models/_system/avatars/
```

### Models on local disk

Models that are still on the server's disk are not in the bucket yet. For each one:
**Admin → Storage → Local disk → Move**, choose **MinIO models**, and type the model's name.
The move copies every file, reads each one back to check its SHA-256, switches the model to
the bucket, and removes the local copy once no download is reading it. Pulls keep working
throughout, including ones that pinned a revision before the switch. Tick **Keep a copy on
this server's disk** for models a runtime loads from the shared model path.

**Check:** the **Local disk** section of the Storage page lists no models, or only ones you
chose to keep there.

## 9. Serve a model

Models in the bucket are served to clients straight from the bucket, so they work even when
no local copy exists. On a GPU machine on the same network:

```bash
export HF_ENDPOINT=http://192.168.1.50:7860
vllm serve "owner/name"
```

Docker:

```bash
docker run --runtime nvidia --gpus all \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  --env "HF_ENDPOINT=http://192.168.1.50:7860" \
  -p 8000:8000 --ipc=host \
  vllm/vllm-openai:latest --model owner/name
```

For a private model, create a read token under **Account → API tokens** and also set
`HF_TOKEN=hht_…`. The same `HF_ENDPOINT` works for the `hf` CLI and Transformers;
`git clone http://192.168.1.50:7860/owner/name` works with `git-lfs` installed. Each model's
**Use this model** button shows these commands with the right address filled in.

## 10. Back up, restore, and upgrade

**Back up** the two places that matter, on your usual schedule:

```bash
pg_dump "postgresql://hugginghack:…@db-host:5432/hugginghack" > hugginghack.sql
mc mirror hh/models /backup/hugginghack-bucket      # or your bucket replication
```

Keep `.env` with them: it holds the configuration and the names of the credentials. The
server's volumes do not need backing up.

**Restore** onto a new server: carry the image across ([step 3](#3-carry-the-images-across)),
restore the database and the bucket, put back `.env`, and start
([step 7](#7-start-and-verify)). The first start rescans the bucket; models are served from
it right away, and local copies build up again as models are restored to the cache.

**Upgrade:** build and carry the new image as in [step 3](#3-carry-the-images-across), then
`docker compose up -d` (with `-f docker-compose.postgres.yml` if you use the overlay). The
database schema upgrades itself on start. Hard-refresh browsers once.

### Local disk

The `/models` volume is a cache. Uploads are written there first and then copied to the
bucket, and the local copy stays for fast pulls. To free space, open a model and choose
**Remove local cache** (the bucket copy stays); **Restore to local cache** brings it back
before loading the model in a runtime that reads the shared path.

## 11. Troubleshooting

| Symptom | Fix |
| --- | --- |
| The server stops at start with an unknown storage target | `SYSTEM_STORAGE_TARGET` or `DEFAULT_STORAGE_TARGET` names an id that is not in `STORAGE_TARGETS_JSON`. Fix the id and start again. |
| The **Site data** box is amber and says **Offline** | The bucket is unreachable or the key lacks permission under the prefix. Check `endpoint_url`, the credential variables, and the policy in [step 4](#4-prepare-the-bucket). Until it is back, pictures show initials and new pictures cannot be uploaded; models already cached locally keep serving. |
| The **Site data** box says **Local disk** | `SYSTEM_STORAGE_TARGET` is not set (it defaults to `local`). Set it to the bucket's id and restart. |
| Pictures did not move to the bucket | The bucket was unreachable at start; the files are still in `data/` and move on the next start once it is reachable. |
| `git pull` fails on a clone made before the server was replaced | The git history was on the old server's disk because `SYSTEM_STORAGE_TARGET` was not set yet. Clone the model again; from then on the history is kept in the bucket. |
| A model shows **S3 only** | Normal: it is pulled from the bucket. Restore it to the local cache only for runtimes that read the shared path. |
| The Storage page warns that a model exists in more than one location | The warning names the copy that is served and the one that is ignored. Remove the copy you do not want. |

## 12. Checklist

Use this list to confirm a deployment is complete.

- [ ] `hugginghack:local` is loaded on the offline host.
- [ ] The bucket exists, and the key can list, read, write, and delete under the prefix.
- [ ] PostgreSQL is reachable, and `DATABASE_URL` (or `POSTGRES_PASSWORD` with the overlay) is set.
- [ ] `.env` sets `PUBLIC_URL`, `STORAGE_TARGETS_JSON`, the credential variables,
      `DEFAULT_STORAGE_TARGET`, and `SYSTEM_STORAGE_TARGET` to the bucket's id.
- [ ] `/api/health` reports `ok`.
- [ ] **Admin → Server** shows PostgreSQL and **Site data** at `s3://…/_system/`.
- [ ] **Admin → Storage** shows the bucket **Connected** and the **Site data** box **In S3**.
- [ ] `_system/README.txt` exists in the bucket.
- [ ] No models remain on **Local disk**, except any you chose to keep there.
- [ ] A GPU machine can `vllm serve` a model with `HF_ENDPOINT` pointing at the server.
- [ ] PostgreSQL and the bucket are in your backups, together with `.env`.
