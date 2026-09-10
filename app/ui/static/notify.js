// §17.1007 — the console never called the operator back.
//
// Before this module there was not one `document.title` write or Notification
// call anywhere in app/ui/static/. A run finishing, a gate opening, a step
// failing, a job parking at `awaiting_assist` — all of it landed silently in a
// tab that may not have been focused. Every polling loop was already correctly
// skipped while the tab is hidden (§17.818), which is the good half of the same
// insight: the console knew when nobody was looking and did nothing with it.
//
// Two channels, deliberately different in cost:
//
//   1. The TITLE BADGE is always on and needs no permission. It is the
//      ambient channel — an operator glancing at their tab strip sees
//      "(1) plan ready · Scaffold Engine" without switching to us.
//   2. A DESKTOP NOTIFICATION is opt-in, requested only from a real click
//      (browsers reject — and Chrome permanently penalises — permission
//      prompts fired on load). Off until the operator asks for it.
//
// Both are dedupe-keyed per transition so a 30s poller cannot re-announce the
// same job every tick.

const BASE_TITLE = "Scaffold Engine";
const PERM_KEY = "scaffold_notify_enabled";

let badgeCount = 0;
let badgeLabel = "";

// Transitions already announced this session. Keyed `<job-id>:<status>` so a
// job moving on and back (e.g. running → blocked → running → blocked) still
// announces the second block, while a poller re-observing the SAME state is
// silent.
const announced = new Set();

/** Has this transition already been announced? Marks it as announced.
 *  Internal — `announce()` is the only caller and the module's real entry point. */
function firstTime(key) {
  if (announced.has(key)) return false;
  announced.add(key);
  // Bound the set — a long-lived tab watching a busy engine shouldn't grow
  // this without limit. 500 keys is far beyond any realistic session.
  if (announced.size > 500) {
    const first = announced.values().next().value;
    announced.delete(first);
  }
  return true;
}

// ── Title badge ───────────────────────────────────────────────────────
function paintTitle() {
  document.title = badgeCount
    ? `(${badgeCount}) ${badgeLabel ? badgeLabel + " · " : ""}${BASE_TITLE}`
    : BASE_TITLE;
}

/** Set the tab badge. `n` of 0 clears it. `label` is a short human phrase
 *  ("plan ready", "run failed") — it is the half an operator reads at a
 *  glance in a crowded tab strip, so it must name the OUTCOME, not the count. */
export function setBadge(n, label = "") {
  badgeCount = Math.max(0, n | 0);
  badgeLabel = label;
  paintTitle();
}

export function clearBadge() {
  setBadge(0, "");
}

// ── Desktop notifications (opt-in) ────────────────────────────────────
export function notifySupported() {
  return typeof window !== "undefined" && "Notification" in window;
}

/** Enabled = the operator opted in AND the browser still grants it. Permission
 *  can be revoked in site settings long after our flag was stored, so both
 *  halves are checked on every send rather than cached. */
export function notifyEnabled() {
  return (
    notifySupported() &&
    localStorage.getItem(PERM_KEY) === "1" &&
    Notification.permission === "granted"
  );
}

/** Request permission. MUST be called from a user gesture. Returns the new
 *  enabled state so the caller can update its own label. */
export async function enableNotifications() {
  if (!notifySupported()) return false;
  let perm = Notification.permission;
  if (perm === "default") {
    try {
      perm = await Notification.requestPermission();
    } catch {
      return false;
    }
  }
  if (perm === "granted") {
    localStorage.setItem(PERM_KEY, "1");
    return true;
  }
  // "denied" is sticky in every browser — storing the flag would leave the
  // toggle reading "on" while nothing ever fires.
  localStorage.removeItem(PERM_KEY);
  return false;
}

export function disableNotifications() {
  localStorage.removeItem(PERM_KEY);
}

/** Fire one notification. No-op unless opted in. `href` is a SPA hash route —
 *  clicking the notification focuses this tab and navigates there, which is
 *  the whole point: the operator is called back TO the thing that happened. */
export function notify(title, body, href) {
  if (!notifyEnabled()) return;
  try {
    const n = new Notification(title, {
      body,
      icon: "/ui/static/logo.svg",
      // Collapse repeats of the same subject in the OS tray rather than
      // stacking five cards for one job.
      tag: href || title,
    });
    n.onclick = () => {
      window.focus();
      if (href) location.hash = href;
      n.close();
    };
  } catch {
    /* notification construction can throw on some mobile browsers — the
       title badge is the channel that always works, so this stays silent. */
  }
}

/** The combined call: badge the tab AND (if opted in) fire a notification,
 *  but only the first time this exact transition is seen, and only while the
 *  operator is looking elsewhere. A terminal event that lands while the tab is
 *  focused needs no announcement — they watched it happen. */
export function announce({ key, count, label, title, body, href }) {
  if (!firstTime(key)) return false;
  if (!document.hidden) return false;
  setBadge(count ?? badgeCount + 1, label);
  notify(title, body, href);
  return true;
}

// Returning to the tab clears the ambient badge — the operator has arrived,
// the badge has done its job, and a stale "(1)" that outlives the reason for
// it teaches operators to ignore the channel.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) clearBadge();
});
