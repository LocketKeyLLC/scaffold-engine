// Settings › Preferences — this browser's choices: theme, density, desktop
// alerts, and sign-out. These were six unlabeled toggles in the sidebar foot.
import * as api from "../api.js";
import { el, mount } from "../util.js";
import { storage } from "../storage.js";
import { toast } from "../components.js";
import * as notify from "../notify.js";
import { THEME_KEY, DENSITY_KEY, applyTheme, applyDensity, UI_BUILD } from "../prefs.js";
import { execMode, setExecMode } from "../exec_mode.js";

export default function preferences(container) {
  const p = api.principal();
  const themeSel = (value, label) => {
    const on = (storage.get(THEME_KEY) || "auto") === value;
    return el("button", { class: "btn btn-sm" + (on ? " btn-primary" : ""), text: label, "aria-pressed": on ? "true" : "false",
      onClick: () => { applyTheme(value); render(); } });
  };
  const densitySel = (value, label) => {
    const on = (storage.get(DENSITY_KEY) || "cozy") === value;
    return el("button", { class: "btn btn-sm" + (on ? " btn-primary" : ""), text: label, "aria-pressed": on ? "true" : "false",
      onClick: () => { applyDensity(value); render(); } });
  };
  const modeSel = (value, label) => {
    const on = execMode() === value;
    return el("button", { class: "btn btn-sm" + (on ? " btn-primary" : ""), text: label, "aria-pressed": on ? "true" : "false",
      onClick: () => { setExecMode(value); render(); } });
  };
  const pref = (title, body, ...controls) => el("div", { class: "pref-row" },
    el("div", { class: "pref-text" }, el("div", { class: "pref-title", text: title }), el("div", { class: "dim pref-body", text: body })),
    el("div", { class: "row row-wrap pref-controls" }, ...controls));

  function render() {
    const alertsOn = notify.notifyEnabled();
    mount(container,
      el("div", { class: "card card-pad pref-card" },
        pref("Theme", "Dark is the default; Auto follows the OS.", themeSel("auto", "◐ Auto"), themeSel("dark", "● Dark"), themeSel("light", "○ Light")),
        pref("Density", "Compact tightens paddings for more rows per screen.", densitySel("cozy", "▢ Cozy"), densitySel("compact", "▦ Compact")),
        // §17.853 — the Auto/Assist mode. It sat in the sidebar; the job page's
        // approve and start controls will carry it, this is the fallback.
        pref("Execution mode", "Assist: you run each step on your machines with the engine guiding — it never touches your hardware. Auto: the engine works every step itself and produces runbooks, configs and code; it connects to a machine only if you opened the write channel (Capabilities \u2192 \u201cLet the engine run approved commands\u201d), and then only to run commands you approve block by block.",
          modeSel("assist", "✦ Assist"), modeSel("auto", "▶ Auto")),
        pref("Desktop alerts", "A system notification when a job needs you — the gate opens, a run finishes or fails, a walkthrough parks. The tab title always updates.",
          notify.notifySupported()
            ? el("button", { class: "btn btn-sm" + (alertsOn ? " btn-primary" : ""), text: alertsOn ? "⚑ On" : "⚐ Off", "aria-pressed": alertsOn ? "true" : "false",
                onClick: async () => {
                  if (alertsOn) { notify.disableNotifications(); toast("Desktop alerts off — the tab title still updates.", "ok"); }
                  else {
                    const ok = await notify.enableNotifications();
                    toast(ok ? "Desktop alerts on — you'll be called back when a job needs you."
                             : "The browser blocked notifications for this site. Allow them in site settings, then try again.", ok ? "ok" : "err");
                  }
                  render();
                } })
            : el("span", { class: "dim", text: "This browser has no Notification API." })),
        pref("Signed in as", p ? `${p.identity} (${p.role})` : "—",
          el("button", { class: "btn btn-sm btn-ghost", text: "Sign out", onClick: () => { api.setKey(""); location.reload(); } })),
        pref("Console build", `ui ${UI_BUILD} — assets revalidate on every reload.`)));
  }
  render();
  return () => {};
}
