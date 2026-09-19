import { el, mount } from "../util.js";

// §17.1114 (Phase 1 ledger U-1) — an unknown hash route used to fall through
// `router.setNotFound` to the DASHBOARD: a dead link looked like "the app went
// somewhere else" and the idea→approve link was dead for weeks before anyone
// could tell (§17.859). A wrong address is now a visible page that names the
// address, so the operator (and the next engineer) sees the defect instead of
// the dashboard.
export function notFoundMessage(path) {
  const p = (path || "").trim() || "/";
  return `There is no page at #${p.startsWith("/") ? p : "/" + p}.`;
}

export default function render(container, params) {
  const path = params && params.path;
  mount(
    container,
    el("div", { class: "view-header" }, el("h1", { text: "Page not found" })),
    el(
      "div",
      { class: "card empty-state" },
      el("div", { class: "empty-icon", text: "🧭" }),
      el("p", { text: notFoundMessage(path) }),
      el("p", { class: "muted", text: "If you followed a link inside a chat reply or a notification, that link is out of date — the job pages live under #/job/<id>." }),
      el("div", { class: "row gap" },
        el("a", { href: "#/", class: "btn btn-primary", text: "Dashboard" }),
        el("a", { href: "#/jobs", class: "btn", text: "Jobs" })
      )
    )
  );
  return () => {};
}
