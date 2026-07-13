# syntax=docker/dockerfile:1.7
# Scaffold Engine — SymbiYosys sidecar.
# Third ground-truth oracle on the engineering-design track (after
# ngspice §17.140 and verilator §17.141). Provides formal verification:
# SystemVerilog assertions → SMT/SAT → PASS / FAIL / UNKNOWN.
#
# Toolchain comes from the OSS CAD Suite — YosysHQ's coordinated daily
# build of yosys + sby + z3 + boolector + abc + supporting tools, all
# tested together. The orchestrator never touches host filesystem at
# build time; everything lands inside /opt/oss-cad-suite.

ARG OSS_CAD_SUITE_TAG=2026-05-12
ARG OSS_CAD_SUITE_ASSET=oss-cad-suite-linux-x64-20260512.tgz

# ────────────────────────────────────────────────────────────────────────────
# Stage 1: fetch + extract OSS CAD Suite into /opt/oss-cad-suite.
# Kept separate from the runtime stage so the ~2 GB layer of extracted
# files can be COPY'd selectively (we only need the install tree, not
# the tarball or any partial download state).
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.14.6-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS fetcher

ARG OSS_CAD_SUITE_TAG
ARG OSS_CAD_SUITE_ASSET

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

RUN curl -sSL -o /tmp/oss-cad-suite.tgz \
        "https://github.com/YosysHQ/oss-cad-suite-build/releases/download/${OSS_CAD_SUITE_TAG}/${OSS_CAD_SUITE_ASSET}" \
    && tar -xzf /tmp/oss-cad-suite.tgz -C /opt \
    && rm /tmp/oss-cad-suite.tgz \
    && test -x /opt/oss-cad-suite/bin/sby \
    && test -x /opt/oss-cad-suite/bin/yosys


# ────────────────────────────────────────────────────────────────────────────
# Stage 2: runtime — same python:3.12.13-slim base as the other
# sidecars and the orchestrator. Copies the OSS CAD Suite tree from
# the fetcher stage and exposes the FastAPI server on :8003.
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.14.6-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS runtime

ARG OSS_CAD_SUITE_TAG

ENV DEBIAN_FRONTEND=noninteractive

# Runtime apt deps: libgomp1 is needed by some OSS CAD Suite binaries
# (yosys's parallel passes); tini gives clean PID-1 signal handling
# for sby's subprocess tree.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=fetcher /opt/oss-cad-suite /opt/oss-cad-suite

RUN groupadd --system --gid 10004 sby \
    && useradd  --system --uid 10004 --gid 10004 \
                --home-dir /code --shell /usr/sbin/nologin sby

WORKDIR /code

ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="/opt/oss-cad-suite/bin:$VIRTUAL_ENV/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY --chown=root:root server.py /code/server.py

LABEL oss_cad_suite.tag="${OSS_CAD_SUITE_TAG}"

USER sby:sby
EXPOSE 8003
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8003", "--log-level", "info"]
