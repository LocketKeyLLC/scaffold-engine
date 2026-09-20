#!/usr/bin/env python3
"""§17.1058 — one-command audit with the developer tools (read-only).

Runs every static/read-only tool the repo carries (§17.1057), collects
counts and the top findings, and writes a Markdown report. Nothing here
mutates the repo, the images or the live orchestrator. Usage:

    make audit-tools                       # full report → .profiles/audit-<date>.md
    python3 scripts/audit_tools.py --quick # skip the slow network/image scans

Each section says HOW it was measured so a reader can rerun one tool.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
os.chdir(ROOT)


def sh(cmd: list[str], *, timeout: int = 900, env: dict | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **(env or {})})
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out after {timeout}s"


def have(b: str) -> bool:
    return shutil.which(b) is not None


class Report:
    def __init__(self) -> None:
        self.sections: list[tuple[str, str, str, list[str]]] = []  # (name, status, headline, details)

    def add(self, name: str, status: str, headline: str, details: list[str] | None = None) -> None:
        self.sections.append((name, status, headline, details or []))

    def render(self) -> str:
        out = [f"# Tool audit — {dt.datetime.now():%Y-%m-%d %H:%M}", "",
               "| Tool | Status | Headline |", "|---|---|---|"]
        for n, s, h, _ in self.sections:
            out.append(f"| {n} | {s} | {h} |")
        out.append("")
        for n, s, h, d in self.sections:
            if d:
                out.append(f"## {n}")
                out.extend(d[:60])
                out.append("")
        return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="skip semgrep, trivy, dive (network / image)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    r = Report()

    # ── ruff: the repo's rule set, then an audit-only wider set (not enforced) ──
    rc, out = sh(["ruff", "check", "app", "tests", "scripts", "--output-format", "concise"])
    n = sum(1 for l in out.splitlines() if ": " in l and l.split(":")[0].endswith(".py"))
    r.add("ruff (repo rules)", "ok" if rc == 0 else "FINDINGS", f"{n} finding(s)", out.splitlines()[:40])
    rc, out = sh(["ruff", "check", "app", "--select", "B,S,SIM,PERF,RUF", "--statistics", "--output-format", "concise"])
    stats = [l for l in out.splitlines() if l.strip() and l[0].isdigit() or "\t" in l]
    r.add("ruff (audit rules B/S/SIM/PERF/RUF)", "advisory", f"{len(stats)} rule(s) with hits — not enforced",
          ["```", *out.splitlines()[-40:], "```"])

    # ── pyright: gate scope (config) then the whole app (audit) ──
    rc, out = sh(["pyright"])
    r.add("pyright (gate scope)", "ok" if rc == 0 else "FINDINGS", out.strip().splitlines()[-1] if out.strip() else "no output")
    rc, out = sh(["pyright", "app", "--outputjson"], timeout=1200)
    try:
        j = json.loads(out[out.index("{"):])
        s = j.get("summary", {})
        diags = j.get("generalDiagnostics", [])
        by_rule: dict[str, int] = {}
        for d in diags:
            if d.get("severity") == "error":
                by_rule[d.get("rule") or "?"] = by_rule.get(d.get("rule") or "?", 0) + 1
        top = sorted(by_rule.items(), key=lambda x: -x[1])[:10]
        r.add("pyright (whole app, basic)", "advisory",
              f"{s.get('errorCount', '?')} errors / {s.get('warningCount', '?')} warnings across {s.get('filesAnalyzed', '?')} files — not enforced",
              [f"- {k}: {v}" for k, v in top] + [f"- {d['file'].replace(str(ROOT)+'/', '')}:{d['range']['start']['line']+1} {d.get('rule')} — {d['message'][:110]}"
                                                  for d in [x for x in diags if x.get('severity') == 'error'][:25]])
    except Exception:
        r.add("pyright (whole app, basic)", "n/a", out.strip().splitlines()[-1] if out.strip() else "no output")

    # ── ast-grep ──
    rc, out = sh([shutil.which("ast-grep") or "ast-grep", "scan", "--json"])
    try:
        hits = [h for h in json.loads(out or "[]") if not h["file"].startswith("tests/fixtures/")]
        errs = [h for h in hits if h["severity"] == "error"]; warns = [h for h in hits if h["severity"] == "warning"]
        by = {}
        for h in warns: by[h["ruleId"]] = by.get(h["ruleId"], 0) + 1
        r.add("ast-grep rules", "ok" if not errs else "FINDINGS", f"{len(errs)} error(s), {len(warns)} advisory warning(s)",
              [f"- {k}: {v}" for k, v in by.items()] + [f"- {h['file']}:{h['range']['start']['line']+1} {h['ruleId']}" for h in warns[:30]])
    except Exception:
        r.add("ast-grep rules", "n/a", out.strip()[:120])

    # ── import-linter ──
    rc, out = sh(["lint-imports"], env={"PYTHONPATH": str(ROOT)})
    kept = sum(1 for l in out.splitlines() if l.endswith("KEPT")); broken = sum(1 for l in out.splitlines() if l.endswith("BROKEN"))
    r.add("import-linter", "ok" if rc == 0 else "FINDINGS", f"{kept} kept, {broken} broken", out.splitlines()[-12:] if rc else [])

    # ── vulture ──
    rc, out = sh(["vulture", "app", "scripts/vulture_whitelist.py", "--min-confidence", "80"])
    lines = [l for l in out.splitlines() if l.strip()]
    r.add("vulture (dead code ≥80%)", "advisory", f"{len(lines)} candidate(s)", [f"- {l}" for l in lines[:40]])

    # ── hadolint ──
    rc, out = sh(["hadolint", "Dockerfile"])
    lines = [l for l in out.splitlines() if l.strip()]
    r.add("hadolint", "ok" if not lines else "advisory", f"{len(lines)} finding(s)", [f"- {l}" for l in lines])

    # ── oasdiff vs origin/main ──
    rc, out = sh(["sh", "-c", "git show origin/main:docs/openapi.json > /tmp/openapi.main.json && oasdiff breaking /tmp/openapi.main.json docs/openapi.json"])
    r.add("oasdiff (breaking vs origin/main)", "ok" if rc == 0 else "FINDINGS", (out.strip().splitlines() or ["no changes"])[-1][:120], out.splitlines()[:20] if rc else [])

    # ── mutation results on record (if a `make mutate` ran) ──
    if (ROOT / "mutants").is_dir():
        rc, out = sh(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-e", "PYTHONPATH=",
                      "-v", f"{ROOT}:/work", "-w", "/work", "scaffold-engine:dev", "sh", "-c", "mutmut results 2>/dev/null"], timeout=300)
        surv = [l.strip() for l in out.replace("\r", "\n").splitlines() if l.strip().endswith(": survived")]
        r.add("mutmut (last run on record)", "advisory", f"{len(surv)} survivor(s) listed", [f"- {l}" for l in surv[:30]])
    else:
        r.add("mutmut", "skipped", "no mutants/ on disk — run `make mutate`")

    if not args.quick:
        # ── semgrep (network: rules) ──
        rc, out = sh(["semgrep", "scan", "--config", "p/python", "--config", "p/security-audit",
                      "--exclude-rule", "python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text",
                      "--metrics=off", "--quiet", "--json", "app"], timeout=1200)
        try:
            j = json.loads(out[out.index("{"):]); res = j.get("results", [])
            by = {}
            for x in res: by[x["check_id"].rsplit(".", 1)[-1]] = by.get(x["check_id"].rsplit(".", 1)[-1], 0) + 1
            r.add("semgrep (p/python + p/security-audit)", "advisory" if res else "ok", f"{len(res)} finding(s)",
                  [f"- {k}: {v}" for k, v in by.items()] + [f"- {x['path']}:{x['start']['line']} {x['check_id'].rsplit('.',1)[-1]}" for x in res[:25]])
        except Exception:
            r.add("semgrep", "n/a", out.strip().splitlines()[-1][:120] if out.strip() else "no output")
        # ── trivy (image) ──
        tag = os.environ.get("SCAFFOLD_IMAGE_TAG", "local")
        rc, out = sh(["trivy", "image", "--severity", "HIGH,CRITICAL", "--scanners", "vuln", "--quiet", "--format", "json", f"scaffold-engine:{tag}"], timeout=1500)
        try:
            j = json.loads(out[out.index("{"):])
            tot = {"HIGH": 0, "CRITICAL": 0}; fixable = 0; pkgs = {}
            for res in j.get("Results", []) or []:
                for v in res.get("Vulnerabilities", []) or []:
                    tot[v["Severity"]] = tot.get(v["Severity"], 0) + 1
                    if v.get("FixedVersion"): fixable += 1
                    pkgs[v["PkgName"]] = pkgs.get(v["PkgName"], 0) + 1
            top = sorted(pkgs.items(), key=lambda x: -x[1])[:12]
            r.add(f"trivy (scaffold-engine:{tag})", "advisory", f"{tot.get('CRITICAL',0)} critical, {tot.get('HIGH',0)} high; {fixable} with a fixed version",
                  [f"- {k}: {v}" for k, v in top])
        except Exception:
            r.add("trivy", "n/a", out.strip().splitlines()[-1][:120] if out.strip() else "no output")
        # ── dive ──
        rc, out = sh(["dive", f"scaffold-engine:{tag}"], env={"CI": "true"}, timeout=600)
        eff = [l.strip() for l in out.splitlines() if "efficiency" in l or "wastedBytes" in l]
        r.add("dive", "ok" if rc == 0 else "advisory", "; ".join(eff)[:120])

    text = r.render()
    outp = pathlib.Path(args.out) if args.out else ROOT / ".profiles" / f"audit-{dt.datetime.now():%Y%m%d-%H%M}.md"
    outp.parent.mkdir(exist_ok=True)
    outp.write_text(text)
    print(text)
    print(f"\n(written to {outp})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
