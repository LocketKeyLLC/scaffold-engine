"""§17.298 — direct-mode handlers lifted from research_agent.py.

§17.280-🟢-3 closeout. Pre-§17.298 ``app/modules/research_agent.py``
was 2501 LOC bundling decompose / search / extract / gap-analysis /
summary alongside four direct-mode handlers (OpenAPI, GitHub, HF,
Forum). The producer modes share little with the iterative-topic loop
beyond a few small SSE / heartbeat helpers, so lifting them into a
dedicated package shrinks the topic-loop module without changing any
operator-facing behavior.

Each module here exports a single ``run_research_<mode>_mode``
coroutine with the same signature it had as a research_agent
function. ``app/modules/research_agent.py`` imports the names back
under their pre-§17.298 underscore-private aliases so the dispatch
in ``run_research`` keeps working byte-for-byte.

Cross-module imports use **late binding** inside ``run_*`` bodies to
break what would otherwise be a circular import: each mode needs
``_ingest_and_finalize_direct`` from research_agent, but research_agent
imports the modes at module-load time. The late ``from ... import``
inside the function body resolves once at first call (Python caches the
module object) and is cheap on repeat calls.

The OpenAPI / GitHub / HF / Forum split mirrors the existing
``app/utils/{github,hf,forum}_ingest.py`` boundary — fetchers are
already module-per-source; the modes are the orchestration layer one
floor up.
"""
