# scaffold-engine-client

Typed Python client for the [Scaffold Engine](https://github.com/AEDeFruscio/scaffold-engine) orchestrator HTTP API.

> **Status:** v1.0.0 skeleton (Sprint J.1.b). Constructor + generic `request()` escape hatch shipped. Typed methods land in J.1.c (sync) and J.1.d (async + streaming). Full quickstart docs in J.1.f.

## Install (dev)

```bash
pip install -e ./sdk
```

## Sync usage

```python
from scaffold_client import Client

with Client("http://localhost:8000", api_key="...") as c:
    health = c.request("GET", "/health")
    print(health)
```

## Async usage

```python
from scaffold_client import AsyncClient

async with AsyncClient("http://localhost:8000", api_key="...") as c:
    health = await c.request("GET", "/health")
```

## Errors

Every transport failure raises a subclass of `ScaffoldError`:

- `ConnectionError` — orchestrator unreachable
- `TimeoutError` — request did not complete in time
- `AuthenticationError` — 401
- `PermissionError` — 403
- `NotFoundError` — 404
- `RateLimitError` — 429
- `RequestError` — other 4xx
- `OrchestratorError` — 5xx

## Schemas

`scaffold_client.schemas` mirrors the orchestrator's Pydantic models 1-to-1.
The vendored copy is kept byte-equal to `app/schemas.py` by a parity test
in the orchestrator suite (`tests/test_sdk_schema_parity.py`). Regenerate
after editing `app/schemas.py` with:

```bash
make sync-schemas
```
