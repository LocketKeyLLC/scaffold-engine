"""§17.2055 — a command the step gives literally is not drafted back with a value turned into a placeholder.

Live (tests/integration/test_auto_mode_live.py, 2 of 14 recorded H1 drafts): H1 says "Run this: `mkdir -p
/tmp/scaffold-auto-itest/stage1`"; the upstream decision D1 chose "The local NVMe pool" (no path). The runbook prompt
said a decision wins where it disagrees with the step's text, so the drafter wrote `mkdir -p
<NVME_POOL_MOUNT>/scaffold-auto-itest/stage1` + a chmod + a findmnt. The gate judged the shape with the dummy `x`
(`mkdir -p x/scaffold-auto-itest/stage1`), refused it as off the write-allow list, and a permission refusal is never
redrafted -- the step's own one-line command was parked.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.modules import prompt_assembly as pa
from app.modules import supervised_runs as sr

MARKER_DIR = "/tmp/scaffold-auto-itest"
H1 = {"node_key": "H1", "title": f"Create the media library staging directory at {MARKER_DIR}/stage1",
      "description": (f"On the host, create the staging directory the library will use.\n\n"
                      f"Run this:\n\n```bash\nmkdir -p {MARKER_DIR}/stage1\n```\n\n"
                      f"Done when `ls -ld {MARKER_DIR}/stage1` prints the directory.")}
H1["prompt_template"] = H1["description"]
MARK = "where this step's own command gives it literally"
POLICY = {"allow": [f"mkdir -p {MARKER_DIR}/", f"tee {MARKER_DIR}/", f"touch {MARKER_DIR}/"], "sudo": False,
          "helper": "23"}

#: the live draft (llm_traces 2800), verbatim
LIVE_2800 = """## Inputs needed

- The mount path of the local NVMe pool (D1 chose it; the NAS is backups only). The step text says `/tmp/scaffold-auto-itest/stage1`, but that is a tmpfs path, not the NVMe pool — I do not have the pool's mount point, so it stays a placeholder below. If you genuinely want the staging directory on `/tmp` for this test, substitute `/tmp/scaffold-auto-itest` for `<NVME_POOL_MOUNT>`.

## Prerequisites

- The NVMe pool is mounted and writable at `<NVME_POOL_MOUNT>` (D1: local NVMe pool, not the NAS).
- You are root or can `sudo` on the host.

## Run this

```bash
findmnt --target <NVME_POOL_MOUNT>
```

```bash
mkdir -p <NVME_POOL_MOUNT>/scaffold-auto-itest/stage1
```

```bash
chmod 0755 <NVME_POOL_MOUNT>/scaffold-auto-itest/stage1
```

## Verify

- `findmnt --target <NVME_POOL_MOUNT>` prints a line whose `TARGET` is `<NVME_POOL_MOUNT>` and whose `FSTYPE` is a real filesystem (not `tmpfs`).
- `ls -ld <NVME_POOL_MOUNT>/scaffold-auto-itest/stage1` prints a `drwxr-xr-x` directory line.
- `df -h <NVME_POOL_MOUNT>/scaffold-auto-itest/stage1` shows the NVMe pool's size, not the root filesystem's.

## Rollback

```bash
rmdir <NVME_POOL_MOUNT>/scaffold-auto-itest/stage1
```
"""

#: the other live draft (llm_traces 2711): the whole path became the placeholder
LIVE_2711 = """## Run this

```bash
mkdir -p <STAGING_DIR>
```

## Verify

- `ls -ld <STAGING_DIR>` prints a line beginning with `d` and the path — the directory exists.
"""

#: the draft the step asks for (what 12 of 14 live drafts wrote)
GOOD = f"""## Run this

```bash
mkdir -p {MARKER_DIR}/stage1
```

## Verify

- `ls -ld {MARKER_DIR}/stage1` prints the directory.
"""


def _spec():
    s = MagicMock()
    s.name = "itest-auto-runner"
    s.headers = {"X-Runner-Token": "tok"}
    return s


def _frame(runbook: str) -> dict:
    return sr.frame_run(H1, runbook, _spec(), POLICY)


def test_the_marker_is_the_production_one():
    assert sr.LITERAL_MADE_A_PLACEHOLDER == MARK


def test_the_live_draft_is_redrafted_not_parked():
    """THE regression, through the production frame and the production redraft decision: on main the frame carried
    only the write-allow refusals of the dummy-filled shape (`mkdir -p x/scaffold-auto-itest/stage1`),
    `shape_retry_note` returned "" and the run parked on a question about a mount point."""
    frame = _frame(LIVE_2800)
    note = sr.shape_retry_note(frame)
    assert note, ("a corrupted literal must reach a redraft, not park on the dummy's permission refusal: "
                  f"{[r['why'][:80] for r in frame['refused']]}")
    assert f"mkdir -p {MARKER_DIR}/stage1" in note
    hits = [r["why"] for r in frame["refused"] if MARK in r["why"]]
    # the mkdir and the step's Done-when `ls -ld`; the chmod and findmnt are not the step's commands
    assert len(hits) == 2, frame["refused"]
    assert "`<NVME_POOL_MOUNT>` stands for `/tmp`" in hits[0]
    assert sr.whose_gap(hits[0]) == "drafter"


def test_a_whole_value_turned_placeholder_is_caught_too():
    frame = _frame(LIVE_2711)
    hits = [r for r in frame["refused"] if MARK in r["why"]]
    assert len(hits) == 2, frame["refused"]          # the run command AND the step's own Done-when check
    assert f"`<STAGING_DIR>` stands for `{MARKER_DIR}/stage1`" in hits[0]["why"]
    assert sr.shape_retry_note(frame)


def test_the_step_s_own_draft_passes_clean():
    frame = _frame(GOOD)
    assert frame["commands"] == [f"mkdir -p {MARKER_DIR}/stage1"]
    assert not frame["refused"], frame["refused"]


def test_a_placeholder_the_step_does_not_give_is_left_alone():
    # the step names no value for the address: asking is right
    node = {"node_key": "T1", "title": "Copy the key", "description": "Run this:\n\n```bash\nssh-copy-id root@<HOST_IP>\n```"}
    assert sr.a_literal_the_step_gives_made_a_placeholder(["ssh-copy-id root@<HOST_IP>"], [], node) == []
    # a different command that merely shares the path is not the step's command
    assert sr.a_literal_the_step_gives_made_a_placeholder(
        ["chmod 0755 <DIR>/scaffold-auto-itest/stage1"], [], H1) == []
    # a secret is never written literally, even where the step did
    node = {"node_key": "T2", "title": "Log in", "description": "Run this:\n\n```bash\ncurl -u admin:hunter2 http://10.0.0.5/api\n```"}
    assert sr.a_literal_the_step_gives_made_a_placeholder(
        ["curl -u admin:<ADMIN_PASSWORD> http://10.0.0.5/api"], [], node) == []


def test_the_same_name_must_stand_for_the_same_value():
    node = {"node_key": "T3", "title": "t", "description": "Run this:\n\n```bash\ncp /a/x /b/x\n```"}
    assert sr.a_literal_the_step_gives_made_a_placeholder(["cp <SRC>/x <SRC>/x"], [], node) == []
    assert sr.a_literal_the_step_gives_made_a_placeholder(["cp <SRC>/x <DST>/x"], [], node)


def test_a_permission_refusal_alone_still_is_not_redrafted():
    """The exemption is only for a corrupted literal: the operator's allow-list is still theirs to widen."""
    frame = _frame(f"## Run this\n\n```bash\nchmod 0755 {MARKER_DIR}/stage1\n```\n\n## Verify\n\n- `ls -ld {MARKER_DIR}/stage1`\n")
    assert any(r["why"].startswith("not on the write-allow list") for r in frame["refused"]), frame["refused"]
    assert sr.shape_retry_note(frame) == ""


def test_the_drafter_is_told_a_decision_replaces_only_what_it_names():
    env = {"facts": ["Operator decision (D1, 2026-11-27) — newer than older notes in any step's text; where they "
                     "disagree, this wins: The local NVMe pool. the NAS is for backups only"]}
    prompt = pa.build_base_prompt(H1, {"description": "a home-lab build"}, env, autonomous=True)
    block = prompt[prompt.index("THE OPERATOR'S DECISIONS"):]
    assert "replaces only what it NAMES" in block
    assert "never turns one into a <PLACEHOLDER>" in block
