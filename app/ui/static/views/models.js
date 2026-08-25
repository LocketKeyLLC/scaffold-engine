// §17.816 (plan 5.4a) — Models view: live per-role model config over the
// §17.813 API. Shows every role with its effective model + provenance
// (override / env / default); switchable roles get Set / Reset / Probe.
// Writes are admin-only server-side; the view hides the controls for a
// non-admin identity (display hygiene — the server still enforces).
import * as api from "../api.js";
import { el, mount } from "../util.js";
import { errorPanel, loading, toast } from "../components.js";

function sourceBadge(source) {
  const cls = { override: "warn", env: "ok", default: "" }[source] || "";
  return el("span", { class: `badge ${cls}`, text: source });
}

export default function models(container) {
  const isAdmin = api.principal()?.is_admin !== false;
  let disposed = false;

  async function load() {
    if (disposed) return;
    mount(container, header(), loading("Loading model roles…"));
    let data;
    try {
      data = await api.get("/models/roles");
    } catch (e) {
      mount(container, header(), errorPanel(e, load));
      return;
    }
    render(data.roles || []);
  }

  function header() {
    return el(
      "div",
      { class: "view-header" },
      el(
        "div",
        {},
        el("h1", { text: "Models" }),
        el("div", {
          class: "sub",
          text:
            "Which model serves each engine role — persisted engine-wide. " +
            "Cloud tags are liveness-probed before a change applies.",
        })
      )
    );
  }

  function render(roles) {
    if (disposed) return;
    const rows = roles.map((r) => roleRow(r));
    mount(
      container,
      header(),
      el(
        "div",
        { class: "card card-pad" },
        el(
          "table",
          { class: "table models-table" },
          el(
            "thead",
            {},
            el(
              "tr",
              {},
              el("th", { text: "Role" }),
              el("th", { text: "Model" }),
              el("th", { text: "Source" }),
              el("th", { text: isAdmin ? "Actions" : "" })
            )
          ),
          el("tbody", {}, ...rows)
        ),
        el("div", {
          class: "faint",
          text:
            "Locked roles (embedder, reranker) are config-only: the embedding " +
            "dim is probed at startup and the reranker is a process singleton — " +
            "set their env vars and restart to change them.",
        })
      )
    );
  }

  function roleRow(r) {
    const token = r.role === "model_embedder_pipeline" ? "embedder" : r.role.replace("model_", "");
    const status = el("span", { class: "faint models-row-status" });

    const cells = [
      el("td", {}, el("code", { text: token }), r.switchable ? null : el("span", { class: "faint", text: " 🔒" })),
      el("td", {}, el("code", { text: r.model }), status),
      el("td", {}, sourceBadge(r.source)),
    ];

    if (!isAdmin || !r.switchable) {
      cells.push(el("td", {}));
      return el("tr", {}, ...cells);
    }

    const input = el("input", {
      class: "input input-sm models-input",
      placeholder: r.env_default || "model tag",
      title: `env default: ${r.env_default}`,
    });
    const setBtn = el("button", { class: "btn btn-sm", text: "Set" });
    const resetBtn = el("button", {
      class: "btn btn-ghost btn-sm",
      text: "Reset",
      title: `Revert to env default (${r.env_default})`,
    });
    const probeBtn = el("button", { class: "btn btn-ghost btn-sm", text: "Probe" });

    async function act(btn, fn, busyLabel) {
      const label = btn.textContent;
      btn.disabled = true;
      btn.textContent = busyLabel;
      try {
        await fn();
      } catch (e) {
        toast(`${token}: ${e.detail || e.message}`, "err");
      } finally {
        btn.disabled = false;
        btn.textContent = label;
      }
    }

    setBtn.addEventListener("click", () =>
      act(setBtn, async () => {
        const model = input.value.trim();
        if (!model) {
          toast("Enter a model tag first.", "err");
          return;
        }
        await api.req(`/models/roles/${r.role}`, { method: "PUT", body: { model } });
        toast(`${token} → ${model} (persisted engine-wide)`, "ok");
        load();
      }, "Setting…")
    );

    resetBtn.addEventListener("click", () =>
      act(resetBtn, async () => {
        await api.del(`/models/roles/${r.role}`);
        toast(`${token} reverted to env default`, "ok");
        load();
      }, "Resetting…")
    );

    probeBtn.addEventListener("click", () =>
      act(probeBtn, async () => {
        const model = input.value.trim() || r.model;
        status.textContent = ` probing ${model}…`;
        const res = await api.post("/models/probe", { model });
        status.textContent = res.ok
          ? ` ✓ ${model} live (${res.latency_ms} ms)`
          : ` ✗ ${model}: ${String(res.error || "").slice(0, 120)}`;
      }, "Probing…")
    );

    cells.push(el("td", {}, el("div", { class: "row models-actions" }, input, setBtn, resetBtn, probeBtn)));
    return el("tr", {}, ...cells);
  }

  load();
  return () => {
    disposed = true;
  };
}
