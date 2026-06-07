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


def test_confirm_chain_runs_phase2_dag_then_stream(runner):
    """`scaffold confirm <id> --chain` should:
       1. POST /ideate/confirm
       2. POST /dag (after Phase 2 returns)
       3. invoke _confirm_chain_continue which streams /execute/all
    Mirrors the OWUI auto-chain in CLI form (U.8.F).
    """
    chain_called: dict = {}

    def _fake_chain_continue(cfg, job_id):
        chain_called["job_id"] = job_id

    with patch("scaffold_cli.main.Client") as ClientCls, \
         patch("scaffold_cli.main._confirm_chain_continue", _fake_chain_continue):
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = {"status": "planning",
                             "workflow_summary": "ready to execute"}
        res = runner.invoke(cli, ["confirm", "job-1", "--chain"])

    assert res.exit_code == 0, res.output
    # Phase 2 was the only direct POST (step 2/3 happen inside the patched fn).
    args, kwargs = post.call_args
    assert args[0] == "/ideate/confirm"
    assert kwargs["json"] == {"job_id": "job-1"}
    # Chain handoff happened with the right job_id.
    assert chain_called == {"job_id": "job-1"}


def test_confirm_chain_rejects_with_json(runner):
    """`--json --chain` should error since chain streams to stdout."""
    res = runner.invoke(cli, ["confirm", "job-1", "--chain", "--json"])
    assert res.exit_code != 0
    assert "incompatible" in res.output.lower()


def test_confirm_without_chain_does_not_invoke_chain(runner):
    """Plain `scaffold confirm` must remain Phase 2 only — no chain."""
    chain_called: dict = {}

    def _fake_chain_continue(cfg, job_id):
        chain_called["job_id"] = job_id  # should NOT happen

    with patch("scaffold_cli.main.Client") as ClientCls, \
         patch("scaffold_cli.main._confirm_chain_continue", _fake_chain_continue):
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = {"status": "planning"}
        runner.invoke(cli, ["confirm", "job-1"])

    assert chain_called == {}


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
# J.3.c — `--costs` flag on `scaffold jobs status`
# ---------------------------------------------------------------------------


def _status_with_costs_mock(status_payload, costs_payload):
    """Build a Client mock whose two get_or_none calls return:
      1. /exec/status/<id> → status_payload
      2. /jobs/<id>/costs → costs_payload
    (in that order, matching jobs_status's call sequence)."""
    cm = patch("scaffold_cli.main.Client")
    return cm, [status_payload, costs_payload]


def test_jobs_status_costs_flag_renders_breakdown(runner):
    """`--costs` calls /jobs/<id>/costs after the status call and
    appends a totals header + per-(provider, model) breakdown table."""
    status = {
        "job_id": "abc",
        "job_title": "Test",
        "job_status": "completed",
        "counts": {"done": 3},
        "total_nodes": 3,
        "compiled_output": None,
        "nodes": [],
        # /exec/status's lightweight cost block (J.3.b) — falls through
        # to it if the breakdown call fails. Set zero here so the
        # assertion below is on the breakdown payload.
        "costs": {
            "total_cost_usd": 0.0, "call_count": 0,
            "total_prompt_tokens": 0, "total_completion_tokens": 0,
            "total_latency_ms": 0,
        },
    }
    costs = {
        "job_id": "abc",
        "total_cost_usd": 0.012345,
        "total_prompt_tokens": 5000,
        "total_completion_tokens": 2000,
        "total_latency_ms": 30000,
        "call_count": 12,
        "by_provider": [
            {"provider": "openai", "model": "gpt-4o-mini", "calls": 8,
             "cost_usd": 0.012, "prompt_tokens": 4000,
             "completion_tokens": 1500, "latency_ms": 22000},
            {"provider": "ollama", "model": "qwen3:4b", "calls": 4,
             "cost_usd": 0.000345, "prompt_tokens": 1000,
             "completion_tokens": 500, "latency_ms": 8000},
        ],
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get_or_none.side_effect = [
            status, costs,
        ]
        res = runner.invoke(cli, ["jobs", "status", "abc", "--costs"])
    assert res.exit_code == 0, res.output
    # Existing status fields still rendered.
    assert "job_id: abc" in res.output
    # Costs section header + totals.
    assert "costs:" in res.output
    assert "$0.0123" in res.output  # total formatted
    assert "calls:    12" in res.output
    assert "prompt=5000" in res.output and "completion=2000" in res.output
    assert "30000 ms" in res.output
    # Breakdown table — columns + per-row content.
    assert "by provider/model:" in res.output
    assert "openai" in res.output and "gpt-4o-mini" in res.output
    assert "ollama" in res.output and "qwen3:4b" in res.output


def test_jobs_status_costs_falls_back_to_status_totals_when_breakdown_unavailable(runner):
    """If /jobs/<id>/costs returns None (e.g. SDK error), the renderer
    still surfaces the lightweight totals from /exec/status's `costs`
    block. Operator-friendly: get *something*, not nothing."""
    status = {
        "job_id": "abc", "job_title": "T", "job_status": "running",
        "counts": {"done": 1}, "total_nodes": 1, "compiled_output": None,
        "nodes": [],
        "costs": {
            "total_cost_usd": 0.005, "call_count": 3,
            "total_prompt_tokens": 1500, "total_completion_tokens": 500,
            "total_latency_ms": 8000,
        },
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        # Second call returns None (breakdown endpoint unavailable).
        ClientCls.return_value.__enter__.return_value.get_or_none.side_effect = [
            status, None,
        ]
        res = runner.invoke(cli, ["jobs", "status", "abc", "--costs"])
    assert res.exit_code == 0
    assert "costs:" in res.output
    assert "$0.0050" in res.output  # total from /exec/status fallback
    assert "calls:    3" in res.output
    # No breakdown section since costs payload was unavailable.
    assert "by provider/model:" not in res.output


def test_jobs_status_costs_json_includes_breakdown_under_costs_breakdown_key(runner):
    """`--costs --json` embeds the breakdown payload alongside the status
    under a top-level `costs_breakdown` key. Existing /exec/status `costs`
    totals stay where they are; the breakdown is additive."""
    status = {
        "job_id": "abc", "job_title": "T", "job_status": "completed",
        "counts": {"done": 1}, "total_nodes": 1, "compiled_output": None,
        "nodes": [],
        "costs": {"total_cost_usd": 0.0, "call_count": 0,
                  "total_prompt_tokens": 0, "total_completion_tokens": 0,
                  "total_latency_ms": 0},
    }
    costs = {
        "job_id": "abc", "total_cost_usd": 0.001,
        "total_prompt_tokens": 100, "total_completion_tokens": 50,
        "total_latency_ms": 200, "call_count": 1,
        "by_provider": [{"provider": "openai", "model": "x", "calls": 1,
                         "cost_usd": 0.001, "prompt_tokens": 100,
                         "completion_tokens": 50, "latency_ms": 200}],
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get_or_none.side_effect = [
            status, costs,
        ]
        res = runner.invoke(cli, ["jobs", "status", "abc", "--costs", "--json"])
    assert res.exit_code == 0
    import json as _json
    body = _json.loads(res.output)
    assert "costs_breakdown" in body
    assert body["costs_breakdown"]["total_cost_usd"] == 0.001
    assert body["costs_breakdown"]["by_provider"][0]["model"] == "x"
    # Original status fields untouched.
    assert body["job_id"] == "abc"
    assert "costs" in body  # /exec/status's lightweight block preserved


def test_jobs_status_without_costs_flag_skips_costs_call(runner):
    """No --costs → only /exec/status is called; no /costs request."""
    status = {
        "job_id": "abc", "job_title": "T", "job_status": "running",
        "counts": {}, "total_nodes": 0, "compiled_output": None,
        "nodes": [],
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        get_or_none = ClientCls.return_value.__enter__.return_value.get_or_none
        get_or_none.return_value = status
        res = runner.invoke(cli, ["jobs", "status", "abc"])
    assert res.exit_code == 0
    # Exactly one call: /exec/status. No /costs follow-up.
    assert get_or_none.call_count == 1
    paths_called = [c.args[0] for c in get_or_none.call_args_list]
    assert any("/exec/status" in p for p in paths_called)
    assert not any("/costs" in p for p in paths_called)


# ---------------------------------------------------------------------------
# X.18 — `scaffold jobs synthesis` + `scaffold jobs list --synthesized`
# ---------------------------------------------------------------------------


def test_jobs_synthesis_on_sets_override_true(runner):
    """`--on` PATCHes /jobs/{id}/synthesis with override=True."""
    with patch("scaffold_cli.main.Client") as ClientCls:
        patch_mock = ClientCls.return_value.__enter__.return_value.patch
        patch_mock.return_value = {"job_id": "abc", "override": True}
        res = runner.invoke(cli, ["jobs", "synthesis", "abc", "--on"])
    assert res.exit_code == 0
    patch_mock.assert_called_once()
    args, kwargs = patch_mock.call_args
    assert args[0] == "/jobs/abc/synthesis"
    assert kwargs["json"] == {"override": True}
    assert "on" in res.output


def test_jobs_synthesis_off_sets_override_false(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        patch_mock = ClientCls.return_value.__enter__.return_value.patch
        patch_mock.return_value = {"job_id": "abc", "override": False}
        res = runner.invoke(cli, ["jobs", "synthesis", "abc", "--off"])
    assert res.exit_code == 0
    _, kwargs = patch_mock.call_args
    assert kwargs["json"] == {"override": False}
    assert "off" in res.output


def test_jobs_synthesis_auto_clears_override(runner):
    """`--auto` sets override to null so the job inherits the global flag."""
    with patch("scaffold_cli.main.Client") as ClientCls:
        patch_mock = ClientCls.return_value.__enter__.return_value.patch
        patch_mock.return_value = {"job_id": "abc", "override": None}
        res = runner.invoke(cli, ["jobs", "synthesis", "abc", "--auto"])
    assert res.exit_code == 0
    _, kwargs = patch_mock.call_args
    assert kwargs["json"] == {"override": None}
    assert "auto" in res.output


def test_jobs_synthesis_requires_decision_flag(runner):
    """No --on / --off / --auto → UsageError exit."""
    res = runner.invoke(cli, ["jobs", "synthesis", "abc"])
    assert res.exit_code != 0
    assert "exactly one of --on, --off, or --auto is required" in res.output


def test_jobs_list_synthesized_true_sends_filter(runner):
    """`--synthesized` adds ?synthesized=true to the GET params."""
    with patch("scaffold_cli.main.Client") as ClientCls:
        get_mock = ClientCls.return_value.__enter__.return_value.get
        get_mock.return_value = {"jobs": [], "total": 0}
        runner.invoke(cli, ["jobs", "list", "--synthesized"])
    _, kwargs = get_mock.call_args
    assert kwargs["params"].get("synthesized") == "true"


def test_jobs_list_no_synthesized_sends_false_filter(runner):
    """`--no-synthesized` adds ?synthesized=false."""
    with patch("scaffold_cli.main.Client") as ClientCls:
        get_mock = ClientCls.return_value.__enter__.return_value.get
        get_mock.return_value = {"jobs": [], "total": 0}
        runner.invoke(cli, ["jobs", "list", "--no-synthesized"])
    _, kwargs = get_mock.call_args
    assert kwargs["params"].get("synthesized") == "false"


def test_jobs_list_no_flag_omits_synthesized_param(runner):
    """No flag → param NOT sent (orchestrator returns all jobs)."""
    with patch("scaffold_cli.main.Client") as ClientCls:
        get_mock = ClientCls.return_value.__enter__.return_value.get
        get_mock.return_value = {"jobs": [], "total": 0}
        runner.invoke(cli, ["jobs", "list"])
    _, kwargs = get_mock.call_args
    assert "synthesized" not in kwargs["params"]


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


# ---------------------------------------------------------------------------
# §17.347 — model providers / set / unset
# ---------------------------------------------------------------------------
def _seed_repo(tmp_path, env_content: str = ""):
    """Create a minimal repo layout with .env and pipelines/scaffold_router/.
    Returns the repo root path."""
    (tmp_path / ".env").write_text(env_content)
    (tmp_path / "pipelines" / "scaffold_router").mkdir(parents=True)
    return tmp_path


def test_model_providers_shows_ready_when_keys_set(runner):
    response = {"fields": [
        {"name": "openai_api_key", "value": "**********"},     # SecretStr-set sentinel
        {"name": "anthropic_api_key", "value": ""},            # not set
        {"name": "ollama_base_url", "value": "http://x:11434"},
    ]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = response
        res = runner.invoke(cli, ["model", "providers"])
    assert res.exit_code == 0
    # ollama always ready
    assert "ollama" in res.output and "ready" in res.output
    # openai ready (key present)
    assert "openai" in res.output
    # anthropic not ready
    assert "anthropic" in res.output and "no API key" in res.output


def test_model_providers_json_mode(runner):
    response = {"fields": [
        {"name": "openai_api_key", "value": ""},
        {"name": "anthropic_api_key", "value": "**********"},
    ]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = response
        res = runner.invoke(cli, ["model", "providers", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    by_name = {r["provider"]: r for r in data}
    assert by_name["ollama"]["status"] == "ready"
    assert by_name["openai"]["status"] == "no API key"
    assert by_name["anthropic"]["status"] == "ready"


def test_model_set_writes_env_and_pipeline_valves(runner, tmp_path):
    root = _seed_repo(tmp_path, env_content="EXISTING=keep_me\n")
    res = runner.invoke(cli, [
        "model", "set", "general", "claude-haiku-4-5",
        "--provider", "anthropic",
        "--repo-root", str(root),
    ])
    assert res.exit_code == 0, res.output
    # .env got both keys appended, old line preserved
    env_text = (root / ".env").read_text()
    assert "EXISTING=keep_me" in env_text
    assert "MODEL_GENERAL=claude-haiku-4-5" in env_text
    assert "MODEL_GENERAL_PROVIDER=anthropic" in env_text
    # pipeline valves got the model key
    valves = json.loads((root / "pipelines" / "scaffold_router" / "valves.json").read_text())
    assert valves["model_general"] == "claude-haiku-4-5"
    # restart hint emitted
    assert "docker restart" in res.output
    assert "open-webui-pipelines" in res.output  # pipeline was touched


def test_model_set_replaces_existing_env_line(runner, tmp_path):
    """If MODEL_GENERAL is already set, replace it in place — don't append a duplicate."""
    root = _seed_repo(tmp_path, env_content="MODEL_GENERAL=qwen3:4b\nOTHER=x\n")
    res = runner.invoke(cli, [
        "model", "set", "general", "qwen3-vl:235b-instruct-cloud",
        "--repo-root", str(root),
    ])
    assert res.exit_code == 0, res.output
    env_text = (root / ".env").read_text()
    # exactly one MODEL_GENERAL line
    assert env_text.count("MODEL_GENERAL=") == 1
    assert "MODEL_GENERAL=qwen3-vl:235b-instruct-cloud" in env_text
    assert "OTHER=x" in env_text


def test_model_set_dry_run_writes_nothing(runner, tmp_path):
    root = _seed_repo(tmp_path, env_content="")
    res = runner.invoke(cli, [
        "model", "set", "router", "qwen3-vl:235b-instruct-cloud",
        "--repo-root", str(root), "--dry-run",
    ])
    assert res.exit_code == 0, res.output
    assert "dry-run" in res.output
    assert (root / ".env").read_text() == ""  # untouched
    assert not (root / "pipelines" / "scaffold_router" / "valves.json").exists()


def test_model_set_rejects_locked_role(runner, tmp_path):
    root = _seed_repo(tmp_path)
    res = runner.invoke(cli, [
        "model", "set", "embedder", "anything", "--repo-root", str(root),
    ])
    assert res.exit_code == 2
    assert "config-locked" in res.output
    assert (root / ".env").read_text() == ""  # untouched


def test_model_set_rejects_unknown_role(runner, tmp_path):
    root = _seed_repo(tmp_path)
    res = runner.invoke(cli, [
        "model", "set", "wizard", "claude-opus-4-7", "--repo-root", str(root),
    ])
    assert res.exit_code == 2
    assert "unknown role" in res.output


def test_model_set_rejects_unknown_provider(runner, tmp_path):
    root = _seed_repo(tmp_path)
    res = runner.invoke(cli, [
        "model", "set", "general", "x", "--provider", "cohere", "--repo-root", str(root),
    ])
    assert res.exit_code == 2
    assert "unknown provider" in res.output


def test_model_set_cloud_heavy_skips_pipeline_valve(runner, tmp_path):
    """cloud_heavy is orchestrator-only — no pipeline valve, no restart hint for pipelines."""
    root = _seed_repo(tmp_path, env_content="")
    res = runner.invoke(cli, [
        "model", "set", "cloud_heavy", "claude-opus-4-7", "--repo-root", str(root),
    ])
    assert res.exit_code == 0, res.output
    assert "MODEL_CLOUD_HEAVY=claude-opus-4-7" in (root / ".env").read_text()
    assert not (root / "pipelines" / "scaffold_router" / "valves.json").exists()
    # Restart hint should NOT include open-webui-pipelines (pipeline not touched)
    assert "open-webui-pipelines" not in res.output


def test_model_unset_removes_env_and_pipeline_valves(runner, tmp_path):
    root = _seed_repo(tmp_path,
        env_content="MODEL_GENERAL=x\nMODEL_GENERAL_PROVIDER=anthropic\nKEEP=y\n")
    valves_path = root / "pipelines" / "scaffold_router" / "valves.json"
    valves_path.write_text(json.dumps({"model_general": "x", "triage_model": "z"}))
    res = runner.invoke(cli, [
        "model", "unset", "general", "--repo-root", str(root),
    ])
    assert res.exit_code == 0, res.output
    env_text = (root / ".env").read_text()
    assert "MODEL_GENERAL=" not in env_text
    assert "MODEL_GENERAL_PROVIDER=" not in env_text
    assert "KEEP=y" in env_text  # other lines preserved
    valves = json.loads(valves_path.read_text())
    assert "model_general" not in valves
    assert valves["triage_model"] == "z"  # other valves preserved


def test_model_unset_keep_provider_flag(runner, tmp_path):
    root = _seed_repo(tmp_path,
        env_content="MODEL_GENERAL=x\nMODEL_GENERAL_PROVIDER=anthropic\n")
    res = runner.invoke(cli, [
        "model", "unset", "general", "--repo-root", str(root), "--keep-provider",
    ])
    assert res.exit_code == 0, res.output
    env_text = (root / ".env").read_text()
    assert "MODEL_GENERAL=" not in env_text
    assert "MODEL_GENERAL_PROVIDER=anthropic" in env_text


def test_model_set_walks_up_for_repo_root(runner, tmp_path, monkeypatch):
    """Without --repo-root, walks up from cwd to find .env."""
    root = _seed_repo(tmp_path)
    subdir = root / "deep" / "nested" / "dir"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    res = runner.invoke(cli, ["model", "set", "router", "qwen3:4b"])
    assert res.exit_code == 0, res.output
    assert "MODEL_ROUTER=qwen3:4b" in (root / ".env").read_text()


def test_model_set_errors_when_no_env_found(runner, tmp_path, monkeypatch):
    """If no .env anywhere up the tree, fail with actionable message — not crash."""
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(cli, ["model", "set", "router", "qwen3:4b"])
    assert res.exit_code == 2
    assert "no .env" in res.output.lower() or "no .env" in res.stderr.lower() or ".env" in res.output


# ---------------------------------------------------------------------------
# §17.349 — compose-shadow warning on model set
# ---------------------------------------------------------------------------
def test_model_set_warns_when_compose_shadows_unparameterized(runner, tmp_path):
    """If docker-compose.yml hardcodes the var as `KEY: literal` (no ${VAR}),
    `scaffold model set` must warn that the .env write will have no effect.
    Closes the §17.348 silent-shadow class for future hosts."""
    root = _seed_repo(tmp_path, env_content="")
    # Hardcoded literal in compose — the §17.348 antipattern
    (root / "docker-compose.yml").write_text(
        "services:\n"
        "  scaffold-orchestrator:\n"
        "    environment:\n"
        "      MODEL_ROUTER: qwen3:4b\n"
    )
    res = runner.invoke(cli, [
        "model", "set", "router", "qwen3-vl:235b-instruct-cloud",
        "--repo-root", str(root),
    ])
    assert res.exit_code == 0, res.output
    assert "MODEL_ROUTER" in (root / ".env").read_text()
    # Warning must surface; mentions both the key and §17.348 for context
    assert "MODEL_ROUTER" in res.output
    assert "docker-compose.yml" in res.output
    assert "§17.348" in res.output or "17.348" in res.output


def test_model_set_no_warning_when_compose_parameterized(runner, tmp_path):
    """The §17.348-correct form `${VAR:-default}` must NOT trigger the warning."""
    root = _seed_repo(tmp_path, env_content="")
    (root / "docker-compose.yml").write_text(
        "services:\n"
        "  scaffold-orchestrator:\n"
        "    environment:\n"
        "      MODEL_ROUTER: ${MODEL_ROUTER:-qwen3-vl:235b-instruct-cloud}\n"
    )
    res = runner.invoke(cli, [
        "model", "set", "router", "anything-new",
        "--repo-root", str(root),
    ])
    assert res.exit_code == 0, res.output
    # No shadow warning when compose uses ${VAR:-...} form
    assert "docker-compose.yml hardcodes" not in res.output


def test_model_set_no_warning_when_compose_missing(runner, tmp_path):
    """No compose file = nothing to warn about."""
    root = _seed_repo(tmp_path, env_content="")
    # Deliberately no docker-compose.yml
    res = runner.invoke(cli, [
        "model", "set", "router", "anything",
        "--repo-root", str(root),
    ])
    assert res.exit_code == 0, res.output
    assert "docker-compose.yml" not in res.output


def test_model_set_warns_on_provider_shadow_too(runner, tmp_path):
    """If --provider is set AND MODEL_<ROLE>_PROVIDER is also compose-shadowed,
    the warning must fire for the provider key too."""
    root = _seed_repo(tmp_path, env_content="")
    (root / "docker-compose.yml").write_text(
        "services:\n"
        "  scaffold-orchestrator:\n"
        "    environment:\n"
        "      MODEL_GENERAL_PROVIDER: ollama\n"
    )
    res = runner.invoke(cli, [
        "model", "set", "general", "claude-haiku-4-5",
        "--provider", "anthropic", "--repo-root", str(root),
    ])
    assert res.exit_code == 0, res.output
    assert "MODEL_GENERAL_PROVIDER" in res.output
    assert "docker-compose.yml" in res.output


# ---------------------------------------------------------------------------
# assist group (Sprint U.8.A)
# ---------------------------------------------------------------------------


def test_assist_start_posts_job_and_hints_next(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = {"session_id": "sess-1", "status": "active"}
        res = runner.invoke(cli, ["assist", "start", "job-1"])
    assert res.exit_code == 0
    args, kwargs = post.call_args
    assert args[0] == "/assist/start"
    assert kwargs["json"] == {"job_id": "job-1"}
    assert "sess-1" in res.output
    assert "scaffold assist next sess-1" in res.output


def test_assist_start_forwards_policies(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = {"session_id": "s"}
        runner.invoke(cli, [
            "assist", "start", "job-1",
            "--handoff-policy", "auto_on_skip",
            "--replan-policy", "disabled",
        ])
    _, kwargs = post.call_args
    assert kwargs["json"]["handoff_policy"] == "auto_on_skip"
    assert kwargs["json"]["replan_policy"] == "disabled"


def test_assist_status_renders_rollup(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = {
            "id": "sess-1", "job_id": "job-1", "status": "active",
            "step_counts": {"pending": 2, "applied": 1},
        }
        res = runner.invoke(cli, ["assist", "status", "sess-1"])
    assert res.exit_code == 0
    assert "sess-1" in res.output
    assert "job-1" in res.output
    assert "active" in res.output
    assert "pending" in res.output


def test_assist_next_prints_node_and_prompt(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = {
            "session_id": "sess-1",
            "node_key": "T2",
            "prompt": "Do the thing.",
        }
        res = runner.invoke(cli, ["assist", "next", "sess-1"])
    assert res.exit_code == 0
    assert "node: T2" in res.output
    assert "Do the thing." in res.output
    assert "scaffold assist submit sess-1 T2" in res.output


def test_assist_next_handles_no_claimable_step(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = {
            "status": "active", "session_id": "sess-1",
            "node_key": None, "step_counts": {"applied": 5},
        }
        res = runner.invoke(cli, ["assist", "next", "sess-1"])
    assert res.exit_code == 0
    assert "no claimable step" in res.output


def test_assist_submit_inline_output(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = {"status": "applied"}
        res = runner.invoke(cli, [
            "assist", "submit", "sess-1", "T2", "--output", "did the thing",
        ])
    assert res.exit_code == 0
    args, kwargs = post.call_args
    assert args[0] == "/assist/sess-1/submit"
    assert kwargs["json"] == {
        "node_key": "T2",
        "output": "did the thing",
        "evidence_kind": "text",
        "action": "submit",
    }
    assert "submitted T2" in res.output


def test_assist_submit_reads_file_evidence(runner, tmp_path):
    f = tmp_path / "diff.patch"
    f.write_text("--- a\n+++ b\n")
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = {"status": "applied"}
        runner.invoke(cli, [
            "assist", "submit", "sess-1", "T2",
            "--file", str(f), "--evidence-kind", "file_diff",
        ])
    _, kwargs = post.call_args
    assert kwargs["json"]["output"] == "--- a\n+++ b\n"
    assert kwargs["json"]["evidence_kind"] == "file_diff"


def test_assist_submit_requires_evidence_unless_kind_none(runner):
    with patch("scaffold_cli.main.Client"):
        res = runner.invoke(cli, ["assist", "submit", "sess-1", "T2"])
    assert res.exit_code != 0
    assert "evidence is required" in res.output


def test_assist_skip_posts_skip_action(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = {"status": "skipped"}
        res = runner.invoke(cli, ["assist", "skip", "sess-1", "T2"])
    assert res.exit_code == 0
    args, kwargs = post.call_args
    assert args[0] == "/assist/sess-1/submit"
    assert kwargs["json"]["action"] == "skip"
    assert kwargs["json"]["evidence_kind"] == "none"


def test_assist_pause_and_resume(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = {}
        runner.invoke(cli, ["assist", "pause", "sess-1"])
        assert post.call_args.args[0] == "/assist/sess-1/pause"
        runner.invoke(cli, ["assist", "resume", "sess-1"])
        assert post.call_args.args[0] == "/assist/sess-1/resume"


def test_assist_abandon_requires_confirmation_without_yes(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        delete = ClientCls.return_value.__enter__.return_value.delete
        # User declines the prompt — input='n\n'
        res = runner.invoke(cli, ["assist", "abandon", "sess-1"], input="n\n")
    assert res.exit_code != 0
    delete.assert_not_called()


def test_assist_abandon_with_yes_skips_prompt(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        delete = ClientCls.return_value.__enter__.return_value.delete
        delete.return_value = {"abandoned": True}
        res = runner.invoke(cli, ["assist", "abandon", "sess-1", "--yes"])
    assert res.exit_code == 0
    assert delete.call_args.args[0] == "/assist/sess-1"


def test_assist_friction_add_records_note(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = {"recorded": True}
        res = runner.invoke(cli, [
            "assist", "friction", "add", "sess-1", "T2", "took", "3", "tries",
        ])
    assert res.exit_code == 0
    args, kwargs = post.call_args
    assert args[0] == "/assist/sess-1/friction"
    assert kwargs["json"] == {"node_key": "T2", "note": "took 3 tries"}


def test_assist_friction_list_renders_notes(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = {
            "session_id": "sess-1",
            "friction": [
                {"node_key": "T1", "created_at": "2026-05-07", "note": "docs lied"},
                {"node_key": "T2", "created_at": "2026-05-07", "note": "took 3 tries"},
            ],
        }
        res = runner.invoke(cli, ["assist", "friction", "list", "sess-1"])
    assert res.exit_code == 0
    assert "docs lied" in res.output
    assert "took 3 tries" in res.output


def test_assist_friction_list_empty_state(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = {
            "session_id": "sess-1", "friction": [],
        }
        res = runner.invoke(cli, ["assist", "friction", "list", "sess-1"])
    assert res.exit_code == 0
    assert "(no friction notes)" in res.output


# ---------------------------------------------------------------------------
# U.8.B verbs: logs, exec retry, status, dag, jobs cleanup, rag dedup,
# rag query/default invocation
# ---------------------------------------------------------------------------


def test_logs_renders_per_node_table(runner):
    """/logs/{id} returns per-node DAG state, not a line-by-line stream."""
    response = {
        "job_id": "abc-123",
        "job_status": "completed",
        "node_count": 2,
        "nodes": [
            {"node_key": "T1", "status": "done", "confidence": 0.92,
             "tool": "LLM", "output_preview": "Plan: refactor modules A, B, C"},
            {"node_key": "T2", "status": "failed", "confidence": 0.41,
             "tool": "LLM", "output_preview": "",
             "failure_reason": "verifier rejected — too vague"},
        ],
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        get = ClientCls.return_value.__enter__.return_value.get
        get.return_value = response
        res = runner.invoke(cli, ["logs", "abc-123"])
    assert res.exit_code == 0
    assert "T1" in res.output and "T2" in res.output
    assert "Plan: refactor" in res.output
    # §17.445 (A1) — failed node's reason now renders on a "why" line.
    assert "verifier rejected" in res.output
    args, kwargs = get.call_args
    assert args[0] == "/logs/abc-123"
    assert kwargs["params"] == {"limit": 50, "offset": 0}


def test_logs_handles_empty_nodes(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = {
            "job_id": "abc", "job_status": "awaiting_confirmation",
            "node_count": 0, "nodes": [],
        }
        res = runner.invoke(cli, ["logs", "abc"])
    assert res.exit_code == 0
    assert "(no DAG nodes" in res.output


def test_logs_passes_include_flags(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        get = ClientCls.return_value.__enter__.return_value.get
        get.return_value = {"job_id": "abc", "job_status": "completed",
                            "node_count": 0, "nodes": []}
        runner.invoke(cli, ["logs", "abc", "--include-output", "--include-compiled"])
    _, kwargs = get.call_args
    assert kwargs["params"]["include_output"] is True
    assert kwargs["params"]["include_compiled"] is True


def test_exec_retry_posts_payload(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = {"status": "running"}
        res = runner.invoke(cli, ["exec", "retry", "abc-123", "T2"])
    assert res.exit_code == 0
    args, kwargs = post.call_args
    assert args[0] == "/exec/retry"
    assert kwargs["json"] == {"job_id": "abc-123", "node_key": "T2"}
    assert "retried T2" in res.output
    assert "scaffold jobs status abc-123" in res.output


def test_status_renders_counts_and_jobs(runner):
    """Mirrors the live /status response shape: `recent_jobs`, not `jobs`."""
    response = {
        "status_counts": {"completed": 3, "running": 1, "blocked": 2},
        "recent_jobs": [
            {"id": "abc-123", "status": "blocked", "title": "Stuck on T2"},
            {"id": "def-456", "status": "completed", "title": "Markdown linter"},
        ],
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        get = ClientCls.return_value.__enter__.return_value.get
        get.return_value = response
        res = runner.invoke(cli, ["status"])
    assert res.exit_code == 0
    assert "completed" in res.output
    assert "blocked" in res.output
    assert "Stuck on T2" in res.output
    assert "Markdown linter" in res.output
    args, kwargs = get.call_args
    assert args[0] == "/status"


def test_status_filter_passes_through(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        get = ClientCls.return_value.__enter__.return_value.get
        get.return_value = {"status_counts": {}, "jobs": []}
        runner.invoke(cli, ["status", "--filter", "blocked", "--limit", "10"])
    _, kwargs = get.call_args
    assert kwargs["params"]["status_filter"] == "blocked"
    assert kwargs["params"]["limit"] == 10


def test_dag_renders_table(runner):
    response = {
        "job_id": "abc-123",
        "job_status": "running",
        "nodes": [
            {"node_key": "T1", "status": "done", "depends_on": [],
             "assigned_model": "qwen3:7b", "title": "Plan"},
            {"node_key": "T2", "status": "pending", "depends_on": ["T1"],
             "assigned_model": "qwen3:7b", "title": "Build"},
        ],
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        get = ClientCls.return_value.__enter__.return_value.get
        get.return_value = response
        res = runner.invoke(cli, ["dag", "abc-123"])
    assert res.exit_code == 0
    assert "T1" in res.output
    assert "T2" in res.output
    assert "qwen3:7b" in res.output
    args, _ = get.call_args
    assert args[0] == "/dag/abc-123"


def test_dag_mermaid_emits_block(runner):
    response = {
        "job_id": "abc-123", "job_status": "running",
        "nodes": [
            {"node_key": "T1", "status": "done", "depends_on": [], "title": "A"},
            {"node_key": "T2", "status": "done", "depends_on": ["T1"], "title": "B"},
        ],
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = response
        res = runner.invoke(cli, ["dag", "abc-123", "--mermaid"])
    assert res.exit_code == 0
    assert "```mermaid" in res.output
    assert "graph TD" in res.output
    assert "T1 --> T2" in res.output


def test_jobs_cleanup_with_yes(runner):
    response = {"reaped_running_to_cancelled": 2, "reaped_orphans_reset": 1}
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = response
        res = runner.invoke(cli, ["jobs", "cleanup", "--yes"])
    assert res.exit_code == 0
    args, kwargs = post.call_args
    assert args[0] == "/jobs/cleanup"
    assert "reaped_running_to_cancelled: 2" in res.output


def test_jobs_cleanup_prompts_without_yes(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        res = runner.invoke(cli, ["jobs", "cleanup"], input="n\n")
    assert res.exit_code != 0
    post.assert_not_called()


def test_rag_dedup_renders_table(runner):
    """Mirrors live response shape: action_taken / similarity_score / existing_entry_id."""
    response = {"entries": [
        {"id": 1, "action_taken": "rejected", "similarity_score": 0.961,
         "existing_entry_id": "scaffold-hid-foo-abc123"},
        {"id": 2, "action_taken": "superseded", "similarity_score": 0.923,
         "existing_entry_id": "scaffold-hid-bar-def456"},
    ], "total": 2}
    with patch("scaffold_cli.main.Client") as ClientCls:
        get = ClientCls.return_value.__enter__.return_value.get
        get.return_value = response
        res = runner.invoke(cli, ["rag", "dedup"])
    assert res.exit_code == 0
    assert "rejected" in res.output
    assert "superseded" in res.output
    assert "0.961" in res.output
    assert "scaffold-hid-foo-abc123" in res.output
    args, kwargs = get.call_args
    assert args[0] == "/rag/dedup"
    assert kwargs["params"] == {"limit": 50, "offset": 0}


def test_rag_query_explicit_subcommand_works(runner):
    """`scaffold rag query <q>` should hit /rag with the query."""
    response = {"results": [{"score": 0.9, "domain": "rag", "text": "Milvus index ..."}]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = response
        res = runner.invoke(cli, ["rag", "query", "milvus", "index"])
    assert res.exit_code == 0
    args, kwargs = post.call_args
    assert args[0] == "/rag"
    assert kwargs["json"]["query"] == "milvus index"


def test_rag_default_invocation_backwards_compatible(runner):
    """`scaffold rag <q>` (no subcommand) must still query — regression guard
    for the U.8.B group conversion."""
    response = {"results": [{"score": 0.9, "domain": "rag", "text": "..."}]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = response
        res = runner.invoke(cli, ["rag", "milvus", "index"])
    assert res.exit_code == 0
    args, kwargs = post.call_args
    assert args[0] == "/rag"
    assert kwargs["json"]["query"] == "milvus index"


# ---------------------------------------------------------------------------
# U.8.E — prompts + gt CLI groups
# ---------------------------------------------------------------------------


def test_prompts_list_renders_table(runner):
    response = {"job_id": "abc", "node_count": 2, "nodes": [
        {"node_key": "T1", "revision": 1, "prompt": "Plan the refactor"},
        {"node_key": "T2", "revision": 3, "prompt": "Implement it"},
    ]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        get = ClientCls.return_value.__enter__.return_value.get
        get.return_value = response
        res = runner.invoke(cli, ["prompts", "list", "abc"])
    assert res.exit_code == 0
    assert "T1" in res.output and "T2" in res.output
    assert "Plan the refactor" in res.output
    args, _ = get.call_args
    assert args[0] == "/prompts/abc"


def test_prompts_list_handles_empty(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = {
            "job_id": "abc", "node_count": 0, "nodes": [],
        }
        res = runner.invoke(cli, ["prompts", "list", "abc"])
    assert res.exit_code == 0
    assert "(no nodes" in res.output


def test_prompts_get_prints_full_prompt(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = {
            "job_id": "abc", "node_key": "T2", "revision": 3,
            "prompt": "Multi-line\nprompt body",
        }
        res = runner.invoke(cli, ["prompts", "get", "abc", "T2"])
    assert res.exit_code == 0
    assert "Multi-line" in res.output
    assert "prompt body" in res.output
    assert "revision: 3" in res.output


def test_prompts_history_renders_revisions(runner):
    response = {"revisions": [
        {"revision": 1, "created_at": "2026-05-01T10:00", "prompt": "v1"},
        {"revision": 2, "created_at": "2026-05-02T10:00", "prompt": "v2"},
    ]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        get = ClientCls.return_value.__enter__.return_value.get
        get.return_value = response
        res = runner.invoke(cli, ["prompts", "history", "abc", "T2"])
    assert res.exit_code == 0
    assert "2 revision" in res.output
    args, _ = get.call_args
    assert args[0] == "/prompts/abc/T2/history"


def test_prompts_update_reads_file_and_posts(runner, tmp_path):
    f = tmp_path / "new_prompt.txt"
    f.write_text("Refactored prompt\nwith two lines\n")
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = {"revision": 4}
        res = runner.invoke(cli, [
            "prompts", "update", "abc", "T2", "--file", str(f),
        ])
    assert res.exit_code == 0
    args, kwargs = post.call_args
    assert args[0] == "/prompts/abc/T2"
    assert kwargs["json"]["prompt"].startswith("Refactored prompt")
    assert "new revision: 4" in res.output


def test_prompts_update_rejects_empty_text(runner, tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("   \n")
    with patch("scaffold_cli.main.Client"):
        res = runner.invoke(cli, [
            "prompts", "update", "abc", "T2", "--file", str(f),
        ])
    assert res.exit_code != 0
    assert "empty" in res.output.lower()


# gt group ----------------------------------------------------------------


def test_gt_stats_renders_summary(runner):
    response = {"total_entries": 1093, "domains": {"llm": 558, "eng": 348},
                "source_types": {"official_docs": 253, "blog": 63}}
    with patch("scaffold_cli.main.Client") as ClientCls:
        get = ClientCls.return_value.__enter__.return_value.get
        get.return_value = response
        res = runner.invoke(cli, ["gt", "stats"])
    assert res.exit_code == 0
    assert "1093 total" in res.output
    assert "llm" in res.output and "official_docs" in res.output
    args, _ = get.call_args
    assert args[0] == "/gt/stats"


def test_gt_list_renders_entries(runner):
    response = {"page": 1, "per_page": 20, "total": 2, "total_pages": 1,
                "entries": [
                    {"entry_id": "scaffold-foo-abc", "domain": "rag",
                     "confidence": 0.91, "title": "Hybrid retrieval"},
                    {"entry_id": "scaffold-bar-def", "domain": "rag",
                     "confidence": 0.85, "title": "Reranker"},
                ]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        get = ClientCls.return_value.__enter__.return_value.get
        get.return_value = response
        res = runner.invoke(cli, ["gt", "list", "--domain", "rag", "--per-page", "20"])
    assert res.exit_code == 0
    assert "Hybrid retrieval" in res.output
    args, kwargs = get.call_args
    assert args[0] == "/gt/list"
    assert kwargs["params"]["domain"] == "rag"
    assert kwargs["params"]["per_page"] == 20


def test_gt_search_posts_query_and_top_k(runner):
    response = {"results": [
        {"score": 0.93, "entry_id": "scaffold-xyz", "title": "TOON spec",
         "snippet": "Token-Oriented Object Notation..."},
    ]}
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = response
        res = runner.invoke(cli, ["gt", "search", "TOON", "format", "--top-k", "5"])
    assert res.exit_code == 0
    assert "TOON spec" in res.output
    args, kwargs = post.call_args
    assert args[0] == "/gt/search"
    assert kwargs["json"]["query"] == "TOON format"
    assert kwargs["json"]["top_k"] == 5


def test_gt_detail_prints_full_entry(runner):
    response = {"entry_id": "scaffold-foo", "title": "X", "domain": "rag",
                "confidence": 0.9, "content": "Long body of content here"}
    with patch("scaffold_cli.main.Client") as ClientCls:
        get = ClientCls.return_value.__enter__.return_value.get
        get.return_value = response
        res = runner.invoke(cli, ["gt", "detail", "scaffold-foo"])
    assert res.exit_code == 0
    assert "scaffold-foo" in res.output
    assert "Long body of content" in res.output
    args, _ = get.call_args
    assert args[0] == "/gt/detail/scaffold-foo"


def test_gt_extract_posts_topic_and_queries(runner):
    response = {"extracted": 7, "ingested": 6, "rejected": 1,
                "target_file": "ground_truths/k8s.toon"}
    with patch("scaffold_cli.main.Client") as ClientCls:
        post = ClientCls.return_value.__enter__.return_value.post
        post.return_value = response
        res = runner.invoke(cli, [
            "gt", "extract", "kubernetes",
            "--query", "lifecycle hooks",
            "--query", "init containers",
        ])
    assert res.exit_code == 0
    args, kwargs = post.call_args
    assert args[0] == "/gt"
    assert kwargs["json"]["topic"] == "kubernetes"
    assert kwargs["json"]["queries"] == ["lifecycle hooks", "init containers"]
    assert "extracted 7" in res.output


# ---------------------------------------------------------------------------
# §17.88 — `scaffold errors resolve` CLI verb
# ---------------------------------------------------------------------------


def test_errors_resolve_default_marks_resolved(runner):
    """Default invocation PATCHes with ``resolved=true`` and no note."""
    eid = "11111111-2222-3333-4444-555555555555"
    response = {
        "error_id": eid, "resolved": True,
        "resolution": None, "resolved_at": "2026-05-10T12:00:00",
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        patch_mock = ClientCls.return_value.__enter__.return_value.patch
        patch_mock.return_value = response
        res = runner.invoke(cli, ["errors", "resolve", eid])
    assert res.exit_code == 0
    args, kwargs = patch_mock.call_args
    assert args[0] == f"/observability/errors/{eid}"
    assert kwargs["json"] == {"resolved": True, "resolution": None}
    assert "resolved" in res.output
    assert "11111111" in res.output


def test_errors_resolve_with_note_forwards_resolution(runner):
    """``--note`` flows through to the body's ``resolution`` field."""
    eid = "11111111-2222-3333-4444-555555555555"
    response = {
        "error_id": eid, "resolved": True,
        "resolution": "fixed by §17.86 timeout bump",
        "resolved_at": "2026-05-10T12:00:00",
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        patch_mock = ClientCls.return_value.__enter__.return_value.patch
        patch_mock.return_value = response
        res = runner.invoke(cli, [
            "errors", "resolve", eid, "--note", "fixed by §17.86 timeout bump",
        ])
    assert res.exit_code == 0
    _, kwargs = patch_mock.call_args
    assert kwargs["json"] == {
        "resolved": True, "resolution": "fixed by §17.86 timeout bump",
    }
    assert "fixed by §17.86 timeout bump" in res.output


def test_errors_resolve_unresolve_flag_reopens(runner):
    """``--unresolve`` sends ``resolved=false`` and reports un-resolved."""
    eid = "11111111-2222-3333-4444-555555555555"
    response = {
        "error_id": eid, "resolved": False,
        "resolution": None, "resolved_at": None,
    }
    with patch("scaffold_cli.main.Client") as ClientCls:
        patch_mock = ClientCls.return_value.__enter__.return_value.patch
        patch_mock.return_value = response
        res = runner.invoke(cli, ["errors", "resolve", eid, "--unresolve"])
    assert res.exit_code == 0
    _, kwargs = patch_mock.call_args
    assert kwargs["json"] == {"resolved": False, "resolution": None}
    assert "un-resolved" in res.output


def test_errors_resolve_orchestrator_404_exits_nonzero(runner):
    """A 404 from the endpoint surfaces as a CLIError + non-zero exit."""
    eid = "00000000-0000-0000-0000-000000000000"
    with patch("scaffold_cli.main.Client") as ClientCls:
        patch_mock = ClientCls.return_value.__enter__.return_value.patch
        patch_mock.side_effect = CLIError(
            f"Resource not found (404): error_log not found: {eid}"
        )
        res = runner.invoke(cli, ["errors", "resolve", eid])
    assert res.exit_code == 1
    assert "(404)" in res.output


def test_errors_list_renders_rows(runner):
    """§17.446 (B4) — `scaffold errors list` tables the error_logs rows."""
    rows = [
        {"id": "e1111111-aaaa", "error_type": "RecursionError", "job_id": "j1234567-bb",
         "error_message": "maximum recursion depth", "resolved": False},
        {"id": "e2222222-cccc", "error_type": "TimeoutError", "job_id": None,
         "error_message": "upstream timed out", "resolved": True},
    ]
    with patch("scaffold_cli.main.Client") as ClientCls:
        get = ClientCls.return_value.__enter__.return_value.get
        get.return_value = rows
        res = runner.invoke(cli, ["errors", "list", "--unresolved"])
    assert res.exit_code == 0
    assert "RecursionError" in res.output and "maximum recursion" in res.output
    args, kwargs = get.call_args
    assert args[0] == "/observability/errors"
    assert kwargs["params"].get("resolved") == "false"


def test_errors_list_empty(runner):
    with patch("scaffold_cli.main.Client") as ClientCls:
        ClientCls.return_value.__enter__.return_value.get.return_value = []
        res = runner.invoke(cli, ["errors", "list"])
    assert res.exit_code == 0
    assert "no error_logs rows" in res.output


def test_alerts_list_renders_rows(runner):
    """§17.446 (B4) — `scaffold alerts list` tables system_alerts rows."""
    rows = [
        {"id": "a1111111-aaaa", "severity": "warning", "kind": "oncall.errors_unresolved",
         "message": "2 unresolved error_logs rows"},
    ]
    with patch("scaffold_cli.main.Client") as ClientCls:
        get = ClientCls.return_value.__enter__.return_value.get
        get.return_value = rows
        res = runner.invoke(cli, ["alerts", "list", "--kind", "oncall.errors_unresolved"])
    assert res.exit_code == 0
    assert "oncall.errors_unresolved" in res.output and "unresolved error_logs" in res.output
    args, kwargs = get.call_args
    assert args[0] == "/observability/alerts"
    assert kwargs["params"].get("kind") == "oncall.errors_unresolved"
