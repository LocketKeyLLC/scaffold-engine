# syntax=docker/dockerfile:1.7
# Scaffold Engine — code-runner sidecar (§17.433).
# Executes UNTRUSTED, LLM-generated code + its tests behind a small FastAPI
# HTTP surface so generated code never runs inside the orchestrator's process
# tree — the software-path ground-truth oracle (analog of the ngspice/
# Verilator/symbiyosys sidecars). Single stage: just python + pytest; the
# generated code runs via `python`/`pytest` from the venv on PATH.
#
# Same python:3.12.13-slim base digest as the orchestrator + EDA sidecars so
# the host pulls one image. Runtime hardening (cap_drop ALL, no-new-privileges,
# read_only rootfs, exec tmpfs, mem/pids limits, no egress) is in
# docker-compose.yml; the strong isolation upgrade is the gVisor `runsc`
# runtime (operator step — see the §17.433 OVERVIEW entry).
FROM python:3.14.6-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS runtime

ENV DEBIAN_FRONTEND=noninteractive

RUN groupadd --system --gid 10004 coderunner \
    && useradd  --system --uid 10004 --gid 10004 \
                --home-dir /code --shell /usr/sbin/nologin coderunner

WORKDIR /code

ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY --chown=root:root server.py /code/server.py

USER coderunner:coderunner
EXPOSE 8010
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8010", "--log-level", "info"]
