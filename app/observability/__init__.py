"""Sprint X.26 — observability surface.

Three closely-coupled concerns live here:

  * metrics.py   — Prometheus exposition (`GET /metrics`)
  * alerts.py    — file + DB sink with dedup/cooldown; CLI entrypoint
  * thresholds.py + calibration_watchdog.py — periodic eval that converts
    X.20's pull-only rollups into push alerts

Wired in two places: `app/main.py` (mount `/metrics`, register lifespan hooks)
and `app/scheduler.py` (interval jobs run alongside the existing APScheduler).
"""
