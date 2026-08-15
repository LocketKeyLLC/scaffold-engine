# Native OpenAI Surface — Implementation Plan

**Status:** proposed · **Branch:** `feat/17787-trace-surfacing` (successor branch to be cut) · **Author:** engine team · **Date:** 2026-08-15

Make the engine **native**: have the orchestrator itself speak the OpenAI chat protocol, so any
OpenAI client drives the engine directly and the OWUI pipeline adapter becomes optional. Three
deliverables, decided with the owner:

1. **Endpoint mapping → NL router / triage** — the intelligence that today lives only in
   `pipelines/scaffold_router.py` (triage, NL routing, `/go` synthesis, `/confirm` auto-chain)
   moves into `app/`, reachable over a native endpoint.
2. **SSE streaming** — that native endpoint streams in OpenAI `chat.completion.chunk` format,
   translating the engine's existing SSE event surface.
3. **An OpenAI "bump"** — **both** a new inbound `POST /v1/chat/completions` (+ `GET /v1/models`)
   **and** a modernization of the outbound `app/providers/openai.py`.

**Primary clients (all three):** OWUI pointed at the engine as an OpenAI connection; the native
`/ui` SPA chat; external OpenAI-protocol clients (curl, `openai` SDK, Continue/Cursor, etc.).
**Triage scope:** full conversational triage ported (qwen-style scoping loop → `/go` synth →
Phase 1 → confirm → Phase 2 → DAG → execute), i.e. true parity, not just intent routing.

---

## 1. Grounding — what exists today (verified 2026-08-15)

### Already in the engine (building blocks)
| Surface | Location | Note |
|---|---|---|
| NL classifier | `POST /route` → `app/routers/route.py:31`, `command_guide.classify_command` (`app/modules/command_guide.py:412`) | read/write/workflow/destructive intents; fail-soft `intent='none'` |
| Phase 1 / 2 / DAG | `/ideate`, `/ideate/confirm`, `/dag`, `/decompose` (`app/routers/workflow.py`) | `/ideate/start` (`workflow.py:99`) is **async JSON**, not SSE — poll `/jobs/{id}` |
| Execute (SSE) | `POST /execute/all` (`app/routers/workflow.py:315`) → `execute_all_nodes` (`app/modules/execution_agent.py:2239`) | per-node SSE; own inline keepalive |
| Assist Mode | `/assist/*` (`app/routers/assist.py`), guide stream `assist.py:358` | has its **own** session store — reuse as-is |
| Outbound OpenAI provider | `app/providers/openai.py` (`OpenAIProvider`, raw httpx) | **no `openai` SDK pinned** |
| LLM-call surface | `model_router.chat` (`app/model_router.py:668`), `stream_chat` (`:721`), `tool_call` (`:752`) | role↔model routed; wrap these inbound |
| SSE constants + framing | `app/sse_events.py`, per-module `_sse` helpers, `app/utils/sse.py:15` `_sse_with_disconnect_watch` | wire format `event: <name>\ndata: <json>\n\n` |
| Auth | `require_api_key` (`app/auth.py:51`), global dep (`app/main.py:530`) | **`X-API-Key` only** — no Bearer |
| Native SPA | `/ui` (`app/ui/`), `api.stream()` consumes POST SSE | §17.778–787 |

### Lives ONLY in `pipelines/scaffold_router.py` (7,879 lines) — no engine equivalent yet
- **Triage** — direct Ollama `/v1/chat/completions` (`_call_triage`, L1481) with `TRIAGE_SYSTEM_PROMPT`
  (L397–582), `_window_messages` (L1399, windows to last N turns **but pins every earlier user
  message**), `_strip_think` (L1427). 4-section Scope/Options/Gaps/My-pick template; emits a `/go`
  offer when load-bearing gaps are covered.
- **`/go` synthesis** — `_synthesize_idea` (L1510), `SYNTHESIS_SYSTEM_PROMPT` (L585–639) with
  timeline reconciliation (§17.694/695/717); full transcript → 3–6 sentence brief; direct Ollama.
- **NL command lifecycle** — `_fast_classify_command` phrase table (L234/161), `_NL_REQUIRED_SLOTS`
  gating (L4466), `_dispatch_nl_command` (L4574), confirm-cards `[nlc]: NL_CONFIRM:<b64>`
  (`_render_nl_confirm` L4930 / `_extract_pending_nl_confirm` L5272 / `_execute_nl_action` L5351),
  `_nl_job_scoped` (L4682), `_resolve_failing_nodes` (L5092, GET `/logs/{id}`).
- **`/confirm` auto-chain** — `_handle_confirm` (L2314): `/ideate/confirm` → `/dag` → execution
  choice; `_execute_and_stream` (L3902) consumes `/execute/all` SSE; keepalive wrapper
  `_post_with_keepalive` (L3303).
- **Dispatch order** — `pipe()` (L1682–2019): task-call short-circuit → slash gate → word-boundary
  slash cmds → resolve active assist → pending pick-list → pending NL-confirm → noise guard →
  sole-active binding → active-assist turn → cross-chat continuity → NL route → triage fallback.

### The decisive design fact
OpenAI `/v1/chat/completions` is **stateless** (client resends full `messages[]`). The pipeline's
triage/NL/confirm machinery is **also** stateless — state is encoded in chat history (markers +
windowing). So the native endpoint reconstructs all conversation state from the incoming
`messages[]`, exactly as the pipeline does from OWUI history. **No new session store** for
triage/confirm; Assist Mode keeps its existing `/assist/*` store.

---

## 2. Target architecture

```
OWUI (OpenAI connection) ┐
external OpenAI clients   ├─▶ POST /v1/chat/completions ─▶ native dispatcher (port of pipe())
/ui SPA chat view        ┘         │                          ├─ slash cmd / NL route (/route + fast-classify)
                                   │                          ├─ confirm-card follow-up (markers in messages[])
GET /v1/models  ◀──────────────────┘                          ├─ triage (windowed, token-streamed)
require_openai_key (Bearer + X-API-Key, OpenAI 401 envelope)  ├─ /go synth → auto-chain (ideate→dag→execute)
                                                              └─ active assist turn (/assist/*)
```

**Auto-chain executes in-process:** the dispatcher calls the engine's own driver generators
(`execute_all_nodes`, research/compile, dag-gen) and translates their SSE events to OpenAI chunks —
**not** a self-HTTP loopback (avoids the single-worker loopback-deadlock trap; see
`web_loopback_needs_sync_def` memory).

### New / touched modules
```
app/openai_schemas.py            # NEW — Pydantic OpenAI wire types (request/chunk/response/models)
app/routers/openai_compat.py     # NEW — /v1 router mounted as a sub-app (bypasses global dep)
app/auth.py                      # +require_openai_key (Bearer OR X-API-Key; OpenAI 401 envelope)
app/native_chat/                 # NEW — the ported dispatcher
  __init__.py
  dispatch.py                    #   pipe()-equivalent precedence chain
  triage.py                      #   TRIAGE_SYSTEM_PROMPT + windowing/pinning + strip-think
  synthesis.py                   #   SYNTHESIS_SYSTEM_PROMPT + /go one-shot
  nl_commands.py                 #   fast-classify table, slot gating, dispatch, confirm-cards
  autochain.py                   #   /go→ideate→brief→confirm→ideate/confirm→dag→execute (in-proc)
  chunking.py                    #   engine SSE event → OpenAI chunk translation
app/providers/openai.py          # bump: reasoning params, strict schema, streamed usage/tool_calls
app/config.py                    # +native_openai_enabled valve (default off); +model_triage role
app/main.py                      # mount /v1 sub-app
app/ui/static/views/chat.js      # Phase 4 — native SPA chat view
```

### Chunk translation (engine SSE → OpenAI `delta`)
| Engine event | Text field | → OpenAI |
|---|---|---|
| `node_token` | `delta` | append `delta.content` |
| `assist_guide_delta` | `text` | append `delta.content` |
| `node_done` | `output` | final flush (skip if `node_token` already forwarded, to avoid dup) |
| `research_complete` | `summary` | final flush |
| `node_start`/`node_retry`/`node_failed`/`dag_generated`/research lifecycle/`queued`/`awaiting_*` | — | status line in `delta.content` |
| `pipeline_complete` | — | terminal chunk, `finish_reason:"stop"` |
| `budget_exhausted`/`blocked` | — | terminal chunk, `finish_reason:"length"`/`"stop"` + status |
| `execution_failed`/`error` | — | error status + `finish_reason:"stop"` |
| `heartbeat`, `: keepalive`, cache/truncation/quality-gate events | — | ignore |

---

## 3. Phases

Each phase = **one commit + a dated `§17.NNN` OVERVIEW.md entry (same commit) + tests + a live
smoke**, per house convention (mirrors the §17.655+ NL-router arc). Proposed numbering
`§17.788…`; the branch head is `§17.787`.

### Phase 0 — Wire protocol + passthrough  ·  §17.788
*Milestone: external OpenAI clients work end-to-end, before any triage port.*
- `app/openai_schemas.py`: `ChatCompletionRequest` (model, messages[], stream, temperature,
  max_tokens, …), `ChatCompletionChunk`, `ChatCompletionResponse`, `ModelList`/`ModelCard`.
- `app/routers/openai_compat.py` mounted as a **sub-app** (MCP mount pattern, `app/main.py:667`) so
  it bypasses the global `X-API-Key` dep; carries `require_openai_key`.
- `require_openai_key` in `app/auth.py`: accept `Authorization: Bearer <SCAFFOLD_API_KEY>` **or**
  `X-API-Key`; 401 returns the OpenAI envelope `{"error":{"message","type","code"}}`.
- `GET /v1/models` → advertise `scaffold-engine`.
- `POST /v1/chat/completions`: **thin passthrough** to `model_router.chat` / `stream_chat`
  (role `model_general`) with correct chunk framing (`chat.completion.chunk`, role-delta first,
  content deltas, `finish_reason`, `data: [DONE]`). Non-stream path returns a full
  `chat.completion` with `usage`.
- **Tests:** unit (schema round-trip, chunk framing, Bearer+X-API-Key auth, 401 envelope);
  **live smoke** — `curl` stream + non-stream, and `openai`-python (`base_url=…/v1`).

### Phase 1 — Outbound provider bump  ·  §17.789  *(independent — may run before/parallel to P0)*
`app/providers/openai.py`:
- Reasoning-model handling: detect o1/o3/gpt-5 family → send `max_completion_tokens` (not
  `max_tokens`), pin `temperature=1`, optional `reasoning_effort` (fixes the current hard-`max_tokens`
  400 on reasoning models — `chat_completion:142`, `stream_chat:244`, `tool_call:338`).
- Strict structured-outputs path in `_apply_openai_response_format` (`:49`): emit `strict:true`
  when the schema qualifies (all-required + `additionalProperties:false`), else keep `strict:false`.
- `stream_options:{include_usage:true}` in `stream_chat` (`:239`); surface the final `usage` chunk so
  streaming can feed cost tracking (`model_router.py:735`).
- Handle `delta.tool_calls` and `delta.refusal` in the SSE loop (`:264`).
- **Open sub-decision — keep raw httpx vs pin `openai` SDK.** Recommendation: **keep raw httpx +
  add params** (the dual-httpx stack — `httpx==0.28.1` app / `httpx2` MCP — makes an SDK pin
  awkward, and the raw path is deliberate per the module docstring). If the SDK is wanted, add
  `openai==<pin>` to `requirements.txt` + `requirements-ci.txt` with a `# §17.789` comment.
- **Tests:** provider unit tests for param selection (reasoning vs standard, strict vs loose).
  Live smoke gated on a real OpenAI/reasoning backend — most roles are cloud-Ollama, which ignores
  full-schema `format` (see `project_structured_outputs` memory), so keep the provider-aware gate.

### Phase 2 — Native NL routing + direct actions  ·  §17.790
Port into `app/native_chat/nl_commands.py`: `_fast_classify_command` phrase table + `_NL_REQUIRED_SLOTS`
gating + intent→action dispatch + confirm-card lifecycle (`[nlc]: NL_CONFIRM:<b64>` encode/decode,
affirmative/negative detection, pending-state reconstruction from incoming `messages[]`).
High-confidence read/action intents (status, results, jobs, rag, research ingests, schedule, gt,
workflow control) execute and stream results as OpenAI content; expensive/destructive intents emit a
confirm card and commit on the next affirmative turn. Reuse `command_guide.classify_command` in-proc
(no self-HTTP to `/route`).
- **Tests:** unit (dispatch table, marker round-trip, slot gating, affirmative/negative). **Live
  `/v1` smoke of representative intents** — mocked `tool_call` never catches intent-anchor drift
  (the load-bearing §17.655 lesson); include "NOT that one" contrast checks.

### Phase 3 — Native triage + `/go` synthesis + `/confirm` auto-chain  ·  §17.791  *(the big one)*
- `app/native_chat/triage.py`: port `TRIAGE_SYSTEM_PROMPT` + `_window_messages` (windowing +
  all-user-message pinning) + `_strip_think`. Route through a **new `model_triage` role**
  (`config.py` default = current triage model) via `model_router.stream_chat` — **token-streamed**,
  a UX upgrade over the pipeline's blocking single call.
- `app/native_chat/synthesis.py`: port `SYNTHESIS_SYSTEM_PROMPT` + timeline reconciliation; full
  transcript (not windowed) → 3–6 sentence brief; fallback-to-joined-user-messages on failure.
- `app/native_chat/autochain.py`: `/go` → `/ideate` (Phase 1) → render brief + feasibility →
  on confirm → `/ideate/confirm` (Phase 2) → `/dag` → `execute_all_nodes` — all **in-process**,
  SSE events translated to OpenAI chunks (status lines + `node_token` deltas + final compiled
  output). Slash commands (`/go`, `/confirm`, `/results`, …) keep working via ported word-boundary
  detection (`_is_cmd`). Honor the `pipe()` precedence chain in `dispatch.py`.
- **Tests:** unit (windowing/pinning determinism, synthesis-prompt assembly, dispatch precedence).
  **Live end-to-end** multi-turn over `/v1` streaming: triage → `/go` → brief → confirm → execute.

### Phase 4 — Client wiring + retire-the-adapter (gated)  ·  §17.792
- Master valve `native_openai_enabled` (**default off**, per default-off-new-surface convention);
  pipeline stays as fallback while off.
- OWUI: register the engine as an OpenAI connection (compose/env doc + admin), verify in-browser
  via Playwright (mint JWT from `WEBUI_SECRET_KEY`; `~/.local/share/playwright-venv`; see
  `reference_browser_screenshot_tool`).
- `/ui` SPA: native chat view (`app/ui/static/views/chat.js`) streaming from `/v1/chat/completions`
  via `api.stream()`.
- **Tests:** OWUI browser smoke; SPA chat smoke; external `openai`-SDK smoke.

---

## 4. Decisions (locked unless redirected)
1. **Auth** — dedicated `/v1` sub-app + `require_openai_key` (Bearer + X-API-Key), OpenAI 401 envelope.
2. **In-process** driver calls for the auto-chain (no self-HTTP loopback).
3. **State from `messages[]`** via ported markers — no new session store (Assist keeps `/assist/*`).
4. Advertise **one** model `scaffold-engine`; optional `scaffold-engine-direct` (raw `model_general`) later.
5. New **`model_triage`** role (config default = current triage model) so triage is provider-switchable.
6. **Provider bump is safe-on**; the **inbound native surface is valve-gated default-off**.

## 5. Cross-cutting conventions
- No hot-reload: `docker restart scaffold-orchestrator` after `app/` edits; recreate with **both**
  `-f docker-compose.yml -f docker-compose.dev.yml` (dev mounts/tests). `docker exec python` imports
  fresh and hides stale live code.
- Tests in the **dev** image (`make test`); pipeline tests need `--noconftest`. Any migration is
  single-statement (asyncpg prepared-statement path).
- Every commit lands its dated `§17.NNN` OVERVIEW.md entry.
- Restart `open-webui-pipelines` only if pipeline files change (Phase 4 fallback path).

## 6. Risks & gotchas
- **Intent-anchor drift** — live model anchors on old broad intents; mocked `tool_call` misses it.
  Every NL/triage phase needs a live `/v1` smoke, not just unit tests.
- **node_token vs node_done dup** — if `node_token` deltas are forwarded, treat `node_done.output`
  as a no-op; if the valve is off, `node_done.output` is the only text source.
- **Client disconnect** — mirror `_sse_with_disconnect_watch` so a closed `/v1` stream cancels
  in-flight work (`CancelledError` is `BaseException`; shield DB writes — see memory).
- **OpenAI 401 shape** — SDKs expect `{"error":{...}}`; a bare FastAPI `detail` 401 confuses them.
- **cloud-Ollama ignores full-schema `format`** — keep the provider-aware structured-outputs gate;
  don't assume the strict path changes behavior on Ollama backends.
- **Schema snapshots** — new endpoints need `make sync-schemas` + `make openapi-snapshot`; the
  `ci-tier-0` pre-push hook gates static parity.

## 7. Open sub-decisions for the owner
- **P1:** keep raw-httpx (recommended) or pin the official `openai` SDK?
- **P0/P4:** advertise one model (`scaffold-engine`) or also `scaffold-engine-direct`?
- **Ordering:** start Phase 0 (recommended — proves the wire before the heavy port), or fold
  Phase 1 (provider bump, independent) in first?
