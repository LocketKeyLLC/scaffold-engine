---
name: verify
description: Runtime verification recipe for scaffold-engine assist changes — drive the deployed orchestrator's /assist/{sid}/message SSE surface on a SCRATCH session and read the stage log lines.
---

# Verifying assist-path changes at the surface

**Never drive the operator's live session** (its scribe distills whatever you send into durable facts). Use a
scratch session: `select id, current_node_key from assist_sessions s join jobs j on j.id=s.job_id where j.title
like 'Internal Status Page%'` (e.g. `0bb0df92-…`, step T2 "Install PM2").

**Deploy** (prod image, no bind mounts): `make build` — refuses while `assist_turn_runs.status='running'`.
Confirm: `docker inspect scaffold-orchestrator --format '{{.State.StartedAt}}'` and `curl -s localhost:8000/health`.

**Drive one turn** (SSE, terminal event is `assist_turn_done`):
```bash
KEY=$(grep '^SCAFFOLD_API_KEY=' .env | cut -d= -f2)
python3 -c 'import json;print(json.dumps({"message": open("paste.txt").read()}))' > body.json
curl -sN -m 400 -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d @body.json http://localhost:8000/assist/<SID>/message > sse.txt &
until grep -q '^event: assist_turn_done' sse.txt; do sleep 5; done
```
Events: `assist_turn_routed` (`action` = fix|ask|submit|note, `override`), `assist_answer` (`text`), `assist_turn_done`.
A no-error shell paste routes to **fix**; a plain how-to question to **ask**; "actually / start over / …" to **note** (pivot).

**Read the stages** (`docker logs scaffold-orchestrator --since 10m`, JSON lines; ignore `q=healthcheck`):
`assist_fix_need_query kind=… q=…` / `assist_research_no_need` → `assist_web_query_*_enforced` →
`searxng:8080/search?q=…` (the real query) → `assist_research_sources` / `assist_evidence_dropped_offtopic` →
`assist_answer_grounding … unsupported= cite_score= regenerated= annotated=`. A footer `⚠️ Unverified specifics`
in `assist_answer.text` is the operator-visible outcome. Turns take 60–120 s on cloud models.

Read-only alternative (no writes): throwaway `scaffold-engine:dev` on `--network ai-network --env-file
<orchestrator printenv>` calling `run_step_fix` / `run_step_research` with `db.commit` = AsyncMock,
`capture_assistant_reply` / `record_friction` patched, then rollback (§17.1027 sprint-log entry).
