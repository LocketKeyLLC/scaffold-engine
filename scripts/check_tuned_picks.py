#!/usr/bin/env python3
"""§17.1174 — the tuned cloud picks must agree across every place they are written.

`presets/tuned-cloud.env` is the CANON: it is what `make apply-preset` writes
into `.env`, and therefore what the engine actually runs. Two copies exist —
the first-run wizard's `CLOUD_PICKS` in `app/ui/static/views/setup.js`, and the
"pin them in .env to opt in" comment in `docker-compose.yml` — and both had
drifted, on 4 of 9 roles:

    role                    preset (canon)           SPA (stale)
    model_verifier          glm-5.3-flash:cloud      kimi-k2.7-code:cloud
    model_router            gemma4:cloud             qwen3.5:397b-cloud
    model_research_extract  deepseek-v4-flash:cloud  glm-5.1:cloud
    model_triage            gemma4:cloud             qwen3.5:397b-cloud

Pressing "Ollama Cloud (tuned)" in the wizard would have regressed four
measured A/B picks — the exact failure the project's own note records as
"three written records went stale". `make check-rerank-drift` is the template:
it pins ONE value across three sites and has held since §17.245.

Pure static gate: no docker, no database, no model calls. Part of ci-tier-0.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PRESET = ROOT / "presets" / "tuned-cloud.env"
SPA = ROOT / "app" / "ui" / "static" / "views" / "setup.js"
COMPOSE = ROOT / "docker-compose.yml"

#: Roles whose value is a measurement. MODEL_ROLE_LEARNING_CANDIDATES is a JSON
#: blob of A/B *candidates*, not a pick, and is deliberately not compared.
_ROLE_RE = re.compile(r"^(MODEL_[A-Z_]+)=(\S+)$")
_SKIP = {"MODEL_ROLE_LEARNING_CANDIDATES", "MODEL_ROLE_LEARNING_ENABLED"}


def _preset() -> dict[str, str]:
    out = {}
    for line in PRESET.read_text(encoding="utf-8").splitlines():
        m = _ROLE_RE.match(line.strip())
        if m and m.group(1) not in _SKIP:
            out[m.group(1)] = m.group(2)
    return out


def _spa() -> dict[str, str]:
    src = SPA.read_text(encoding="utf-8")
    m = re.search(r"const CLOUD_PICKS = \{(.*?)\n\};", src, re.S)
    if not m:
        raise SystemExit(f"✗ could not find CLOUD_PICKS in {SPA.relative_to(ROOT)} — "
                         "the object's shape changed; update this gate.")
    return {f"MODEL_{k.upper().removeprefix('MODEL_')}": v
            for k, v in re.findall(r'(\w+)\s*:\s*"([^"]+)"', m.group(1))}


def _compose() -> dict[str, str]:
    src = COMPOSE.read_text(encoding="utf-8")
    m = re.search(r"pin them in \.env to opt in:(.*?)\n      MODEL_ROUTER:", src, re.S)
    if not m:
        raise SystemExit(f"✗ could not find the tuned-picks comment in "
                         f"{COMPOSE.relative_to(ROOT)} — update this gate.")
    return dict(re.findall(r"(MODEL_[A-Z_]+)=(\S+)", m.group(1)))


def main() -> int:
    canon, spa, compose = _preset(), _spa(), _compose()
    if not canon:
        print(f"✗ no MODEL_* lines in {PRESET.relative_to(ROOT)}")
        return 1
    bad = False
    for label, other in (("app/ui/static/views/setup.js (the wizard preset)", spa),
                         ("docker-compose.yml (the opt-in comment)", compose)):
        missing = sorted(set(canon) - set(other))
        extra = sorted(set(other) - set(canon))
        drift = sorted(r for r in set(canon) & set(other) if canon[r] != other[r])
        if missing or extra or drift:
            bad = True
            print(f"\033[1;31m✗ tuned-pick drift in {label}:\033[0m")
            for r in drift:
                print(f"    {r:<26} preset={canon[r]:<26} there={other[r]}")
            for r in missing:
                print(f"    {r:<26} preset={canon[r]:<26} there=<absent>")
            for r in extra:
                print(f"    {r:<26} preset=<absent>{'':<18} there={other[r]}")
    if bad:
        print("\033[1;33m  Fix: presets/tuned-cloud.env is the canon (it is what "
              "`make apply-preset` writes). Update the copies to match it — or, if "
              "a NEW A/B moved a pick, update the preset FIRST and then the copies."
              "\033[0m")
        return 1
    print(f"✓ tuned cloud picks agree across 3 sites: {len(canon)} roles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
