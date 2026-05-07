# scaffold-engine-client

Typed Python client for the [Scaffold Engine](https://github.com/AEDeFruscio/scaffold-engine) orchestrator HTTP API. Pinned to API version **v1.0.0** (`docs/openapi.json`).

```bash
pip install scaffold-engine-client
```

For local development against an unreleased orchestrator:

```bash
pip install -e ./sdk
```

## Quick start (sync)

```python
from scaffold_client import Client

with Client("http://localhost:8000", api_key="...") as c:
    health = c.health()                 # GET /health, no auth required
    job = c.ideate("Build a markdown linter")
    job_id = job["job_id"]

    # Confirm to kick off Phase 2 (research → plan → execute) — long-running.
    c.confirm(job_id)                   # bump timeout= on the constructor first

    # Browse + manage
    for row in c.jobs.list(limit=10)["jobs"]:
        print(row["id"], row["status"], row["title"])
```

## Quick start (async)

`AsyncClient` mirrors `Client` for non-streaming endpoints and adds streaming
helpers (`aiter_research`, `aiter_execute_all`, `aiter_research_reply`,
`aiter_research_pdf`) for the SSE-based endpoints.

```python
import asyncio
from scaffold_client import AsyncClient

async def main():
    async with AsyncClient("http://localhost:8000", api_key="...") as c:
        async for event in c.aiter_research("kubernetes operators", depth="medium"):
            if event["event"] == "extraction_complete":
                print("extracted:", event["data"]["count"])
            if event["event"] == "convergence":
                break

asyncio.run(main())
```

Breaking out of the `async for` cleanly closes the underlying httpx stream;
the orchestrator's keepalive watchdog detects the dead socket within ~2s and
finalizes the session as `cancelled`.

## Errors

Every transport failure raises a subclass of `ScaffoldError`. Catch the base
class once if you don't care about the specific cause; otherwise branch:

```python
from scaffold_client import (
    ScaffoldError, ConnectionError, TimeoutError,
    AuthenticationError, PermissionError, NotFoundError,
    RateLimitError, RequestError, OrchestratorError,
)

try:
    c.jobs.status(job_id)
except NotFoundError:
    print("job no longer exists")
except AuthenticationError:
    print("rotate the API key")
except ScaffoldError as exc:
    print(f"orchestrator call failed: {exc}")
```

| Subclass | HTTP | When |
|---|---|---|
| `ConnectionError` | — | Cannot reach the orchestrator (DNS/TCP failure, container down) |
| `TimeoutError` | — | Request did not complete within `timeout=` |
| `AuthenticationError` | 401 | API key missing / invalid / rejected |
| `PermissionError` | 403 | Key valid but lacks permission |
| `NotFoundError` | 404 | Resource (job, schedule, session, …) does not exist |
| `RateLimitError` | 429 | Caller throttled |
| `RequestError` | 4xx | Other client errors |
| `OrchestratorError` | 5xx | Server-side failure |

## API surface

### Top-level workflow

| Method | Endpoint |
|---|---|
| `c.health()` | `GET /health` |
| `c.status()` | `GET /status` |
| `c.logs(job_id)` | `GET /logs/{job_id}` |
| `c.ideate(idea, *, domain=None, model=None)` | `POST /ideate` |
| `c.confirm(job_id, *, feedback=None, push_to_github=False)` | `POST /ideate/confirm` |
| `c.optimize(prompt, *, ...)` | `POST /optimize` |
| `c.execute(job_id, *, ...)` | `POST /execute` |
| `c.skip(job_id, node_key)` | `POST /skip` |

### Resource sub-objects

| Resource | Methods |
|---|---|
| `c.jobs` | `list`, `status`, `delete`, `update`, `cleanup`, `retry` |
| `c.dag` | `get`, `create` |
| `c.prompts` | `list`, `get`, `history`, `update` |
| `c.gt` | `create`, `list`, `search`, `detail`, `stats` |
| `c.rag` | `search`, `dedup` |
| `c.schedule` | `list`, `create`, `delete` |

### Streaming (AsyncClient only)

| Method | Endpoint |
|---|---|
| `c.aiter_research(topic, ...)` | `POST /research` |
| `c.aiter_research_reply(session_id, reply)` | `POST /research/reply` |
| `c.aiter_research_pdf(pdf, ...)` | `POST /research/pdf` (multipart) |
| `c.aiter_execute_all(job_id, ...)` | `POST /execute/all` |

Each yields `{"event": str, "data": Any}` dicts. Pass `include_heartbeats=True`
to surface `: keepalive` comments as `{"event": "heartbeat", "data": None}`
events; otherwise they are filtered.

### Generic escape hatch

Endpoints not yet wrapped (or new ones added in patch releases) are reachable
via:

```python
c.request("GET", "/some/path", params={"limit": 10})
await c.request("POST", "/some/path", json={"foo": 1})
```

## Schemas

`scaffold_client.schemas` re-exports the Pydantic models from
`app/schemas.py` (vendored byte-equal). Use them to validate inputs/outputs
on the caller side:

```python
from scaffold_client.schemas import IdeaInput, JobSummary

req = IdeaInput(idea="markdown linter", domain="eng")
result = c.ideate(**req.model_dump(exclude_none=True))
job = JobSummary.model_validate(result)
```

The vendored copy is kept in sync by a parity test in the orchestrator
suite (`tests/test_sdk_schema_parity.py`); regenerate after editing
`app/schemas.py` with `make sync-schemas`.

## Versioning

The SDK version (`scaffold_client.__version__`) tracks the FastAPI app
version it pins to. v1.x.y of the SDK works with v1.x.y of the
orchestrator API. A major bump on the orchestrator (breaking contract
change) requires a major SDK bump. The committed OpenAPI snapshot
(`docs/openapi.json`) is the canonical contract for each release.

## Contributing

Tests live in `sdk/tests/`. Run inside the orchestrator dev container:

```bash
make test-sdk
```

The byte-equality parity test for the vendored schemas runs as part of the
main orchestrator suite (`make test`). After editing `app/schemas.py`:

```bash
make sync-schemas
make test
```
