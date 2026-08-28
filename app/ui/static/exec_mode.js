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
  localStorage.setItem(KEY, mode === "auto" ? "auto" : "assist");
}

export function isAssist() {
  return execMode() === "assist";
}

/** Start an assist session for a job and navigate to it. Returns true on
 * success (caller should stop its own flow). */
export async function startAssistFor(api, jobId, toast) {
  try {
    const s = await api.post("/assist/start", { job_id: jobId });
    const sid = s.session_id || s.id;
    if (sid) {
      toast?.("Assist mode — the engine guides, you drive.", "ok");
      location.hash = `#/assist/${sid}`;
      return true;
    }
  } catch (e) {
    toast?.(`Could not start assist: ${e.detail || e.message}`, "err");
  }
  return false;
}
