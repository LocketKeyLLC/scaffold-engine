// Settings › Status — per-service health + the current model-role bindings.
// This was the bottom half of the Dashboard; it is reference, not work, so it
// lives here and the Home page keeps only a one-line warning when something
// needs attention.
import * as api from "../api.js";
import { el, mount } from "../util.js";
import { loading, errorPanel } from "../components.js";

export default function status(container) {
  let disposed = false;
  const outlet = el("div", {}, loading("Checking the stack…"));
  mount(container, outlet);

  async function load() {
    let health = null, roles = null;
    try {
      [health, roles] = await Promise.all([
        api.health(),
        api.get("/models/roles").catch(() => null),
      ]);
    } catch (e) {
      if (!disposed) mount(outlet, errorPanel(e, load));
      return;
    }
    if (disposed) return;
    const checks = Object.entries(health?.checks || {}).filter(([, c]) => c && typeof c === "object" && typeof c.status === "string");
    const warns = health?.warnings || [];
    const switchable = (roles?.roles || []).filter((r) => r.switchable);
    mount(
      outlet,
      warns.length
        ? el("div", { class: "card card-pad setup-checklist" },
            el("div", { class: "setup-checklist-head" },
              el("span", { class: "setup-checklist-title", text: `${warns.length} item${warns.length === 1 ? "" : "s"} need attention` }),
              el("span", { class: "spacer" }),
              el("a", { class: "btn btn-sm", href: "#/setup", text: "Open the setup wizard" })),
            el("ul", { class: "setup-checklist-items" }, ...warns.map((w) => el("li", { text: w }))))
        : null,
      el("div", { class: "grid grid-2" },
        el("div", { class: "card card-pad" },
          el("div", { class: "section-head" }, el("h2", { text: "Services" }),
            el("span", { class: "badge " + (health?.status === "healthy" ? "st-completed" : "st-blocked"), text: health?.status || "unknown" })),
          checks.length
            ? el("div", { class: "health-items" }, ...checks.map(([name, c]) =>
                el("span", { class: "health-item", title: c.crashed && Object.keys(c.crashed).length   // §17.1090
                    ? "crashed gates: " + Object.entries(c.crashed).map(([g, v]) => `${g} ×${v.count}`).join(", ") : "" },
                  el("span", { class: "health-dot", dataset: { state: ["up", "degraded", "unknown"].includes(c.status) ? c.status : "down" } }),
                  name,
                  c.latency_ms != null ? el("span", { class: "faint", text: `${c.latency_ms} ms` }) : null)))
            : el("p", { class: "dim", text: "No service checks reported." })),
        el("div", { class: "card card-pad" },
          el("div", { class: "section-head" }, el("h2", { text: "Model roles" }), el("span", { class: "spacer" }),
            el("a", { class: "btn btn-ghost btn-sm", href: "#/settings/models", text: "Change" })),
          switchable.length
            ? el("div", { class: "roles-list" }, ...switchable.map((r) => el("div", {}, el("span", { class: "role-k", text: r.role.replace("model_", "") }), r.model)))
            : el("p", { class: "dim", text: "Role bindings need an admin key to read." })))
    );
  }
  load();
  return () => { disposed = true; };
}
