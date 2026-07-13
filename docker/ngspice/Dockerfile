# syntax=docker/dockerfile:1.7
# Scaffold Engine — ngspice sidecar.
# Isolates the ngspice CLI behind a small FastAPI HTTP surface so the
# orchestrator never runs untrusted SPICE input in its own process tree.
# Network-only contract — no shared volumes, no host filesystem access.
FROM python:3.14.6-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS runtime

WORKDIR /code

RUN apt-get update && apt-get install -y --no-install-recommends \
        ngspice \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10002 ngspice \
    && useradd  --system --uid 10002 --gid 10002 \
                --home-dir /code --shell /usr/sbin/nologin ngspice

ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY --chown=root:root server.py /code/server.py

USER ngspice:ngspice
EXPOSE 8001
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001", "--log-level", "info"]
