// §17.816 (plan 5.4a) — Models view: live per-role model config over the
// §17.813 API. Shows every role with its effective model + provenance
// (override / env / default); switchable roles get Set / Reset / Probe.
// Writes are admin-only server-side; the view hides the controls for a
// non-admin identity (display hygiene — the server still enforces).
//
// §17.900 — the view now owns CONNECTIONS as well as roles. Before this, the
// page could only re-point a role at a pulled Ollama tag: provider was env-only
// and `PUT /models/roles/{role}` rejected anything absent from the Ollama tag
// list, so ChatGPT/Claude/HF were unreachable from the UI even though the
// providers worked. Two sections now:
//   1. Connections — paste a key, test it, see which backends are live.
//   2. Roles — per-role provider dropdown + a model picker scoped to it.
// Model lists are fetched per provider and cached for the life of the view, so
// switching a role's provider re-scopes its picker without a page reload.
import * as api from "../api.js";
import { el, mount } from "../util.js";
import { errorPanel, loading, toast } from "../components.js";

function sourceBadge(source) {
  const cls = { override: "warn", env: "ok", default: "" }[source] || "";
  return el("span", { class: `badge ${cls}`, text: source });
}

// Provider → how the operator gets models onto it. Shown under the key field so
// "plug and play" includes knowing what to plug in.
const PROVIDER_HELP = {
  ollama:
    "Local models. Pull one with `ollama pull <model>` — a GGUF from HuggingFace " +
    "works too: `ollama pull hf.co/<user>/<repo>`. No key needed.",
  openai: "Your OpenAI API key. Find it at platform.openai.com → API keys.",
  anthropic: "Your Anthropic API key. Find it at console.anthropic.com → API keys.",
  huggingface:
    "A HuggingFace access token (hf.co → Settings → Access Tokens) for HOSTED " +
    "inference. To run a HF model locally instead, pull its GGUF into Ollama.",
};

export default function models(container) {
  const isAdmin = api.principal()?.is_admin !== false;
  let disposed = false;
  // provider name → string[] of model ids. Populated lazily; null = failed.
  const catalog = new Map();

  async function modelsFor(provider) {
    if (catalog.has(provider)) return catalog.get(provider);
    try {
      const res = await api.get("/models/available", { query: { provider } });
      const list = res.models || [...(res.local || []), ...(res.cloud || [])];
      catalog.set(provider, res.reachable ? list : null);
    } catch {
      catalog.set(provider, null);
    }
    return catalog.get(provider);
  }

  async function load() {
    if (disposed) return;
    mount(container, header(), loading("Loading models & connections…"));
    let roles, conns;
    try {
      [roles, conns] = await Promise.all([
        api.get("/models/roles"),
        api.get("/models/connections"),
      ]);
    } catch (e) {
      mount(container, header(), errorPanel(e, load));
      return;
    }
    if (disposed) return;
    catalog.clear();
    render(roles.roles || [], conns);
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
            "Connect a backend, then choose which model serves each engine role. " +
            "Everything here is persisted engine-wide and survives a restart.",
        })
      )
    );
  }

  // ── Connections ────────────────────────────────────────────────────
  function connectionCard(c, defaultProvider) {
    const dot = el("span", {
      class: "conn-dot",
      dataset: { state: c.configured ? (c.last_error ? "warn" : "ok") : "off" },
    });
    const status = el("span", {
      class: "faint conn-status",
      text: c.configured
        ? c.last_error
          ? `last test failed: ${String(c.last_error).slice(0, 90)}`
          : c.last_ok_at
            ? "connected"
            : "configured — not tested yet"
        : "not connected",
    });

    const keyInput = el("input", {
      type: "password",
      class: "input input-sm",
      placeholder: c.requires_key
        ? c.api_key === "(set)"
          ? "key stored — type a new one to replace"
          : `paste your key (${c.key_hint})`
        : "no key needed",
      autocomplete: "off",
    });
    if (!c.requires_key) keyInput.disabled = true;

    const urlInput = el("input", {
      class: "input input-sm",
      placeholder: c.default_url,
      value: c.base_url === c.default_url ? "" : c.base_url || "",
      title: `default: ${c.default_url}`,
    });

    const saveBtn = el("button", { class: "btn btn-sm btn-primary", text: "Save" });
    const testBtn = el("button", { class: "btn btn-sm", text: "Test" });
    const forgetBtn = el("button", {
      class: "btn btn-ghost btn-sm",
      text: "Forget",
      title: "Delete the stored connection and revert to environment values",
    });

    async function act(btn, fn, busy) {
      const label = btn.textContent;
      btn.disabled = true;
      btn.textContent = busy;
      try {
        await fn();
      } catch (e) {
        toast(`${c.provider}: ${e.detail || e.message}`, "err");
      } finally {
        btn.disabled = false;
        btn.textContent = label;
      }
    }

    saveBtn.addEventListener("click", () =>
      act(saveBtn, async () => {
        const body = { enabled: true };
        // Omit api_key entirely when untouched so the server keeps the stored
        // one (the field is a password input; echoing it back would be worse).
        if (c.requires_key && keyInput.value) body.api_key = keyInput.value;
        if (urlInput.value.trim()) body.base_url = urlInput.value.trim();
        await api.req(`/models/connections/${c.provider}`, { method: "PUT", body });
        keyInput.value = "";
        toast(`${c.provider} saved.`, "ok");
        load();
      }, "Saving…")
    );

    testBtn.addEventListener("click", () =>
      act(testBtn, async () => {
        status.textContent = "testing…";
        const res = await api.post(`/models/connections/${c.provider}/test`, {});
        dot.dataset.state = res.ok ? "ok" : "err";
        status.textContent = res.detail || (res.ok ? "connected" : "failed");
        if (res.ok) catalog.set(c.provider, res.models || []);
      }, "Testing…")
    );

    forgetBtn.addEventListener("click", () =>
      act(forgetBtn, async () => {
        if (!confirm(`Forget the stored ${c.provider} connection?`)) return;
        await api.del(`/models/connections/${c.provider}`);
        toast(`${c.provider} connection removed.`, "ok");
        load();
      }, "…")
    );

    return el(
      "div",
      { class: "card card-pad conn-card" },
      el(
        "div",
        { class: "row row-wrap conn-head" },
        dot,
        el("strong", { text: c.label }),
        c.provider === defaultProvider
          ? el("span", { class: "badge ok", text: "default" })
          : null,
        el("span", { class: "spacer" }),
        status
      ),
      // A stored key we can no longer decrypt is actionable, not cosmetic.
      c.key_unreadable
        ? el("div", {
            class: "conn-warn",
            text:
              "⚠ The stored key can't be decrypted (the encryption secret changed). " +
              "Enter it again to restore this connection.",
          })
        : null,
      isAdmin
        ? el(
            "div",
            { class: "conn-fields" },
            el("label", { class: "conn-label", text: "API key" }),
            keyInput,
            el("label", { class: "conn-label", text: "Endpoint" }),
            urlInput
          )
        : null,
      el("div", { class: "faint conn-help", text: PROVIDER_HELP[c.provider] || "" }),
      isAdmin
        ? el("div", { class: "row conn-actions" }, saveBtn, testBtn,
            c.key_source === "db" ? forgetBtn : null)
        : null
    );
  }

  function connectionsSection(conns) {
    const defSel = el("select", { class: "input input-sm" },
      ...conns.providers.map((p) =>
        el("option", { value: p, text: p, selected: p === conns.default_provider ? "" : null })));
    defSel.value = conns.default_provider;
    defSel.addEventListener("change", async () => {
      try {
        await api.req("/models/default-provider", {
          method: "PUT", body: { provider: defSel.value },
        });
        toast(`Default backend → ${defSel.value}`, "ok");
        load();
      } catch (e) {
        toast(`${e.detail || e.message}`, "err");
        defSel.value = conns.default_provider;
      }
    });

    return el(
      "div",
      { class: "models-section" },
      el(
        "div",
        { class: "row row-wrap section-head" },
        el("h2", { text: "Connections" }),
        el("span", { class: "spacer" }),
        isAdmin
          ? el("label", { class: "row faint default-provider" },
              "Default backend ", defSel)
          : null
      ),
      el("p", {
        class: "dim",
        text:
          "Any role with no backend of its own uses the default. Keys are " +
          "encrypted before they are stored and are never shown again.",
      }),
      el("div", { class: "grid grid-2 conn-grid" },
        ...conns.connections.map((c) => connectionCard(c, conns.default_provider)))
    );
  }

  // ── Roles ──────────────────────────────────────────────────────────
  function render(roles, conns) {
    if (disposed) return;
    mount(
      container,
      header(),
      connectionsSection(conns),
      el(
        "div",
        { class: "models-section" },
        el("h2", { text: "Roles" }),
        el("p", {
          class: "dim",
          text:
            "Each role can run on a different backend — keep cheap local models " +
            "for triage and routing, and put the heavy reasoning roles on a " +
            "hosted model.",
        }),
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
                el("th", { text: "Backend" }),
                el("th", { text: "Model" }),
                el("th", { text: "Source" }),
                el("th", { text: isAdmin ? "Actions" : "" })
              )
            ),
            el("tbody", {}, ...roles.map((r) => roleRow(r, conns)))
          ),
          el("div", {
            class: "faint",
            text:
              "Locked roles (embedder, reranker) are config-only: the embedding " +
              "dim is probed at startup and the reranker is a process singleton — " +
              "set their env vars and restart to change them.",
          })
        )
      )
    );
  }

  function roleRow(r, conns) {
    const token = r.role === "model_embedder_pipeline" ? "embedder" : r.role.replace("model_", "");
    const status = el("span", { class: "faint models-row-status" });

    if (!isAdmin || !r.switchable) {
      return el(
        "tr",
        {},
        el("td", {}, el("code", { text: token }),
          r.switchable ? null : el("span", { class: "faint", text: " 🔒" })),
        el("td", {}, el("span", { class: "badge", text: r.provider })),
        el("td", {}, el("code", { text: r.model }), status),
        el("td", {}, sourceBadge(r.source)),
        el("td", {})
      );
    }

    // Backend picker. Changing it re-scopes the model list below, so an
    // operator never types a model name into the wrong backend.
    const provSel = el("select", { class: "input input-sm models-provider" },
      ...conns.providers.map((p) => el("option", { value: p, text: p })));
    provSel.value = r.provider;

    // A datalist keeps this a free-text field (so an unlisted model is still
    // settable) while offering the backend's real catalog.
    const listId = `models-${r.role}`;
    const datalist = el("datalist", { id: listId });
    const input = el("input", {
      class: "input input-sm models-input",
      // Not the current value echoed back (reads as a filled field) — an
      // explicit change affordance. The env default stays on the tooltip.
      placeholder: "change model…",
      title: `env default: ${r.env_default}`,
      list: listId,
    });

    async function fillCatalog() {
      const list = await modelsFor(provSel.value);
      if (disposed) return;
      mount(datalist, ...(list || []).map((m) => el("option", { value: m })));
      input.placeholder = list === null
        ? `${provSel.value} not connected`
        : list.length ? "change model…" : "no models listed";
    }
    provSel.addEventListener("change", fillCatalog);
    fillCatalog();

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
          toast("Enter or pick a model first.", "err");
          return;
        }
        await api.req(`/models/roles/${r.role}`, {
          method: "PUT", body: { model, provider: provSel.value },
        });
        toast(`${token} → ${provSel.value}/${model} (persisted engine-wide)`, "ok");
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

    // The generate-probe is an Ollama path (it hits /api/generate directly to
    // dodge model_router's fallback). For a remote backend the equivalent
    // check is the connection Test button, so say so rather than 404-ing.
    probeBtn.addEventListener("click", () =>
      act(probeBtn, async () => {
        if (provSel.value !== "ollama") {
          status.textContent = " — use Test on the backend's card above";
          return;
        }
        const model = input.value.trim() || r.model;
        status.textContent = ` probing ${model}…`;
        const res = await api.post("/models/probe", { model });
        status.textContent = res.ok
          ? ` ✓ ${model} live (${res.latency_ms} ms)`
          : ` ✗ ${model}: ${String(res.error || "").slice(0, 120)}`;
      }, "Probing…")
    );

    return el(
      "tr",
      {},
      el("td", {}, el("code", { text: token })),
      el("td", {}, provSel),
      el("td", {}, el("code", { text: r.model }), status),
      el("td", {}, sourceBadge(r.source)),
      el("td", {}, el("div", { class: "row models-actions" }, input, datalist, setBtn, resetBtn, probeBtn))
    );
  }

  load();
  return () => {
    disposed = true;
  };
}
