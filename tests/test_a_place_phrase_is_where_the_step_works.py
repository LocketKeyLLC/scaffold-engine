r"""§17.1581 — three seams the panel job's first fix step (c2792d4b T4, 2026-10-15) exposed at once.

T3 "Identify loading failure causes" was a planner-marked `decision` node that asked the operator nothing; the pause
asked "How should I proceed?", the answer closed it done with the decision text as its whole output, and T4 drafted
with no cause list. T4 "Fix Radarr endpoint backend — edit the Radarr handler ON CONTAINER 111 …" was hosted on
Radarr's CT 103 by a service-name match, and its script stopped Radarr and `sed`-wrote the WORD `RADARR_API_KEY`
into config.xml as the key.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace as NS

from app.modules import decision_pause as dp
from app.modules import develop as dv
from app.modules import supervised_runs as sr
from app.modules.machine_truth import place_guest, subject_guest
from app.modules.runbook_templates import guests_the_host_is_told_to_touch

T4 = {"node_key": "T4", "title": "Fix Radarr endpoint backend", "tool": "Shell", "description": "",
      "prompt_template": "Edit the Radarr handler on container 111 to read RADARR_API_KEY from engine-held secrets (env var), "
                         "send proper X-Api-Key header, and return JSON data or a clear JSON error. Do not touch other endpoints."}
PANEL = NS(name="control-panel", guest="111", unit="control-panel.service", ports=("3001",), workdir="/opt/control-panel-backend", vm=False)
RADARR = NS(name="radarr", guest="103", unit="radarr.service", ports=("7878",), workdir="/var/lib/radarr", vm=False)
INV = {"cts": {"111": "running", "103": "running", "104": "running", "105": "running"}, "vms": {},
       "names": {"111": "control-panel", "103": "radarr", "104": "sonarr", "105": "download-client"}, "unread": []}
BRIEF = "Fix Control Panel Loading Sections on Container 111. Fix the backend on container 111 so every endpoint returns real data. Work only on container 111."


def test_the_place_the_step_names_is_where_it_works_not_the_service_it_talks_to():
    assert place_guest(T4) == "111"
    assert subject_guest(T4, INV) == "111"
    assert guests_the_host_is_told_to_touch(T4, [PANEL, RADARR]) == {"111"}
    h = dv.host_for(T4, [PANEL, RADARR], brief=BRIEF)
    assert h is not None and (h.guest, h.workdir) == ("111", "/opt/control-panel-backend")
    # the place names a guest with no developable service: not developed (as §17.1454 for a title id)
    assert dv.host_for(T4, [RADARR], brief=BRIEF) is None
    for t in ("LXC 111: add the Palworld status route", "deploy the files to container 111 and restart"):
        assert place_guest({"title": t}) == "111", t


def test_without_a_place_phrase_the_named_services_still_say_which_guests_a_step_touches():
    add132 = {"title": "Make Radarr and Sonarr drive qBittorrent", "description": "point both at the download client"}
    assert place_guest(add132) is None
    touched = guests_the_host_is_told_to_touch(add132, [PANEL, RADARR, NS(name="sonarr", guest="104", unit="sonarr.service", ports=("8989",), workdir="", vm=False)])
    assert touched == {"103", "104"}
    assert place_guest({"title": "Move the media from container 101 to container 103"}) is None     # two places: none


def test_a_decision_node_that_asks_the_operator_nothing_is_a_task():
    t3 = {"title": "Identify loading failure causes", "description": "",
          "prompt_template": "Correlate each endpoint's failure with backend code: missing upstream key, wrong auth header, "
                             "upstream 401, CORS, JSON parse error, or frontend fetch bug. Produce a per-endpoint cause list."}
    assert not dp.asks_the_operator(t3)
    for ask in ({"title": "Decide with the operator: Should the host itself also stop accepting router advertisements?"},
                {"title": "Choose the download client", "description": "qBittorrent or SABnzbd"},
                {"title": "Which libraries should Jellyfin get?", "description": ""}):
        assert dp.asks_the_operator(ask), ask["title"]
    src = inspect.getsource(dp.pending_decision)
    assert "if asks_the_operator(node):" in src and "SET node_type = 'task'" in src and "LIMIT 1" not in src
    assert src.index("asks_the_operator(node)") < src.index("SET node_type = 'task'")


def test_a_held_secrets_name_written_as_its_value_is_refused_and_its_right_forms_pass():
    policy = {"held": ["RADARR_API_KEY", "PANEL_PASSWORD"], "allow": ["ANY"]}
    script = ("set -e\nsystemctl stop radarr.service\n"
              "sed -i 's|<ApiKey>.*</ApiKey>|<ApiKey>RADARR_API_KEY</ApiKey>|' /var/lib/radarr/config.xml\n")
    out = sr.a_secret_name_written_as_its_value(["bash /tmp/in_ct_103.sh start"], [{"path": "/tmp/in_ct_103.sh", "content": script}], policy)
    assert len(out) == 1 and "`RADARR_API_KEY`" in out[0]["why"] and "writes the secret's NAME where its VALUE belongs" in out[0]["why"]
    assert out[0]["command"].startswith("sed -i")
    assert sr.a_secret_name_written_as_its_value(['curl -H "X-Api-Key: RADARR_API_KEY" http://127.0.0.1:7878/api/v3/system/status'], [], policy)
    assert sr.a_secret_name_written_as_its_value(["printf '%s' '{\"radarrApiKey\": \"RADARR_API_KEY\"}' > /opt/x/config.json"], [], policy)
    for good in ('curl -H "X-Api-Key: $RADARR_API_KEY" http://127.0.0.1:7878/',
                 'curl -H "X-Api-Key: ${RADARR_API_KEY}" http://127.0.0.1:7878/',
                 'pct exec 111 -- sh -c \'curl -H "X-Api-Key: <RADARR_API_KEY>" http://127.0.0.1:7878/\'',
                 "RADARR_API_KEY=$(grep -oP '(?<=<ApiKey>)[^<]+' /var/lib/radarr/config.xml); echo ok",
                 "echo 'the panel reads RADARR_API_KEY from its environment'"):
        assert sr.a_secret_name_written_as_its_value([good], [], policy) == [], good
    assert sr.a_secret_name_written_as_its_value(["x=RADARR_API_KEY"], [], {"held": []}) == []        # nothing held: no judgment
    assert "a_secret_name_written_as_its_value(cmds, files, policy, verify)" in inspect.getsource(sr.frame_run)
    rb = "## Write these files\n\n### /tmp/in_ct_103.sh\n```bash\n" + script + "```\n\n## Run this\n\n```bash\nbash /tmp/in_ct_103.sh\n```\n\n## Verify\n\n- `pct exec 103 -- grep -c ApiKey /var/lib/radarr/config.xml` shows `1`\n"
    frame = sr.frame_run({"node_key": "T4", "title": "t"}, rb, NS(name="pve-runner"), {**policy, "can_write_files": True})
    assert any("NAME where its VALUE belongs" in r["why"] for r in frame["refused"]) and frame["suggested"] == "myself"
