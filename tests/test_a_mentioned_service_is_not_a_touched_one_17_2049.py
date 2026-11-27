r"""§17.2049 — a service the step only MENTIONS in a note of what a change carries is not a service it acts on.

Live (2026-11-26, the server-redeploy job, RD79 and RD80): the step is "VM 110: redeploy the engine at main <sha>"
and its body tells the history of the commit it deploys — "(main <sha>; it carries … (PRs #1216–#1225: …, Prowlarr-only
arr endpoints refused, …) on top of …)". `names_in` matched any known service word anywhere in the body, so the pause
logged `service_guests named=['prowlarr']`, read Prowlarr in two guests (~25 s per redeploy) and carried 10.5k chars of
Prowlarr evidence into a frame that only redeploys the engine. A parenthetical that cites a PR or a §-section is that
history; a parenthetical that lists what to act on ("(e.g., Palworld, media, network, FlareSolverr)", §17.1590) is not.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace as NS

from app.modules import execution_agent as ea
from app.modules import runbook_templates as rt
from app.modules import service_truth as st

#: the redeploy step's shape (history trimmed; addresses and model names left out — the repo is public)
REDEPLOY = {
    "title": "VM 110: redeploy the engine at main 1a2b3c4d",
    "description": "",
    "prompt_template": (
        "Inside VM 110: redeploy the scaffold engine's services at the newest commit of its ref (main 1a2b3c4d; it "
        "carries deps (PRs #1221/#1222: python slim base; redis, sse-starlette) on top of 5e6f7a8b, which carries "
        "§17.2021–2042 (PRs #1216–#1225: dependency batch, code-gate and verifier/delivery fixes, runbook shell fixes, "
        "research reads PDFs, Prowlarr-only arr endpoints refused, failed-step panel; the engine-test sandbox is "
        "rebuilt too) on top of 9c0d1e2f, which carries §17.1957–1960 (PRs #1171–#1174: a Sonarr check judged whole) "
        "on top of 3a4b5c6d; §17.1631 keeps Ollama on the compose files it runs with), keeping the .env secrets (this "
        "engine, from its own repository), using the Ollama already running there. Settings:\n"
        "OLLAMA_CONTAINER=ollama\nCOMPOSE_PROFILES=owui\n"
        "Done when `curl -s http://localhost:8000/health` prints `\"status\":\"healthy\"`."),
}


def test_a_redeploy_that_tells_its_commits_history_names_no_service():
    assert st.names_in(REDEPLOY) == []


def test_a_step_with_no_place_does_not_touch_the_guest_of_a_service_its_history_mentions():
    # without "VM 110" to place it (§17.1581 returns early on a place), the touched guests come from `names_in`
    node = dict(REDEPLOY, title="Redeploy the engine at main 1a2b3c4d",
                prompt_template=REDEPLOY["prompt_template"].replace("Inside VM 110: r", "R"))
    assert "102" not in rt.guests_the_host_is_told_to_touch(node, [NS(name="prowlarr", guest="102")])
    assert "102" in rt.guests_the_host_is_told_to_touch({"title": "Restart Prowlarr"}, [NS(name="prowlarr", guest="102")])


def test_a_step_that_acts_on_a_service_still_names_it():
    assert st.names_in({"title": "Restart Prowlarr in CT 102", "description": ""}) == ["prowlarr"]
    assert st.names_in({"title": "Read the download queue",
                        "prompt_template": "Read Sonarr's queue and list every item stuck in import."}) == ["sonarr"]
    # §17.1590 — a parenthetical that LISTS what to act on still counts
    t7 = {"title": "Fix remaining endpoint backends",
          "prompt_template": "For any endpoint not covered by Radarr/Sonarr/Scaffold (e.g., Palworld, media, network, "
                             "FlareSolverr), fix its handler on container 111 to return real data or a clear JSON error."}
    assert st.names_in(t7) == ["radarr", "sonarr", "palworld", "flaresolverr"]


def test_several_services_acted_on_beside_a_change_note_keep_only_the_acted_on():
    node = {"title": "Make Radarr and Sonarr actually drive qBittorrent",
            "prompt_template": "Point Radarr and Sonarr at qBittorrent's API and prove the connection (the arr fixes of "
                               "PR #961 refused Prowlarr-only endpoints; Jellyfin is untouched by them)."}
    assert st.names_in(node) == ["radarr", "sonarr", "qbittorrent"]


def test_the_title_is_never_blanked_and_an_unbalanced_paren_swallows_nothing():
    assert st.names_in({"title": "Restart Prowlarr (after PR #1226 lands)"}) == ["prowlarr"]
    assert st.names_in({"title": "x", "prompt_template": "Restart Sonarr (see §17.1654 and then Radarr"}) == ["sonarr", "radarr"]


def test_the_pause_reads_its_services_through_names_in():
    # a gate goes blind when code moves: the pause's measured services come from this function
    assert "_st.names_in(run_node)" in inspect.getsource(ea)
