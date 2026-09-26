# The web UI is plain JavaScript, the same on every platform: build it natively even when
# the image targets another architecture (docker buildx build --platform linux/amd64).
FROM --platform=$BUILDPLATFORM node:22-alpine AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

# HF_HOME: huggingface_hub's cache must be writable whatever PUID/PGID the container runs as.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/tmp/huggingface

WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY --from=frontend-build /frontend/dist ./static

# Runs unprivileged by default. Bind-mounted /models and /data keep their host owner,
# so they must be writable by uid 1000 (or set PUID/PGID; see docs/AIRGAPPED.md).
# OpenShift runs the image under a random uid in group 0, so the folders it writes also
# belong to group 0 and are group-writable (Red Hat's guidelines for arbitrary user ids).
RUN groupadd --gid 1000 hugginghack \
    && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin hugginghack \
    && mkdir -p /models /data \
    && chown 1000:0 /models /data \
    && chmod 775 /models /data \
    # No setuid or setgid programs (su, passwd, mount, …): nothing in the image can
    # become root, whatever the pod's securityContext says.
    && find / -xdev -perm /6000 -type f -exec chmod a-s {} +
USER 1000:1000

# docker buildx build --build-arg VERSION=… --build-arg REVISION=$(git rev-parse --short HEAD)
ARG VERSION=""
ARG REVISION=""
LABEL org.opencontainers.image.title="HuggingHack" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.description="Self-hosted Hugging Face-style model hub for air-gapped networks" \
      org.opencontainers.image.source="https://github.com/GilGedje/HuggingHack" \
      io.k8s.display-name="HuggingHack" \
      io.openshift.tags="huggingface,model-hub,air-gapped" \
      io.openshift.non-scalable="false"

EXPOSE 7860

# Also used by plain `docker run`; docker-compose.yml sets the same check.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/api/health', timeout=5)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860", "--proxy-headers", "--no-server-header"]

