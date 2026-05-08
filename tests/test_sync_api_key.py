"""Sprint X.8 — sync_api_key.sh strict 5-place propagation.

Tests run the script against a sandboxed scratch dir (SCAFFOLD_REPO_ROOT
+ SCAFFOLD_BASHRC_PATH overrides), so the live repo + ~/.bashrc are
never touched.

Cases:
  - KEY arg: writes to .env (creates or replaces) + all valves.json + bashrc
  - No arg, .env has key: verifies + propagates to other places
  - No arg, .env missing: exits non-zero (unrecoverable)
  - Idempotent: re-run after a successful run produces 0 changes
  - Idempotent: existing-line replacement vs. append-with-marker semantics
    on bashrc are both honored
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sync_api_key.sh"
PIPELINE_NAMES = (
    "scaffold_router",
    "execution_handler",
    "dag_viewer",
    "gt_browser",
    "prompt_inspector",
)


def _seed_repo(tmp_path: Path, *, env_key: str | None = None) -> Path:
    """Create a scratch repo with the same shape sync_api_key.sh expects."""
    repo = tmp_path / "scaffold"
    repo.mkdir()
    (repo / "pipelines").mkdir()
    for name in PIPELINE_NAMES:
        d = repo / "pipelines" / name
        d.mkdir()
        (d / "valves.json").write_text(json.dumps(
            {"api_key": "stale-old-key", "orchestrator_url": "x"},
        ) + "\n")
    if env_key is not None:
        (repo / ".env").write_text(f"SCAFFOLD_API_KEY={env_key}\n")
    return repo


def _run_script(repo: Path, bashrc: Path, *args, expect_rc: int = 0) -> str:
    """Invoke the script in a sandboxed env. Returns combined stdout/stderr."""
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={
            **os.environ,
            "SCAFFOLD_REPO_ROOT": str(repo),
            "SCAFFOLD_BASHRC_PATH": str(bashrc),
            # Force non-tty so colour escapes don't pollute assertions.
            "TERM": "dumb",
        },
        capture_output=True, text=True,
    )
    assert proc.returncode == expect_rc, (
        f"unexpected rc={proc.returncode}; stderr={proc.stderr!r}; "
        f"stdout={proc.stdout!r}"
    )
    return proc.stdout + proc.stderr


def _read_valves_keys(repo: Path) -> dict[str, str]:
    return {
        name: json.loads((repo / "pipelines" / name / "valves.json").read_text())["api_key"]
        for name in PIPELINE_NAMES
    }


@pytest.mark.smoke
class TestSyncApiKeyArg:
    """KEY supplied as positional arg: full strict sync everywhere."""

    def test_writes_to_env_creates_when_missing(self, tmp_path):
        repo = _seed_repo(tmp_path)  # no .env
        bashrc = tmp_path / ".bashrc"
        bashrc.write_text("# pre-existing\n")
        new_key = "sk-scaffold-abcdef1234567890abcdef1234567890"

        _run_script(repo, bashrc, new_key)

        env_text = (repo / ".env").read_text()
        assert f"SCAFFOLD_API_KEY={new_key}" in env_text

    def test_writes_to_env_replaces_existing_line(self, tmp_path):
        repo = _seed_repo(tmp_path, env_key="sk-scaffold-old-1234567890")
        bashrc = tmp_path / ".bashrc"
        bashrc.write_text("")
        new_key = "sk-scaffold-newkey1234567890abcdef12345678"

        _run_script(repo, bashrc, new_key)

        env_text = (repo / ".env").read_text()
        # Single line for the key — no duplicate.
        key_lines = [l for l in env_text.splitlines() if l.startswith("SCAFFOLD_API_KEY=")]
        assert len(key_lines) == 1
        assert key_lines[0] == f"SCAFFOLD_API_KEY={new_key}"

    def test_writes_all_valves_json(self, tmp_path):
        repo = _seed_repo(tmp_path, env_key="sk-scaffold-old-1234567890")
        bashrc = tmp_path / ".bashrc"
        bashrc.write_text("")
        new_key = "sk-scaffold-syncedkey1234567890abcdef1234"

        _run_script(repo, bashrc, new_key)

        keys = _read_valves_keys(repo)
        assert all(v == new_key for v in keys.values()), f"drift: {keys}"

    def test_appends_export_line_when_bashrc_lacks_it(self, tmp_path):
        repo = _seed_repo(tmp_path, env_key="sk-scaffold-old-1234567890")
        bashrc = tmp_path / ".bashrc"
        bashrc.write_text("# user's existing rc\nalias ll='ls -la'\n")
        new_key = "sk-scaffold-newkey9999999999999999999999"

        _run_script(repo, bashrc, new_key)

        text = bashrc.read_text()
        # Original lines preserved.
        assert "alias ll='ls -la'" in text
        # New export line + marker comment appended.
        assert "managed by sync_api_key.sh" in text
        assert f"export SCAFFOLD_API_KEY={new_key}" in text

    def test_replaces_existing_export_line_in_place(self, tmp_path):
        repo = _seed_repo(tmp_path, env_key="sk-scaffold-old-1234567890")
        bashrc = tmp_path / ".bashrc"
        bashrc.write_text(
            "export SCAFFOLD_API_KEY=sk-scaffold-stale1234567890\n"
            "alias gs='git status'\n"
        )
        new_key = "sk-scaffold-fresh1234567890abcdef12345678"

        _run_script(repo, bashrc, new_key)

        text = bashrc.read_text()
        assert f"export SCAFFOLD_API_KEY={new_key}" in text
        # The old key is gone.
        assert "sk-scaffold-stale" not in text
        # User's other lines preserved.
        assert "alias gs='git status'" in text
        # No duplicate marker comment (we only append the marker on first
        # touch; replacement keeps a single export line).
        assert text.count("export SCAFFOLD_API_KEY=") == 1


@pytest.mark.smoke
class TestSyncApiKeyVerifyMode:
    """No KEY arg: read from .env, propagate to other places."""

    def test_propagates_env_key_to_valves_and_bashrc(self, tmp_path):
        env_key = "sk-scaffold-fromenv1234567890abcdef1234"
        repo = _seed_repo(tmp_path, env_key=env_key)
        bashrc = tmp_path / ".bashrc"
        bashrc.write_text("")

        _run_script(repo, bashrc)

        keys = _read_valves_keys(repo)
        assert all(v == env_key for v in keys.values())
        assert f"export SCAFFOLD_API_KEY={env_key}" in bashrc.read_text()
        # .env unchanged — verify mode never rewrites the source.
        assert (repo / ".env").read_text().rstrip() == f"SCAFFOLD_API_KEY={env_key}"

    def test_no_arg_no_env_exits_nonzero(self, tmp_path):
        repo = _seed_repo(tmp_path)  # no .env
        bashrc = tmp_path / ".bashrc"
        bashrc.write_text("")

        out = _run_script(repo, bashrc, expect_rc=2)
        assert "no KEY arg" in out or "not set" in out

    def test_env_with_only_comment_treated_as_unset(self, tmp_path):
        repo = _seed_repo(tmp_path)
        (repo / ".env").write_text("# SCAFFOLD_API_KEY=commented-out\n")
        bashrc = tmp_path / ".bashrc"
        bashrc.write_text("")

        out = _run_script(repo, bashrc, expect_rc=2)
        assert "not set" in out


@pytest.mark.smoke
class TestSyncApiKeyIdempotent:
    """Re-running on an already-synced repo must produce 0 changes."""

    def test_second_run_reports_zero_changes(self, tmp_path):
        env_key = "sk-scaffold-idem1234567890abcdef12345678"
        repo = _seed_repo(tmp_path, env_key=env_key)
        bashrc = tmp_path / ".bashrc"
        bashrc.write_text("")

        # First run: stale valves get updated.
        _run_script(repo, bashrc)
        # Second run: everything's aligned now.
        out2 = _run_script(repo, bashrc)

        # Summary line shows 0 changes; only "already aligned" rows.
        assert "changed=0" in out2
        # No "Next steps" reminder when nothing changed.
        assert "Next steps" not in out2
