"""§17.854 (audit H4) — ratchet gate against the MagicMock-settings trap.

``patch("some.module.settings")`` with no ``spec=`` replaces the whole Settings
object with a bare MagicMock, so every valve the test does NOT explicitly pin
reads as a truthy Mock. That means the test exercises neither the code-default
NOR the live configuration — a class of blind spot that has bitten this suite
three times (a new default-off valve silently exercised ON, or a flipped default
hidden). The fix is the ``realistic_settings`` fixture (tests/conftest.py) or
``monkeypatch.setattr(settings, field, value)``, which keep a REAL Settings
object so unpinned valves read their real defaults.

This is a host static scan (no app import, no services) so it runs in the
ci-tier-0 lane with --noconftest. It ratchets: the count of bare
``patch("...settings")`` occurrences must not exceed the grandfathered baseline,
so NEW traps fail CI while the existing ones are migrated opportunistically.
Lower BASELINE as they're converted.
"""
from __future__ import annotations

import re
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
# A bare settings patch: patch("...settings") or patch('...settings') with no
# spec= in the same call. We match the opening call and check the same line.
_BARE_SETTINGS_PATCH = re.compile(r"""patch\(\s*['"][\w.]*\.settings['"]\s*\)""")

# Grandfathered count as of §17.854 (audit H4). New bare-settings patches must
# use realistic_settings / monkeypatch.setattr instead. DECREASE this as the
# existing ones migrate; it must never increase.
BASELINE = 10


def _scan() -> list[str]:
    hits: list[str] = []
    for f in sorted(_TESTS_DIR.rglob("test_*.py")):
        if "__pycache__" in f.parts or f.name == Path(__file__).name:
            continue  # don't match this scanner's own docstring / regex
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in _BARE_SETTINGS_PATCH.finditer(text):
            line = text[: m.start()].count("\n") + 1
            hits.append(f"{f.name}:{line}")
    return hits


def test_no_new_bare_settings_patches():
    hits = _scan()
    assert len(hits) <= BASELINE, (
        f"{len(hits)} bare `patch(\"...settings\")` occurrences exceed the "
        f"baseline of {BASELINE} — a new one replaces Settings with a truthy "
        f"MagicMock (unpinned valves read True). Use the `realistic_settings` "
        f"fixture or `monkeypatch.setattr(settings, ...)` instead.\nHits:\n  "
        + "\n  ".join(hits)
    )
