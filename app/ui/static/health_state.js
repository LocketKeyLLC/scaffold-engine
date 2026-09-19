// §17.1116 (Phase 1 ledger U-5) — the engine-reachability state machine.
//
// The only health indicator was a dot in the sidebar foot with three states
// (up / degraded / down) and no memory: "unreachable" could not say since
// when, and the §17.1055 collapsed layout hides the sidebar entirely, so the
// operator's walkthrough layout had NO indicator at all. This is the pure
// state: `nextHealth(prev, sample)` folds one poll result into the previous
// state and keeps `lastOkAt` / `since` / `failures`; `healthText` renders it.
// app.js renders the sidebar dot AND a fixed connection pill (outside the
// sidebar) that shows whenever the state is not `up`.

export const INITIAL = Object.freeze({ state: "checking", since: null, lastOkAt: null, failures: 0, status: null });

/** Fold one /health sample into the previous state. `sample` is
 *  { ok: true, status } for a response, or { ok: false, error } for a failed call. */
export function nextHealth(prev, sample, now = Date.now()) {
  const p = prev || INITIAL;
  if (sample && sample.ok) {
    const s = String(sample.status || "");
    const healthy = s === "ok" || s === "healthy" || s === "up";
    const state = healthy ? "up" : "degraded";
    return {
      state,
      since: p.state === state ? p.since : now,
      lastOkAt: now,              // the engine answered — it is reachable
      failures: 0,
      status: s || null,
    };
  }
  return {
    state: "unreachable",
    since: p.state === "unreachable" ? p.since : now,
    lastOkAt: p.lastOkAt,
    failures: (p.failures || 0) + 1,
    status: null,
    error: sample && sample.error ? String(sample.error) : (p.error || ""),
  };
}

function clock(ts) {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function ago(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 90) return `${s}s`;
  const m = Math.round(s / 60);
  return m < 90 ? `${m} min` : `${Math.round(m / 60)} h`;
}

/** Short operator-facing text for the sidebar and the pill. */
export function healthText(st, now = Date.now()) {
  const h = st || INITIAL;
  switch (h.state) {
    case "up": return "orchestrator up";
    case "degraded": return `orchestrator degraded (${h.status || "?"})`;
    case "unreachable": {
      const since = h.since != null ? ` since ${clock(h.since)}` : "";
      const seen = h.lastOkAt != null ? ` · last seen ${ago(now - h.lastOkAt)} ago` : "";
      return `engine unreachable${since}${seen}`;
    }
    default: return "checking…";
  }
}

/** The sidebar dot's data-state (keeps the existing CSS: up / degraded / down / unknown). */
export function dotState(st) {
  const h = st || INITIAL;
  return h.state === "unreachable" ? "down" : h.state === "checking" ? "unknown" : h.state;
}

/** Should the fixed connection pill be visible? Only when something is wrong. */
export function pillVisible(st) {
  const h = st || INITIAL;
  return h.state === "degraded" || h.state === "unreachable";
}
