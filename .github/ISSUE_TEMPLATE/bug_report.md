---
name: Bug report
about: Something isn't working as documented
title: "[bug] "
labels: bug
---

**What happened**
A clear description of the bug.

**What you expected**
What you expected to happen instead.

**Steps to reproduce**
1.
2.
3.

**`make doctor` output**
<!-- Run `make doctor` and paste the result. It confirms container health,
     Ollama reachability, and API-key sync — most setup issues show up here. -->

```
paste here
```

**Environment**
- OS / distro:
- Docker + Compose version (`docker compose version`):
- Ollama version (`ollama --version`):
- scaffold-engine version / commit (`git rev-parse --short HEAD`):
- Inference: CPU-only or GPU?

**Logs**
<!-- Relevant lines from `docker logs scaffold-orchestrator`. Redact any secrets. -->

```
paste here
```

**Additional context**
Anything else that helps.
