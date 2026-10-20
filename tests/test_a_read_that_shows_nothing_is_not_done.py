"""§17.1706 — a read-only block the judge cannot confirm, that printed almost nothing, is not done."""
import pytest
from unittest.mock import patch

from app.modules import supervised_runs as sr

WAITING = {"node_key": "SC4", "title": "Read Scrubs S1 pack grab and queue",
           "description": "Read-only, change nothing. Inside Sonarr's container (CT 104): print every Scrubs queue item.",
           "files": [{"path": "/tmp/in_ct_104.sh",
                      "content": "cat > /tmp/in_ct_104_remote.sh <<'REMOTE'\nset +e; trap 'exit 0' EXIT  # "
                                 + sr.READS_ONLY_MARKER + "\npython3 - <<'PY'\nprint(1)\nPY\nREMOTE\n"}],
           "commands": ["bash /tmp/in_ct_104.sh start"]}
# live, job e5e22b92 SC4 (2026-10-20)
LIVE = [{"command": "write /tmp/in_ct_104.sh", "output": "wrote /tmp/in_ct_104.sh (5520 bytes, 107 lines)"},
        {"command": "bash /tmp/in_ct_104.sh start", "output": "started scaffold-SC4 inside 104\nScrubs series not found\n"
                                                              "scaffold-SC4 finished (exit 0)"},
        {"command": "bash /tmp/in_ct_104.sh wait", "output": "(the log was printed by the phase that saw scaffold-SC4 finish)\n"
                                                             "scaffold-SC4 finished (exit 0)"}]
DATA = [{"command": "bash /tmp/in_ct_104.sh start", "output": "started scaffold-SC4 inside 104\n"
         + "\n".join(f"Scrubs (2001) S01 … | downloading | episode {i}" for i in range(10, 25)) + "\nscaffold-SC4 finished (exit 0)"}]


def _judge(verdict):
    async def j(claim, cmds, pasted, expects=None):
        return [{"id": "V1", "verdict": verdict, "reason": "the claim's expected queue listing is not demonstrated"}]
    return j


@pytest.mark.asyncio
async def test_the_live_not_found_read_is_not_done():
    with patch.object(sr, "_verify_verdicts", side_effect=_judge("unknown")):
        out = await sr.judge_a_read_only_block(WAITING, "SC4", LIVE)
    assert out and "Scrubs series not found" in out[0]["reason"]


@pytest.mark.asyncio
async def test_an_unsure_judge_does_not_fail_a_read_that_printed_data():
    with patch.object(sr, "_verify_verdicts", side_effect=_judge("unknown")):
        assert await sr.judge_a_read_only_block(WAITING, "SC4", DATA) == []


@pytest.mark.asyncio
async def test_a_confirmed_short_read_is_done():
    with patch.object(sr, "_verify_verdicts", side_effect=_judge("confirmed")):
        assert await sr.judge_a_read_only_block(WAITING, "SC4", LIVE) == []


def test_the_templates_chatter_is_not_substance():
    assert sr.read_substance("\n".join(o["output"] for o in LIVE)) == ["Scrubs series not found"]
