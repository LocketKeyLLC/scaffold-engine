// §17.1059 — the ONE place the SPA touches localStorage. Site-data blocked,
// some private-mode configurations and thumbnail/preview contexts make every
// localStorage access THROW; the first read on the boot path was the API key
// (api.js), so such a browser died with an uncaught exception instead of
// showing the login card. Every read/write goes through here and degrades to
// "no stored value" — the page must render correctly without one.
export const storage = {
  get(key, fallback = null) {
    try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); return true; } catch { return false; }
  },
  remove(key) {
    try { localStorage.removeItem(key); return true; } catch { return false; }
  },
};
