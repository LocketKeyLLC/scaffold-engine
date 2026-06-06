"""§17.433 — code-execution sandbox client package.

Talks to the scaffold-coderunner sidecar that runs untrusted LLM-generated
code + its tests in isolation (the software-path ground-truth oracle). See
app/sandbox/client.py and docker/coderunner/.
"""
from app.sandbox.client import CodeRunResult, run_code

__all__ = ["CodeRunResult", "run_code"]
