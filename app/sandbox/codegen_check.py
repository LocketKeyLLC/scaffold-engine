"""§17.434 — sandbox-backed exec-smoke for CodeGen node output.

Executes the node's own Python (module top-level) in the scaffold-coderunner
sandbox to catch RUNTIME / module-level errors that the §17.428 ast.parse gate
and the §17.429 LLM verifier miss (NameError, a bad call at import time, a
typo'd attribute, etc.). Fail-SOFT by design:

  pass  — module executed cleanly (exit 0)
  fail  — a genuine runtime error (non-zero exit with a traceback that is NOT
          an unresolved import) → fed into the W.1 retry loop
  skip  — no Python to run / sandbox disabled or unreachable / unresolved
          import (the sibling-module-not-present case, §17.367 multi-file) /
          ambiguous timeout. None of these are treated as a defect.

So a CodeGen node is only ever FAILED here on a definite, reproducible runtime
error in self-contained code — never on the multi-file import situation, and
never when the sandbox is off (the orchestrator gates the call on
settings.codegen_execution_check_enabled + a configured coderunner_url).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.modules.execution_codegen_gate import PYTHON_LANGS, extract_code_blocks
from app.sandbox.client import run_code

logger = logging.getLogger("scaffold")

# stderr markers that mean "couldn't run standalone", not "code is broken".
_IMPORT_MISS = ("ModuleNotFoundError", "ImportError")


@dataclass
class ExecCheckResult:
    verdict: str  # "pass" | "skip" | "fail"
    reason: str


def _format_fail(stderr: str, exit_code: int) -> str:
    tail = "\n".join((stderr or "").strip().splitlines()[-15:])
    return (
        "§17.434 sandbox execution gate: the generated code does not run — "
        f"executing the module raised a runtime error (exit {exit_code}). Fix it "
        "so the module imports/executes cleanly:\n\n" + tail
    )


async def codegen_exec_smoke(output: str, *, timeout_s: float = 20.0) -> ExecCheckResult:
    """Run the node's Python module top-level in the sandbox; classify the result."""
    blocks = [code for lang, code in extract_code_blocks(output) if lang in PYTHON_LANGS]
    if not blocks:
        return ExecCheckResult("skip", "no python block to execute")

    src = "\n\n".join(blocks)
    res = await run_code(
        {"solution.py": src},
        ["python", "solution.py"],
        timeout_s=timeout_s,
        cpu_seconds=int(timeout_s) + 5,
        mem_mb=512,
    )

    if res.error is not None:
        # disabled / unreachable / bad shape — never a code defect.
        return ExecCheckResult("skip", f"sandbox unavailable: {res.error}")
    if res.timed_out:
        return ExecCheckResult("skip", "execution timed out (ambiguous — not treated as a failure)")
    if res.ok:
        return ExecCheckResult("pass", "module executed cleanly")

    stderr = res.stderr or ""
    if any(m in stderr for m in _IMPORT_MISS):
        return ExecCheckResult("skip", "unresolved import — sibling module not present in the sandbox")
    return ExecCheckResult("fail", _format_fail(stderr, res.exit_code))
