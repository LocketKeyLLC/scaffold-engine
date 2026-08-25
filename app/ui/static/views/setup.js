// §17.817 (plan 5.7) — the first-run "Connect your models" wizard: the
// "connect models and go" centerpiece. Steps: Ollama connectivity → preset
// (Local-only / Ollama Cloud tuned picks / keep current) → per-role review
// with probes → apply via the §17.813 API → health board → go.
// First-run state is SERVER-side (/meta/first-run, mig 070) so it triggers
// once per install, not once per browser.
import * as api from "../api.js";
import { el, mount } from "../util.js";
import { errorPanel, loading, toast } from "../components.js";

// The operator-tuned cloud picks (§17.632 general A/B, §17.567 verifier A/B,
// §17.498 coder, §17.631 research_extract). Applied only for tags actually
// pulled on the target daemon; missing tags keep the current value.
const CLOUD_PICKS = {
  model_general: "deepseek-v4-pro:cloud",
  model_coder: "kimi-k2.7-code:cloud",
  model_verifier: "kimi-k2.7-code:cloud",
  model_router: "qwen3.5:397b-cloud",
  model_research_extract: "glm-5.1:cloud",
  model_cloud_heavy: "qwen3.5:397b-cloud",
  model_cloud_alt: "qwen3.5:397b-cloud",
  model_triage: "qwen3.5:397b-cloud",
  model_fallback: "qwen3.5:latest",
};

export default function setup(container) {
  let disposed = false;
  let avail = null; // /models/available
  let roles = [];   // /models/roles (switchable only)
  let plan = {};    // role -> chosen model
  const body = el("div", {});

  function header(subtitle) {
    return el(
      "div",
      { class: "view-header" },
      el(
        "div",
        {},
        el("h1", { text: "Connect your models" }),
        el("div", { class: "sub", text: subtitle })
      ),
      el("button", {
        class: "btn btn-ghost btn-sm",
        text: "Skip — keep current models",
        onClick: finish,
      })
    );
  }

  async function finish() {
    try {
      await api.post("/meta/first-run/complete");
    } catch {
      /* non-admin or older server — just leave */
    }
    location.hash = "#/new";
  }

  // ── Step 1: connectivity ────────────────────────────────────────────
  async function stepConnect() {
    mount(container, header("Step 1 of 4 — reach your Ollama daemon"), body);
    mount(body, loading("Probing Ollama…"));
    try {
      [avail, roles] = await Promise.all([
        api.get("/models/available"),
        api.get("/models/roles").then((r) => (r.roles || []).filter((x) => x.switchable)),
      ]);
    } catch (e) {
      mount(body, errorPanel(e, stepConnect));
      return;
    }
    if (disposed) return;
    if (!avail.reachable) {
      mount(
        body,
        el(
          "div",
          { class: "card card-pad" },
          el("h3", { text: "⚠ Ollama unreachable" }),
          el("p", { text: `Nothing answered at ${avail.ollama_url}. Start Ollama (or fix OLLAMA_BASE_URL) and retry.` }),
          el("button", { class: "btn btn-primary", text: "Retry", onClick: stepConnect })
        )
      );
      return;
    }
    mount(
      body,
      el(
        "div",
        { class: "card card-pad" },
        el("h3", { text: "✓ Ollama connected" }),
        el("p", { class: "faint", text: avail.ollama_url }),
        el("p", { text: `${avail.local.length} local model(s), ${avail.cloud.length} cloud model(s) available.` }),
        el("button", { class: "btn btn-primary", text: "Choose models →", onClick: stepPreset })
      )
    );
  }

  // ── Step 2: preset ──────────────────────────────────────────────────
  function stepPreset() {
    mount(container, header("Step 2 of 4 — pick a starting point"), body);
    const pulled = new Set([...avail.local, ...avail.cloud]);
    const current = Object.fromEntries(roles.map((r) => [r.role, r.model]));

    function card(title, desc, disabled, note, buildPlan) {
      const b = el("button", { class: "btn btn-primary", text: "Use this", disabled });
      if (!disabled)
        b.addEventListener("click", () => {
          plan = buildPlan();
          stepReview();
        });
      return el(
        "div",
        { class: "card card-pad setup-preset" },
        el("h3", { text: title }),
        el("p", { text: desc }),
        note ? el("p", { class: "faint", text: note }) : null,
        b
      );
    }

    mount(
      body,
      el(
        "div",
        { class: "grid grid-3 setup-presets" },
        card(
          "Local only",
          "Everything runs on this machine. Private, free, slower on CPU.",
          avail.local.length === 0,
          avail.local.length ? `Uses ${avail.local[0]} for every role — adjust next.` : "No local models pulled yet (ollama pull …).",
          () => Object.fromEntries(roles.map((r) => [r.role, avail.local.includes(r.model) ? r.model : avail.local[0]]))
        ),
        card(
          "Ollama Cloud (tuned)",
          "The A/B-tuned cloud picks per role — fast, needs an Ollama Cloud account.",
          avail.cloud.length === 0,
          avail.cloud.length ? "Only picks you have pulled apply; others keep current." : "No cloud models available on this daemon.",
          () => Object.fromEntries(roles.map((r) => {
            const pick = CLOUD_PICKS[r.role];
            return [r.role, pick && pulled.has(pick) ? pick : r.model];
          }))
        ),
        card(
          "Keep current",
          "Review the models each role uses right now and tweak per-role.",
          false,
          null,
          () => ({ ...current })
        )
      )
    );
  }

  // ── Step 3: review + apply ──────────────────────────────────────────
  function stepReview() {
    mount(container, header("Step 3 of 4 — review each role, then apply"), body);
    const allTags = [...avail.local, ...avail.cloud];
    const selects = {};
    const rows = roles.map((r) => {
      const sel = el(
        "select",
        { class: "input input-sm" },
        ...allTags.map((t) =>
          el("option", { value: t, text: t, selected: t === plan[r.role] })
        ),
        allTags.includes(plan[r.role]) ? null : el("option", { value: plan[r.role], text: plan[r.role], selected: true })
      );
      selects[r.role] = sel;
      const status = el("span", { class: "faint models-row-status" });
      const probe = el("button", { class: "btn btn-ghost btn-sm", text: "Probe" });
      probe.addEventListener("click", async () => {
        probe.disabled = true;
        status.textContent = " probing…";
        try {
          const res = await api.post("/models/probe", { model: sel.value });
          status.textContent = res.ok ? ` ✓ live (${res.latency_ms} ms)` : ` ✗ ${String(res.error || "").slice(0, 90)}`;
        } catch (e) {
          status.textContent = ` ✗ ${e.detail || e.message}`;
        } finally {
          probe.disabled = false;
        }
      });
      return el(
        "tr",
        {},
        el("td", {}, el("code", { text: r.role.replace("model_", "") })),
        el("td", {}, sel, status),
        el("td", { class: "faint" }, r.model === plan[r.role] ? "" : `now: ${r.model}`),
        el("td", {}, probe)
      );
    });
    const applyBtn = el("button", { class: "btn btn-primary", text: "Apply & continue →" });
    const applyStatus = el("div", { class: "faint setup-apply-status" });
    applyBtn.addEventListener("click", async () => {
      applyBtn.disabled = true;
      const changes = roles.filter((r) => selects[r.role].value !== r.model);
      let failed = 0;
      for (const r of changes) {
        applyStatus.textContent = `Setting ${r.role.replace("model_", "")} → ${selects[r.role].value}…`;
        try {
          await api.req(`/models/roles/${r.role}`, { method: "PUT", body: { model: selects[r.role].value } });
        } catch (e) {
          failed++;
          toast(`${r.role.replace("model_", "")}: ${e.detail || e.message}`, "err");
        }
      }
      applyStatus.textContent = changes.length
        ? `${changes.length - failed}/${changes.length} roles updated (persisted engine-wide).`
        : "No changes — current models kept.";
      if (failed) {
        applyBtn.disabled = false;
        return;
      }
      stepHealth();
    });
    mount(
      body,
      el(
        "div",
        { class: "card card-pad" },
        el(
          "table",
          { class: "table" },
          el("thead", {}, el("tr", {},
            el("th", { text: "Role" }), el("th", { text: "Model" }),
            el("th", { text: "" }), el("th", { text: "" }))),
          el("tbody", {}, ...rows)
        ),
        el("div", { class: "row" }, applyBtn, applyStatus)
      )
    );
  }

  // ── Step 4: health board ────────────────────────────────────────────
  async function stepHealth() {
    mount(container, header("Step 4 of 4 — green board means go"), body);
    mount(body, loading("Checking the stack…"));
    let h = null;
    try {
      h = await api.health();
    } catch {
      /* rendered below */
    }
    if (disposed) return;
    const checks = (h && h.checks) || {};
    // Stats-only entries (cache hit-rates, alert counters) carry no status —
    // they're telemetry, not up/down checks; skip them here.
    const cards = Object.entries(checks)
      .filter(([, c]) => c && typeof c === "object" && typeof c.status === "string")
      .map(([name, c]) =>
      el(
        "div",
        { class: "card card-pad setup-health-card" },
        el("div", { class: "row" },
          el("span", { class: `health-dot`, dataset: { state: c.status === "up" ? "up" : "down" } }),
          el("strong", { text: name })),
        el("div", { class: "faint", text: c.status + (c.latency_ms != null ? ` · ${c.latency_ms} ms` : "") })
      )
    );
    const allUp = h && h.status === "healthy";
    mount(
      body,
      el("div", { class: "grid grid-3 setup-health" }, ...cards),
      el(
        "div",
        { class: "card card-pad" },
        el("p", {
          text: allUp
            ? "Everything is up. You're ready — describe an idea and go."
            : "Some checks are down — the engine still works for what's green; fix the rest anytime.",
        }),
        el("p", {
          class: "faint",
          text: "Optional features: SearXNG powers live web research; the code-runner sandbox executes generated code. Both are compose services you can enable later.",
        }),
        el("button", { class: "btn btn-primary", text: "Finish — go build 🚀", onClick: finish })
      )
    );
  }

  stepConnect();
  return () => {
    disposed = true;
  };
}
