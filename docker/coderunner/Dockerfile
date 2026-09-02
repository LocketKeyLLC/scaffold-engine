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
FROM python:3.14.7-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS runtime

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
