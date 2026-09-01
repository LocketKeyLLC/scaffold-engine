// §17.853 — the global Auto / Assist execution mode.
//
// Operator finding: pressing Execute launched AUTONOMOUS execution when they
// expected assist mode — there was no mode concept in the UI at all, and the
// distinction matters enormously for infrastructure jobs:
//   ✦ Assist — you execute each step on your machines with the engine
//     guiding, verifying, and adapting (the engine NEVER touches your
//     hardware; it has no terminal access by design).
//   ▶ Auto  — the engine works every step itself: it writes the runbooks,
//     configs, code, and documents autonomously. It still never connects to
//     your machines — an Auto run PRODUCES artifacts; it does not apply them.
// Default is Assist: the safe, expected mode for hands-on projects.
//
// Persisted per-browser; every execution entry point (plan editor, theater,
// approve auto-run, flow guide) reads it through here — one source of truth.
const KEY = "scaffold_exec_mode";

export function execMode() {
  return localStorage.getItem(KEY) === "auto" ? "auto" : "assist";
}

export function setExecMode(mode) {
  const m = mode === "auto" ? "auto" : "assist";
  localStorage.setItem(KEY, m);
  // §17.854 (audit S4) — notify open views (theater/plan run buttons) so their
  // labels don't lie about what Execute will do after a sidebar toggle.
  window.dispatchEvent(new CustomEvent("scaffold:execmode", { detail: m }));
}

/** Subscribe to exec-mode changes; returns an unsubscribe fn for view cleanup. */
export function onExecModeChange(handler) {
  const fn = (e) => handler(e.detail);
  window.addEventListener("scaffold:execmode", fn);
  return () => window.removeEventListener("scaffold:execmode", fn);
}

export function isAssist() {
  return execMode() === "assist";
}

/** Start an assist session for a job and navigate to it. Returns true on
 * success (caller should stop its own flow).
 *
 * §17.895 — every caller now gets the SAME handling of the two non-success
 * shapes the server can return, instead of each entry point inventing its own:
 *   - `assist_unavailable` (umbrella job / 0 DAG nodes, §17.561) is a 200 with
 *     guidance, NOT an error — it used to fall through as a silent `false` and
 *     the operator saw nothing happen at all.
 *   - a thrown error surfaces its detail.
 * `opts.navigate` is the hook the approve chain needs so it can route through
 * the SPA router instead of stomping location.hash mid-chain. */
export async function startAssistFor(api, jobId, toast, opts = {}) {
  const go = opts.navigate || ((hash) => { location.hash = hash; });
  try {
    const s = await api.post("/assist/start", { job_id: jobId });
    const sid = s && (s.session_id || s.id);
    if (sid) {
      toast?.("Assist mode — the engine guides, you drive.", "ok");
      // §17.859 — land in the job hub's Run tab (it embeds the walkthrough).
      go(`#/job/${jobId}/run`);
      return true;
    }
    if (s && s.assist_unavailable) {
      // Not an error — a 200 with guidance (§17.561: umbrella / 0-node jobs).
      // `reason` is a machine code ("umbrella" | "no_dag"), never operator
      // prose, so it gets translated here rather than shown raw.
      // components.js supports "" | "ok" | "err" only; this is informational,
      // and the caller's fallback navigation is the visible half of the answer.
      toast?.(
        s.reason === "umbrella"
          ? `This is an umbrella job — its ${s.children_total || ""} component job${
              s.children_total === 1 ? "" : "s"
            } run automatically; open one of those to walk through it.`.replace("  ", " ")
          : "This job has no plan steps yet, so there is nothing to walk through — generate the plan first."
      );
      return false;
    }
    toast?.("Could not start assist: the engine returned no session.", "err");
  } catch (e) {
    toast?.(`Could not start assist: ${e.detail || e.message}`, "err");
  }
  return false;
}
