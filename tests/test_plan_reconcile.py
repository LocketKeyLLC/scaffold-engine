"""§17.1043 — plan reconciliation, fix-confirmed trigger (app/modules/plan_reconcile.py).

The homelab session's shape: the operator pasted a failure carrying the wrong
path, the fix reply named the right one, the operator's next paste showed it
working and the step was committed — and every later step still carried the
old path. These pin the deterministic correction rules, the pending-only
application, the operator-facing note, and the wiring into the commit path.
"""
from __future__ import annotations

import pathlib

import pytest

import app.modules.execution_agent  # noqa: F401 — load-bearing (real app.database)
from app.modules import plan_reconcile as pr

pytestmark = pytest.mark.asyncio

FAILING = ["root@pve:~# pct exec 111 -- bash -c 'cd /opt//opt/control-panel && node server.js'\n"
           "bash: line 1: cd: /opt//opt/control-panel: No such file or directory"]
FIX = ["## Diagnosis\nThe path is doubled. The backend lives in `/opt/control-panel-backend`.\n"
       "```bash\npct exec 111 -- bash -c 'cd /opt/control-panel-backend && nohup node server.js &'\n```"]
CLOSING = ("root@pve:~# pct exec 111 -- ss -tlnp | grep 3001\n"
           "LISTEN 0 511 0.0.0.0:3001 users:((\"node\",pid=812))\n"
           "cd /opt/control-panel-backend worked")


def test_values_in_sees_paths_including_the_doubled_separator():
    kinds = {(v["kind"], v["value"]) for v in pr.values_in(FAILING[0])}
    assert ("path", "/opt//opt/control-panel") in kinds


def test_the_live_case_yields_one_path_correction():
    corr, declined = pr.derive_corrections(
        failing_pastes=FAILING, fix_replies=FIX, closing_paste=CLOSING,
        plan_text="Later step: cd /opt//opt/control-panel and restart")
    assert corr == [{"kind": "path", "old": "/opt//opt/control-panel", "new": "/opt/control-panel-backend"}]
    assert declined == []


def test_a_new_value_the_operator_never_confirmed_is_not_applied():
    corr, _ = pr.derive_corrections(failing_pastes=FAILING, fix_replies=FIX,
                                    closing_paste="ok done", plan_text="")
    assert corr == []


def test_an_old_value_still_present_in_the_closing_paste_is_not_superseded():
    corr, _ = pr.derive_corrections(
        failing_pastes=FAILING, fix_replies=FIX,
        closing_paste=CLOSING + "\nstill also using /opt//opt/control-panel for logs", plan_text="")
    assert corr == []


def test_two_candidates_of_one_kind_are_declined_not_guessed():
    fix = ["Use port 3001 or, if busy, port 3002."]
    corr, declined = pr.derive_corrections(
        failing_pastes=["curl: (7) Failed to connect to 127.0.0.1 port 8080"], fix_replies=fix,
        closing_paste="listening on port 3001 and port 3002", plan_text="")
    assert corr == [] and declined and declined[0]["kind"] == "port"


def test_a_port_correction_from_the_ledger_fact_counts_as_confirmed():
    corr, _ = pr.derive_corrections(
        failing_pastes=["connect to 127.0.0.1 port 8080: refused"], fix_replies=["Bind to port 3001 instead."],
        closing_paste="ok", plan_text="", confirmed_facts="control panel listens on port 3001")
    assert corr == [{"kind": "port", "old": "8080", "new": "3001"}]


def test_parent_directories_do_not_count_as_separate_path_candidates():
    """Live (scratch session f745ee6a): the fix named `/etc/nginx/conf.d/x.conf`
    and the operator listed `/etc/nginx/`; the parent directory must not make
    the pair ambiguous."""
    failing = ["ls: cannot access '/etc/nginx/sites-available/': No such file\nroot@u:~# ls /etc/nginx/\nconf.d nginx.conf"]
    fix = ["Create it at `/etc/nginx/conf.d/status.hamlet-labs.net.conf` (the include is /etc/nginx/conf.d/*.conf)."]
    closing = "root@u:~# ls -l /etc/nginx/conf.d/status.hamlet-labs.net.conf\n-rw-r--r-- 1 root root 210 status.hamlet-labs.net.conf\nOK"
    corr, declined = pr.derive_corrections(failing_pastes=failing, fix_replies=fix, closing_paste=closing,
                                           plan_text="Create /etc/nginx/sites-available/status.hamlet-labs.net")
    assert declined == []
    assert corr == [{"kind": "path", "old": "/etc/nginx/sites-available",
                     "new": "/etc/nginx/conf.d/status.hamlet-labs.net.conf"}]


def test_plan_changes_touch_pending_nodes_only_and_flag_stale_walkthroughs():
    corr = [{"kind": "path", "old": "/opt//opt/control-panel", "new": "/opt/control-panel-backend"}]
    nodes = [
        {"node_key": "T36", "status": "done", "prompt_template": "cd /opt//opt/control-panel (done)"},
        {"node_key": "T37", "status": "done", "prompt_template": "the step just committed"},
        {"node_key": "T38", "status": "pending", "prompt_template": "Restart from /opt//opt/control-panel"},
        {"node_key": "T39", "status": "pending", "prompt_template": "Document the build"},
    ]
    steps = [
        {"node_key": "T38", "status": "pending", "guidance": "cd /opt//opt/control-panel && …"},
        {"node_key": "T39", "status": "pending", "guidance": None},
        {"node_key": "T36", "status": "committed", "guidance": "cd /opt//opt/control-panel"},
    ]
    out = pr.plan_changes(nodes, steps, corr, source_node_key="T37")
    assert [u["node_key"] for u in out["node_updates"]] == ["T38"]
    t38 = out["node_updates"][0]["prompt_template"]
    assert t38.startswith("Restart from /opt/control-panel-backend") and pr.PROVENANCE_PREFIX + " T37" in t38
    assert out["guidance_resets"] == ["T38"]


def test_plan_changes_are_idempotent_and_do_not_match_inside_longer_values():
    corr = [{"kind": "ip", "old": "192.168.1.2", "new": "192.168.1.25"}]
    nodes = [{"node_key": "T5", "status": "pending", "prompt_template": "curl 192.168.1.25 and 192.168.1.2"}]
    out = pr.plan_changes(nodes, [], corr, source_node_key="T4")
    assert out["node_updates"][0]["prompt_template"].startswith("curl 192.168.1.25 and 192.168.1.25")
    again = pr.plan_changes([{"node_key": "T5", "status": "pending",
                              "prompt_template": out["node_updates"][0]["prompt_template"]}], [], corr,
                            source_node_key="T4")
    assert again["node_updates"] == []


def test_render_note_names_the_change_the_steps_and_the_rewrites():
    note = pr.render_note({
        "source_node_key": "T37",
        "node_updates": [{"node_key": "T38", "corrections": [
            {"kind": "path", "old": "/opt//opt/control-panel", "new": "/opt/control-panel-backend"}]}],
        "guidance_resets": ["T38"],
    })
    assert note.startswith("🔁 **Plan updated after step T37**")
    assert "`/opt//opt/control-panel` → `/opt/control-panel-backend` (path) in T38" in note
    assert "walkthrough for T38 will be rewritten" in note
    assert pr.render_note({}) == "" and pr.render_note({"node_updates": [], "guidance_resets": []}) == ""


async def test_the_trigger_is_off_by_valve_and_silent_without_a_fix(monkeypatch):
    monkeypatch.setattr(pr.settings, "plan_reconcile_enabled", False)
    assert await pr.reconcile_after_commit(db=None, session_id="s", job_id="j", node_key="T1", evidence="x") is None
    monkeypatch.setattr(pr.settings, "plan_reconcile_enabled", True)

    async def no_fix(**kw):
        return None
    monkeypatch.setattr(pr, "_fix_context", no_fix)
    assert await pr.reconcile_after_commit(db=None, session_id="s", job_id="j", node_key="T1", evidence="x") is None


def test_the_commit_path_and_both_surfaces_are_wired():
    root = pathlib.Path(__file__).resolve().parents[1]
    agent = (root / "app/modules/assist_agent.py").read_text()
    body = agent[agent.index("async def submit_step("):agent.index("async def _maybe_replan(")]
    assert "reconcile_after_commit(" in body and '"reconciliation": reconciliation' in body
    assert body.index("# Detect session completion.") < body.index("reconcile_after_commit(")  # runs last
    turn = (root / "app/modules/assist_turn.py").read_text()
    assert turn.count("_reconciliation_note(session_id") >= 2 and '"kind": "note"' in turn
    router = (root / "app/routers/assist.py").read_text()
    assert "reconciliation_note" in router
    spa = (root / "app/ui/static/views/assist.js").read_text()
    assert "res.reconciliation_note" in spa


def test_every_nullable_bound_parameter_in_the_module_is_cast():
    """Live (scratch session f745ee6a): `(:since IS NULL OR created_at >= :since)`
    raised AmbiguousParameterError under asyncpg and the trigger silently did
    nothing on a real commit. Every `:since` / `:e` must be CAST."""
    src = (pathlib.Path(__file__).resolve().parents[1] / "app/modules/plan_reconcile.py").read_text()
    import re as _re
    bare = [m.group(0) for m in _re.finditer(r"(?<!CAST\()(?<!\w):(since|e)\b", src)]
    assert bare == [], bare


# ---- §17.1044 — the decision trigger --------------------------------------

NOTES = ("Options: (1) certbot --nginx (auto plugin) — fastest, but the plugin rewrites the server block; "
         "(2) certbot certonly --webroot — leaves Nginx config untouched, needs a webroot path; "
         "(3) DNS-01 with a Cloudflare token — no port 80 needed. Suggested: (2).")


def test_options_are_parsed_with_labels():
    opts = pr.parse_options(NOTES)
    assert [o["n"] for o in opts] == [1, 2, 3]
    assert opts[0]["label"].startswith("certbot --nginx") and opts[1]["label"].startswith("certbot certonly --webroot")
    assert pr.parse_options("no options here") == [] and pr.parse_options("(1) only one") == []


def test_the_trailing_suggestion_is_not_part_of_the_last_option():
    """Live (T5 of scratch job 9cbfa37d): "… (2) DNS-01 validation: … Suggested:
    certbot --nginx (HTTP-01). Record the operator's choice." made `certbot` and
    `--nginx` look shared, so option 1 had no distinctive terms."""
    notes = ("OPERATOR DECISION — options: (1) certbot --nginx (HTTP-01): Port 80 is reachable; tradeoff: no wildcard. "
             "(2) DNS-01 validation: Port 80 blocked; tradeoff: more setup than HTTP-01. "
             "Suggested: certbot --nginx (HTTP-01). Record the operator's choice.")
    opts = pr.parse_options(notes)
    assert [o["label"] for o in opts] == ["certbot --nginx (HTTP-01)", "DNS-01 validation"]
    assert "certbot" not in opts[1]["text"]
    assert {"certbot", "--nginx"} <= pr.option_terms(opts[0], [opts[1]])


def test_numbered_lines_are_parsed_too():
    opts = pr.parse_options("Choose:\n1. Caddy — automatic HTTPS\n2. Nginx — manual certbot\n")
    assert [(o["n"], o["label"]) for o in opts] == [(1, "Caddy"), (2, "Nginx")]


def test_the_choice_is_read_from_a_number_or_from_distinctive_terms():
    opts = pr.parse_options(NOTES)
    assert pr.choose(opts, "Decision: option 2 — certonly with the webroot at /var/www/html.")["n"] == 2
    assert pr.choose(opts, "We will use the DNS-01 challenge with the Cloudflare token.")["n"] == 3
    assert pr.choose(opts, "Decision: go ahead.") is None


def test_decision_changes_add_the_directive_downstream_and_flag_rejected_option_steps():
    opts = pr.parse_options(NOTES)
    chosen, rejected = opts[1], [opts[0], opts[2]]
    nodes = [
        {"node_key": "T2", "status": "done", "prompt_template": NOTES},
        {"node_key": "T5", "status": "pending", "prompt_template": "Write the HTTP block with the webroot location."},
        {"node_key": "T8", "status": "pending", "prompt_template": "Run certbot --nginx -d status.example.net so the plugin rewrites the server block."},
        {"node_key": "T14", "status": "pending", "prompt_template": "Configure monitors in the UI."},
        {"node_key": "T3", "status": "done", "prompt_template": "certbot --nginx was considered"},
    ]
    steps = [{"node_key": "T8", "status": "pending", "guidance": "sudo certbot --nginx -d … the plugin rewrites"},
             {"node_key": "T5", "status": "pending", "guidance": "tee the server block"}]
    out = pr.decision_changes(nodes, steps, source_node_key="T2", downstream={"T5", "T8"},
                              chosen=chosen, rejected=rejected)
    keys = {u["node_key"]: u for u in out["node_updates"]}
    assert set(keys) == {"T5", "T8"}
    assert keys["T8"]["reason"] == "names a rejected option" and keys["T5"]["reason"] == "depends on the decision"
    assert keys["T5"]["prompt_template"].endswith("do not apply as written.")
    assert "chosen (2) certbot certonly --webroot" in keys["T5"]["prompt_template"]
    assert out["guidance_resets"] == ["T8"]
    # idempotent: a node already carrying the directive is not updated again
    again = pr.decision_changes([dict(nodes[1], prompt_template=keys["T5"]["prompt_template"])], [],
                                source_node_key="T2", downstream={"T5"}, chosen=chosen, rejected=rejected)
    assert again["node_updates"] == []


def test_render_note_for_a_decision_names_choice_flags_and_the_proposal():
    note = pr.render_note({"trigger": "decision", "source_node_key": "T2",
                           "chosen": {"n": 2, "label": "certbot certonly --webroot"},
                           "node_updates": [{"node_key": "T5", "reason": "depends on the decision"},
                                            {"node_key": "T8", "reason": "names a rejected option"}],
                           "guidance_resets": ["T8"], "replan_proposal": {"proposals": [{}]}})
    assert note.startswith("🔁 **Decision at step T2 applied to the plan** — chosen (2) certbot certonly --webroot.")
    assert "T5, T8" in note and "T8 were written for an option you did not choose" in note
    assert "walkthrough for T8 will be rewritten" in note and "plan-change proposal" in note


def test_a_decision_commit_is_dispatched_by_node_type_and_the_turn_loop_forwards_the_proposal():
    root = pathlib.Path(__file__).resolve().parents[1]
    src = (root / "app/modules/plan_reconcile.py").read_text()
    body = src[src.index("async def reconcile_after_commit("):]
    assert 'node_type") or "").strip().lower() == "decision"' in body and "_decision_trigger(" in body
    assert body.index("_decision_trigger(") < body.index("_fix_context(")
    assert "assess_note_impact(" in src  # structural impact goes through surface-and-ask, never applied silently
    turn = (root / "app/modules/assist_turn.py").read_text()
    assert 'rec.get("replan_proposal")' in turn and "ASSIST_REPLAN_PROPOSAL, {\"proposal\": rec[\"replan_proposal\"]}" in turn
