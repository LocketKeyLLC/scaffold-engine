"""§17.1960 — a written file whose folder the step itself creates cannot land (files are written before commands)."""
from app.modules.supervised_runs import a_file_in_a_folder_the_step_makes


def test_the_live_t15_write_is_refused():
    cmds = ["mkdir -p /opt/playwright-toolchain/panel-verify/shots", "cd /opt/playwright-toolchain/panel-verify && node sections.mjs"]
    files = [{"path": "/opt/playwright-toolchain/panel-verify/sections.mjs", "content": "x"}]
    hits = a_file_in_a_folder_the_step_makes(cmds, files)
    assert len(hits) == 1 and "install -D" in hits[0]["why"]


def test_a_folder_made_by_install_d_counts_too():
    hits = a_file_in_a_folder_the_step_makes(["install -d -m 0700 /tmp/panel-verify"],
                                             [{"path": "/tmp/panel-verify/routes.sh", "content": "x"}])
    assert hits


def test_a_file_in_an_existing_folder_passes():
    assert a_file_in_a_folder_the_step_makes(["mkdir -p /opt/x/out", "python3 /tmp/run.py"],
                                             [{"path": "/tmp/run.py", "content": "x"}]) == []
    assert a_file_in_a_folder_the_step_makes([], [{"path": "/opt/x/run.py", "content": "x"}]) == []


def test_the_frame_gate_runs_it():
    import inspect
    from app.modules import supervised_runs
    assert "a_file_in_a_folder_the_step_makes(cmds, shape_files)" in inspect.getsource(supervised_runs)


def test_a_standing_folder_is_never_judged_made():
    assert a_file_in_a_folder_the_step_makes(["mkdir -p /tmp/panel-verify/shots"],
                                             [{"path": "/tmp/sections.mjs", "content": "x"}]) == []


def test_these_refusals_reach_a_redraft():
    # §17.1269 — a refusal shape_retry_note cannot recognise is a dead end; the alias-imported §17.1958 producer is
    # invisible to the AST registry test, so it is checked here by its output
    from app.modules import supervised_runs as sr
    from app.modules.prints_a_secret import prints_a_units_environment
    hits = (a_file_in_a_folder_the_step_makes(["mkdir -p /opt/a/b"], [{"path": "/opt/a/b/x.py", "content": "x"}])
            + prints_a_units_environment(["systemctl cat x.service", "systemctl show x.service"], []))
    assert len(hits) == 3
    for h in hits:
        assert sr.shape_retry_note({"kind": "run", "refused": [h]}), h["why"][:80]
