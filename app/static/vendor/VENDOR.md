# Vendored web-UI assets (§17.459)

These are the front-end libraries the native web UI loads, vendored locally
instead of from a CDN. Rationale: the orchestrator is self-hosted and may run
airgapped — a runtime dependency on `unpkg.com` would break `/web/*` with no
network, and a mutable CDN URL is inconsistent with the project's
pin-everything-by-digest posture (see §15 invariants).

Loaded from `app/templates/web/_layout.html` as `/static/vendor/<file>`.

| File | Library | Version | Source (canonical) | SHA256 |
|---|---|---|---|---|
| `htmx-2.0.4.min.js` | htmx | 2.0.4 | `https://unpkg.com/htmx.org@2.0.4` → `/dist/htmx.min.js` | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `htmx-ext-sse-2.2.2.js` | htmx SSE extension | 2.2.2 | `https://unpkg.com/htmx-ext-sse@2.2.2` → `/sse.js` | `83eca6fa0611fe2b0bf1700b424b88b5eced38ef448ef9760a2ea08fbc875611` |

The bare unpkg URLs above resolve (301) to the listed dist paths — these files
are byte-identical to what the page previously loaded at runtime, so behaviour is
unchanged. The SSE extension's canonical bundle is `sse.js` (not minified).

## Updating

1. `curl -sL https://unpkg.com/htmx.org@<ver> -o app/static/vendor/htmx-<ver>.min.js`
   (and the SSE ext likewise).
2. `sha256sum app/static/vendor/*` and update the table above.
3. Bump the `<script src>` versions in `app/templates/web/_layout.html`.
4. Verify: `sha256sum -c` against the table, and load `/web/jobs` in a browser
   with devtools open (no failed requests; htmx + sse attributes still work).
