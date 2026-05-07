"""Click command behavior — output formatting + exit codes."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from scaffold_cli.client import CLIError
from scaffold_cli.main import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Stop any inherited config from leaking into command tests."""
    for v in ("SCAFFOLD_API_URL", "SCAFFOLD_API_KEY", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------
def test_version_prints_version_and_config_source(runner):
    res = runner.invoke(cli, ["version"])
    assert res.exit_code == 0
    assert "scaffold-cli" in res.output
    assert "api_url:" in res.output
    assert "default" in res.output  # no config = default source


def test_version_reflects_flag_overrides(runner):
    res = runner.invoke(cli, ["--api-url", "http://x:9", "--api-key", "k", "version"])
    assert res.exit_code == 0
    assert "http://x:9" in res.output
    assert "(flag)" in res.output
    assert "set" in res.output


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def test_doctor_renders_per_subsystem_status(runner):
    health = {
        "checks": {
            "postgresql": {"status": "up", "latency_ms": 4},
            "ollama": {"status": "up", "latency_ms": 12},
            "milvus": {"status": "up", "latency_ms": 7},
            "redis": {"status": "up", "latency_ms": 1},
        }
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = health
        res = runner.invoke(cli, ["doctor"])
    assert res.exit_code == 0
    for name in ("postgresql", "ollama", "milvus", "redis"):
        assert name in res.output


def test_doctor_exits_nonzero_when_any_subsystem_down(runner):
    health = {
        "checks": {
            "postgresql": {"status": "up"},
            "ollama": {"status": "down", "latency_ms": 5000},
        }
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = health
        res = runner.invoke(cli, ["doctor"])
    assert res.exit_code == 1
    assert "down" in res.output


def test_doctor_neutral_status_does_not_fail(runner):
    """Subsystems without an up/down keyword (cache stats, info-only checks)
    must NOT cause a non-zero exit — they're informational, not pass/fail."""
    health = {
        "checks": {
            "postgresql": {"status": "up"},
            "embedding_cache": {"hits": 100, "misses": 5},  # no status key
        }
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = health
        res = runner.invoke(cli, ["doctor"])
    assert res.exit_code == 0


def test_doctor_translates_connection_failure_to_friendly_text(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.side_effect = CLIError(
            "Cannot reach orchestrator at http://localhost:8000. Is it running?"
        )
        res = runner.invoke(cli, ["doctor"])
    assert res.exit_code == 1
    assert "Cannot reach orchestrator" in res.output


# ---------------------------------------------------------------------------
# ideate
# ---------------------------------------------------------------------------
def test_ideate_prints_job_id_and_next_step(runner):
    response = {
        "status": "awaiting_confirmation",
        "job_id": "abc-123",
        "feasibility": {"feasible": True, "confidence": 0.8, "summary": "Looks OK"},
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.post.return_value = response
        res = runner.invoke(cli, ["ideate", "build", "a", "thing"])
    assert res.exit_code == 0
    assert "abc-123" in res.output
    assert "awaiting_confirmation" in res.output
    assert "scaffold confirm abc-123" in res.output
    assert "Looks OK" in res.output


def test_ideate_passes_idea_text_to_orchestrator(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = {"status": "awaiting_confirmation", "job_id": "x"}
        runner.invoke(cli, ["ideate", "build", "an", "indexer", "--domain", "rag"])
    args, kwargs = post.call_args
    assert kwargs.get("json") == {"idea": "build an indexer", "domain": "rag"}


def test_ideate_json_flag_emits_raw_response(runner):
    response = {"status": "awaiting_confirmation", "job_id": "x"}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.post.return_value = response
        res = runner.invoke(cli, ["ideate", "--json", "x"])
    parsed = json.loads(res.output)
    assert parsed == response


def test_ideate_requires_text(runner):
    res = runner.invoke(cli, ["ideate"])
    assert res.exit_code != 0


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------
def test_confirm_passes_job_id_and_feedback(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = {"status": "completed"}
        runner.invoke(cli, ["confirm", "job-1", "use", "Postgres"])
    args, kwargs = post.call_args
    assert kwargs.get("json") == {"job_id": "job-1", "feedback": "use Postgres"}


def test_confirm_omits_feedback_when_not_supplied(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = {"status": "completed"}
        runner.invoke(cli, ["confirm", "job-1"])
    _, kwargs = post.call_args
    assert kwargs.get("json") == {"job_id": "job-1"}


# ---------------------------------------------------------------------------
# jobs list / status
# ---------------------------------------------------------------------------
def test_jobs_list_renders_table(runner):
    response = {"jobs": [
        {"id": "11111111-1111-1111-1111-111111111111", "status": "completed", "title": "First"},
        {"id": "22222222-2222-2222-2222-222222222222", "status": "running",   "title": "Second"},
    ]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = response
        res = runner.invoke(cli, ["jobs", "list"])
    assert res.exit_code == 0
    assert "First" in res.output
    assert "Second" in res.output
    assert "completed" in res.output


def test_jobs_list_handles_empty(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = {"jobs": []}
        res = runner.invoke(cli, ["jobs", "list"])
    assert res.exit_code == 0
    assert "(no jobs)" in res.output


def test_jobs_list_passes_filter_params(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        get = ClientCls.return_value.__enter__.return_value.get
        get.return_value = {"jobs": []}
        runner.invoke(cli, ["jobs", "list", "--limit", "10", "--status", "running"])
    _, kwargs = get.call_args
    assert kwargs["params"] == {"limit": 10, "status": "running"}


def test_jobs_status_returns_nonzero_when_missing(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get_or_none.return_value = None
        res = runner.invoke(cli, ["jobs", "status", "missing"])
    assert res.exit_code == 1
    assert "not found" in res.output


def test_jobs_status_prints_key_fields(runner):
    """Renders the /exec/status/<id> response shape (job_id/job_title/
    job_status + counts + next_node), not the historical /jobs/<id> shape."""
    job = {
        "job_id": "abc",
        "job_title": "Test",
        "job_status": "running",
        "compiled_output": None,
        "counts": {"done": 2, "running": 1, "pending": 3},
        "total_nodes": 6,
        "next_node": {"node_key": "T2", "title": "Run unit tests"},
        "nodes": [],
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get_or_none.return_value = job
        res = runner.invoke(cli, ["jobs", "status", "abc"])
    assert res.exit_code == 0
    assert "job_id: abc" in res.output
    assert "title:  Test" in res.output
    assert "status: running" in res.output
    # Counts rendered as "k=v" pairs sorted alphabetically.
    assert "done=2" in res.output and "running=1" in res.output
    assert "next:   T2" in res.output


# ---------------------------------------------------------------------------
# Global flag plumbing
# ---------------------------------------------------------------------------
def test_global_flags_pass_through_to_client(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = {}
        runner.invoke(
            cli,
            ["--api-url", "http://manual:7000", "--api-key", "k-flag",
             "doctor"],
        )
    args, kwargs = ClientCls.call_args
    assert args[0] == "http://manual:7000"
    assert args[1] == "k-flag"
