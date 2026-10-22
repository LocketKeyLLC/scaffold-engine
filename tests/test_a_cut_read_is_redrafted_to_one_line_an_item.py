"""§17.1745 — a read whose last run was CUT is not offered again printing a block for every item.

Live (job e5e22b92 PR6 "Read the … pack grabs in the queue", 2026-10-22): the first run printed 471 lines (a season pack
is one Sonarr queue record per episode) and §17.1691 failed it, telling the next draft to print one short line per item.
§17.1260/1366 carried that reason into the redraft, and the redraft printed nine lines for each matching record again --
the same cut, framed `refused: []`. Titles here are made up.
"""
from __future__ import annotations

from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

# the live redraft's program, the parts that matter
BLOCK_PER_ITEM = """import urllib.request, json
items = json.load(urllib.request.urlopen('http://localhost:8989/api/v3/queue'))['records']
mm = [r for r in items if 'Moon Patrol' in r.get('title', '')]
print(f"Total queue records: {len(items)}")
print(f"Moon Patrol items: {len(mm)}")
print()
for r in mm:
    title = r.get('title', '')[:90]
    episodes = r.get('episodes', [])
    print(f"Title: {title}")
    print(f"  Status: {r.get('status', '')}")
    print(f"  TrackedDownloadState: {r.get('trackedDownloadState', '')}")
    print(f"  TrackedDownloadStatus: {r.get('trackedDownloadStatus', '')}")
    print(f"  StatusMessages: {r.get('statusMessages', [])}")
    print(f"  SizeLeft: {r.get('sizeleft', 0)}")
    print(f"  Episodes mapped: {len(episodes)}")
    print(f"  Seasons: {sorted(set(e.get('seasonNumber') for e in episodes))}")
    print()
"""
ONE_LINE_PER_GROUP = """import urllib.request, json, collections
items = json.load(urllib.request.urlopen('http://localhost:8989/api/v3/queue'))['records']
groups = collections.Counter((r.get('title', '')[:90], r.get('status', '')) for r in items if 'Moon Patrol' in r.get('title', ''))
print(f"records={len(items)} groups={len(groups)}")
for (title, status), n in sorted(groups.items()):
    if status == 'warning':
        print(f"WARN {n:3d} {status} {title}")
    else:
        print(f"{n:3d} {status} {title}")
"""
CUT = ("supervised run stopped — this read printed 471 lines inside the guest and the record keeps the last 300, so what "
       "it saw is mostly not in it -- the step is not done. Print what the step needs, not the raw response: counts, and "
       "one short line per item with only the fields that matter (a `python3 - <<'PY'` program that makes the call and "
       "prints a summary).")
NODE = {"node_key": "PR6", "title": "Read the Moon Patrol pack grabs in the queue",
        "description": "Read-only, change nothing. Inside Sonarr's container (CT 104): print the matching queue records.",
        "last_verification_reason": CUT}
POLICY = {"allow": ["ANY"], "sudo": True, "helper": "24", "secrets": [], "can_write_files": True}


def _cmd(prog: str) -> str:
    return "pct exec 104 -- python3 - <<'PY'\n" + prog + "PY"


def test_both_cut_reasons_carry_the_marker_the_redraft_is_judged_by():
    lines = sr.a_read_was_cut({"node_key": "X", "title": "List the queue", "description": "Read-only, change nothing."},
                              [{"command": "bash x", "output": "[CUT: the step printed 471 lines; only the last 300 are kept]"}])
    chars = sr.a_read_was_cut({"node_key": "X", "title": "List the queue", "description": "Read-only, change nothing."},
                              [{"command": "bash x", "output": "x" * 20_000}])
    assert sr.READ_CUT_REMEDY in lines and sr.READ_CUT_REMEDY in chars


def test_the_live_redraft_is_refused():
    hit = sr.a_cut_read_still_prints_a_block_per_item(NODE, [_cmd(BLOCK_PER_ITEM)])
    assert len(hit) == 1 and "prints 9 lines for every item" in hit[0]["why"], hit
    assert hit[0]["command"] == "for r in mm:"


def test_one_line_per_group_passes_and_a_branch_counts_once():
    assert sr.a_cut_read_still_prints_a_block_per_item(NODE, [_cmd(ONE_LINE_PER_GROUP)]) == []


def test_a_step_that_was_never_cut_may_print_a_block_per_item():
    assert sr.a_cut_read_still_prints_a_block_per_item(dict(NODE, last_verification_reason=""), [_cmd(BLOCK_PER_ITEM)]) == []
    other = dict(NODE, last_verification_reason="supervised run stopped — `bash x` exited 1: HTTP Error 400")
    assert sr.a_cut_read_still_prints_a_block_per_item(other, [_cmd(BLOCK_PER_ITEM)]) == []


def test_a_loop_nested_in_the_item_loop_is_more_than_one_line():
    prog = "for r in rs:\n    for e in r['episodes']:\n        print(e)\n"
    assert sr.a_cut_read_still_prints_a_block_per_item(NODE, [_cmd(prog)])


def test_the_engine_template_frame_refuses_it_and_the_chain_redrafts():
    """The production path: the run_in_container template the live draft came from, judged by frame_run."""
    def framed(prog: str) -> dict:
        rb = rt.render(rt.RUN_IN_CONTAINER, rt.values_for(rt.RUN_IN_CONTAINER, NODE, None, {},
                                                          {"REMOTE_COMMANDS": "python3 - <<'PY'\n" + prog + "PY",
                                                           "VERIFY_INSIDE": "true"}))
        spec = type("S", (), {"name": "pve-runner", "headers": {}})()
        return sr.frame_run(NODE, rb, spec, POLICY, env={"profile": "root@pve"})
    frame = framed(BLOCK_PER_ITEM)
    hits = [r for r in frame["refused"] if "lines for every item" in r["why"]]
    assert len(hits) == 1, [r["why"][:90] for r in frame["refused"]]
    assert "run" not in {o["id"] for o in frame["options"]}
    assert sr.shape_retry_note({"refused": hits, "commands": frame["commands"]}), "registered: the chain redrafts"
    assert sr.whose_gap(hits[0]["why"]) == "drafter"
    assert not [r for r in framed(ONE_LINE_PER_GROUP)["refused"] if "lines for every item" in r["why"]]
