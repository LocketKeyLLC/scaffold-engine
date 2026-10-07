# §17.1409 — the rehearsal service. A draft's code runs here, chrooted into a fresh copy
# of this image's own filesystem, against COPIES of the real files, before anyone
# approves it. No Docker socket, no extra capabilities, an internal network with no
# route out, and nothing of the real machines but the copied files it is handed.
FROM node:20-bookworm-slim
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends python3 curl ca-certificates procps \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/node_global
RUN npm init -y >/dev/null && npm install express@4 >/dev/null
WORKDIR /
ENV NODE_PATH=/opt/node_global/node_modules
# units name /usr/bin/node (Debian's path); the node image keeps it in /usr/local/bin
RUN ln -sf /usr/local/bin/node /usr/bin/node
COPY shims/ /usr/local/shim/
RUN chmod +x /usr/local/shim/* && mkdir -p /rehearsal
ENV PATH=/usr/local/shim:$PATH
COPY rehearse.py /usr/local/bin/rehearse.py
# the pristine tree each job is copied from (the chroot gets PATH/NODE_PATH from rehearse.py's env)
RUN mkdir /pristine && tar -C / --exclude=./proc --exclude=./sys --exclude=./dev --exclude=./pristine \
      --exclude=./jobs -cf - . | tar -C /pristine -xf - && mkdir -p /pristine/dev /pristine/proc /pristine/tmp \
 && chmod 1777 /pristine/tmp \
 && rm -rf /pristine/var/cache/apt /pristine/var/lib/apt/lists /pristine/root/.npm \
 && chown -R root:root /pristine && chmod -R a+rX /pristine
# (root-owned and readable, so the hardened runtime -- every capability dropped but
#  SYS_CHROOT and MKNOD -- can copy it without CAP_CHOWN or CAP_DAC_READ_SEARCH)
COPY rehearsal_server.py /usr/local/bin/rehearsal_server.py
EXPOSE 8799
HEALTHCHECK --interval=30s --timeout=5s CMD ["curl", "-fsS", "http://127.0.0.1:8799/health"]
CMD ["python3", "/usr/local/bin/rehearsal_server.py"]
