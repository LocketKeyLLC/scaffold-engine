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
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.12.13-slim AS runtime

WORKDIR /code

# Runtime-only system deps. (curl + jq retained for healthchecks/debug;
# 'make' dropped — runtime never invokes Make.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        jq \
    && rm -rf /var/lib/apt/lists/*

# Copy the populated venv from builder.
ENV VIRTUAL_ENV=/opt/venv
COPY --from=builder /opt/venv /opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Prune dev-only packages so this stage carries prod deps only.
COPY requirements-dev.txt /tmp/requirements-dev.txt
COPY scripts/_prune_dev_deps.py /tmp/_prune_dev_deps.py
RUN python /tmp/_prune_dev_deps.py /tmp/requirements-dev.txt \
    && rm /tmp/requirements-dev.txt /tmp/_prune_dev_deps.py

# Copy pre-downloaded HF cache.
COPY --from=builder /code/.cache /code/.cache

# Application code only — no tests/, no Makefile, no pyproject in runtime.
COPY app/ /code/app/
COPY scripts/ /code/scripts/
COPY db/ /code/db/

EXPOSE 8000
CMD ["python", "-m", "app.run_server"]


# ────────────────────────────────────────────────────────────────────────────
# Stage 3: dev — runtime + dev deps + tests + Makefile. Used by make test/CI.
# Selected via `target: dev` in docker-compose.dev.yml.
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.12.13-slim AS dev

WORKDIR /code

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        jq \
        make \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv
COPY --from=builder /opt/venv /opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY --from=builder /code/.cache /code/.cache

COPY app/         /code/app/
COPY tests/       /code/tests/
COPY scripts/     /code/scripts/
COPY db/          /code/db/
COPY Makefile     /code/Makefile
COPY pyproject.toml /code/pyproject.toml

EXPOSE 8000
CMD ["python", "-m", "app.run_server"]
