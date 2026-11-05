"""§17.1905 — the labs delivered into the engine: the wiring module each one's job wrote, started when the engine
starts. A job that writes code for the engine ends in a pull request (`scripts/deliver_job.py`) that adds its files
and one line here; the lifespan calls each module's `install(app)` once the scheduler is up, so its recurring runs,
routes and stream consumers start with the engine and nowhere else.

Live (follow-up 5b4880c0, 2026-11-04): 29 `paper_*` modules, every one passing its own tests in the sandbox, lived
only in the job's output -- nothing in the engine imported them -- and the plan's next step asked the operator to
store the keys for an end-to-end trade that no code in the engine could place."""
from __future__ import annotations

import importlib
import logging

logger = logging.getLogger("scaffold")

#: wiring modules under `app.modules`, one per delivered lab, each exposing `install(app) -> None`
LABS: tuple[str, ...] = ("paper_wire_engine_entry_module",)
#: what `install_labs` did for each lab when the engine started (read by the delivery question, §17.1907)
INSTALLED: dict[str, str] = {}


def install_labs(app, labs: tuple[str, ...] | None = None) -> dict[str, str]:
    """Start each delivered lab; return lab -> "installed" or the reason it is not. A lab that fails to start is
    logged at error and named in the result -- the engine itself still starts (one lab is not the engine)."""
    out: dict[str, str] = {}
    for lab in LABS if labs is None else labs:
        try:
            mod = importlib.import_module(f"app.modules.{lab}")
            install = getattr(mod, "install", None)
            if not callable(install):
                out[lab] = "no install(app)"
                logger.error('event="lab_not_installed" lab=%s reason="no install(app)"', lab)
                continue
            install(app)
            out[lab] = "installed"
            logger.info('event="lab_installed" lab=%s', lab)
        except Exception as exc:
            out[lab] = f"failed: {exc!r}"
            logger.error('event="lab_install_failed" lab=%s error=%r', lab, exc)
    INSTALLED.update(out)
    return out
