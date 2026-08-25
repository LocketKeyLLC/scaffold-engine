// §17.816 (plan 5.4j) — Alerts panel: system_alerts (append-only signal log)
// + unresolved error_logs with the resolve action (PATCH /observability/
// errors/{id}) so the alert_unresolved_errors threshold can actually be
// cleared from the UI.
import * as api from "../api.js";
import { el, mount, timeAgo } from "../util.js";
import { emptyState, errorPanel, loading, toast } from "../components.js";

function sevBadge(sev) {
  const cls = { critical: "err", error: "err", warning: "warn", info: "" }[sev] || "";
  return el("span", { class: `badge ${cls}`, text: sev });
}

export default function alerts(container) {
  let disposed = false;
  const alertsBox = el("div", {});
  const errorsBox = el("div", {});

  async function loadAlerts() {
    mount(alertsBox, loading("Loading alerts…"));
    try {
      const res = await api.get("/observability/alerts", { query: { limit: 50 } });
      const rows = res.alerts || res.items || [];
      if (!rows.length) {
        mount(alertsBox, emptyState({ icon: "✓", title: "No system alerts", small: true }));
        return;
      }
      mount(
        alertsBox,
        el(
          "table",
          { class: "table" },
          el("thead", {}, el("tr", {},
            el("th", { text: "When" }), el("th", { text: "Kind" }),
            el("th", { text: "Severity" }), el("th", { text: "Message" }))),
          el("tbody", {}, ...rows.map((a) =>
            el("tr", {},
              el("td", { class: "faint", text: timeAgo(a.created_at) }),
              el("td", {}, el("code", { text: a.kind })),
              el("td", {}, sevBadge(a.severity)),
              el("td", { class: "alerts-msg", text: a.message }))
          ))
        )
      );
    } catch (e) {
      mount(alertsBox, errorPanel(e, loadAlerts));
    }
  }

  async function resolveError(id, btn) {
    btn.disabled = true;
    try {
      await api.patch(`/observability/errors/${id}`, { resolved: true, resolution: "resolved from /ui alerts panel" });
      toast("Error marked resolved.", "ok");
      loadErrors();
    } catch (e) {
      toast(`Could not resolve: ${e.detail || e.message}`, "err");
      btn.disabled = false;
    }
  }

  async function loadErrors() {
    mount(errorsBox, loading("Loading unresolved errors…"));
    try {
      const res = await api.get("/observability/errors", { query: { resolved: "false", limit: 50 } });
      const rows = res.errors || res.items || [];
      if (!rows.length) {
        mount(errorsBox, emptyState({ icon: "✓", title: "No unresolved errors", small: true }));
        return;
      }
      mount(
        errorsBox,
        el(
          "table",
          { class: "table" },
          el("thead", {}, el("tr", {},
            el("th", { text: "When" }), el("th", { text: "Where" }),
            el("th", { text: "Error" }), el("th", { text: "" }))),
          el("tbody", {}, ...rows.map((r) => {
            const btn = el("button", { class: "btn btn-sm", text: "Resolve" });
            btn.addEventListener("click", () => resolveError(r.id, btn));
            return el("tr", {},
              el("td", { class: "faint", text: timeAgo(r.created_at) }),
              el("td", {}, el("code", { text: r.component || r.source || r.path || "?" })),
              el("td", { class: "alerts-msg", text: (r.message || r.error || "").slice(0, 300) }),
              el("td", {}, btn));
          }))
        )
      );
    } catch (e) {
      mount(errorsBox, errorPanel(e, loadErrors));
    }
  }

  mount(
    container,
    el(
      "div",
      { class: "view-header" },
      el(
        "div",
        {},
        el("h1", { text: "Alerts" }),
        el("div", {
          class: "sub",
          text: "System alerts (append-only signal log) and unresolved errors — resolve them here to clear the oncall threshold.",
        })
      )
    ),
    el("h2", { class: "alerts-h2", text: "System alerts" }),
    alertsBox,
    el("h2", { class: "alerts-h2", text: "Unresolved errors" }),
    errorsBox
  );
  loadAlerts();
  loadErrors();

  return () => {
    disposed = true;
  };
}
