"""§17.1673 — a read that prints an *arr's settings list puts every credential in it into the record
(live, job 5db1b088 NB1: `curl … /api/v1/indexer` dumped Prowlarr's whole indexer list)."""
from unittest.mock import MagicMock

from app.modules import supervised_runs as sr
from app.modules.prints_a_secret import prints_arr_settings

NB1 = 'pct exec 102 -- curl -s -H "X-Api-Key: $PROWLARR_API_KEY" http://127.0.0.1:9696/api/v1/indexer'


def test_the_live_dump_is_refused_with_the_status_read_offered():
    hits = prints_arr_settings([NB1], [])
    assert hits and "/api/v1/indexerstatus" in hits[0]["why"]
    for r in ("downloadclient", "notification", "applications", "indexerproxy", "importlist", "indexer/12"):
        assert prints_arr_settings([f'curl -s -H "X-Api-Key: k" http://127.0.0.1:7878/api/v3/{r}'], [])


def test_reads_that_carry_no_credential_pass():
    ok = ['curl -s -H "X-Api-Key: k" http://127.0.0.1:9696/api/v1/indexerstatus',
          'curl -s -H "X-Api-Key: k" http://127.0.0.1:9696/api/v1/indexer/schema',
          'curl -s -o /dev/null -w "%{http_code}" -H "X-Api-Key: k" "http://127.0.0.1:9696/api/v1/indexer"',
          'curl -s -H "X-Api-Key: k" http://127.0.0.1:9696/api/v1/indexer | grep -c nekoBT',
          'curl -s -H "X-Api-Key: k" http://127.0.0.1:8989/api/v3/health',
          'python3 -c "import json" "$(curl -s http://127.0.0.1:9696/api/v1/indexer)"',
          'curl -s -H "X-Api-Key: k" http://127.0.0.1:9696/api/v1/indexer | python3 -c "import json,sys; print(1)"']
    for c in ok:
        assert prints_arr_settings([c], []) == [], c


def test_the_frame_refuses_the_dump():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": []}
    node = {"node_key": "NB1", "title": "Diagnose nekoBT indexer failure in Prowlarr", "tool": "Shell",
            "prompt_template": "Read only."}
    frame = sr.frame_run(node, "## Run this\n\n```bash\n" + NB1 + "\n```\n", spec, policy, services=[],
                         inventory={"vms": {}, "cts": {"102": "running"}, "names": {"102": "prowlarr"}})
    assert any("§17.1673" in r["why"] for r in frame["refused"]), frame["refused"]


def test_the_engines_own_key_read_does_not_hide_the_dump():
    """the frame rewrites $PROWLARR_API_KEY into `$(cat …config.xml | sed …)` -- its `|` must not end the match"""
    c = ('pct exec 102 -- sh -c \'curl -s -H "X-Api-Key: $(cat /var/lib/prowlarr/config.xml 2>/dev/null | sed -n '
         '"s:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p" | head -n 1)" http://127.0.0.1:9696/api/v1/indexer\'')
    assert prints_arr_settings([c], [])


def test_a_write_is_not_this_rule():
    assert prints_arr_settings(['curl -s --fail-with-body -X POST "http://192.168.1.21:9696/api/v1/indexer" '
                                '-H "X-Api-Key: k" -d @body.json'], []) == []


def test_the_filter_the_refusal_offers_is_accepted_and_a_key_filter_is_not():
    """rules must not teach what the gate refuses"""
    base = 'curl -s -H "X-Api-Key: k" http://127.0.0.1:9696/api/v1/indexer'
    assert prints_arr_settings([base + " | grep -o '\"name\": *\"[^\"]*\"'"], []) == []
    assert prints_arr_settings([base + " | grep -o '\"apiKey[^}]*'"], [])
    assert "grep -o" in prints_arr_settings([base], [])[0]["why"]
