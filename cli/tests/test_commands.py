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
# ---------------------------------------------------------------------------
# whatnow (Sprint U.6)
# ---------------------------------------------------------------------------


def test_whatnow_renders_actionable_jobs(runner):
    """whatnow filters out terminal statuses and shows recommended actions."""
    response = {"jobs": [
        {"id": "11111111-1111-1111-1111-111111111111", "status": "awaiting_confirmation",
         "title": "Markdown linter"},
        {"id": "22222222-2222-2222-2222-222222222222", "status": "completed",
         "title": "Old finished job"},
        {"id": "33333333-3333-3333-3333-333333333333", "status": "blocked",
         "title": "Stuck on T2"},
    ]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = response
        res = runner.invoke(cli, ["whatnow"])
    assert res.exit_code == 0
    # Awaiting + blocked should appear
    assert "Markdown linter" in res.output
    assert "awaiting_confirmation" in res.output
    assert "Stuck on T2" in res.output
    assert "blocked" in res.output
    # Completed should be filtered out
    assert "Old finished job" not in res.output


def test_whatnow_empty_when_no_actionable(runner):
    """When everything is terminal, whatnow says so explicitly."""
    response = {"jobs": [
        {"id": "1", "status": "completed", "title": "Done"},
        {"id": "2", "status": "cancelled", "title": "Abandoned"},
    ]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = response
        res = runner.invoke(cli, ["whatnow"])
    assert res.exit_code == 0
    assert "Nothing needs your attention" in res.output


def test_whatnow_json_returns_structured_list(runner):
    response = {"jobs": [
        {"id": "11111111-1111-1111-1111-111111111111", "status": "awaiting_confirmation",
         "title": "Test"},
    ]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = response
        res = runner.invoke(cli, ["whatnow", "--json"])
    assert res.exit_code == 0
    parsed = json.loads(res.output)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["status"] == "awaiting_confirmation"
    assert "confirm" in parsed[0]["valid_actions"]


def test_whatnow_respects_limit(runner):
    response = {"jobs": [
        {"id": str(i) * 36, "status": "awaiting_confirmation", "title": f"job {i}"}
        for i in range(10)
    ]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = response
        res = runner.invoke(cli, ["whatnow", "--limit", "3"])
    assert res.exit_code == 0
    assert "3 job(s) need attention" in res.output


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


# ---------------------------------------------------------------------------
# Sprint U.7 — CLI parity sweep (jobs find/rename/delete, schedule, rag,
# optimize, skip, model). Covers the new commands at the verb-level so the
# Click wiring can't silently break.
# ---------------------------------------------------------------------------

def test_jobs_find_passes_q_param_and_renders_results(runner):
    response = {"jobs": [
        {"id": "abc-1234-5678", "status": "completed", "title": "linter project"},
    ], "total": 1}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = response
        res = runner.invoke(cli, ["jobs", "find", "linter"])
    args, kwargs = ClientCls.return_value.__enter__.return_value.get.call_args
    assert args[0] == "/jobs"
    assert kwargs["params"]["q"] == "linter"
    assert res.exit_code == 0
    assert "linter project" in res.output


def test_jobs_rename_uses_patch_with_title_body(runner):
    response = {"id": "abc-1234", "title": "renamed!", "status": "completed",
                "node_count": 0, "created_at": "2026-05-07T00:00:00+00:00",
                "updated_at": "2026-05-07T00:00:00+00:00"}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.patch.return_value = response
        res = runner.invoke(cli, ["jobs", "rename", "abc-1234", "renamed!"])
    args, kwargs = ClientCls.return_value.__enter__.return_value.patch.call_args
    assert args[0] == "/jobs/abc-1234"
    assert kwargs["json"]["title"] == "renamed!"
    assert res.exit_code == 0
    assert "renamed" in res.output


def test_jobs_delete_with_yes_skips_confirmation(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.delete.return_value = {"deleted": True}
        res = runner.invoke(cli, ["jobs", "delete", "abc-1234", "--yes"])
    args, _ = ClientCls.return_value.__enter__.return_value.delete.call_args
    assert args[0] == "/jobs/abc-1234"
    assert res.exit_code == 0
    assert "deleted" in res.output


def test_skip_posts_with_both_ids(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.post.return_value = {"status": "running"}
        res = runner.invoke(cli, ["skip", "abc-1234", "T2"])
    args, kwargs = ClientCls.return_value.__enter__.return_value.post.call_args
    assert args[0] == "/skip"
    assert kwargs["json"] == {"job_id": "abc-1234", "node_key": "T2"}
    assert res.exit_code == 0


def test_schedule_list_renders_table(runner):
    response = {"schedules": [
        {"id": 1, "topic": "k8s news", "depth": "medium",
         "cron_expression": "0 9 * * 1", "timezone": "UTC",
         "next_run_at": "2026-05-12T09:00:00+00:00",
         "run_count": 4, "failure_count": 0},
    ]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = response
        res = runner.invoke(cli, ["schedule", "list"])
    assert res.exit_code == 0
    assert "k8s news" in res.output
    assert "0 9 * * 1" in res.output


def test_schedule_add_forwards_tz_to_endpoint(runner):
    response = {"id": 9, "topic": "ny news", "cron_expression": "0 9 * * 1",
                "depth": "medium", "timezone": "America/New_York"}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.post.return_value = response
        res = runner.invoke(cli, [
            "schedule", "add", "0 9 * * 1", "ny news",
            "--tz", "America/New_York",
        ])
    args, kwargs = ClientCls.return_value.__enter__.return_value.post.call_args
    assert args[0] == "/schedule"
    assert kwargs["json"]["timezone"] == "America/New_York"
    assert kwargs["json"]["topic"] == "ny news"
    assert res.exit_code == 0


def test_schedule_delete_with_yes_skips_confirmation(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.delete.return_value = {"deleted": 5}
        res = runner.invoke(cli, ["schedule", "delete", "5", "--yes"])
    args, _ = ClientCls.return_value.__enter__.return_value.delete.call_args
    assert args[0] == "/schedule/5"
    assert res.exit_code == 0


def test_rag_posts_query_with_top_k(runner):
    response = {"results": [
        {"score": 0.91, "domain": "rag", "text": "Milvus uses HNSW_SQ8 by default..."},
    ]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.post.return_value = response
        res = runner.invoke(cli, ["rag", "milvus index", "--top-k", "10"])
    args, kwargs = ClientCls.return_value.__enter__.return_value.post.call_args
    assert args[0] == "/rag"
    assert kwargs["json"]["query"] == "milvus index"
    assert kwargs["json"]["top_k"] == 10
    assert res.exit_code == 0
    assert "0.910" in res.output or "0.91" in res.output


def test_optimize_posts_prompt_and_renders_result(runner):
    response = {
        "optimized_prompt": "Write a function that gzips files older than 7 days.",
        "clarity_score": 0.87,
        "intent_verified": True,
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.post.return_value = response
        res = runner.invoke(cli, ["optimize", "Please could you maybe write a function"])
    args, kwargs = ClientCls.return_value.__enter__.return_value.post.call_args
    assert args[0] == "/optimize"
    assert kwargs["json"]["prompt"].startswith("Please could you")
    assert res.exit_code == 0
    assert "gzips" in res.output


def test_research_list_calls_sessions_endpoint(runner):
    response = {"sessions": [
        {"id": "sess-abcd-1234", "status": "completed", "topic": "k8s pods",
         "depth": "medium", "total_entries_ingested": 14},
    ], "total": 1}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = response
        res = runner.invoke(cli, ["research", "list"])
    args, _ = ClientCls.return_value.__enter__.return_value.get.call_args
    assert args[0] == "/research/sessions"
    assert res.exit_code == 0
    assert "k8s pods" in res.output


def test_research_rename_sends_topic_patch(runner):
    response = {"id": "sess-abcd", "topic": "new topic"}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.patch.return_value = response
        res = runner.invoke(cli, ["research", "rename", "sess-abcd", "new", "topic"])
    args, kwargs = ClientCls.return_value.__enter__.return_value.patch.call_args
    assert args[0] == "/research/sessions/sess-abcd"
    assert kwargs["json"]["topic"] == "new topic"
    assert res.exit_code == 0


def test_model_list_filters_config_to_model_fields(runner):
    response = {"fields": [
        {"name": "model_general", "value": "qwen3-vl:235b", "is_default": True},
        {"name": "model_coder", "value": "qwen2.5-coder:7b", "is_default": True},
        {"name": "ollama_url", "value": "http://172.18.0.1:11434", "is_default": True},
    ]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = response
        res = runner.invoke(cli, ["model", "list"])
    assert res.exit_code == 0
    assert "model_general" in res.output
    assert "model_coder" in res.output
    assert "ollama_url" not in res.output  # filtered out


def test_model_available_reads_health_models_loaded(runner):
    response = {"checks": {"ollama": {"status": "up",
                "models_loaded": ["qwen3:4b", "qwen2.5:7b", "qwen3-embedding:8b"]}}}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = response
        res = runner.invoke(cli, ["model", "available"])
    assert res.exit_code == 0
    for m in ("qwen3:4b", "qwen2.5:7b", "qwen3-embedding:8b"):
        assert m in res.output
