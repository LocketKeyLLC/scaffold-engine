// Per-browser preferences shared by the rail (theme button) and Settings ›
// Preferences. theme_boot.js re-applies both before first paint; the keys are
// the same ones it reads.
import { storage } from "./storage.js";

// Visible build stamp. Bump per UI change round — it exists so "is my tab
// running the latest UI?" is answerable at a glance instead of by diffing
// pixels (the §17.840/§17.842 stale-module debugging sink).
export const UI_BUILD = "r12";   // UX overhaul phase 1 — rail shell, Home, Knowledge + Settings hubs

export const THEME_KEY = "scaffold_theme";
export const DENSITY_KEY = "scaffold_density";
export const THEME_LABELS = { auto: "◐", dark: "●", light: "○" };
export const THEME_NAMES = { auto: "Auto (follows the OS)", dark: "Dark", light: "Light" };

export function currentTheme() {
  return storage.get(THEME_KEY) || "auto";
}

export function applyTheme(next) {
  if (next === "auto") {
    storage.remove(THEME_KEY);
    delete document.documentElement.dataset.theme;
  } else {
    storage.set(THEME_KEY, next);
    document.documentElement.dataset.theme = next;
  }
}

/** Theme cycles auto → dark → light. Returns the new value. */
export function cycleTheme() {
  const order = ["auto", "dark", "light"];
  const next = order[(order.indexOf(currentTheme()) + 1) % order.length];
  applyTheme(next);
  return next;
}

export function applyDensity(value) {
  if (value === "compact") {
    storage.set(DENSITY_KEY, "compact");
    document.documentElement.dataset.density = "compact";
  } else {
    storage.remove(DENSITY_KEY);
    delete document.documentElement.dataset.density;
  }
}
