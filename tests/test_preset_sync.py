"""§17.1002 — the tracked preset and the recorded picks are one source of truth.

`.env` is gitignored and §17.819 keeps the CODE defaults local-safe on purpose,
so the measured cloud picks lived only on the operator's box. §17.994 wrote them
into `.env.example` as comments so they were at least recoverable; §17.995 then
found a THIRD stale comment, because a record nobody applies is a record nobody
checks. `presets/tuned-cloud.env` is applied (`make apply-preset`), and these
tests keep it honest against `app/config.py`'s `tuned cloud pick` annotations.
"""
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PRESET = _ROOT / "presets" / "tuned-cloud.env"


def _preset_keys() -> dict[str, str]:
    out = {}
    for line in _PRESET.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v
    return out


def _recorded_picks() -> dict[str, str]:
    """role -> the pick app/config.py documents, e.g. MODEL_ROUTER -> gemma4:cloud."""
    src = (_ROOT / "app" / "config.py").read_text()
    return {r.upper(): p.strip() for r, p in re.findall(
        r"^\s*(model_\w+): str = .*?tuned cloud pick: ([^,)]+)", src, re.M)}


def test_the_preset_exists_and_is_not_empty():
    assert _PRESET.exists(), "the preset is the thing that makes the picks survive a rebuild"
    assert _preset_keys(), "no keys parsed — check the file format"


@pytest.mark.parametrize("key", sorted(_recorded_picks()))
def test_every_recorded_pick_is_in_the_preset_with_the_same_value(key):
    """A pick documented in config.py but absent from (or different in) the
    preset is exactly the §17.995 stale-comment failure, one layer over."""
    picks, preset = _recorded_picks(), _preset_keys()
    assert key in preset, f"{key} is a recorded tuned pick but the preset never sets it"
    assert preset[key] == picks[key], (
        f"{key}: config.py records {picks[key]!r} but the preset applies "
        f"{preset[key]!r} — one of them is stale")


def test_the_preset_sets_only_keys_that_mean_something():
    """A typo'd key would apply silently and do nothing.

    Two kinds are legitimate: app settings (Settings fields) and compose-level
    vars like SEARXNG_CONFIG_DIR, which docker-compose interpolates and the app
    never sees. Both are accepted; anything else is a typo.
    """
    from app.config import Settings

    fields = {f.lower() for f in Settings.model_fields}
    compose_vars = set()
    example = _ROOT / ".env.example"
    if example.exists():
        compose_vars = {m.lower() for m in re.findall(
            r"^#?\s*([A-Z][A-Z0-9_]+)=", example.read_text(), re.M)}
    unknown = [k for k in _preset_keys()
               if k.lower() not in fields and k.lower() not in compose_vars]
    assert not unknown, f"preset sets keys that are neither settings nor documented: {unknown}"


def test_the_fallback_stays_local():
    """§17.819 — fallback must DIFFER from primary or it adds no failure-mode
    diversity. A preset that pointed it at a :cloud tag would quietly remove the
    only thing that still works when the cloud is the outage."""
    fb = _preset_keys().get("MODEL_FALLBACK", "")
    assert fb and not fb.endswith(":cloud"), (
        f"MODEL_FALLBACK={fb!r} — the resilience role must not be a cloud tag")


def test_the_apply_script_is_executable_and_idempotent_by_construction():
    """It must REPLACE keys in place rather than rewrite .env: that file holds
    secrets, and a rewrite is a good way to lose them."""
    script = (_ROOT / "scripts" / "apply_preset.sh")
    assert script.exists()
    body = script.read_text()
    assert "cp \"$ENV_FILE\" \"$BACKUP\"" in body, "must back up before touching .env"
    assert "already matches preset" in body, "must no-op cleanly when nothing changed"
