"""§17.2030 — a call to a resource only Prowlarr has, aimed at Radarr, Sonarr or Lidarr, is refused.

Live (job c75d77fa T3v2, 2026-10-12, §17.1499): the "Verify testable" step drafted `…/api/v3/applications/test` on
Radarr (CT 103) and Sonarr (CT 104). Radarr answered 404 on `/api/v3/applications` (measured); `applications` is the
list of apps PROWLARR syncs to. Radarr v3, Sonarr v3 and Lidarr v1's OpenAPI documents have no `applications`,
`appprofile` or `indexerproxy`; Prowlarr v1's has all three.
"""
from __future__ import annotations

from app.modules import arr_api_shapes as aas
from app.modules import supervised_runs as sr

KEY = "-H \"X-Api-Key: $(grep -oP '(?<=<ApiKey>)[^<]+' /var/lib/{app}/config.xml)\""
RADARR_TEST = f"pct exec 103 -- curl -s -X POST {KEY.format(app='radarr')} http://127.0.0.1:7878/api/v3/applications/testall"
SONARR_TEST = f"pct exec 104 -- curl -s -X POST {KEY.format(app='sonarr')} http://127.0.0.1:8989/api/v3/applications/testall"
RADARR_RIGHT = RADARR_TEST.replace("applications", "downloadclient")
SONARR_RIGHT = SONARR_TEST.replace("applications", "indexer")
PROWLARR = f"pct exec 102 -- curl -s {KEY.format(app='prowlarr')} http://127.0.0.1:9696/api/v1/applications"


def test_the_live_draft_is_refused_on_both_apps():
    hits = aas.calls_to_prowlarrs_resources([RADARR_RIGHT], [RADARR_TEST, SONARR_TEST], None)
    assert len(hits) == 1 and hits[0]["command"] == RADARR_TEST
    assert "Prowlarr resource: Radarr does not serve it" in hits[0]["why"]
    assert "/api/v3/downloadclient/testall" in hits[0]["why"]
    hits = aas.calls_to_prowlarrs_resources([SONARR_TEST], [], None)
    assert "Sonarr does not serve it" in hits[0]["why"]


def test_each_prowlarr_only_resource_and_lidarr():
    for res in ("applications", "appprofile", "indexerproxy"):
        hit = aas.calls_to_prowlarrs_resources([f"curl -s http://127.0.0.1:8686/api/v1/{res}"], [], None)
        assert hit and f"`/{res}` is a Prowlarr resource: Lidarr" in hit[0]["why"] and "/api/v1/indexer/testall" in hit[0]["why"]


def test_a_program_whose_base_is_radarr_is_refused():
    prog = ("python3 - <<'PY'\nimport urllib.request\nbase = 'http://127.0.0.1:7878/api/v3'\n"
            "req = urllib.request.Request(base + '/applications', headers={'X-Api-Key': k})\nPY\n")
    hit = aas.calls_to_prowlarrs_resources(["bash /tmp/check.sh"], [], [{"path": "/tmp/check.sh", "content": prog}])
    assert hit and hit[0]["command"].startswith("req = urllib.request.Request(base + '/applications'")


def test_what_the_apps_do_serve_and_prowlarr_itself_pass():
    assert aas.calls_to_prowlarrs_resources([RADARR_RIGHT, SONARR_RIGHT, PROWLARR], [RADARR_RIGHT], None) == []
    # a program that talks to Prowlarr AND Radarr names `/applications` for Prowlarr: not refused
    both = ("base_p = 'http://127.0.0.1:9696/api/v1'\nbase_r = 'http://127.0.0.1:7878/api/v3'\n"
            "apps = get(base_p + '/applications')\nclients = get(base_r + '/downloadclient')\n")
    assert aas.calls_to_prowlarrs_resources(["x"], [], [{"path": "/tmp/p.py", "content": both}]) == []
    # a longer resource word is not the resource
    assert aas.calls_to_prowlarrs_resources(["curl http://127.0.0.1:7878/api/v3/applications-list-x"], [], None) == []


# ------------------------------------------------------------ the production gate: the frame refuses it
POLICY = {"allow": ["ANY"], "sudo": True, "secrets": [], "can_write_files": True}
NODE = {"node_key": "T3v2", "tool": "Shell", "title": "Verify testable", "description": "",
        "prompt_template": "Read-only: test the download clients and indexers Radarr (CT 103) and Sonarr (CT 104) use."}


def _frame(commands: list[str], verify: list[str]) -> dict:
    runbook = ("## Run this\n\n```bash\n" + "\n".join(commands) + "\n```\n\n## Verify\n\n```bash\n"
               + "\n".join(verify) + "\n```\n")
    spec = type("S", (), {"name": "pve-runner", "headers": {}, "endpoint": "http://10.9.8.7:8790/mcp/"})()
    return sr.frame_run(NODE, runbook, spec, POLICY, env={"profile": "root@pve"},
                        inventory={"cts": {}, "vms": {}, "names": {}})


def _ours(frame: dict) -> list:
    return [r for r in frame.get("refused") or [] if "§17.2030" in str(r)]


READ_RADARR = f"pct exec 103 -- curl -s {KEY.format(app='radarr')} http://127.0.0.1:7878/api/v3/{{res}} | head -c 300"


def test_the_frame_refuses_the_live_calls_and_passes_the_right_ones():
    """The live draft's test calls (Run) and a read of the same resource (Verify) are refused; the right ones are not."""
    assert _ours(_frame([RADARR_TEST, SONARR_TEST], [READ_RADARR.format(res="downloadclient")]))
    assert _ours(_frame([RADARR_RIGHT, SONARR_RIGHT], [READ_RADARR.format(res="applications")]))
    assert not _ours(_frame([RADARR_RIGHT, SONARR_RIGHT], [READ_RADARR.format(res="downloadclient")]))
