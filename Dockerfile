# syntax=docker/dockerfile:1.7
# ────────────────────────────────────────────────────────────────────────────
# Stage 1: builder — installs ALL deps (prod + dev), pre-downloads HF weights.
# Discarded once runtime/dev are built.
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.12.13-slim@sha256:ec948fa5f90f4f8907e89f4800cfd2d2e91e391a4bce4a6afa77ba265bc3a2fe AS builder

WORKDIR /code

# Build-time only: compilers/headers if any wheel needs them.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Use a dedicated venv so we can copy site-packages cleanly into runtime.
ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN pip install --no-cache-dir "setuptools==71.1.0"

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt
# Dev deps live in the same venv but only the runtime stage prunes them.
RUN pip install --no-cache-dir -r requirements-dev.txt

# §17.243 — pre-download reranker weights into the HF cache at the
# layout sentence-transformers actually reads from at runtime.
#
# HF_HOME is the standard env var; HF's snapshot_download then writes
# to ``$HF_HOME/hub/models--<org>--<repo>/...`` — which is exactly
# where sentence-transformers' CrossEncoder load looks. Without
# setting HF_HOME here, the old layout was ``cache_dir/models--*/``
# (no /hub) and the production code path (which uses
# HF_HOME=/code/.cache/huggingface from compose) silently bypassed
# the baked cache and re-downloaded on every fresh deployment.
#
# §17.244 — model name is now a build ARG (default matches
# settings.model_reranker in app/config.py:173). Compose forwards
# the .env value via build.args so a `MODEL_RERANKER=…` swap in
# .env + `docker compose build scaffold-orchestrator` bakes the
# new model into the image — keeping the Dockerfile, .env, and
# app/config.py defaults aligned at deploy time.
#
# Net image size unchanged — same ~600 MB of weights, just at the
# right path. Fresh deployments now run the orchestrator + harness
# sidecars in HF_HUB_OFFLINE mode from the image's pre-baked cache
# (no rate-limited HF Hub round-trip; see §17.239).
ARG MODEL_RERANKER=tomaarsen/Qwen3-Reranker-0.6B-seq-cls
ENV HF_HOME=/code/.cache/huggingface
# §17.423 — retry with backoff. The bare snapshot_download failed the WHOLE
# image build whenever the HF Hub returned "429 Too Many Requests" on the
# model-info call — a recurring CI flake (broke the §17.419 + §17.421 full-suite
# runs). 5 attempts with increasing backoff (15/30/45/60 s) ride out a transient
# rate-limit; a genuinely persistent failure still fails the build (exit 1)
# rather than baking an image with no reranker weights.
RUN for i in 1 2 3 4 5; do \
      if python -c "from huggingface_hub import snapshot_download; snapshot_download('${MODEL_RERANKER}')"; then \
        echo "snapshot_download: ${MODEL_RERANKER} cached (attempt $i)"; \
        break; \
      fi; \
      if [ "$i" = "5" ]; then \
        echo "snapshot_download: failed after 5 attempts" >&2; \
        exit 1; \
      fi; \
      echo "snapshot_download attempt $i failed (likely HF 429); backing off $((i * 15))s..." >&2; \
      sleep $((i * 15)); \
    done


# ────────────────────────────────────────────────────────────────────────────
# Stage 2: runtime — production image. No dev deps, no tests, no Makefile.
# Runs as non-root user `scaffold` (UID/GID 10001). The named-volume mount
# points (/code/.cache/huggingface, /var/log/scaffold) are pre-created and
# chowned so first-creation of the volumes inherits the non-root ownership.
# Existing volumes from a root-era build need a one-time chown — see
# scripts/chown_named_volumes.sh.
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.12.13-slim@sha256:ec948fa5f90f4f8907e89f4800cfd2d2e91e391a4bce4a6afa77ba265bc3a2fe AS runtime

WORKDIR /code

# Runtime-only system deps. (curl + jq retained for healthchecks/debug;
# 'make' dropped — runtime never invokes Make.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        jq \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. Fixed UID/GID 10001 so volume ownership is
# stable across rebuilds and reproducible across hosts.
RUN groupadd --system --gid 10001 scaffold \
    && useradd  --system --uid 10001 --gid 10001 \
                --home-dir /code --shell /usr/sbin/nologin scaffold

# Copy the populated venv from builder. Ownership stays root:root —
# the venv is read-only at runtime, scaffold only needs +rx.
ENV VIRTUAL_ENV=/opt/venv
COPY --from=builder /opt/venv /opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
# Image-intrinsic so `from scaffold_client import ...` (app/web/routes.py)
# and `cd /code/cli && python -m scaffold_cli ...` work without compose
# having to re-declare them on every service.
ENV PYTHONPATH="/code:/code/sdk"
# §17.243 — image-intrinsic HF_HOME (matches the builder pre-bake path
# above). Compose still sets this for clarity, but the image is now
# self-contained — a fresh `docker run scaffold-engine:dev` will find
# the cached reranker without any external configuration.
ENV HF_HOME="/code/.cache/huggingface"

# Prune dev-only packages so this stage carries prod deps only.
COPY requirements-dev.txt /tmp/requirements-dev.txt
COPY scripts/_prune_dev_deps.py /tmp/_prune_dev_deps.py
RUN python /tmp/_prune_dev_deps.py /tmp/requirements-dev.txt \
    && rm /tmp/requirements-dev.txt /tmp/_prune_dev_deps.py

# Copy pre-downloaded HF cache, owned by scaffold so the named volume
# mounted at this path inherits 10001:10001 on first creation.
COPY --from=builder --chown=scaffold:scaffold /code/.cache /code/.cache

# Application code — owned by root, world-readable. scaffold reads via
# the world-rx bits; no write needed (code is shipped, not mutated).
COPY --chown=root:root app/                       /code/app/
COPY --chown=root:root scripts/                   /code/scripts/
COPY --chown=root:root db/                        /code/db/
# sdk/scaffold_client is imported at runtime by app/web/routes.py.
COPY --chown=root:root sdk/scaffold_client/       /code/sdk/scaffold_client/
# cli/scaffold_cli backs the host-side make targets that shell into the
# running container.
COPY --chown=root:root cli/scaffold_cli/          /code/cli/scaffold_cli/

# Pre-create the log mount point with scaffold ownership so a fresh
# scaffold-logs named volume is created writable for UID 10001. /code
# itself stays root-owned: the app does not write under /code.
RUN mkdir -p /var/log/scaffold && chown scaffold:scaffold /var/log/scaffold

USER scaffold:scaffold
EXPOSE 8000
CMD ["python", "-m", "app.run_server"]


# ────────────────────────────────────────────────────────────────────────────
# Stage 3: dev — runtime + dev deps + tests + Makefile. Used by make test/CI.
# Selected via `target: dev` in docker-compose.dev.yml.
# Runs as the same scaffold UID/GID (10001) as runtime so test artifacts
# created via the writable bench mount land at predictable ownership.
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.12.13-slim@sha256:ec948fa5f90f4f8907e89f4800cfd2d2e91e391a4bce4a6afa77ba265bc3a2fe AS dev

WORKDIR /code

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        jq \
        make \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 scaffold \
    && useradd  --system --uid 10001 --gid 10001 \
                --home-dir /code --shell /bin/bash scaffold

# X.28: docker-compose.dev.yml's `user: "1000:1000"` override needs a
# matching /etc/passwd entry — huggingface_hub calls pwd.getpwuid(1000)
# during reranker load and aborts with KeyError when the UID is unknown.
# Runtime stage doesn't need this user (always runs as scaffold:10001).
RUN groupadd --gid 1000 dev \
    && useradd  --uid 1000 --gid 1000 --home-dir /tmp --shell /bin/bash --no-create-home dev

ENV VIRTUAL_ENV=/opt/venv
COPY --from=builder /opt/venv /opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
ENV PYTHONPATH="/code:/code/sdk"
# §17.243 — same HF_HOME as runtime stage. Dev image is also a sidecar
# substrate; harness sidecars need to find the cached reranker.
ENV HF_HOME="/code/.cache/huggingface"

COPY --from=builder --chown=scaffold:scaffold /code/.cache /code/.cache

COPY --chown=root:root app/           /code/app/
COPY --chown=root:root tests/         /code/tests/
COPY --chown=root:root scripts/       /code/scripts/
COPY --chown=root:root db/            /code/db/
COPY --chown=root:root sdk/           /code/sdk/
COPY --chown=root:root cli/           /code/cli/
# §17.400/§17.401 — pipelines/ into the DEV/test stage only (the prod
# `runtime` stage deliberately omits it; pipelines live in the OWUI
# container). The suite has vendor-parity tests (test_status_icons_vendor.py
# etc.) that READ pipelines/_vendor/*, AND pipeline-bootstrap fixtures that
# WRITE pipelines/<name>/valves.json — so this must be owned by the runtime
# `scaffold` user (§17.401: root:root made ~526 tests error with
# PermissionError on the valves.json write). Mirrors local dev, where
# docker-compose.dev.yml bind-mounts ./pipelines writable.
COPY --chown=scaffold:scaffold pipelines/ /code/pipelines/
COPY --chown=root:root Makefile       /code/Makefile
COPY --chown=root:root pyproject.toml /code/pyproject.toml

RUN mkdir -p /var/log/scaffold && chown scaffold:scaffold /var/log/scaffold

USER scaffold:scaffold
EXPOSE 8000
CMD ["python", "-m", "app.run_server"]
