# syntax=docker/dockerfile:1.7
# ────────────────────────────────────────────────────────────────────────────
# Stage 1: builder — installs ALL deps (prod + dev), pre-downloads HF weights.
# Discarded once runtime/dev are built.
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.12.13-slim AS builder

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

# Pre-download reranker weights (Task #16) into a stage-shared cache dir.
RUN python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download('tomaarsen/Qwen3-Reranker-0.6B-seq-cls', \
                  cache_dir='/code/.cache/huggingface')"


# ────────────────────────────────────────────────────────────────────────────
# Stage 2: runtime — production image. No dev deps, no tests, no Makefile.
# Runs as non-root user `scaffold` (UID/GID 10001). The named-volume mount
# points (/code/.cache/huggingface, /var/log/scaffold) are pre-created and
# chowned so first-creation of the volumes inherits the non-root ownership.
# Existing volumes from a root-era build need a one-time chown — see
# scripts/chown_named_volumes.sh.
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.12.13-slim AS runtime

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
FROM python:3.12.13-slim AS dev

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

COPY --from=builder --chown=scaffold:scaffold /code/.cache /code/.cache

COPY --chown=root:root app/           /code/app/
COPY --chown=root:root tests/         /code/tests/
COPY --chown=root:root scripts/       /code/scripts/
COPY --chown=root:root db/            /code/db/
COPY --chown=root:root sdk/           /code/sdk/
COPY --chown=root:root cli/           /code/cli/
COPY --chown=root:root Makefile       /code/Makefile
COPY --chown=root:root pyproject.toml /code/pyproject.toml

RUN mkdir -p /var/log/scaffold && chown scaffold:scaffold /var/log/scaffold

USER scaffold:scaffold
EXPOSE 8000
CMD ["python", "-m", "app.run_server"]
