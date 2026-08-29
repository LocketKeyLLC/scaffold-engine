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
  // §17.840 — user creation is a full first-class step: when the admin
  // account is still unclaimed the wizard is 5 steps (1 = create account);
  // a claimed install keeps the original 4-step connect-models flow.
  let stepOffset = 0; // 1 while the account step is part of this run
  const stepLabel = (n, what) => `Step ${n + stepOffset} of ${4 + stepOffset} — ${what}`;

  function header(subtitle, title = "Connect your models") {
    return el(
      "div",
      { class: "view-header" },
      el(
        "div",
        {},
        el("h1", { text: title }),
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

  // ── Step 1 (§17.840): user creation — a full wizard step ────────────
  // Shown while unclaimed (a claimed install starts at connect-models).
  // Creation rides this session's key (the endpoint is admin-authed), so
  // nobody on the network can race the claim. Afterwards the sign-in page
  // greets by name and takes the password instead of the pasted key.
  async function stepAccount() {
    const acct = await api.accountStatus();
    if (disposed) return;
    if (!acct || acct.claimed) {
      stepConnect();
      return;
    }
    stepOffset = 1;
    // Skipping is remembered (same key as the dashboard card's Dismiss) so
    // the boot-time route into setup stops nagging a deliberate decliner.
    const skip = () => {
      localStorage.setItem("scaffold_account_prompt_dismissed", "1");
      stepConnect();
    };
    mount(
      container,
      el(
        "div",
        { class: "view-header" },
        el(
          "div",
          {},
          el("h1", { text: "Create your account" }),
          el("div", { class: "sub", text: "Step 1 of 5 — who's driving this engine" })
        ),
        el("button", { class: "btn btn-ghost btn-sm", text: "Skip for now", onClick: skip })
      ),
      body
    );
    const point = (t) =>
      el("li", { class: "setup-account-point", text: t });
    const name = el("input", { class: "input", placeholder: "Display name (e.g. Adam)", autocomplete: "name" });
    const pw = el("input", { class: "input", type: "password", placeholder: "Password (8+ characters)", autocomplete: "new-password" });
    const pw2 = el("input", { class: "input", type: "password", placeholder: "Repeat password", autocomplete: "new-password" });
    const status = el("div", { class: "gate-status" });
    const create = el("button", { class: "btn btn-primary", text: "Create account & continue →" });
    create.addEventListener("click", async () => {
      if (!name.value.trim()) { status.textContent = "Pick a display name."; return; }
      if (pw.value.length < 8) { status.textContent = "Password needs at least 8 characters."; return; }
      if (pw.value !== pw2.value) { status.textContent = "Passwords don't match."; return; }
      create.disabled = true;
      status.textContent = "Creating…";
      try {
        await api.setupAccount(name.value.trim(), pw.value);
        toast(`Welcome, ${name.value.trim()} — your account is ready.`, "ok");
        stepConnect();
      } catch (e) {
        status.textContent = e.detail || e.message;
        create.disabled = false;
      }
    });
    mount(
      body,
      el(
        "div",
        { class: "card card-pad setup-account" },
        el("h3", { text: "Welcome to Scaffold Engine 👋" }),
        el("p", {
          class: "dim",
          text: "Let's start with you. Pick a name and password for this engine's administrator account:",
        }),
        el(
          "ul",
          { class: "setup-account-points" },
          point("Sign in with your password from now on — no API keys to hunt down."),
          point("The console greets you by name and knows you're the administrator."),
          point("Your API key keeps working for the CLI, scripts, and recovery.")
        ),
        name, pw, pw2,
        el("div", { class: "row setup-account-actions" }, create, el("button", { class: "btn btn-ghost", text: "Skip for now", onClick: skip })),
        status
      )
    );
    name.focus();
  }

  // ── Step 1: connectivity ────────────────────────────────────────────
  async function stepConnect() {
    mount(container, header(stepLabel(1, "reach your Ollama daemon")), body);
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
    mount(container, header(stepLabel(2, "pick a starting point")), body);
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
    mount(container, header(stepLabel(3, "review each role, then apply")), body);
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
          // §17.858 — a local tag over the warm-probe threshold gets an
          // honest "slow for this box" tag right on the row.
          status.textContent = res.ok
            ? ` ✓ live (${res.latency_ms} ms${res.slow ? " — slow for this box" : ""})`
            : ` ✗ ${String(res.error || "").slice(0, 90)}`;
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
      // §17.858 — hand the applied picks to the health step so it can run
      // the slow-box check against what the engine will actually use.
      stepHealth(Object.fromEntries(roles.map((r) => [r.role, selects[r.role].value])));
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
  // §17.858 — appliedPlan (role → model, what Step 3 just applied) drives the
  // slow-box check: probe the applied model_general when it's LOCAL and, if
  // the server flags it slow, surface the honest warning HERE — the "green
  // board means go" moment is exactly where false confidence forms. The
  // §17.841 fresh-install E2E showed a 15-16 GB box burns 43 min of node
  // retries against the 600s default with nothing telling the operator why.
  const isCloudTag = (t) => t.endsWith(":cloud") || t.endsWith("-cloud");

  function slowBoxCheck(appliedPlan) {
    const general = appliedPlan && appliedPlan.model_general;
    if (!general || isCloudTag(general)) return null;
    const box = el("div", { class: "card card-pad setup-slowbox" },
      el("p", { class: "faint", text: `Measuring local model speed (${general})…` }));
    api.post("/models/probe", { model: general }).then((res) => {
      if (disposed) return;
      if (!res.ok || !res.slow) { box.remove(); return; }
      const warm = res.warm_latency_ms != null ? res.warm_latency_ms : res.latency_ms;
      mount(
        box,
        el("h3", { text: "⚠ This box looks too slow for local models" }),
        el("p", {
          text:
            `A tiny 8-token test generation on ${general} took ` +
            `${(warm / 1000).toFixed(1)}s even warmed up (healthy is under ` +
            `${(res.slow_threshold_ms / 1000).toFixed(0)}s). Real jobs make several much ` +
            `longer calls per step and will likely exceed the ` +
            `${res.node_timeout_seconds}s per-step timeout — they'll retry, then fail.`,
        }),
        el("p", {
          text:
            "Two ways out: go back a step and pick the Ollama Cloud preset " +
            "(fast, needs an Ollama Cloud account), or raise NODE_TIMEOUT_SECONDS " +
            "in .env and accept multi-hour steps.",
        })
      );
    }).catch(() => box.remove());
    return box;
  }

  async function stepHealth(appliedPlan) {
    mount(container, header(stepLabel(4, "green board means go")), body);
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
      slowBoxCheck(appliedPlan),
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

  stepAccount();
  return () => {
    disposed = true;
  };
}
