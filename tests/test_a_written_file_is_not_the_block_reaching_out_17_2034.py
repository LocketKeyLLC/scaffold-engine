"""§17.2034 — two shapes the 2026-10-12 audit ledger recorded and nothing fixed.

1. §17.1303's address rule ("`X` is not guest N's address: the engine measured Y") read EVERY ssh target in the
   block's texts, including a file the block only WRITES. A service's code that ssh's from container 111 to VM 106
   at runtime names 106's address -- not a wrong address for 111 -- and was refused as one. The rule now reads only
   what the block RUNS: its commands and the files it executes, at heredoc depth <= 1 (§17.1338/1420's line), and
   not an ssh that itself runs inside a guest.

2. What a read prints comes back with every secret replaced by `***` (the runner's `redact`, the engine's
   `scrub_run_output`). A step that reads a config and writes the whole file back carries the mask in place of the
   key; the service restarts with the credential `***`. Nothing refused it. It is refused now, by shape.
"""
from __future__ import annotations

import pytest

from app.modules import runbook_preconditions as rp
from app.modules.machine_truth import GuestTruth

INV = {"cts": {"111": "running", "101": "running"}, "vms": {"106": "running"},
       "names": {"111": "control-panel", "106": "palworld-server", "101": "jellyfin"}}
NODE = {"node_key": "ADD150", "title": "LXC 111: add the Palworld restart route",
        "description": "On container 111, add a route that restarts the Palworld server in VM 106."}
TRUTH = GuestTruth(gid="111", kind="ct", status="running", mac="bc:24:11:00:01:11", address="192.0.2.29")
ROUTE = ("const PALWORLD = 'steam@192.0.2.44';\n"
         "router.post('/api/palworld/restart', (req, res) => {\n"
         "  execFileSync('ssh', ['-o', 'BatchMode=yes', 'steam@192.0.2.44', 'systemctl restart palworld']);\n"
         "  res.json({ok: true});\n});\n")
PUSH = ["pct push 111 /tmp/palworld.js /opt/control-panel/routes/palworld.js",
        "pct exec 111 -- systemctl restart control-panel"]


def _addr(rows: list[dict]) -> list[dict]:
    return [r for r in rows if "'s address: the engine measured" in r["why"]]


@pytest.mark.asyncio
async def test_a_delivered_files_ssh_to_another_machine_is_not_this_guests_address():
    rows = await rp.unmet(PUSH, None, plan=[], node=NODE, inventory=INV, truth=TRUTH,
                          files=[{"path": "/tmp/palworld.js", "content": ROUTE}])
    assert _addr(rows) == [], [r["why"][:100] for r in rows]


@pytest.mark.asyncio
async def test_a_file_the_block_runs_is_still_judged():
    """The bite: the same wrong address in a script the block EXECUTES is still refused, beside the delivered file."""
    rows = await rp.unmet(PUSH + ["bash /tmp/reach.sh"], None, plan=[], node=NODE, inventory=INV, truth=TRUTH,
                          files=[{"path": "/tmp/palworld.js", "content": ROUTE},
                                 {"path": "/tmp/reach.sh", "content": "ssh -o BatchMode=yes root@192.0.2.77 true\n"}])
    hits = _addr(rows)
    assert len(hits) == 1 and "192.0.2.77" in hits[0]["command"] and "`192.0.2.29`" in hits[0]["why"], hits


@pytest.mark.asyncio
async def test_a_command_the_block_runs_is_still_judged():
    rows = await rp.unmet(["ssh root@192.0.2.77 'systemctl restart control-panel'"], None, plan=[], node=NODE,
                          inventory=INV, truth=TRUTH)
    assert len(_addr(rows)) == 1


@pytest.mark.asyncio
async def test_content_a_command_writes_two_heredocs_deep_is_not_run_here():
    script = ("cat > /tmp/in_ct_111_remote.sh <<'REMOTE'\n"
              "cat > /opt/control-panel/routes/palworld.js <<'JS'\n" + ROUTE + "JS\nREMOTE\n"
              "pct push 111 /tmp/in_ct_111_remote.sh /root/.scaffold_step.sh\n")
    rows = await rp.unmet([script], None, plan=[], node=NODE, inventory=INV, truth=TRUTH)
    assert _addr(rows) == []


# ───── 2: the mask is not the value

ARR_CONFIG = ("<Config>\n  <BindAddress>*</BindAddress>\n  <Port>7878</Port>\n  <ApiKey>***</ApiKey>\n"
              "  <AuthenticationMethod>Forms</AuthenticationMethod>\n</Config>\n")


@pytest.mark.parametrize("content", [
    ARR_CONFIG,
    "[Preferences]\nWebUI\\Password_PBKDF2=\"@ByteArray(***)\"\nWebUI\\Port=8080\n",
    "SONARR_API_KEY=***\nSONARR_URL=http://127.0.0.1:8989\n",
    '{\n  "apiKey": "***",\n  "port": 8096\n}\n',
])
def test_a_file_carrying_the_mask_where_a_value_goes_is_refused(content):
    rows = rp.a_redacted_value_written_back(["pct push 105 /tmp/config.xml /var/lib/radarr/config.xml"],
                                            [{"path": "/tmp/config.xml", "content": content},
                                             {"path": "/tmp/other.conf", "content": "port=1\n"}])
    assert len(rows) == 1 and "/tmp/config.xml" in rows[0]["why"] and "runner's mask" in rows[0]["why"], rows


def test_a_command_carrying_the_mask_is_refused_once():
    rows = rp.a_redacted_value_written_back(
        ['curl -s -H "X-Api-Key: ***" http://127.0.0.1:7878/api/v3/system/status',
         "curl -u admin:*** http://127.0.0.1:8080/api/v2/app/version"], [])
    assert len(rows) == 1 and "this block's commands" in rows[0]["why"], rows


@pytest.mark.parametrize("text", [
    "# *** do not edit below ***",                           # a banner
    'echo "*** done ***"',
    "**Note:** ***important***",                             # markdown
    "return {k: (\"***\" if re.search(r'key|token', k) else v) for k, v in d.items()}",   # code that masks
    "password = ********",                                   # an app's own 8-star mask is not ours
    'curl -s -H "X-Api-Key: $(grep -oP \'(?<=<ApiKey>)[^<]+\' /var/lib/radarr/config.xml)" http://127.0.0.1:7878',
    "sed -i 's/^ApiKey=.*/ApiKey=$NEW_KEY/' /etc/app.conf",
])
def test_what_is_not_a_masked_value_is_left_alone(text):
    assert rp.a_redacted_value_written_back([text], [{"path": "/tmp/f", "content": text}]) == []


@pytest.mark.asyncio
async def test_unmet_says_it():
    rows = await rp.unmet(["pct push 105 /tmp/config.xml /var/lib/radarr/config.xml"], None, plan=[],
                          files=[{"path": "/tmp/config.xml", "content": ARR_CONFIG}])
    assert any("runner's mask" in r["why"] for r in rows), [r["why"][:80] for r in rows]
