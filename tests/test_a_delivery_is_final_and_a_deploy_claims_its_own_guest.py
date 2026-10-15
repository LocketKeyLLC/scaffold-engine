r"""§17.1589 — four seams of one job (c2792d4b T7/T8, 2026-10-15).

T7 "Fix remaining endpoint backends — … (e.g., Palworld, media, network, FlareSolverr), fix its handler on container 111"
was developed on the panel (one clean round) and then the FlareSolverr DEPLOY capability claimed the frame because the
text has "FlareSolverr" and, further on, "container": CT 107 was stopped, started and re-installed, and the step recorded
done while the panel's handlers were never touched. T8 "Fix UI fetch error handling" was not routed to develop (no build
word for a UI edit) and its container script wrote `<RADARR_API_KEY>` into a file inside the guest; the frame offered Run,
the run refused "missing (used in the file …)", and a driver looped.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace as NS

from app.modules import supervised_runs as sr
from app.modules.step_decomposition import builds_a_capability

T7 = {"node_key": "T7", "title": "Fix remaining endpoint backends", "tool": "Shell",
      "prompt_template": "For any endpoint not covered by Radarr/Sonarr/Scaffold (e.g., Palworld, media, network, FlareSolverr), fix its "
                         "handler on container 111 to return real data or a clear JSON error. Preserve existing features."}
T8 = {"node_key": "T8", "title": "Fix UI fetch error handling", "tool": "Shell",
      "prompt_template": "Edit the hand-written UI on container 111 so every section's fetch has a timeout and a .catch that replaces "
                         "'Loading ...' with the error message from the JSON response. Preserve dark theme and all existing sections."}


def test_a_deploy_claims_only_a_step_placed_on_its_own_guest():
    assert sr.rewrite_deploy_flaresolverr(T7, ["x"], ["y"])[2] == []                                   # placed on 111: not this deploy
    assert sr.rewrite_deploy_flaresolverr({"title": "Deploy FlareSolverr"}, ["x"], ["y"])[2]              # the deploy itself
    assert sr.rewrite_deploy_flaresolverr({"title": "Stand up the FlareSolverr container"}, ["x"], ["y"])[2]
    assert sr.rewrite_deploy_flaresolverr({"title": "Create FlareSolverr on container 107", "description": "its own LXC"}, ["x"], ["y"])[2]
    assert not sr._FLARESOLVERR_RE.search("FlareSolverr), fix its handler on container 111")           # adjacency, not anywhere
    assert sr._FLARESOLVERR_RE.search("the FlareSolverr LXC") and sr._FLARESOLVERR_RE.search("FlareSolverr proxy container")
    byparr = {"title": "Fix remaining endpoint backends", "prompt_template": "the Byparr health route on container 111"}
    assert sr.rewrite_deploy_byparr(byparr, ["x"], ["y"])[2] == []
    assert sr.rewrite_deploy_byparr({"title": "Deploy Byparr as its own LXC"}, ["x"], ["y"])[2]


def test_a_developed_delivery_is_never_rewritten_by_a_capability():
    rb = ("## Write these files\n\n### /tmp/scaffold-dev-T7--deliver.sh\n```bash\necho delivering\n```\n\n## Run this\n\n```bash\n"
          "bash /tmp/scaffold-dev-T7--deliver.sh\n```\n\n## Verify\n\n- `pct exec 111 -- systemctl is-active control-panel.service` shows `active`\n")
    node = {"node_key": "T7", "title": "Deploy FlareSolverr", "tool": "Shell", "description": "the FlareSolverr container"}   # a title the deploy WOULD claim
    frame = sr.frame_run(node, rb, NS(name="pve-runner"), {"allow": ["ANY"], "can_write_files": True})
    assert frame["commands"] == ["bash /tmp/scaffold-dev-T7--deliver.sh"] and not any("pct create 107" in c for c in frame["commands"])
    assert sr.is_develop_delivery([{"path": "/tmp/scaffold-dev-T7--deliver.sh", "content": "x"}]) and not sr.is_develop_delivery([{"path": "/tmp/in_ct_111.sh"}])
    src = inspect.getsource(sr.frame_run)
    assert "_orig_cmds, _orig_verify = list(cmds), list(verify)" in src and "if _comp_notes and is_develop_delivery(files):" in src
    assert src.index("_orig_cmds, _orig_verify = list(cmds)") < src.index("rewrite_completeness_check(node, cmds") < src.index("is_develop_delivery(files)")


def test_a_ui_edit_is_a_software_build():
    assert builds_a_capability(T8["title"] + " " + T8["prompt_template"])
    assert builds_a_capability("Edit the UI on container 111 so each section renders the JSON data it receives")
    assert builds_a_capability("restyle index.html with the dark theme")
    for no in ("install nodejs", "set onboot on VM 110", "disable IPv6 in the guest", "point the resolver at Pi-hole"):
        assert not builds_a_capability(no), no


def test_a_secret_placeholder_in_a_file_or_a_guest_script_is_refused_before_run_is_offered():
    script = ("cat > /tmp/in_ct_111_remote.sh <<'REMOTE'\ncat > /opt/control-panel-backend/.env <<'EOF'\nRADARR_API_KEY=<RADARR_API_KEY>\n"
              "SONARR_API_KEY=<SONARR_API_KEY>\nEOF\nREMOTE\npct push 111 /tmp/in_ct_111_remote.sh /root/.s.sh\n")
    files = [{"path": "/tmp/in_ct_111.sh", "content": script}]
    out = sr.secret_inside_a_guest_script(files, {"RADARR_API_KEY", "SONARR_API_KEY", "PANEL_PASSWORD"})
    assert len(out) == 2 and all("is read inside the guest" in o["why"] for o in out)
    assert sr.secrets_in_files(files, [], ["RADARR_API_KEY"])                                # resolved by a store, still in a file
    assert sr.secrets_in_files(files, [], []) == []                                           # nothing known, nothing marked: §17.1280's old answer
    rb = ("## Write these files\n\n### /tmp/in_ct_111.sh\n```bash\n" + script + "```\n\n## Run this\n\n```bash\nbash /tmp/in_ct_111.sh\n```\n\n"
          "## Verify\n\n- `pct exec 111 -- grep -c RADARR /opt/control-panel-backend/.env` shows `1`\n")
    frame = sr.frame_run(T8, rb, NS(name="pve-runner"), {"allow": ["ANY"], "can_write_files": True, "held": ["RADARR_API_KEY", "SONARR_API_KEY"]})
    assert frame["suggested"] == "myself" and any("cannot be written into a file" in r["why"] or "is read inside the guest" in r["why"] for r in frame["refused"])
