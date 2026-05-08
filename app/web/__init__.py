"""Sprint J.2 — native single-page web UI.

Phase 1 (J.2.a): read-only browse — jobs list + job detail. Submit/
confirm/execute flows land in J.2.b / J.2.c.

Mounted by app/main.py:
  - StaticFiles at ``/static``
  - APIRouter from app.web.routes (with prefix ``/web``)
  - ``GET /`` → 302 to ``/web/jobs``

The UI consumes the orchestrator over HTTP-loopback via
``scaffold_client.Client``, dogfooding the SDK as the second consumer
after the CLI. Routes are auth-bypassed; the embedded SDK Client
carries ``settings.scaffold_api_key`` for the loopback request.
"""
