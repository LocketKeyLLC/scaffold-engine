# Quarterly RAG / pipeline perf re-baseline — runbook

Closes Tier-2 audit-tail item #14 (§17.29). Shipped in §17.354.

## When to run

- **Quarterly**, automatically. Local cron (`crontab -l`):
  ```
  0 8 1 1,4,7,10 *  /home/aedefruscio/scaffold-engine/scripts/quarterly_calibration_pr.sh
  ```
  The cron drafts a PR against `tests/fixtures/golden_set.json` with a
  placeholder rebaseline entry. The operator then runs `make rebaseline`
  locally and fills in the real numbers before marking the PR ready.

- **After a notable infrastructure / model change**. Examples:
  - Cloud-model swap on any of the 8 valve-switchable roles
  - Embedder change (locked at boot — embedder swap is a separate event)
  - Reranker change (CrossEncoder model bump)
  - Milvus version bump or index-type change
  - Major dependency upgrade affecting the hot path (pymilvus, sentence_transformers, ollama-python)

- **After a `bench-check` failure**. Once the regression's root cause is understood and
  accepted (or the regression is explained by a deliberate change like a model swap),
  rebaseline so the new normal becomes the gate's median.

## What `make rebaseline` does

Four sequential steps, all `_ensure_dev`-gated (requires the dev image stack to be live):

| Step | Target | Wall-clock | What it measures |
|---|---|---|---|
| 1 | `make bench-rag` | ~10 min | Per-stage RAG retrieval: embed / Milvus parallel-search / reranker-per-pair. Schema 1.1 (§17.352). |
| 2 | `make bench-embed` | ~30 s | Embedder cold + cached + warm-no-cache; observable cache speedup. |
| 3 | `make bench` (pipeline) | ~5 min | End-to-end: `/ideate` → `/ideate/confirm` → `/dag` → `/execute/all` (§17.353 four-phase shape). |
| 4 | `make bench-check` | <1 s | Aggregate regression gate over all three JSONL files. Exit 2 on regression. |

Each bench appends a row to its `tests/benchmarks/*.jsonl` file. `bench-check` reads
the latest row + the median of the prior N (default 3) and fails when the latest exceeds
the median × threshold (default 1.5×, configurable per-gate in the Makefile).

## Interpreting results

After a successful run you'll see rows in:
- `tests/benchmarks/results.jsonl` — pipeline phases + raw inference + system metrics
- `tests/benchmarks/bench_rag_results.jsonl` — per-query + per-stage RAG breakdown
- `tests/benchmarks/bench_embed_results.jsonl` — embedder phases + cache speedup ratio

The single most informative number is **`summary.stage.rerank_per_pair_warm_mean_ms`** in
the RAG bench (§17.352). On this hardware (T480 i5-8350U, CPU-only CrossEncoder) it
dominates the entire RAG cost at ~1.3 s/pair. Any RAG-quality work that doesn't reduce
per-pair cost OR candidate count will not move the aggregate.

The next most informative is **`pipeline.total_pipeline_s`** in the pipeline bench
(§17.353). Post-cloud-flip (§17.346) baseline is ~250 s for a 5-node CodeGen-shape
benchmark idea; pre-cloud-flip historical baselines were 1539-2502 s (the cloud-flip
is the 8-14× speedup §17.351 documented).

## On regression

Exit code 2 from any bench-check sub-gate means latest > median × threshold for that
metric. Procedure:

1. **Identify which gate fired.** `make bench-check` prints each sub-gate's outcome.
   Per-stage RAG gates (§17.352) point at a specific stage (embed / search / rerank).
   Pipeline gate is total-only.

2. **Read the per-phase breakdown.**
   - For RAG: open the latest row in `bench_rag_results.jsonl`, compare `summary.stage.*`
     against the second-to-last row. Drift in one stage vs all stages tells you whether
     the cause is local (one component) or systemic (e.g. CPU thermal throttling).
   - For pipeline: open the latest row in `results.jsonl`, compare the four
     `pipeline.{idea_submission,confirmation,dag_generation,execution}.duration_s`
     against historical. The phase that moved is the suspect.

3. **Decide accept-or-investigate.**
   - **Investigate** when the regression is unexpected (no recent infra/model change
     correlates). Bisect against git log between the last clean baseline and now.
     The reranker-per-pair metric in particular tends to track CrossEncoder model loads
     and PyTorch thread-pool settings — those are common drift sources.
   - **Accept** when the regression is explained by a deliberate change (e.g. a model
     swap that trades latency for quality). Re-run `make rebaseline` to anchor the new
     normal; the next gate fire will compare against the new median.

4. **Document the decision.** If a fire was real-bug, add a `§17.x` entry in
   `OVERVIEW.md` per the project's running-log convention. If accepted, note the
   rebaseline reason in the PR the quarterly cron drafted (or open a new one).

## Cron + `/health.calibration` integration

The quarterly cron alerts route through `app.observability.alerts` →
`system_alerts` table + file sink. `GET /health.calibration` SELECTs the most recent
`calibration.*` alert and reports a status of:

| `/health.calibration.status` | Meaning |
|---|---|
| `ok` | Most recent fire succeeded |
| `failed` | Most recent fire emitted `calibration.failed` |
| `missed` | Watchdog detected no-fire past grace window |
| `in_progress` | `calibration.started` with no terminal alert yet |
| `unknown` | No `calibration.*` alerts on record, or DB probe failed |

`unknown` is the honest pre-first-fire state — not an error. After the first fire
(scheduled 2026-07-01 08:00 UTC per §17.357 verification) it should transition to
`ok` or `failed`.

## Adding the cron on a fresh host

If installing on a new host without cron:
```sh
# Add to crontab; replaces any prior entry
(crontab -l 2>/dev/null | grep -v quarterly_calibration_pr.sh; \
 echo "# Scaffold Engine — quarterly RAG calibration kickoff"; \
 echo "0 8 1 1,4,7,10 *  $HOME/scaffold-engine/scripts/quarterly_calibration_pr.sh >> /tmp/quarterly_calibration.log 2>&1") | crontab -
```

The script's header preamble has the same example. Pre-§17.357 the example referenced
the stale `/mnt/adamssd/...` path — corrected in §17.357.

## Related

- §17.57 (X.21) — bench framework + regression gates shipped
- §17.351 — post-cloud-flip pipeline baseline refresh
- §17.352 — per-stage RAG decomposition + ci-tier-2 wiring
- §17.353 — bench_pipeline modernization to /ideate path
- §17.354 — this runbook + `make rebaseline` target
- §17.357 — quarterly cron path correction
- §17.323, §17.194 — `/health.calibration` design + empty-state semantics
