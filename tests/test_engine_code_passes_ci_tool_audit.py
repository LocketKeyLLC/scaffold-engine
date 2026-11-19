"""§17.1939 — code the engine writes for itself passes CI's tool-audit gate (pyright basic over app/, vulture at 100 %):
a gate on each step's files, and before delivery a fix step for each module it refuses (with the linter's, §17.1936).

Live (follow-up 5b4880c0, the lab PR #1161, 2026-11-05): every engine gate and 398 lab tests passed, and CI's
tool-audit job went red twice -- `sum()` over Decimals typed `Decimal | float`, then a parameter nothing read."""
from __future__ import annotations

import inspect
import json
import re
import shutil
from pathlib import Path

import pytest

from app.modules import engine_source as es

pytestmark = pytest.mark.tool_audit           # the real gate (tests/conftest.py stubs it for every other test)
from app.modules import lab_together as lt

ROOT = Path(__file__).resolve().parent.parent

_DECIMAL = ("from decimal import Decimal\n\n\n"
            "def average_entry(rows: list[tuple[Decimal, Decimal]]) -> Decimal:\n"
            "    return sum(q * p for q, p in rows) / sum(q for q, _ in rows)\n")
_DEAD = "def run(prices: list[float], forward_days: int = 5) -> float:\n    return sum(prices)\n"
_CLEAN = ("from decimal import Decimal\n\nZERO = Decimal(\"0\")\n\n\n"
          "def average_entry(rows: list[tuple[Decimal, Decimal]]) -> Decimal:\n"
          "    return sum((q * p for q, p in rows), ZERO) / sum((q for q, _ in rows), ZERO)\n")


def test_the_gate_holds_cis_settings_and_versions():
    conf = ROOT / "pyrightconfig.json"
    if not conf.exists():                                                       # not copied into every test image
        pytest.skip("pyrightconfig.json is not in this image (CI's checkout has it)")
    cfg = json.loads(conf.read_text())
    assert {k: v for k, v in cfg.items() if k not in ("include", "exclude")} == es.PYRIGHT_CONFIG
    wf, req = ROOT / ".github/workflows/ci.yml", ROOT / "requirements.txt"     # not mounted in every test image
    if wf.exists() and req.exists():
        pins = re.search(r"pip install (pyright==\S+) (vulture==\S+)", wf.read_text())
        assert pins and pins[1] in req.read_text() and pins[2] in req.read_text()


@pytest.mark.skipif(shutil.which("pyright") is None or shutil.which("vulture") is None,
                    reason="pyright/vulture are not installed in this image")
@pytest.mark.asyncio
async def test_the_two_red_ci_findings_are_refused_and_a_clean_file_is_not():
    got = await es.tool_audit_findings({"app/modules/paper_calc.py": _DECIMAL, "app/modules/paper_bt.py": _DEAD,
                                        "app/modules/paper_ok.py": _CLEAN,
                                        "tests/test_paper_calc.py": "def test_x(unused_fixture):\n    pass\n"})
    heads = sorted(g.split(" -- ")[0] for g in got)
    assert len(heads) == 2, got
    assert heads[0].startswith("`app/modules/paper_bt.py:1` vulture: unused variable 'forward_days'")
    assert heads[1].startswith("`app/modules/paper_calc.py:5` pyright: Type \"Decimal | float\" is not assignable")


@pytest.mark.asyncio
async def test_no_tools_is_no_finding(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    assert await es.tool_audit_findings({"app/modules/paper_bt.py": _DEAD}) == []


def test_ruff_does_not_repeat_the_engines_own_undefined_name():
    own = "`tests/test_p.py:9` reads `_mock_ws`, which nothing in the file imports, assigns or defines -- `NameError`"
    dup = "`tests/test_p.py:9` ruff F821: Undefined name `_mock_ws` -- fix it: the repo's linter refuses the file"
    other = "`tests/test_p.py:12` ruff F821: Undefined name `other` -- fix it: the repo's linter refuses the file"
    assert es.without_repeated_names([own, dup, other]) == [own, other]


def test_the_gate_and_the_delivery_check_are_wired():
    src = inspect.getsource(es.engine_findings)
    assert "await tool_audit_findings(" in src and "return without_repeated_names(found)" in src
    assert "tool_audit_findings(" in inspect.getsource(lt.lint_fix_steps)
