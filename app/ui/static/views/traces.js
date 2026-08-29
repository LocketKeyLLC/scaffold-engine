// §17.816 (plan 5.4e) — Traces: browse a job's LLM request/response content
// (llm_traces, mig 063 / §17.786-787). §17.859 — split for the job hub: the
// per-job browser is the hub's Traces tab (renderJobTraces); this view keeps
// only the global picker, and its rows deep-link into the hub.
import * as api from "../api.js";
import { el, mount, shortId, timeAgo } from "../util.js";
import { emptyState, errorPanel, loading } from "../components.js";

const KINDS = ["", "generate", "chat", "tool_call", "embed"];

// ── Per-job trace browser (the hub's Traces tab) ─────────────────────
export function renderJobTraces(container, jobId) {
  let disposed = false;

  const kind = el(
    "select",
    { class: "input input-sm" },
    ...KINDS.map((k) => el("option", { value: k, text: k || "all kinds" }))
  );
  const tracesBox = el("div", {});

  async function loadTraces() {
    mount(tracesBox, loading("Loading traces…"));
    try {
      const res = await api.get(`/trace/${jobId}`, {
        query: { limit: 100, kind: kind.value || null },
      });
      if (disposed) return;
      const rows = res.traces || [];
      if (!rows.length) {
        mount(
          tracesBox,
          emptyState({
            icon: "▤",
            title: "No trace content",
            body: res.capture_enabled
              ? "No captured calls for this job (it may predate capture)."
              : "Trace capture is OFF (trace_capture_enabled) — enable it to record request/response content.",
          })
        );
        return;
      }
      mount(
        tracesBox,
        el("div", { class: "faint traces-hint", text: `${rows.length} calls (oldest first)` }),
        ...rows.map((t, i) =>
          el(
            "details",
            { class: "card card-pad traces-row" },
            el(
              "summary",
              { class: "row traces-summary" },
              el("strong", { text: `#${i + 1}` }),
              el("span", { class: "badge", text: t.request_kind || t.call_kind || "?" }),
              el("code", { text: t.model || "?" }),
              t.node_key ? el("span", { class: "badge", text: t.node_key }) : null,
              t.error ? el("span", { class: "badge err", text: "error" }) : null,
              el("span", { class: "faint", text: timeAgo(t.created_at) })
            ),
            el("h4", { text: "Request" }),
            el("pre", { class: "traces-pre", text: renderReq(t) }),
            el("h4", { text: t.error ? "Error" : "Response" }),
            el("pre", { class: "traces-pre", text: (t.error || t.response_text || "").slice(0, 8000) })
          )
        )
      );
    } catch (e) {
      if (!disposed) mount(tracesBox, errorPanel(e, loadTraces));
    }
  }

  function renderReq(t) {
    if (t.messages) {
      try {
        const msgs = typeof t.messages === "string" ? JSON.parse(t.messages) : t.messages;
        return msgs.map((m) => `[${m.role}] ${String(m.content).slice(0, 1500)}`).join("\n\n").slice(0, 8000);
      } catch {
        return String(t.messages).slice(0, 8000);
      }
    }
    const sys = t.system ? `[system] ${t.system}\n\n` : "";
    return (sys + (t.prompt || "")).slice(0, 8000);
  }

  kind.addEventListener("change", loadTraces);
  mount(container, el("div", { class: "row traces-toolbar" }, el("span", { class: "spacer" }), kind), tracesBox);
  loadTraces();

  return () => {
    disposed = true;
  };
}

// ── Global picker (#/traces) ─────────────────────────────────────────
export default function traces(container) {
  let disposed = false;
  const tracesBox = el("div", {});

  async function pickJob() {
    mount(tracesBox, loading("Loading recent jobs…"));
    try {
      const res = await api.get("/jobs", { query: { limit: 25 } });
      if (disposed) return;
      const jobs = res.jobs || [];
      if (!jobs.length) {
        mount(tracesBox, emptyState({ icon: "▤", title: "No jobs yet" }));
        return;
      }
      mount(
        tracesBox,
        el("div", { class: "faint traces-hint", text: "Pick a job to inspect its LLM calls:" }),
        ...jobs.map((j) =>
          el(
            "div",
            { class: "card card-pad traces-job-row" },
            el("a", { href: `#/job/${j.id}/traces`, text: j.title || shortId(j.id) }),
            el("span", { class: "badge", text: j.status }),
            el("span", { class: "faint", text: timeAgo(j.updated_at || j.created_at) })
          )
        )
      );
    } catch (e) {
      if (!disposed) mount(tracesBox, errorPanel(e, pickJob));
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
        el("h1", { text: "LLM Traces" }),
        el("div", {
          class: "sub",
          text: "Full request/response content per job (llm_traces). Recorded only while trace capture is on.",
        })
      )
    ),
    tracesBox
  );
  pickJob();

  return () => {
    disposed = true;
  };
}
