"""
§17.149 — Seed the engineering RAG corpus with curated filter-
topology references.

Why this exists: §17.146's topology-select stage retrieves chunks
from the ``eng`` partition and asks an LLM to propose candidates
citing the retrieval set. With the corpus carrying only anthropic-
SDK artefacts (leftovers from prior /research runs), the LLM
correctly refused to fabricate filter candidates — the §17.146
integration test surfaced this as a SKIPPED rather than failed run.
This script provides the missing reference content.

Scope: 13 hand-curated entries covering analog filter LPF / HPF /
BPF topology families, each ~600–1200 chars with a citation back to
a canonical public reference (Wikipedia or equivalent).

Behavior:

  * ``--dry-run``  Print the ingest plan, don't write.
  * ``--with-urls`` Additionally ingest a set of canonical reference
                    URLs via ``run_research`` (depth=shallow, domain=eng).
                    Slower (~30–60s per URL); skipped by default.
  * Idempotent — the §9.x dedup pipeline rejects exact content-hash
    matches, so re-running this script is a no-op for unchanged
    entries. The script logs the per-entry stats so an operator can
    see what was new vs. deduplicated.

Run from inside the orchestrator container:

    docker exec scaffold-orchestrator python scripts/seed_eng_topologies.py
    docker exec scaffold-orchestrator python scripts/seed_eng_topologies.py --dry-run
    docker exec scaffold-orchestrator python scripts/seed_eng_topologies.py --with-urls

Exit codes:
  0 success
  1 bad CLI flags
  2 ingest path returned an error (e.g. Milvus unavailable)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

logger = logging.getLogger("scaffold.seed_eng_topologies")

DOMAIN = "eng_design"  # §17.329 — circuit content lives in its own partition
SOURCE_TYPE = "curated"
CONFIDENCE = 0.90  # hand-curated by an engineer; high prior.


# ---------------------------------------------------------------------------
# Hand-curated topology references
# ---------------------------------------------------------------------------

SEEDS: list[dict[str, Any]] = [
    # --- Low-pass family ---
    {
        "title": "RC passive low-pass filter (first-order)",
        "tags": ["filter", "lowpass", "passive", "rc", "analog", "first_order"],
        "source_url": "https://en.wikipedia.org/wiki/Low-pass_filter#Passive_electronic_realization",
        "content": (
            "A first-order RC low-pass filter consists of a resistor R in "
            "series with the signal path followed by a capacitor C to ground. "
            "The transfer function H(s) = 1 / (1 + sRC) has a single pole at "
            "s = -1/(RC). The -3 dB corner frequency is fc = 1/(2*pi*R*C). "
            "Above fc the magnitude rolls off at -20 dB/decade (-6 dB/octave). "
            "Component selection: pick a target fc, choose R in the 1k-100k "
            "range to balance source loading and noise, then solve C = "
            "1/(2*pi*R*fc). Output impedance equals R at DC; load impedance "
            "should be >>R to avoid loading effects (use a buffer if not). "
            "Phase shift at fc is -45 deg, approaching -90 deg well above fc. "
            "ngspice realization: V1 in 0 AC 1; R1 in out R; C1 out 0 C; "
            ".ac dec 100 1 100k; meas ac fc_3db when vdb(out)=-3 fall=1."
        ),
    },
    {
        "title": "RL passive low-pass filter (first-order)",
        "tags": ["filter", "lowpass", "passive", "rl", "analog", "first_order"],
        "source_url": "https://en.wikipedia.org/wiki/Low-pass_filter#Passive_electronic_realization",
        "content": (
            "A first-order RL low-pass filter consists of an inductor L in "
            "series with the signal followed by a resistor R to ground "
            "(the output is taken across R). H(s) = R/(R + sL). The -3 dB "
            "corner is fc = R/(2*pi*L). Roll-off above fc is -20 dB/decade. "
            "RL realization is preferred over RC at high frequencies where "
            "small inductors (microhenries) are cheaper than equivalent-fc "
            "capacitors. At low audio frequencies the inductor sizes become "
            "impractical (millihenries to henries). DC behavior: inductor "
            "is a short, the filter passes the DC component unattenuated. "
            "Output impedance varies with frequency between R and R+jωL. "
            "Common application: switching power supply output filter; "
            "RF receiver front-end."
        ),
    },
    {
        "title": "Sallen-Key low-pass filter (2-pole active)",
        "tags": ["filter", "lowpass", "active", "sallen_key", "analog", "second_order"],
        "source_url": "https://en.wikipedia.org/wiki/Sallen%E2%80%93Key_topology",
        "content": (
            "The Sallen-Key low-pass topology is a 2-pole active filter built "
            "around a single op-amp configured as a unity-gain buffer (or "
            "non-inverting amplifier for K>1). Standard schematic: two "
            "resistors R1, R2 in the signal path, with C2 from the node "
            "between them to ground, C1 from the op-amp output back to the "
            "junction of R1 and R2 (positive feedback). H(s) = 1/(1 + s(R1*C1 "
            "+ R2*C1 + R1*C2*(1-K))*... + s^2 R1 R2 C1 C2). For Butterworth "
            "response (Q=0.7071) with equal-value components, choose R1=R2=R "
            "and C1=2*C2, with fc = 1/(2*pi*R*sqrt(C1*C2)). Advantages over "
            "passive RLC: no inductors, low component count, low output "
            "impedance from the op-amp buffer. Disadvantages: gain-bandwidth "
            "of the op-amp limits high-frequency response; positive feedback "
            "can cause stability issues at high Q."
        ),
    },
    {
        "title": "Multiple-feedback (MFB) low-pass filter (2-pole)",
        "tags": ["filter", "lowpass", "active", "mfb", "analog", "second_order"],
        "source_url": "https://en.wikipedia.org/wiki/Multiple_feedback_topology",
        "content": (
            "The multiple-feedback (MFB) low-pass topology is a 2-pole "
            "inverting active filter using a single op-amp. Components: R1 "
            "from input to virtual-ground node, R2 from the virtual-ground "
            "node to op-amp output, R3 from virtual-ground to op-amp output "
            "(forms the feedback path), C1 in parallel with R2, C2 from "
            "virtual-ground to ground. Transfer function H(s) = -R2/R1 * 1/"
            "(1 + s*C1*(R2+R3+R2*R3/R1) + s^2*R2*R3*C1*C2). DC gain is "
            "-R2/R1 (inverting). For Butterworth response, pick fc and Q, "
            "then solve the component values from the standard MFB design "
            "equations. Advantages: low component spread, inherently "
            "inverting (suits AC-coupled stages), no positive feedback so "
            "stability is robust at high Q. Disadvantages: limited to "
            "moderate gain-bandwidth designs."
        ),
    },
    {
        "title": "LC ladder low-pass filter (higher-order passive)",
        "tags": ["filter", "lowpass", "passive", "lc_ladder", "analog", "higher_order"],
        "source_url": "https://en.wikipedia.org/wiki/Network_synthesis_filters#LC_filters",
        "content": (
            "An LC ladder is a cascade of alternating series inductors and "
            "shunt capacitors implementing a higher-order low-pass response. "
            "Standard topology: source impedance Rs, then L1-C1-L2-C2-...-Ln "
            "with the output across the final shunt element into load Rl. "
            "The order N equals the count of reactive elements. Filter "
            "design proceeds via the prototype tables (Butterworth, "
            "Chebyshev, Bessel, elliptic) normalized to 1 rad/s and 1 ohm, "
            "then denormalized: L = (Rl/wc)*Ln_prototype, C = (1/(wc*Rl))*"
            "Cn_prototype. Roll-off above fc is -20*N dB/decade. Higher-"
            "order LC ladders achieve sharper transitions than equivalent-"
            "order RC active filters with lower noise figure but at the "
            "cost of bulky inductors at audio frequencies. Common in RF "
            "(MHz–GHz) and switching power-supply output stages."
        ),
    },
    # --- High-pass family ---
    {
        "title": "CR passive high-pass filter (first-order)",
        "tags": ["filter", "highpass", "passive", "cr", "analog", "first_order"],
        "source_url": "https://en.wikipedia.org/wiki/High-pass_filter#First-order_continuous-time_implementation",
        "content": (
            "A first-order CR high-pass filter consists of a capacitor C in "
            "series with the signal followed by a resistor R to ground "
            "(output across R). H(s) = sRC/(1 + sRC). The -3 dB corner is "
            "fc = 1/(2*pi*R*C). Below fc the magnitude rolls off at +20 "
            "dB/decade going lower. DC is blocked entirely (the capacitor "
            "is open at DC). Common applications: AC coupling between "
            "amplifier stages, DC offset removal, microphone pre-amp input. "
            "Phase shift at fc is +45 deg, approaching +90 deg well below "
            "fc. Sensitive to source impedance: a high source impedance "
            "interacts with C to shift the corner frequency lower. Choose "
            "R >> source impedance and C sized for the target fc. ngspice "
            "realization: V1 in 0 AC 1; C1 in mid C; R1 mid 0 R; output at "
            "mid. meas ac fc_3db when vdb(mid)=-3 rise=1."
        ),
    },
    {
        "title": "Sallen-Key high-pass filter (2-pole active)",
        "tags": ["filter", "highpass", "active", "sallen_key", "analog", "second_order"],
        "source_url": "https://en.wikipedia.org/wiki/Sallen%E2%80%93Key_topology",
        "content": (
            "The Sallen-Key high-pass is the dual of the Sallen-Key low-"
            "pass: swap each R for a C and each C for a R. Signal-path "
            "elements C1 and C2 in series, with R2 from the junction "
            "between them to ground, R1 from the op-amp output back to the "
            "junction (positive feedback). H(s) = s^2/(s^2 + s*(...)+1/"
            "(R1*R2*C1*C2)). For Butterworth Q=0.7071 with C1=C2=C, choose "
            "R2 = 2*R1, with fc = 1/(2*pi*C*sqrt(R1*R2)). Roll-off below "
            "fc is +40 dB/decade. Same advantages and limitations as the "
            "low-pass variant. Common pitfall: the op-amp's input bias "
            "current flows through the input capacitor — use a JFET or "
            "CMOS-input op-amp for DC-coupled high-impedance signal "
            "sources, or add a high-value bias-return resistor."
        ),
    },
    {
        "title": "Multiple-feedback (MFB) high-pass filter (2-pole)",
        "tags": ["filter", "highpass", "active", "mfb", "analog", "second_order"],
        "source_url": "https://en.wikipedia.org/wiki/Multiple_feedback_topology",
        "content": (
            "The MFB high-pass is the dual of the MFB low-pass: replace "
            "each R with a C and each C with an R while preserving the "
            "topology. Components: C1 from input to virtual-ground node, "
            "C2 in parallel with the feedback resistor (between virtual-"
            "ground and op-amp output), C3 from virtual-ground to op-amp "
            "output, R1 from virtual-ground to ground, R2 in the feedback "
            "path. H(s) = -s^2*C1*C3/(C2 + s(...) + s^2(...)). Inverting "
            "topology — DC gain is zero (capacitors block DC). Roll-off "
            "below fc is +40 dB/decade. Used wherever an inverting 2-pole "
            "high-pass is needed: audio mic preamps, ECG signal "
            "conditioning to reject baseline drift, instrumentation amp "
            "AC-coupling stages."
        ),
    },
    {
        "title": "LC ladder high-pass filter (higher-order passive)",
        "tags": ["filter", "highpass", "passive", "lc_ladder", "analog", "higher_order"],
        "source_url": "https://en.wikipedia.org/wiki/Network_synthesis_filters",
        "content": (
            "An LC high-pass ladder swaps the series and shunt elements of "
            "the low-pass ladder: capacitors go in series with the signal "
            "path; inductors go to ground in shunt. The order N equals the "
            "total number of reactive elements. Design proceeds via "
            "prototype tables and frequency transformation: replace each "
            "low-pass component L by C' = 1/(L*wc^2) and each C by L' = "
            "1/(C*wc^2). Roll-off below fc is +20*N dB/decade. Used "
            "primarily in RF receiver front-ends to reject lower-band "
            "interference, and as crossover networks in audio (combined "
            "with an LPF) where it's the high-frequency leg of the "
            "speaker crossover. Component values at audio frequencies are "
            "impractical; LC high-pass is typical above 100 kHz."
        ),
    },
    # --- Band-pass family ---
    {
        "title": "Cascaded RC band-pass filter",
        "tags": ["filter", "bandpass", "passive", "rc", "analog", "cascaded"],
        "source_url": "https://en.wikipedia.org/wiki/Band-pass_filter",
        "content": (
            "The simplest band-pass filter is a cascade of a first-order "
            "CR high-pass (cutoff f_low) followed by a first-order RC "
            "low-pass (cutoff f_high). The pass-band is between f_low and "
            "f_high. Wide pass-bands (f_high >> f_low) are well-behaved; "
            "narrow pass-bands suffer from interaction between the two "
            "sections and require buffering. The intermediate node sees "
            "both sections' loading effects unless an op-amp buffer is "
            "inserted between them, which is the practical realization for "
            "anything beyond audio-frequency wide-band cases. Centre "
            "frequency fc = sqrt(f_low * f_high). Q at the centre is low "
            "(typically <1 for cascaded RC). Used for AC-coupling stages "
            "with bandwidth-limit, baseband signal conditioning, and "
            "envelope-detector front-ends."
        ),
    },
    {
        "title": "Multiple-feedback (MFB) band-pass filter",
        "tags": ["filter", "bandpass", "active", "mfb", "analog", "second_order"],
        "source_url": "https://en.wikipedia.org/wiki/Multiple_feedback_topology",
        "content": (
            "The MFB band-pass uses a single op-amp with three resistors and "
            "two capacitors to realize a 2-pole, Q up to ~10, inverting "
            "band-pass. Schematic: R1 from input to virtual-ground node; C1 "
            "from virtual-ground to op-amp output; R2 in series with C1 "
            "(forming feedback path); R3 from virtual-ground to ground; C2 "
            "from input-summing node to op-amp output. Centre frequency "
            "f0 = 1/(2*pi*sqrt(R2*R3*C1*C2)). Mid-band gain Av = -R2/(2*R1) "
            "(inverting). Q = sqrt(R2*R3*C1*C2)/(R3*(C1+C2)). Used widely "
            "in tone-control, signal-detection narrow-band filters, FM "
            "discriminators, and parametric-EQ centre stages. Avoid Q>10 "
            "with a single op-amp; cascade two MFB sections or use the "
            "state-variable topology for high-Q designs."
        ),
    },
    {
        "title": "State-variable filter (3-output: LP/BP/HP)",
        "tags": ["filter", "bandpass", "lowpass", "highpass", "active", "state_variable", "analog"],
        "source_url": "https://en.wikipedia.org/wiki/State_variable_filter",
        "content": (
            "The state-variable filter uses three op-amps (summer + two "
            "integrators) to realize simultaneous low-pass, band-pass, and "
            "high-pass outputs from a common signal path. The integrator "
            "outputs are the LP and BP signals; the summer's output is the "
            "HP signal. Centre frequency f0 = 1/(2*pi*R*C) where R and C "
            "are the integrator components (made equal at both stages for "
            "tracking). Q is set by a separate resistor in the summer's "
            "feedback path independently of f0. Tunable: a dual-gang "
            "potentiometer in place of the integrator Rs sweeps f0 over a "
            "decade while preserving Q. Q up to ~100 is achievable. "
            "Hardware cost is three op-amps vs one for MFB / Sallen-Key, "
            "but the orthogonal control of f0 and Q makes it the canonical "
            "choice for synth filters, parametric EQs, and any application "
            "needing variable Q at fixed f0."
        ),
    },
    {
        "title": "Twin-T notch filter (narrow band-reject)",
        "tags": ["filter", "notch", "bandreject", "passive", "active", "twin_t", "analog"],
        "source_url": "https://en.wikipedia.org/wiki/Twin-T_oscillator#Twin-T_filter",
        "content": (
            "The Twin-T is a passive RC network producing a deep notch "
            "(theoretically infinite attenuation) at a single frequency "
            "f0 = 1/(2*pi*R*C). Schematic: two parallel T-sections, one "
            "with two Rs and a shunt 2C-to-ground, the other with two Cs "
            "and a shunt R/2-to-ground; signal in at one end, out the "
            "other. The two paths produce equal-magnitude opposite-sign "
            "transfer at f0, summing to zero in the ideal case. In "
            "practice, component tolerance limits the notch depth to "
            "~40–60 dB. Q is poor for the passive form (~0.25); active "
            "Twin-T variants (op-amp in positive feedback around the "
            "passive network) achieve high-Q notches up to ~100. Common "
            "applications: 50/60 Hz mains rejection in instrumentation "
            "amplifiers, removing a specific carrier from a baseband, "
            "anti-howl in PA systems."
        ),
    },
]


# Canonical reference URLs for the optional ``--with-urls`` augmentation.
# Each URL is ingested via run_research(depth="shallow") so the existing
# distill / dedup pipeline handles fetch + content extraction.
URLS_FOR_RESEARCH: list[str] = [
    "https://en.wikipedia.org/wiki/Low-pass_filter",
    "https://en.wikipedia.org/wiki/High-pass_filter",
    "https://en.wikipedia.org/wiki/Band-pass_filter",
    "https://en.wikipedia.org/wiki/Sallen%E2%80%93Key_topology",
    "https://en.wikipedia.org/wiki/Multiple_feedback_topology",
    "https://en.wikipedia.org/wiki/State_variable_filter",
]


# ---------------------------------------------------------------------------
# Public helpers — exported so unit tests can call them directly.
# ---------------------------------------------------------------------------

def build_entries() -> list[dict[str, Any]]:
    """Convert ``SEEDS`` into the shape ``ingest_entries`` expects."""
    return [
        {
            "title": s["title"],
            "content": s["content"].strip(),
            "domain_tags": list(s["tags"]),
            "source_url": s["source_url"],
            "source_type": SOURCE_TYPE,
            "confidence": CONFIDENCE,
        }
        for s in SEEDS
    ]


async def _with_http_clients(coro):
    """Eager-init the shared httpx client registry, run ``coro``,
    then close. The orchestrator does this in its lifespan handler;
    a standalone CLI script has to do it itself or the embedder
    (via ``model_router.embed``) hits the registered Ollama client
    before ``init_clients`` runs and gets ``not initialized``."""
    from app.utils import http_clients
    http_clients.init_clients()
    try:
        return await coro
    finally:
        await http_clients.close_clients()


async def ingest_curated() -> dict:
    """Run the curated batch through the existing ingest pipeline.
    Imported lazily so --dry-run / --help don't drag the orchestrator's
    init chain."""
    from app.modules.rag_pipeline import ingest_entries
    return await _with_http_clients(
        ingest_entries(build_entries(), domain=DOMAIN)
    )


async def ingest_urls(urls: list[str]) -> dict:
    """Run each URL through ``run_research`` sequentially. Returns a
    summary dict {url: status}. Failures per-url are logged but do
    not abort the batch — partial corpus seeding is still useful."""
    from app.modules.research_agent import run_research

    async def _run() -> dict[str, str]:
        summary: dict[str, str] = {}
        for url in urls:
            logger.info("url_ingest_start: url=%s", url)
            events: list[str] = []
            try:
                async for ev in run_research(
                    url, depth="shallow", domain=DOMAIN
                ):
                    events.append(ev)
                summary[url] = "ok"
                logger.info(
                    "url_ingest_ok: url=%s events=%d", url, len(events)
                )
            except Exception as exc:
                summary[url] = f"error: {exc}"
                logger.error(
                    "url_ingest_failed: url=%s error=%s", url, exc
                )
        return summary

    return await _with_http_clients(_run())


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def _print_plan(with_urls: bool) -> None:
    entries = build_entries()
    print(f"DRY RUN — would ingest {len(entries)} curated entries into domain={DOMAIN!r}:")
    for e in entries:
        print(
            f"  - {e['title']!r:60s} "
            f"({len(e['content'])} chars, source={e['source_url']})"
        )
    if with_urls:
        print(f"\n  + {len(URLS_FOR_RESEARCH)} URLs via run_research(depth='shallow'):")
        for u in URLS_FOR_RESEARCH:
            print(f"    - {u}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan without touching the corpus.",
    )
    parser.add_argument(
        "--with-urls",
        action="store_true",
        help="Also ingest canonical URLs via run_research (slow, network).",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 1

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.dry_run:
        _print_plan(with_urls=args.with_urls)
        return 0

    # Live ingest — curated baseline always runs.
    try:
        stats = asyncio.run(ingest_curated())
    except Exception as exc:
        logger.error("curated_ingest_failed: %s", exc)
        return 2
    logger.info("curated_ingest_done: stats=%s", stats)

    if args.with_urls:
        try:
            url_summary = asyncio.run(ingest_urls(URLS_FOR_RESEARCH))
        except Exception as exc:
            logger.error("url_ingest_batch_failed: %s", exc)
            return 2
        oks = sum(1 for v in url_summary.values() if v == "ok")
        logger.info(
            "url_ingest_done: oks=%d/%d summary=%s",
            oks, len(URLS_FOR_RESEARCH), url_summary,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
