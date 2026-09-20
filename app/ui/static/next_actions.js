// §17.1134 (ledger D-6) — the server's recovery vocabulary (`next_actions` on
// /status rows and /exec/status) becomes buttons. One module: the noise filter
// (parity-tested against sdk/scaffold_client/next_actions.py), a label for
// EVERY registry action (the parity gate fails on a new action without one),
// and the target: a hub route to navigate to, or an id-in-path call.

// Mirrors `_NOISE_ACTIONS` in sdk/scaffold_client/next_actions.py (gated).
export const NOISE_ACTIONS = ["wait"];

export function filterRenderable(actions) {
  return (actions || []).filter((a) => a && typeof a === "object" && !NOISE_ACTIONS.includes(a.action));
}

// action → label. `{node}` is the node key when the action names one.
export const ACTION_LABELS = {
  retry_node: "Retry {node}",
  skip_node: "Skip {node}",
  resume: "Resume",
  rerun: "Re-run",
  delete: "Delete job",
  abandon: "Abandon",
  restart_research: "Restart research",
  restart_assist: "Restart assist",
  start_assist: "Start assist",
  next_step: "Next step",
  submit: "Submit evidence",
  view_plan: "View plan",
  view_output: "View output",
  confirm: "Approve",
  reconfirm: "Re-approve",
  pause: "Pause",
  wait: "Wait",
};

function nodeOf(a) {
  const m = /\/exec retry \S+ (\S+)|\/skip \S+ (\S+)|node_key[=: ]+([A-Za-z0-9_]+)/.exec(a.command || "");
  return (m && (m[1] || m[2] || m[3])) || a.node_key || "";
}

export function actionLabel(a) {
  const tpl = ACTION_LABELS[a.action] || a.action.replace(/_/g, " ");
  return tpl.replace("{node}", nodeOf(a) || "node").trim();
}

function sessionOf(a) {
  const m = /\/assist\/([0-9a-f-]{36})/i.exec(a.endpoint || a.command || "");
  return m ? m[1] : null;
}

/** Where a chip goes. Navigation targets are hub tabs where the real verb
 *  already lives (retry/skip/resume live on the Run tab, §17.859); calls are
 *  only the id-in-path endpoints the registry spells out. */
export function actionTarget(a, jobId) {
  const sid = sessionOf(a);
  const job = jobId || (/\/jobs\/([0-9a-f-]{36})/i.exec(a.endpoint || "") || [])[1] || "";
  switch (a.action) {
    case "view_plan": return { kind: "nav", href: `#/job/${job}/plan` };
    case "view_output": return { kind: "nav", href: `#/job/${job}/output` };
    case "retry_node": case "skip_node": case "rerun": return { kind: "nav", href: `#/job/${job}/run` };
    case "confirm": case "reconfirm": return { kind: "nav", href: `#/job/${job}` };
    case "start_assist": case "next_step": case "submit": case "restart_assist":
      return { kind: "nav", href: sid ? `#/assist/${sid}` : `#/job/${job}/run` };
    case "resume":
      return sid ? { kind: "call", method: "POST", endpoint: `/assist/${sid}/resume` }
                 : { kind: "call", method: "POST", endpoint: `/jobs/${job}/resume` };
    case "pause": return sid ? { kind: "call", method: "POST", endpoint: `/assist/${sid}/pause` } : { kind: "nav", href: `#/job/${job}/run` };
    case "restart_research": return { kind: "nav", href: `#/job/${job}`, };
    case "abandon": return sid ? { kind: "call", method: "DELETE", endpoint: `/assist/${sid}`, confirm: "Abandon this assist session?" } : { kind: "nav", href: `#/job/${job}` };
    case "delete": return { kind: "call", method: "DELETE", endpoint: `/jobs/${job}`, confirm: "Delete this job and everything under it?" };
    default: return { kind: "nav", href: `#/job/${job}` };
  }
}
