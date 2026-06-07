# Scaffold-Engine UX Roadmap — Helpful · Truthful · Assistive

> Produced 2026-06-07 (§17.443). A research + design artifact, **not** a commitment to build.
> Grounded in four read-only internal audits (OWUI chat, agentic output, assist mode, operator
> tooling) cross-referenced against an adversarially-verified external best-practices survey
> (2025–2026 SOTA). All internal claims cite `file:line`; all external techniques cite a paper/source.
> Status: roadmap only — no code changes accompany this document.

---

## The one insight that organizes everything

All four internal audits independently converged on the same finding:

> **The engine already computes rich trust & guidance signals — it just never surfaces them.**
> Per-node failure reasons, RAG below-threshold / reranker-skipped flags, research source URLs,
> brief `ambiguities`, assist `divergence`, friction notes, `error_summary` — captured server-side,
> almost none plumbed to the chat / web / CLI surfaces.

The highest-leverage UX wins here are **plumbing existing data**, not new ML — and that aligns with
what the external research says builds trust (citation surfacing, uncertainty communication, "show
your work"). Start there.

A second finding: the **triage "Gaps" engine is a genuine asset** (every audit praised it —
structured, anti-hallucination-guarded, worked examples, `scaffold_router.py:272-426`). The weakness
is that **everything downstream of triage stops asking and stops gating**. Build on the Gaps engine;
don't rebuild it.

---

## ⭐ Phase A — if you do only five things (all high-impact, mostly small)

| # | Change | Axis | Why | Effort |
|---|---|---|---|---|
| A1 | **Surface per-node failure reason** in `/exec/status`, `/logs`, web detail, `scaffold logs`. Data already exists in `dag_nodes.last_verification_reason` (mig 026, written `execution_agent.py:884`) but the node SELECTs / `NodeLog` schema (`status.py:225-235`) omit it. | assistive/truthful | The single biggest debugging gap — a `failed` job shows **no "why"** without grepping Postgres/logs. | S |
| A2 | **Attribute research summaries.** `source`/`source_url` is stripped before the summarizer (`research_agent.py:771-788`) and `_build_research_complete_payload` returns no `sources[]` (`:847-866`). | truthful | The summary is an un-attributed synthesis though per-entry URLs exist in state. Maps to **post-hoc citation (P-Cite)** — SOTA for synthesis (75% vs 37% coverage). | M |
| A3 | **Show RAG uncertainty.** The `/rag` renderer (`scaffold_router.py:3298-3328`) reads only `source_type`+`confidence`+`source_url`; it ignores `metadata.below_threshold` / `threshold_relaxed` / `skipped_rerank` / `warnings` (set in `rag_pipeline.py:840-851`, `:944-950`). | truthful | A top-3 fallback below the 0.8 threshold currently looks identical to a high-confidence hit — the textbook trust failure. | S |
| A4 | **Fix `/help`.** Add `/cancel` (handled `:2583`) + the `/assist` family (handled `:1134-1140`) — both undiscoverable today — and drop the fabricated "**22 commands**" count (`scaffold_router.py:821`; `/help` actually documents ~30, dispatcher handles more). | helpful/truthful | Stale magic number + invisible commands; cheapest credibility win. | S |
| A5 | **`/go` correction gate.** `_handle_go` shows the brief then **immediately** auto-chains (`scaffold_router.py:1236-1237`) — Phase 1 fires before the user can correct a bad synthesis, costing 10–25 min on CPU. | assistive/truthful | Prevents the worst friction event: committing minutes to a wrong brief. Mirror the existing `/execute` confirmation pattern (`:1384-1411`). | M |

---

## Full roadmap by axis

### Axis 1 — HELPFUL (discoverability, friction, transparency)

| Item | Gap (file:line) | SOTA mapping | Effort |
|---|---|---|---|
| Discoverable, grouped command surface | `/help` flat & stale; dispatcher handles ~30+ across `pipe()` (`:1126-1159`) + `_handle_command` (`:2525-2666`) | Codex CLI typed-`/` popup, ~44 function-grouped commands ([openai docs]) | S–M |
| Inline cancel + ETA on long ops | One-time "10-25 min" warning (`:1467`); no inline `/cancel` hint during the wait | "show your work" / progress-visibility patterns | S |
| RAG dead-end → escalate to `/research` | Bare "No matches" (`:3291`); reverse of the good `/research`→`/rag` nudge (`:1311-1322`) | next-best-action | S |
| Low-confidence verdict changes guidance | Feasibility `confidence` rendered (`:1874`, `:3833`) but the same `/confirm` block is offered at 30% as at 95% | uncertainty communication | S |
| Standardize error voice; stop raw-JSON leakage | `_fmt` dumps raw JSON (`:3761`), `<details>` JSON footer on every `/idea` (`:3861`), `Error {status}: {r.text}` (`:3666`); inconsistent empathetic-vs-machine register | trust patterns | M |
| Multiturn-collaboration posture | Triage already embodies it; extend "offer a concrete next step every turn" downstream | **CollabLLM**, ICML 2025 Outstanding Paper (arXiv 2502.00640): user study +17.6% satisfaction, −10.4% time | M (prompt-level) |

### Axis 2 — TRUTHFUL (grounding, verification, abstention)

| Item | Gap (file:line) | SOTA mapping | Effort |
|---|---|---|---|
| Chain-of-Verification stage for research/prose nodes | Generic verifier is deliberately lenient ("PASS if … even partially", `execution_verify.py:28-52`); no fabrication check for non-code nodes | **CoVe** (Meta, arXiv 2309.11495): draft→verify-questions→**independent** answers→revise. **Black-box compatible.** | M–L |
| Faithfulness gate / score on RAG + research answers | No automated groundedness check surfaced | **RAGAS** faithfulness (arXiv 2309.15217, 0.95 human agreement): extract claims, LLM-verify each vs context, supported-ratio. **Black-box compatible.** | M |
| Confidence provenance labeling | Three different `0.82`s (verifier / source-type / rerank) shown raw, unlabeled (truthful audit #7) | uncertainty communication | S |
| Citation strategy chosen per task | Research synthesis vs codegen/EDA fact-checks treated alike | **P-Cite vs G-Cite** (arXiv 2509.21557): post-hoc for synthesis (coverage), generation-time for verification (precision) | M |
| Exec-smoke "skip" ≠ "pass" downstream | `codegen_exec_smoke` returns `skip` on sandbox-off/import-miss with no persisted distinction (`codegen_check.py:64-74`) | verification visibility | S |
| Semantic contradiction detection | `_check_contradictions` is title-word-overlap (`research_agent.py:105-124`) — fires on agreement, misses value conflicts; never blocks/annotates | claim-level fact-checking | M |
| Scheduled `/research/verify` staleness sweep | `compare_hash` drift detection exists (`research_verify.py:344-362`) but is opt-in, unscheduled, and RAG never consults verify state | retrieval-augmented verification | L |

> ⚠️ **Self-hosted caveat (load-bearing):** the strongest abstention/attribution papers —
> **MIRAGE** (arXiv 2406.13663), **SABER** (arXiv 2605.18792), **FRANQ** (arXiv 2505.21072) —
> require **white-box model internals** (hidden states / saliency). With Ollama cloud + quantized
> local models you **don't have that access**. Treat them as *concepts to emulate*, and implement the
> **black-box proxies** above (CoVe, LLM-judge faithfulness, post-hoc citation), which work over any
> chat API.

### Axis 3 — ASSISTIVE (clarification, recovery, proactivity)

| Item | Gap (file:line) | SOTA mapping | Effort |
|---|---|---|---|
| Information-gain clarifying questions | Triage Gaps engine is strong but asks a fixed 4-bucket set; doesn't *select* by value or decide *when to stop* | **Active Task Disambiguation** (ICLR 2025 Spotlight, arXiv 2502.04485, Bayesian Experimental Design) + **SAGE-Agent** (arXiv 2511.08798: EVPI, +7–39% coverage with **1.5–2.7× fewer questions**, separates specification vs model uncertainty) | M–L |
| Surface brief `ambiguities` at confirm gate | Captured `idea_refinement.py:93`, read by nothing; only feasibility `clarifications_needed` reaches the user (`scaffold_router.py:1881`) | clarification UX | S |
| Surface assist `divergence` flag | `assist_steps.divergence` written (`assist_replan.py:354`), read by no renderer | mixed-initiative HITL | M |
| `/status` list passes node keys + `error_summary` | List path calls `next_actions_for` with neither (`status.py:152`) → generic actions with literal `{node_key}`, no reaper guidance | graceful recovery | M |
| Readiness gating at `/confirm` | `/confirm` proceeds with unanswered high-severity gaps — triage Gaps work discarded once a brief exists | when-to-ask vs proceed | M |
| Friction → proactive remediation | Friction logged/listed (`assist_agent.py:630/650`) but nothing acts on it | next-best-action | M |
| Proactive stall nudge pre-reap | Recovery hints attach only *after* a status change; a job idling under the threshold gets no early prompt | anticipatory assistance | M |
| Assist resume-from-where-you-left-off | `reaper_assist_abandoned` says start a fresh `/idea` (`recovery.py:356`), discarding mirrored evidence that survives in `dag_nodes.output_text`; re-entry is already supported (`assist_agent.py:38`) | graceful recovery | S |

### Operator surface (cross-axis)

| Item | Gap (file:line) | Axis | Effort |
|---|---|---|---|
| Web job-detail shows `error_summary` + per-node failure | `job_detail.html` renders status badges but never `error_summary` (in payload `execution_handler.py:125`) | truthful/assistive | S |
| CLI to *read* alerts/errors | Endpoints exist (`routers/alerts.py:23`, `observability.py:30/58/82`); no `scaffold alerts list` / `errors list` — can only resolve a UUID you can't see | helpful | S |
| `/health warnings[]` array | Top-level `status` keys only on pg/ollama/milvus (`main.py:997`); Redis/cache down, missed calibration, recent OOM all leave `status:"healthy"` | truthful | S |
| Web recovery buttons | `next_actions` shown as plain text for failed/blocked (`job_detail.html:60-72`); retry/skip require CLI/curl | assistive | M |
| `scaffold jobs watch <id>` live tail | `scaffold logs` is one-shot (`main.py:3194`); SDK already streams (`aiter_execute_all`) | helpful | M |
| Verify `make doctor --explain` target exists | CLI hints it (`main.py:180`); unverified, flag-after-target is unusual | assistive | S (verify first) |

---

## Recommended sequencing

- **Phase A** — the 5 quick wins above (surface what you already compute). One focused sprint.
- **Phase B** — Axis-2 *surfacing*: citation display (A2 done) + faithfulness score next to RAG/research output + confidence provenance labels + the operator-surface plumbing (web error banner, alerts CLI, `/health warnings`).
- **Phase C** — deeper verification & clarification: CoVe verifier stage (default-off valve, research nodes only), RAGAS-style faithfulness gate, info-gain clarifying questions on the Gaps engine.

## Caveats on the research itself

- Benchmark numbers are largely **author-reported, not independently replicated** — treat percentages as directional.
- Every verification/clarification layer adds **LLM calls** — real latency/cost on CPU/cloud. Gate behind valves, default-off, apply *selectively* (e.g., CoVe only on research-synthesis nodes).
- The internal audits were **static reads**; per project convention the top picks warrant a quick verify-pass before implementation.
- `coderunner_url` / `codegen_execution_check_enabled` live state was not checked by the audit — confirm exec-smoke is active before relying on items that assume it.

## Sources

External (adversarially verified, 25/25 claims confirmed):
- Codex CLI slash commands — https://developers.openai.com/codex/cli/slash-commands
- CollabLLM (multiturn collaboration, ICML 2025) — https://arxiv.org/abs/2502.00640
- Chain-of-Verification / CoVe (Meta, ACL Findings 2024) — https://arxiv.org/abs/2309.11495
- RAGAS (reference-free RAG eval) — https://arxiv.org/abs/2309.15217
- MIRAGE (model-internals attribution, EMNLP 2024) — https://arxiv.org/abs/2406.13663
- SABER (trust-or-abstain) — https://arxiv.org/abs/2605.18792
- FRANQ (faithfulness vs factuality UQ) — https://arxiv.org/abs/2505.21072
- Generation-time vs post-hoc citation (NeurIPS 2025 workshop) — https://arxiv.org/abs/2509.21557
- Active Task Disambiguation (ICLR 2025 Spotlight) — https://arxiv.org/abs/2502.04485
- SAGE-Agent / structured-uncertainty clarification — https://arxiv.org/abs/2511.08798

Internal audits: OWUI conversational (`pipelines/scaffold_router.py`), agentic output truthfulness
(`app/modules/rag_pipeline.py`, `research_agent.py`, `research_verify.py`, `execution_agent.py`,
`execution_verify.py`), assist mode (`app/modules/assist_*.py`, `recovery.py`, `app/routers/assist.py`),
operator tooling (`cli/`, `sdk/`, `app/main.py` `/health`, `app/observability/*`, `app/web/`).
