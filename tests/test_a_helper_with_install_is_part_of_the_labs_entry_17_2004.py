"""§17.2004 — a module that exposes `install(app)` and is imported by the lab's entry is part of that entry, not a
second one: the export names one wiring module, and the delivery check does not ask for the helper in `app/labs.py`.

Live (69fe61c4 T12, 2026-11-24): the operator answered "Merged and redeployed — engine runs it" and the engine refused:
"`paper_serve_static_asset_under` was not installed when the engine started (not listed in `app/labs.py`)". That module
mounts the static files through its own `install(app)`, and the lab's entry `paper_wire_engine_entry_module` imports
and calls it (§17.2000 REACH). The delivery had printed "no single wiring module" for the same reason."""
from __future__ import annotations

from app.modules import engine_plan as ep
from app.modules.module_reach import entries

ENTRY = '''from fastapi import FastAPI

from app.modules.lab_static import install as install_static


def install(app: FastAPI) -> None:
    install_static(app)
'''
STATIC = '''from fastapi import FastAPI


def install(app: FastAPI) -> None:
    app.mount("/labs/lab/static", object())
'''
OTHER = '''def install(app) -> None:
    pass
'''


def _files(**mods: str) -> dict[str, tuple[str, str]]:
    return {f"app/modules/{m}.py": (c, "T1") for m, c in mods.items()}


def test_the_helper_the_entry_imports_is_not_a_second_entry():
    assert ep.wiring_modules(_files(lab_static=STATIC, lab_wire=ENTRY)) == ["lab_wire"]
    # through a module in between, too
    mid = "from app.modules import lab_static\n"
    entry = ENTRY.replace("from app.modules.lab_static import install as install_static", "from app.modules import lab_mid")
    assert ep.wiring_modules(_files(lab_static=STATIC, lab_mid=mid, lab_wire=entry.replace("install_static(app)", "pass"))) \
        == ["lab_wire"]


def test_two_entries_nothing_connects_and_two_that_import_each_other_are_both_kept():
    assert ep.wiring_modules(_files(lab_wire=ENTRY.replace("from app.modules.lab_static import install as install_static\n", "")
                                    .replace("install_static(app)", "pass"), lab_other=OTHER)) == ["lab_wire", "lab_other"]
    a = "from app.modules import b\n\ndef install(app):\n    pass\n"
    b = "from app.modules import a\n\ndef install(app):\n    pass\n"
    assert entries({"a": a, "b": b}, ["a", "b"]) == ["a", "b"]


def test_a_module_without_install_is_never_an_entry():
    assert ep.wiring_modules(_files(lab_static=STATIC.replace("def install", "def mount"), lab_wire=ENTRY)) == ["lab_wire"]
    assert ep.wiring_modules({}) == []
