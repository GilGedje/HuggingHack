# Images for OpenShift (linux/amd64)

Built image archives for carrying across the air gap to an OpenShift (or any amd64
Kubernetes) cluster. The archives themselves are not in git (`.gitignore`); rebuild them with
the commands below. This folder keeps the recipe and, locally, the files.

| File | What |
| --- | --- |
| `hugginghack-<version>-linux-amd64.tar.gz` | `docker save` of `hugginghack:<version>`, gzip-compressed |
| `hugginghack-<version>-linux-amd64.tar.gz.sha256` | its SHA-256, to check after the transfer |

## What the image is

- Built from the repository's `Dockerfile` for `linux/amd64`, tagged `hugginghack:<version>`.
- Runs as a non-root user (uid 1000) and never needs root. OpenShift's restricted-v2 SCC runs it
  under a random uid in group 0: `/data` and `/models` belong to group 0 and are group-writable,
  so it works with or without the chart's emptyDir volumes, and with a read-only root
  filesystem when those volumes are mounted (as the chart does).
- No build tools, compilers, `curl` or `git` inside; `git clone` from HuggingHack still works
  (the server speaks git's HTTP protocol itself).
- Verified before saving: architecture `amd64`; healthy under a random uid in group 0 with
  capabilities dropped, both on a writable root with no volumes and on a read-only root with
  temporary volumes; no permission errors.

## Build (on a connected machine, any architecture)

```bash
VERSION=1.3.1                      # the app version (backend/app/config.py, Chart.yaml appVersion)
docker buildx build --platform linux/amd64 -t hugginghack:$VERSION --load .
docker save hugginghack:$VERSION | gzip -6 > helm/ocp-images/hugginghack-$VERSION-linux-amd64.tar.gz
(cd helm/ocp-images && shasum -a 256 hugginghack-$VERSION-linux-amd64.tar.gz > hugginghack-$VERSION-linux-amd64.tar.gz.sha256)
```

The web UI stage builds natively (it is plain JavaScript); only the Python stage runs under
emulation on an ARM Mac.

## Load into the internal registry (offline side)

```bash
shasum -a 256 -c hugginghack-1.3.1-linux-amd64.tar.gz.sha256
# with podman or docker
podman load -i hugginghack-1.3.1-linux-amd64.tar.gz
podman tag hugginghack:1.3.1 registry.internal/hugginghack/hugginghack:1.3.1
podman push registry.internal/hugginghack/hugginghack:1.3.1
# or without a container engine
skopeo copy docker-archive:hugginghack-1.3.1-linux-amd64.tar.gz \
  docker://registry.internal/hugginghack/hugginghack:1.3.1
```

Then set `image.repository` / `image.tag` in the umbrella values (see `../README.md`). With
`postgresql.enabled`, mirror `postgres:17-alpine` for amd64 the same way
(`docker pull --platform linux/amd64 postgres:17-alpine`, then save and push).
