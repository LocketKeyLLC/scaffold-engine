"""§17.1680 — a template block the engine marked read-only is its own evidence (live, job c2518102 T2 "Fetch Sonarr wanted
missing": one read of Sonarr's missing list was refused "this block has no check at all")."""
from unittest.mock import MagicMock

from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

READ = ('curl -s -H "X-Api-Key: $(sed -n "s:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p" /var/lib/sonarr/config.xml)" '
        '"http://127.0.0.1:8989/api/v3/wanted/missing?page=1&pageSize=100" | jq ".totalRecords"')
WRITE = "sed -i 's/^Port=.*/Port=9999/' /var/lib/sonarr/config.xml"


def _frame(remote, title):
    vals = {"GID": "104", "STEP": "T2", "REMOTE_COMMANDS": remote, "VERIFY_INSIDE": "", "GUEST_USER_NAME": "GUEST_USER"}
    vals["STRICT"] = rt.strict_line(remote)
    rb = rt.render(rt.RUN_IN_CONTAINER, vals)
    rb = rb.replace('## Verify\n\n```bash\npct exec 104 -- bash -c ""\n```\n', "")
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": [], "can_write_files": True}
    return sr.frame_run({"node_key": "T2", "title": title, "tool": "Shell", "prompt_template": "Read only."}, rb, spec,
                        policy, services=[], inventory={"vms": {}, "cts": {"104": "running"}, "names": {"104": "sonarr"}})


def test_a_read_only_block_needs_no_separate_check():
    assert sr.READS_ONLY_MARKER in rt.strict_line(READ)
    f = _frame(READ, "Fetch Sonarr wanted missing")
    assert not [r for r in f["refused"] if "no check at all" in r["why"]], f["refused"]


def test_a_block_that_writes_still_needs_one():
    assert sr.READS_ONLY_MARKER not in rt.strict_line(WRITE)
    f = _frame(WRITE, "Change Sonarr's port")
    assert [r for r in f["refused"] if "no check at all" in r["why"]], f["refused"]
