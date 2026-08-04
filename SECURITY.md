# Security Policy

## Supported versions

Only the latest tagged release and the current `main` branch receive security fixes.

| Version | Supported |
|---|---|
| main | ✅ |
| v1.0.x | ✅ |
| < v1.0 | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately via GitHub: **Security → Report a vulnerability** on this repository (private vulnerability reporting), or email **smokie92188@gmail.com** with `[scaffold-engine security]` in the subject line.

Include what you can: affected endpoint/module, reproduction steps, and impact. You should receive an acknowledgment within 7 days.

## Scope notes

scaffold-engine is designed to run self-hosted on a trusted machine:

- The native web UI at `localhost:8000/web/*` is intentionally auth-bypassed on the loopback bind.
- The Prometheus `/metrics` endpoint has no auth by default.
- Simulation sidecars (ports 8001–8003) bind to `127.0.0.1` and exist to isolate untrusted simulator input.

Reports that these surfaces are reachable when an operator deliberately exposes them beyond localhost are configuration issues, not vulnerabilities. Reports of container escape, SSRF via the research/fetch pipeline, prompt-injection paths that reach the shell or filesystem, SQL injection, or auth bypass on non-loopback binds are very much in scope.
