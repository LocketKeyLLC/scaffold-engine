// Pre-paint theme/density boot. Loaded as a plain (non-module) script BEFORE
// the stylesheet so a manual choice never flashes the default look. Must stay
// an external file — the strict CSP (script-src 'self') forbids inline <script>.
// Keys are shared with the sidebar-foot toggles in app.js.
(function () {
  try {
    var t = localStorage.getItem("scaffold_theme");
    if (t === "dark" || t === "light") document.documentElement.dataset.theme = t;
    if (localStorage.getItem("scaffold_density") === "compact") {
      document.documentElement.dataset.density = "compact";
    }
  } catch (e) {
    /* storage unavailable — defaults apply */
  }
})();
