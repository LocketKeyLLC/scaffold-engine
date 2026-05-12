# syntax=docker/dockerfile:1.7
# Scaffold Engine — Verilator sidecar.
# Isolates the Verilator + g++ toolchain behind a small FastAPI HTTP
# surface so untrusted HDL input never compiles inside the orchestrator's
# process tree. Like ngspice (§17.140), this is the ground-truth oracle
# for digital design: every numeric claim in an HDL design report joins
# back to a sim_runs row with tool='verilator'.
#
# Two-stage build: stage 1 compiles Verilator from source (so we control
# the exact version), stage 2 carries only the verilator binary + the
# runtime toolchain Verilator-generated C++ needs (g++, make, perl).

ARG VERILATOR_VERSION=v5.024

# ────────────────────────────────────────────────────────────────────────────
# Stage 1: builder — clones + builds Verilator from upstream source.
# Same python:3.12.13-slim base as the runtime stage (and as the
# orchestrator's Dockerfile) so we only pull one image digest. python3
# is needed at build time anyway (Verilator's configure invokes it).
# Discarded once the runtime stage is built.
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.12.13-slim@sha256:ec948fa5f90f4f8907e89f4800cfd2d2e91e391a4bce4a6afa77ba265bc3a2fe AS builder

ARG VERILATOR_VERSION

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
        autoconf g++ make perl \
        flex bison help2man \
        libfl-dev zlib1g-dev ccache \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN git clone --depth 1 --branch "${VERILATOR_VERSION}" \
        https://github.com/verilator/verilator.git verilator

WORKDIR /build/verilator
RUN autoconf \
    && ./configure --prefix=/usr/local \
    && make -j "$(nproc)" \
    && make install \
    && rm -f /usr/local/bin/verilator_bin_dbg


# ────────────────────────────────────────────────────────────────────────────
# Stage 2: runtime — verilator binary + the toolchain its generated C++
# needs at runtime (g++, make, perl). Plus FastAPI for the HTTP surface.
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.12.13-slim@sha256:ec948fa5f90f4f8907e89f4800cfd2d2e91e391a4bce4a6afa77ba265bc3a2fe AS runtime

ARG VERILATOR_VERSION
ENV DEBIAN_FRONTEND=noninteractive

# Runtime deps: g++/make/perl are needed because verilator emits C++ and
# invokes make to build the simulation binary on every /run call.
# ccache is invoked by verilator's generated Makefile by default — if
# absent, the build fails with "ccache: No such file or directory" even
# though it would otherwise be a pure g++ build. We could disable it via
# OBJCACHE='' but installing ccache also gives us a real warm-cache win
# (a second /run call with the same testbench reuses object files).
RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ make perl libfl2 zlib1g ccache \
    && rm -rf /var/lib/apt/lists/*

# Copy the verilator install from the builder stage. /usr/local layout:
#   bin/verilator           (Perl wrapper script)
#   bin/verilator_bin       (C++ binary, stripped)
#   bin/verilator_coverage  (coverage report tool)
#   share/verilator/        (headers, includes, examples)
COPY --from=builder /usr/local/bin/verilator         /usr/local/bin/verilator
COPY --from=builder /usr/local/bin/verilator_bin     /usr/local/bin/verilator_bin
COPY --from=builder /usr/local/bin/verilator_coverage /usr/local/bin/verilator_coverage
COPY --from=builder /usr/local/share/verilator       /usr/local/share/verilator

RUN groupadd --system --gid 10003 verilator \
    && useradd  --system --uid 10003 --gid 10003 \
                --home-dir /code --shell /usr/sbin/nologin verilator

WORKDIR /code

ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY --chown=root:root server.py /code/server.py

LABEL verilator.version="${VERILATOR_VERSION}"

# ccache's default cache dir is $HOME/.ccache, which on this read_only
# rootfs (cap_drop ALL, no writable HOME) yields
# "ccache: error: Read-only file system" on every build. Pointing it at
# the /tmp tmpfs gives the build a writable cache and ccache turns into
# a real per-container warm-cache for repeat /run calls.
ENV CCACHE_DIR=/tmp/ccache

USER verilator:verilator
EXPOSE 8002
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8002", "--log-level", "info"]
