"""Single source of truth for the SDK version.

Kept in sync with ``pyproject.toml`` and the FastAPI app version
(``app/main.py``). All three move together at major-version boundaries —
v1.x.y of the SDK pins to v1.x.y of the orchestrator API contract.
"""
__version__ = "1.0.0"
