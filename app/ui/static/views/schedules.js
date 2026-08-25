// §17.816 (plan 5.4d) — Schedules view: recurring research schedules with the
// HONEST last_status (§17.812 Phase-2G made failed runs report failed and
// guard-refused runs 'skipped' instead of blanket success). List / add / delete.
import * as api from "../api.js";
import { el, fmtDate, mount, timeAgo } from "../util.js";
import { emptyState, errorPanel, loading, toast } from "../components.js";

const DEPTHS = ["shallow", "medium", "deep"];
const DOMAINS = ["", "prompt", "rag", "llm", "spec", "eng", "eng_design"];

function statusBadge(s) {
  const cls = { success: "ok", failed: "err", skipped: "warn" }[s] || "";
  return el("span", { class: `badge ${cls}`, text: s || "never ran" });
}

export default function schedules(container) {
  let disposed = false;

  const topic = el("input", { class: "input sched-topic", placeholder: "Research topic…" });
  const cron = el("input", {
    class: "input input-sm sched-cron",
    placeholder: "cron (e.g. 0 6 * * 1)",
    title: "Standard 5-field crontab, evaluated in the server timezone below.",
  });
  const tz = el("input", { class: "input input-sm sched-tz", value: "America/New_York" });
  const depth = el(
    "select",
    { class: "input input-sm" },
    ...DEPTHS.map((d) => el("option", { value: d, text: d, selected: d === "medium" }))
  );
  const domain = el(
    "select",
    { class: "input input-sm" },
    ...DOMAINS.map((d) => el("option", { value: d, text: d || "auto domain" }))
  );
  const addBtn = el("button", { class: "btn btn-primary", text: "Add schedule" });
  const listBox = el("div", {});

  async function add() {
    const t = topic.value.trim();
    const c = cron.value.trim();
    if (!t || !c) {
      toast("Topic and cron expression are required.", "err");
      return;
    }
    addBtn.disabled = true;
    try {
      await api.post("/schedule", {
        topic: t,
        cron_expression: c,
        timezone: tz.value.trim() || "UTC",
        depth: depth.value,
        domain: domain.value || null,
      });
      toast("Schedule added.", "ok");
      topic.value = "";
      load();
    } catch (e) {
      toast(`Could not add: ${e.detail || e.message}`, "err");
    } finally {
      addBtn.disabled = false;
    }
  }

  async function remove(id, label) {
    if (!confirm(`Delete schedule "${label}"?`)) return;
    try {
      await api.del(`/schedule/${id}`);
      toast("Schedule deleted.", "ok");
      load();
    } catch (e) {
      toast(`Could not delete: ${e.detail || e.message}`, "err");
    }
  }

  function renderList(rows) {
    if (disposed) return;
    if (!rows.length) {
      mount(listBox, emptyState({
        icon: "◷",
        title: "No schedules",
        body: "Recurring research runs land in the corpus on a cron cadence.",
      }));
      return;
    }
    mount(
      listBox,
      el(
        "table",
        { class: "table" },
        el(
          "thead",
          {},
          el(
            "tr",
            {},
            el("th", { text: "Topic" }),
            el("th", { text: "Cadence" }),
            el("th", { text: "Last run" }),
            el("th", { text: "Next run" }),
            el("th", { text: "Runs" }),
            el("th", { text: "" })
          )
        ),
        el(
          "tbody",
          {},
          ...rows.map((s) =>
            el(
              "tr",
              {},
              el(
                "td",
                {},
                el("div", { text: s.topic }),
                el("div", { class: "faint", text: `${s.depth}${s.domain ? " · " + s.domain : ""}` })
              ),
              el("td", {}, el("code", { text: s.cron_expression }), el("div", { class: "faint", text: s.timezone })),
              el(
                "td",
                {},
                statusBadge(s.last_status),
                el("div", { class: "faint", text: s.last_run_at ? timeAgo(s.last_run_at) : "—" })
              ),
              el("td", { text: s.enabled ? fmtDate(s.next_run_at) : "disabled" }),
              el("td", {}, `${s.run_count ?? 0}`, s.failure_count
                ? el("span", { class: "badge err", text: ` ${s.failure_count} failed` })
                : null),
              el(
                "td",
                {},
                el("button", {
                  class: "btn btn-ghost btn-sm",
                  text: "Delete",
                  onClick: () => remove(s.id, s.topic),
                })
              )
            )
          )
        )
      )
    );
  }

  async function load() {
    mount(listBox, loading("Loading schedules…"));
    try {
      const res = await api.get("/schedule", { query: { limit: 100 } });
      renderList(res.schedules || []);
    } catch (e) {
      mount(listBox, errorPanel(e, load));
    }
  }

  addBtn.addEventListener("click", add);

  mount(
    container,
    el(
      "div",
      { class: "view-header" },
      el(
        "div",
        {},
        el("h1", { text: "Schedules" }),
        el("div", {
          class: "sub",
          text: "Recurring research on a cron cadence. Status is honest: failed runs show failed, lock-refused runs show skipped.",
        })
      )
    ),
    el(
      "div",
      { class: "card card-pad" },
      el("div", { class: "row sched-controls" }, topic, cron, tz, depth, domain, addBtn)
    ),
    listBox
  );
  load();

  return () => {
    disposed = true;
  };
}
