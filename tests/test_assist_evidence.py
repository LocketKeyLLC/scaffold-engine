"""§17.1027 — the evidence layer: need → ranked evidence → verified answer.

Fixtures are the operator's VERBATIM turns from the live log, because the
defect was found there: over one day every fix-path research query was the
command line they had just typed —

    assist_fix_research queries=['qm config 106', 'qm config 106']
    assist_fix_research queries=['pct exec 111 -- ls -la /opt', ...]
    assist_fix_research queries=['no changes', 'no changes']

Assertions are PROPERTIES (a typed command never becomes the query; the OPEN
item does; an unsupported value is named), never a pinned phrase — the
§17.1025 lesson about tests that are the answer key.
"""
from __future__ import annotations

import pytest

from app.modules import assist_evidence as ev
from app.modules.assist_evidence import (
    Need,
    derive_need,
    finalize_query,
    rank_evidence,
    recap_open_lines,
    split_paste,
    unsupported_specifics,
    verify_answer,
)

# ── live turns (session 613dd1df, 2026-09-11) ────────────────────────────
PASTE_STAT = "root@pve:~# pct exec 120 -- stat -c %s /etc/caddy/Caddyfile\n406"
PASTE_LS = ("root@pve:~# pct exec 111 -- ls -la /opt\ntotal 20\n"
            "drwxr-xr-x  5 root root 4096 Sep  6 15:20 .\n"
            "drwxr-xr-x  3 root root 4096 Sep  6 15:21 control-panel-backend")
PASTE_STATUS = "no changes"
PASTE_ERROR = ("root@pve:~# systemctl start control-panel\n"
               "Job for control-panel.service failed because the control process "
               "exited with error code.\nSee \"systemctl status control-panel.service\"")
PASTE_HEREDOC = ("root@pve:~# pct exec 120 -- tee /etc/caddy/Caddyfile > /dev/null <<'EOF'\n"
                 "example.duckdns.org {\n    handle_errors {\n        respond 500\n    }\n}\n"
                 "EOF\nroot@pve:~# pct exec 120 -- systemctl reload caddy")
RECAP_T37 = """GOAL: Validate the entire build — confirm all services, firewall, VPN, GPU, and control panel are working.

DONE:
- VMs 106 and 110 started and running

OPEN:
- Control-panel backend (LXC 111) not listening on 3001 — attempted start with wrong path `/opt//opt/control-panel` (actual dir is `/opt/control-panel-backend`)
- No validation yet of: firewall rules, VPN, GPU passthrough

CONSTRAINTS:
- VM 110 has no QEMU guest agent

NEXT: Start the control-panel backend from the correct directory"""
NOTES_ROUTER = [{"text": "The router is a Spectrum SAX1V1K; the ISP app is the only way to configure it."}]
QUESTION_ROUTER = ("i am in the spectrum app under the router, which is where the DNS "
                   "server forwarding is but it only lists Primary DNS Server and "
                   "Secondary DNS server")


# ── split_paste ───────────────────────────────────────────────────────────

def test_typed_commands_are_separated_from_printed_output():
    cmds, out = split_paste(PASTE_STAT)
    assert cmds == ["root@pve:~# pct exec 120 -- stat -c %s /etc/caddy/Caddyfile"]
    assert out == ["406"]


def test_heredoc_bodies_belong_to_the_command_not_the_output():
    cmds, out = split_paste(PASTE_HEREDOC)
    assert len(cmds) == 2
    assert out == [], out  # the file's contents are neither command nor symptom


def test_bare_dollar_prompt_and_powershell_prompt_are_commands():
    cmds, out = split_paste("$ ls -la\nfoo\nPS C:\\Users\\me> dir\nbar")
    assert cmds == ["$ ls -la", "PS C:\\Users\\me> dir"]
    assert out == ["foo", "bar"]


# ── derive_need: the live defect ──────────────────────────────────────────

@pytest.mark.parametrize("paste", [PASTE_STAT, PASTE_LS, PASTE_STATUS])
def test_a_successful_paste_with_no_open_item_is_not_researched(paste):
    need = derive_need(paste, title="Validate entire build")
    assert need.kind == "none"
    assert not need.researchable
    assert need.query == ""


@pytest.mark.parametrize("paste", [PASTE_STAT, PASTE_LS, PASTE_STATUS])
def test_the_typed_command_never_becomes_the_query(paste):
    need = derive_need(paste, title="Validate entire build", step_recap=RECAP_T37)
    low = need.query.lower()
    for token in ("pct", "stat", "ls -la", "root@", "no changes"):
        assert token not in low, (token, need.query)


def test_with_no_error_the_open_item_is_the_subject():
    need = derive_need(PASTE_STAT, title="Validate entire build", step_recap=RECAP_T37)
    assert need.kind == "goal"
    assert need.researchable
    assert "listening" in need.subject.lower()
    # the query is built from the OPEN line's own words, not the paste
    assert any(w in need.query.lower() for w in ("backend", "listening"))
    # DONE lines are not researched
    assert "started" not in need.query.lower()


def test_an_error_line_is_the_subject_and_the_prompt_prefix_is_not():
    need = derive_need(PASTE_ERROR, title="Start the control panel", step_recap=RECAP_T37)
    assert need.kind == "error"
    assert "failed" in need.subject.lower()
    assert not need.query.lower().startswith("root@")
    assert "systemctl start" not in need.query  # the typed line is not the query


def test_a_config_file_pasted_in_a_heredoc_is_not_a_symptom():
    need = derive_need(PASTE_HEREDOC, title="Configure the proxy")
    assert need.kind != "error", need


def test_prose_that_opens_like_a_request_is_a_question():
    need = derive_need("where do i set up forwarding in the app?", title="Expose services")
    assert need.kind == "question"
    need = derive_need("how do I check whether the service is listening", title="x")
    assert need.kind == "question"


def test_the_ask_path_can_assert_it_is_a_question():
    # routing already decided; a sentence with no interrogative lead still counts
    need = derive_need(QUESTION_ROUTER, assume_question=True)
    assert need.kind == "question"


def test_named_hardware_leads_the_query_when_the_text_is_about_it():
    need = derive_need(QUESTION_ROUTER, operator_notes=NOTES_ROUTER, assume_question=True)
    assert need.hardware and need.hardware[0] in need.query.split()[0]


def test_hardware_is_not_bolted_onto_an_unrelated_paste():
    need = derive_need(PASTE_ERROR, title="Start the control panel",
                       operator_notes=NOTES_ROUTER, step_recap=RECAP_T37)
    assert not any(h.lower() in need.query.lower() for h in ("sax1v1k",))


def test_goal_terms_join_a_question_only_when_it_is_about_them():
    """Live: a router question asked while the step's OPEN item had moved on
    to a backend service got the backend's words appended to its query."""
    about_router = "OPEN: operator cannot find the forwarding screen in the spectrum app"
    about_backend = "OPEN: control-panel backend (LXC 111) not listening on 3001"
    q_router = "On the spectrum app it only gives you the option to fill out primary server and secondary server. please help."
    with_it = derive_need(q_router, goal_terms=about_router, assume_question=True)
    without = derive_need(q_router, goal_terms=about_backend, assume_question=True)
    assert with_it.goal_terms, with_it
    assert without.goal_terms == [], without
    assert "backend" not in without.query.lower()


def test_wants_latest_is_read_from_the_subject():
    assert derive_need("what is the latest release of the kernel?", title="").wants_latest
    assert not derive_need("why does the service exit on start?", title="").wants_latest


def test_recap_open_lines_prefers_open_then_goal_and_skips_done():
    lines = recap_open_lines(RECAP_T37)
    assert lines[0].startswith("Control-panel backend")
    assert lines[-1].startswith("Validate the entire build")
    assert not any("started and running" in ln for ln in lines)
    assert recap_open_lines("") == []


# ── finalize_query: a hint is a request, this is a rule ───────────────────

def test_finalize_enforces_hardware_and_goal_terms_the_generator_dropped():
    need = Need(kind="question", subject="q", hardware=["SAX1V1K"],
                goal_terms=["port forwarding"])
    q = finalize_query(need, "spectrum app primary server secondary server")
    assert q.split()[0] == "SAX1V1K"
    assert "port forwarding" in q
    assert len(q.split()) <= 12


def test_finalize_does_not_duplicate_terms_already_present():
    need = Need(kind="question", subject="q", hardware=["SAX1V1K"], goal_terms=["listening"])
    q = finalize_query(need, "SAX1V1K service not listening")
    assert q.lower().count("sax1v1k") == 1
    assert q.lower().count("listening") == 1


def test_finalize_falls_back_to_the_need_when_the_generator_returned_nothing():
    need = Need(kind="goal", subject="backend not listening on the port", query="backend listening")
    assert finalize_query(need, "") == "backend listening"
    assert finalize_query(Need(kind="none"), "") == ""


# ── rank_evidence ─────────────────────────────────────────────────────────

def _src(text, url="", date="", kind="web"):
    return {"query": "q", "kind": kind, "text": text, "url": url, "date": date}


def test_offtopic_sources_are_dropped_and_newer_relevant_ones_lead():
    need = Need(kind="goal", subject="backend not listening on the port",
                query="backend listening", goal_terms=["backend", "listening"])
    srcs = [
        _src("A recipe for sourdough bread and how to knead it", url="u1", date="2026-01-01"),
        _src("The backend was not listening because the unit file path was wrong", url="u2", date="2023-05-01"),
        _src("Listening sockets and the backend service — a newer guide", url="u3", date="2025-08-10"),
    ]
    out = rank_evidence(srcs, need)
    assert [s["url"] for s in out] == ["u3", "u2"]  # off-topic gone; newest first


def test_rank_keeps_everything_when_there_is_too_little_to_judge_on():
    need = Need(kind="question", subject="?", query="", goal_terms=[])
    srcs = [_src("anything"), _src("else")]
    assert len(rank_evidence(srcs, need)) == 2
    assert len(rank_evidence(srcs, None)) == 2


def test_rank_dedupes_by_url_and_tolerates_missing_dates():
    need = Need(kind="goal", subject="s", query="backend listening", goal_terms=["backend", "listening"])
    srcs = [_src("backend listening guide", url="same"), _src("backend listening guide again", url="same"),
            _src("backend listening, undated", url="")]
    out = rank_evidence(srcs, need)
    assert len(out) == 2


# ── unsupported_specifics: values from nowhere ────────────────────────────

CORPUS = ("Question: how do I expose the media server\n"
          "[1] (web · published 2025-03-01 · https://example.org/guide) query: q\n"
          "Install release 24.04 on the host at 192.168.1.20; the service listens on 8096.\n"
          "## Project background\nThe game server uses port 8211.")


def test_values_present_in_the_grounding_are_not_flagged():
    ans = ("Open https://example.org/guide and install 24.04 on 192.168.1.20. "
           "The media service listens on port 8096 and the game server on 8211 [1].")
    assert unsupported_specifics(ans, CORPUS) == []


def test_values_from_memory_are_flagged_by_kind():
    ans = ("Download 22.04.3 from https://releases.example.com/22.04.3/ and put "
           "it on 192.168.0.1, then open port 32400.")
    found = {f["kind"]: f["value"] for f in unsupported_specifics(ans, CORPUS)}
    assert found["version"] == "22.04.3"
    assert found["url"].startswith("https://releases.example.com")
    assert found["ip"] == "192.168.0.1"
    assert found["port"] == "32400"


def test_well_known_ports_local_urls_and_placeholders_are_never_flagged():
    ans = ("Browse to http://192.168.1.20:8096/ or http://localhost:8096 — "
           "HTTPS is on port 443 and SSH on port 22. Set <SERVER_IP> yourself.")
    found = unsupported_specifics(ans, CORPUS)
    assert found == [], found


def test_templated_urls_and_example_addresses_are_not_claims():
    """§17.1029 — measured on stored walkthroughs: a URL with a shell variable
    is a template, and RFC 5737 / wildcard / loopback addresses are examples."""
    ans = ("curl -k https://${PVE_HOST}:8006/api2/json and https://{host}/x; "
           "bind to 0.0.0.0 or 127.0.0.1; the doc example uses 203.0.113.42 and 192.0.2.7")
    assert unsupported_specifics(ans, "") == []


def test_a_value_that_is_a_substring_of_another_is_not_credited():
    # 3001 must not pass because 13001 appears in the corpus
    found = unsupported_specifics("listen on port 3001", "the id is 13001")
    assert [f["value"] for f in found] == ["3001"]


def test_ip_octets_are_not_mistaken_for_versions():
    found = unsupported_specifics("host 10.0.0.5 runs it", "host 10.0.0.5")
    assert found == []


# ── §17.1028: a flagged value stays flagged until a source or the operator says it ──

LIVE_FOOTER_REPLY = ("## Fix\n2. Check the version:\n```bash\npm2 -v\n```\n"
                     "You should see a version number, for example `5.4.2`.\n\n---\n"
                     "⚠️ **Unverified specifics** — these values appear in no source, fact, or "
                     "note for this step; confirm each before relying on it: `5.4.2` (version)")


def test_flagged_values_are_parsed_back_from_the_footer():
    assert ev.flagged_values([LIVE_FOOTER_REPLY]) == {"5.4.2"}
    assert ev.flagged_values([{"content": LIVE_FOOTER_REPLY}, "no footer here"]) == {"5.4.2"}
    multi = LIVE_FOOTER_REPLY.replace("`5.4.2` (version)", "`5.4.2` (version), `192.168.0.1` (ip)")
    assert ev.flagged_values([multi]) == {"5.4.2", "192.168.0.1"}
    assert ev.flagged_values(None) == set()


def test_the_engines_own_prior_reply_does_not_credit_the_value():
    """Live: the fix reply that carried `5.4.2` under the footer was captured
    as a turn; the next ask reply repeated it with NO footer because the
    conversation block now contained it."""
    corpus_with_echo = "## Conversation\nYou (assistant): " + LIVE_FOOTER_REPLY
    found = unsupported_specifics("Run pm2 -v; you should see `5.4.2`.", corpus_with_echo,
                                  trusted="", flagged=ev.flagged_values([LIVE_FOOTER_REPLY]))
    assert [f["value"] for f in found] == ["5.4.2"]


def test_a_flagged_value_is_credited_once_a_source_or_the_operator_states_it():
    flagged = {"5.4.2"}
    src = "[1] (web · published 2026-09-01 · https://x.example/rel) pm2 5.4.2 released"
    assert unsupported_specifics("Install `5.4.2`.", src, trusted=src, flagged=flagged) == []
    op = "Question: it printed 5.4.2, is that current?"
    assert unsupported_specifics("Yes, `5.4.2`.", op, trusted=op, flagged=flagged) == []


def test_operator_text_keeps_only_the_operators_half():
    hist = [{"role": "user", "content": "it printed 3001"},
            {"role": "assistant", "content": "try port 8211"},
            {"role": "operator", "content": "ok"}]
    out = ev.operator_text(hist)
    assert "3001" in out and "ok" in out and "8211" not in out


@pytest.mark.asyncio
async def test_verify_answer_carries_a_flag_across_turns(no_judge, valves):
    """Turn 1 flags it; turn 2's corpus contains turn 1's reply; still flagged."""
    out1, rep1 = await verify_answer("You should see `5.4.2`.", sources=[], corpus="")
    assert rep1["annotated"] and "`5.4.2`" in out1
    flagged = ev.flagged_values([out1])
    out2, rep2 = await verify_answer("As before, expect `5.4.2`.", sources=[],
                                     corpus="You (assistant): " + out1, trusted="",
                                     flagged=flagged)
    assert rep2["annotated"] and "`5.4.2`" in out2


# ── §17.1030: a value a source confirmed earlier stays credited ──────────

def test_sourced_now_reports_what_this_turns_sources_state():
    src = [{"text": "npm registry: pm2 latest is 7.0.4 (2026-08-24)"}]
    got = ev.sourced_now("Expect `7.0.4`; also try 192.168.9.9", src)
    assert [g["value"] for g in got] == ["7.0.4"]
    assert ev.sourced_now("Expect `7.0.4`", []) == []


def test_a_value_sourced_on_an_earlier_turn_is_credited_without_refetching():
    """Live: `7.0.4` was cited from the npm registry on the ask turn, then
    flagged on the walkthrough because that turn's research did not refetch it."""
    assert unsupported_specifics("Expect `7.0.4`.", "", sourced={"7.0.4"}) == []
    assert [f["value"] for f in unsupported_specifics("Expect `7.0.4`.", "")] == ["7.0.4"]


def test_sourced_values_from_environment_reads_the_ledger():
    env = {"sourced_values": [{"value": "7.0.4", "kind": "version", "node_key": "T2"}, "1.2.3"]}
    assert ev.sourced_values_from_environment(env) == {"7.0.4", "1.2.3"}
    assert ev.sourced_values_from_environment({}) == set()


@pytest.mark.asyncio
async def test_verify_answer_reports_sourced_now_and_credits_the_ledger(no_judge, valves):
    src = [{"text": "release notes: version 7.0.4 is current"}]
    out, rep = await verify_answer("Install `7.0.4`.", sources=src, corpus="")
    assert not rep["annotated"] and [s["value"] for s in rep["sourced_now"]] == ["7.0.4"]
    out2, rep2 = await verify_answer("Still on `7.0.4`.", sources=[], corpus="",
                                     sourced={"7.0.4"})
    assert not rep2["annotated"] and rep2["unsupported"] == []
    out3, rep3 = await verify_answer("Try `9.9.9`.", sources=[{"text": "nothing here"}], corpus="")
    assert rep3["annotated"] and rep3["sourced_now"] == []


# ── §17.1031: the answer must address the question that was asked ────────

NODE_Q = ("what is the current Node.js LTS release version number on nodejs.org, "
          "so I can check node -v matches it?")
# the live reply run d4b2cdb9 gave to that question
PM2_WEB_REPLY = ("## 👉 Do this next\n\n**Run this now:**\n\n```bash\npm2 web\n```\n\n"
                 "Then tell me what it prints — the line with the URL will show the default port.\n\n"
                 "---\n\nI don't have the PM2 web documentation in my research, so I can't "
                 "confirm the default port from the provided sources.")
NODE_REPLY = ("## 👉 Do this next\n\n```bash\nnode -v\n```\n\nI can't give you the LTS "
              "version number from my research sources — none of them state it. Open "
              "nodejs.org and look for the LTS label.")


def test_the_live_wrong_answer_is_detected_and_the_right_one_passes():
    need = derive_need(NODE_Q, assume_question=True)
    assert ev.question_terms(need) >= {"node", "nodejs", "release", "version"}
    assert not ev.addresses_question(PM2_WEB_REPLY, need)
    assert ev.addresses_question(NODE_REPLY, need)


def test_the_check_is_lenient_and_only_applies_to_questions():
    # a question with fewer than two distinctive terms cannot be judged
    assert ev.addresses_question("anything", derive_need("why?", assume_question=True))
    # a terse direct answer is never judged — "Use 10.0.0.1" IS the answer
    q = derive_need("which address should the second computer connect to?", assume_question=True)
    assert ev.addresses_question("Use 10.0.0.1", q)
    long_wrong = " ".join(["unrelated"] * 30)
    assert not ev.addresses_question(long_wrong, q)
    # non-question needs are never judged
    assert ev.addresses_question("anything", Need(kind="goal", subject="backend not listening"))
    assert ev.addresses_question("anything", None)


@pytest.mark.asyncio
async def test_verify_answer_regenerates_an_offtopic_answer_with_the_question_restated(no_judge, valves):
    need = derive_need(NODE_Q, assume_question=True)
    notices = []

    async def regen(notice):
        notices.append(notice)
        return NODE_REPLY

    out, rep = await verify_answer(PM2_WEB_REPLY, sources=[], corpus="", need=need, regenerate=regen)
    assert rep["regenerated"] and not rep["annotated"] and not rep["off_question"]
    assert "did NOT address the question" in notices[0] and NODE_Q in notices[0]
    assert out == NODE_REPLY


@pytest.mark.asyncio
async def test_a_still_offtopic_answer_is_visibly_flagged(no_judge, valves):
    need = derive_need(NODE_Q, assume_question=True)

    async def regen(notice):
        return PM2_WEB_REPLY

    out, rep = await verify_answer(PM2_WEB_REPLY, sources=[], corpus="", need=need, regenerate=regen)
    assert rep["annotated"] and rep["off_question"]
    assert "may not answer what you asked" in out


def test_the_synthesis_prompt_ends_with_the_question():
    """§17.1031 — the conversation block used to be the last thing the model
    read; the question is now restated after it. Source-scan, both draws."""
    import inspect
    from app.modules import assist_research_lib as lib
    src = inspect.getsource(lib.research_one)
    assert src.count("Answer THIS question (not an earlier one in the") == 2


# ── §17.1032: a URL on the operator's own host is not a product claim ────

def test_owned_hosts_come_from_the_operators_ledger_only():
    env = {"facts": ["Caddy serves defrusciohomelab.duckdns.org on 192.168.1.20"],
           "substitutions": {"PVE": "pve.home.arpa", "N": "5"}, "profile": "root@pve"}
    notes = [{"text": "the router admin page is at router.local"}]
    hosts = ev.owned_hosts(env, notes)
    assert {"defrusciohomelab.duckdns.org", "pve.home.arpa", "router.local"} <= hosts
    assert "192.168.1.20" not in hosts
    assert ev.owned_hosts({}, None) == set()


def test_a_url_on_an_owned_host_is_credited_but_a_public_one_is_not():
    """Live: the fix draft's `https://…duckdns.org/panel/` was flagged because
    the exact URL was not in the ledger, though the domain is the operator's."""
    owned = {"defrusciohomelab.duckdns.org"}
    ans = ("curl -I https://defrusciohomelab.duckdns.org/panel/ and then "
           "curl -L https://github.com/Radarr/Radarr/releases/download/v9.9.9/x.tar.gz")
    found = unsupported_specifics(ans, "the source mentions github.com in passing", owned=owned)
    assert [f["value"] for f in found] == ["https://github.com/Radarr/Radarr/releases/download/v9.9.9/x.tar.gz"]


@pytest.mark.asyncio
async def test_verify_answer_takes_owned_hosts(no_judge, valves):
    out, rep = await verify_answer("Open https://defrusciohomelab.duckdns.org/panel/ now.",
                                   sources=[], corpus="", owned_hosts={"defrusciohomelab.duckdns.org"})
    assert not rep["annotated"]


# ── §17.1033: an interpreter run on a file of another language ───────────

LIVE_SHAPE_DRAFT = ("## Fix\n2. Start it with the virtual environment:\n\n```bash\n"
                    "pct exec 111 -- bash -c 'cd /opt/control-panel-backend\n"
                    "source /opt/control-panel-venv/bin/activate\n"
                    "nohup python3 /opt/control-panel-backend/server.js > /var/log/control-panel.log 2>&1 &'\n"
                    "```\n\nReplace `server.js` with the actual filename you saw.")


def test_the_live_python3_server_js_command_is_flagged():
    got = ev.command_shape_issues(LIVE_SHAPE_DRAFT)
    assert len(got) == 1
    assert got[0]["command"] == "python3 /opt/control-panel-backend/server.js"
    assert got[0]["expects"] == ["py", "pyw"]


@pytest.mark.parametrize("ok", [
    "```bash\npython3 app.py\n```", "```bash\nnode server.js\n```",
    "```bash\nbash install.sh\n```", "```bash\npython3 -m http.server 8080\n```",
    "```bash\npython3 -c 'print(1)'\n```", "```bash\nnode --version\n```",
    "```bash\nsudo /usr/bin/python3 /opt/x/main.py\n```",
    "```bash\npython3 setup\n```",            # no extension: unknown, not judged
    "```bash\nnode config.json\n```",       # not a script extension: not judged
    "run `pm2 start server.js`",               # pm2 is not an interpreter
])
def test_matching_and_unjudgeable_commands_are_not_flagged(ok):
    assert ev.command_shape_issues(ok) == [], ok


@pytest.mark.parametrize("bad, cmd", [
    ("```bash\nnode app.py\n```", "node app.py"),
    ("```bash\nbash tool.py\n```", "bash tool.py"),
    ("run `python3 build.sh` now", "python3 build.sh"),
    ("```sh\nsh /opt/a/run.js\n```", "sh /opt/a/run.js"),
])
def test_cross_language_pairings_are_flagged(bad, cmd):
    assert [g["command"] for g in ev.command_shape_issues(bad)] == [cmd]


@pytest.mark.asyncio
async def test_verify_answer_regenerates_a_mismatched_command_then_flags(no_judge, valves):
    notices = []

    async def regen_fixed(notice):
        notices.append(notice)
        return LIVE_SHAPE_DRAFT.replace("server.js", "app.py")

    out, rep = await verify_answer(LIVE_SHAPE_DRAFT, sources=[], corpus="", regenerate=regen_fixed)
    assert rep["regenerated"] and not rep["annotated"] and rep["command_shape"] == []
    assert "CANNOT work as written" in notices[0] and "python3 /opt/control-panel-backend/server.js" in notices[0]

    async def regen_same(notice):
        return LIVE_SHAPE_DRAFT

    out2, rep2 = await verify_answer(LIVE_SHAPE_DRAFT, sources=[], corpus="", regenerate=regen_same)
    assert rep2["annotated"] and "Command mismatch" in out2 and "`python3 /opt/control-panel-backend/server.js`" in out2


# ── §17.1034: a value only the plan vouches for is noted, not passed silently ──

def test_ledger_text_is_the_operators_own_records_only():
    env = {"facts": ["Proxmox host at 192.168.1.156"], "profile": "root@pve",
           "substitutions": {"DOMAIN": "home.example"},
           "system_state": {"106": {"kind": "vm"}}}
    t = ev.ledger_text(env, [{"text": "router is at 10.0.0.1"}])
    for s in ("192.168.1.156", "root@pve", "DOMAIN=home.example", "10.0.0.1", "106"):
        assert s in t
    assert ev.ledger_text({}, None) == ""


@pytest.mark.asyncio
async def test_a_plan_only_value_is_noted_and_a_confirmed_one_is_not(no_judge, valves):
    """Live: `192.168.1.1` was in the plan text and node outputs, in no fact,
    note or source — it passed silently; now it is noted."""
    plan = "Task: configure the router at 192.168.1.1 and expose the panel on 3001"
    ans = "Type 192.168.1.1 into the primary field, then check port 3001."
    out, rep = await verify_answer(ans, sources=[], corpus=plan, confirmed="")
    assert not rep["annotated"] and rep["unsupported"] == []
    assert [u["value"] for u in rep["plan_only"]] == ["192.168.1.1", "3001"]
    assert "From the plan, not yet confirmed" in out and "`192.168.1.1`" in out
    out2, rep2 = await verify_answer(ans, sources=[], corpus=plan,
                                     confirmed="facts: gateway 192.168.1.1; backend listens on 3001")
    assert rep2["plan_only"] == [] and "From the plan" not in out2
    out3, rep3 = await verify_answer(ans, sources=[{"text": "router 192.168.1.1, port 3001"}],
                                     corpus=plan, confirmed="")
    assert rep3["plan_only"] == []


@pytest.mark.asyncio
async def test_plan_only_never_regenerates_and_unsupported_still_wins(no_judge, valves):
    calls = []

    async def regen(notice):
        calls.append(notice)
        return "unchanged"

    plan = "Task: router at 192.168.1.1"
    out, rep = await verify_answer("Use 192.168.1.1.", sources=[], corpus=plan, confirmed="",
                                   regenerate=regen)
    assert calls == [] and rep["plan_only"] and not rep["annotated"]
    out2, rep2 = await verify_answer("Use 10.9.9.9.", sources=[], corpus=plan, confirmed="")
    assert [u["value"] for u in rep2["unsupported"]] == ["10.9.9.9"] and rep2["plan_only"] == []


# ── §17.1036: documentation outranks forums; interface labels need a source ──

def test_an_encyclopedia_wiki_path_is_not_product_documentation():
    assert ev.source_authority("https://en.wikipedia.org/wiki/Documentation") < ev._DOC_AUTHORITY
    assert ev.source_authority("https://help.ui.com/hc/en-us/articles/1-Port-Forwarding") >= ev._DOC_AUTHORITY


def test_a_long_questions_own_nouns_are_relevance_terms():
    q = ("In the UniFi Network application on a Dream Machine Pro (the current interface), give me "
         "the exact click path and field names to forward TCP 443 from the WAN to 192.168.1.40, and "
         "tell me whether the rule needs a matching firewall rule created separately.")
    terms = ev.content_terms(derive_need(q, assume_question=True))
    assert {"forward", "firewall", "unifi"} <= terms


@pytest.mark.asyncio
async def test_documentation_sources_fall_back_to_snippets_when_the_fetched_doc_page_is_offtopic(monkeypatch):
    """The live chain: help centre 403s, an off-topic encyclopedia page is
    fetched, and the on-topic documentation snippet must still be returned."""
    from app.modules import assist_research_lib as lib
    need = derive_need("In the UniFi Network application, exact click path to forward TCP 443 "
                       "and whether a firewall rule is created", assume_question=True)

    async def deep(q, top_n, skip=None):
        return [{"query": q, "kind": "web", "url": "https://en.wikipedia.org/wiki/Documentation",
                 "text": "Documentation is any communicable material used to describe a system", "date": ""}]

    async def structured(q, max_results=5):
        return [{"title": "UniFi Gateway Port Forwarding", "url": "https://help.ui.com/hc/en-us/articles/1",
                 "content": "Create a port forwarding rule: Settings, Firewall & Security, Port Forwarding", "date": ""},
                {"title": "Forum thread", "url": "https://forums.example.net/t/1", "content": "port forward help", "date": ""}]
    monkeypatch.setattr(lib, "_deep_web_sources", deep)
    monkeypatch.setattr(lib, "_searxng_structured", structured)
    monkeypatch.setattr(lib.settings, "assist_research_fetch_top_n", 4)
    extra = await lib._documentation_sources(need, "unifi port forward tcp 443", [])
    assert [e["url"] for e in extra] == ["https://help.ui.com/hc/en-us/articles/1"]
    assert extra[0]["kind"] == "searxng" and ev.max_source_authority(extra) >= ev._DOC_AUTHORITY


def test_the_verifier_owns_the_footer_namespace():
    """Live: a fresh answer echoed an earlier reply's "No official documentation"
    footer while its own retrieval HAD documentation; the verifier had not
    flagged it. Footer-shaped lines in a draft are stripped before checking."""
    draft = ("Open Settings → Firewall.\n\n---\nℹ️ **No official documentation was retrieved for this** — "
             "verify on screen.\n\n---\n⚠️ **Unverified specifics** — `9.9.9` (version)")
    out = ev.strip_verifier_footers(draft)
    assert out == "Open Settings → Firewall."
    assert ev.strip_verifier_footers("plain answer") == "plain answer"


@pytest.mark.asyncio
async def test_an_echoed_footer_is_removed_and_only_the_true_one_reappears(no_judge, valves):
    need = derive_need("where is the firewall setting and which tab?", assume_question=True)
    draft = ("Open the Firewall tab, click the Options button, tick the Enable checkbox, set the "
             "dropdown to In, then click Add in the Rules panel and pick a menu entry.\n\n---\n"
             "ℹ️ **No official documentation was retrieved for this** — the labels above come from general knowledge.")
    docs = [{"kind": "searxng", "url": "https://docs.example.com/firewall", "text": "firewall tab options rules"}]
    out, rep = await verify_answer(draft, sources=docs, corpus="", need=need)
    assert not rep["unsourced_interface"]
    assert "No official documentation" not in out and out.count("---") == 0


def test_pages_about_the_word_documentation_are_skipped_before_any_fetch():
    from app.modules.assist_research_lib import _about_the_word
    assert _about_the_word({"url": "https://en.wikipedia.org/wiki/Documentation", "title": "Documentation"})
    assert _about_the_word({"url": "https://www.merriam-webster.com/dictionary/documentation", "title": "Documentation Definition"})
    assert _about_the_word({"url": "https://scribe.com/library/what-is-documentation", "title": "Documentation"})
    assert not _about_the_word({"url": "https://help.ui.com/hc/en-us/articles/1-Port-Forwarding", "title": "UniFi Gateway - Port Forwarding"})
    assert not _about_the_word({"url": "https://docs.example.com/documentation/firewall", "title": "Firewall documentation"})


@pytest.mark.asyncio
async def test_the_docs_fetch_never_spends_a_fetch_on_an_encyclopedia_page(monkeypatch):
    from app.modules import assist_research_lib as lib
    fetched_urls = []

    async def structured(q, max_results=5):
        return [{"title": "Documentation", "url": "https://en.wikipedia.org/wiki/Documentation", "content": "x"},
                {"title": "Vendor docs", "url": "https://docs.example.com/x", "content": "port forward"}]

    async def fake_fetch(results):
        fetched_urls.extend(r["url"] for r in results)
        return [{"url": r["url"], "content": "port forward documentation " * 10, "date": ""} for r in results]
    monkeypatch.setattr(lib, "_searxng_structured", structured)
    import app.modules.research_agent as ra
    monkeypatch.setattr(ra, "_fetch_and_extract", fake_fetch)
    out = await lib._deep_web_sources("documentation port forward", top_n=2, skip=lib._about_the_word)
    assert fetched_urls == ["https://docs.example.com/x"]
    assert [o["url"] for o in out] == ["https://docs.example.com/x"]


def test_documentation_shaped_urls_carry_authority():
    assert ev.source_authority("https://pve.example.com/wiki/Firewall") >= ev._DOC_AUTHORITY
    assert ev.source_authority("https://docs.example.com/x") >= ev._DOC_AUTHORITY
    assert ev.source_authority("https://example.com/admin-guide/net") >= ev._DOC_AUTHORITY
    assert ev.source_authority("https://forum.example.com/thread/1") < ev._DOC_AUTHORITY
    assert ev.source_authority("https://reddit.com/r/x/y") < ev._DOC_AUTHORITY
    assert ev.source_authority("") == 0.0


def test_rank_prefers_documentation_at_equal_relevance_then_recency():
    need = Need(kind="question", subject="firewall levels", query="firewall levels enable",
                goal_terms=["firewall", "levels"])
    forum = _src("firewall levels thread on the forum", url="https://forum.example.com/t/9", date="2025-09-01")
    docs = _src("firewall levels in the admin guide", url="https://pve.example.com/wiki/Firewall", date="2022-01-01")
    out = rank_evidence([forum, docs], need)
    assert out[0]["url"].endswith("/wiki/Firewall") and out[0]["authority"] >= ev._DOC_AUTHORITY


@pytest.mark.asyncio
async def test_interface_specifics_with_no_documentation_get_the_honesty_note(no_judge, valves):
    need = derive_need("where is the firewall option in the admin panel and which tab?", assume_question=True)
    ans = ("Open the Firewall tab, click the Options button, tick the Enable checkbox, then in the "
           "Rules panel click Add and pick the direction from the dropdown menu.")
    out, rep = await verify_answer(ans, sources=[{"kind": "web", "url": "https://forum.example.com/t/1",
                                                  "text": "forum chatter"}], corpus="", need=need)
    assert rep["unsourced_interface"] and "No official documentation was retrieved" in out
    out2, rep2 = await verify_answer(ans, sources=[{"kind": "web", "url": "https://docs.example.com/firewall",
                                                    "text": "the firewall tab and options"}], corpus="", need=need)
    assert not rep2["unsourced_interface"] and "No official documentation" not in out2
    # a non-interface answer, or a non-question need, never gets the note
    out3, rep3 = await verify_answer("Run the command and paste what it prints.", sources=[], corpus="", need=need)
    assert not rep3["unsourced_interface"]


def test_the_ask_path_runs_a_documentation_query_when_none_was_fetched():
    import inspect
    from app.modules import assist_research_lib as lib
    src = inspect.getsource(lib.research_one)
    assert "_documentation_sources(" in src
    assert src.index("_documentation_sources(") < src.index("sources = rank_evidence(")
    hsrc = inspect.getsource(lib._documentation_sources)
    # §17.1037 — the documentation words must survive the 12-word cap: they lead.
    assert '_cap_query("documentation "' in hsrc and ".split()[:10]" in hsrc
    # §17.1037c — authority is judged on the RANKED (on-topic) set, both sides
    assert "max_source_authority(rank_evidence(list(sources), need))" in hsrc
    assert "fetched = rank_evidence(await _deep_web_sources(doc_q, top_n=2, skip=_about_the_word), need)" in hsrc
    assert "max_source_authority(fetched) < _DOC_AUTHORITY" in hsrc
    # and a documentation-grade snippet fallback exists for pages that extract to nothing
    assert "_searxng_structured(doc_q" in hsrc and "source_authority(r[\"url\"]) >= _DOC_AUTHORITY" in hsrc


def test_documentation_query_keeps_its_keywords_on_a_long_question():
    from app.modules.assist_research_lib import _cap_query
    base = "docker compose restart unless-stopped host reboot stop exit code 1 deployment approach"
    q = _cap_query("documentation " + " ".join(base.split()[:10]))
    assert q.startswith("documentation") and len(q.split()) <= 12


def test_brief_hostnames_are_operator_owned():
    env = {"facts": [], "_brief_text": "Deploy Nextcloud reachable at cloud.example-home.net behind Traefik"}
    assert "cloud.example-home.net" in ev.owned_hosts(env, None)
    assert unsupported_specifics("Open https://cloud.example-home.net/login", "",
                                 owned=ev.owned_hosts(env, None)) == []


def test_the_funnel_carries_the_brief_text_and_the_reader_never_persists_it():
    import inspect
    from app.modules import assist_agent
    src = inspect.getsource(assist_agent.assemble_generation_memory)
    assert '"_brief_text"' in src and "_brief_text(_brief)" in src
    from app.modules.assist_environment import _environment_from_metadata
    assert "_brief_text" not in _environment_from_metadata({"environment": {"_brief_text": "x"}})


# ── verify_answer: regenerate once, then annotate ─────────────────────────

@pytest.fixture
def no_judge(monkeypatch):
    async def _none(answer, sources):
        return None
    monkeypatch.setattr(ev, "citation_report", _none)


@pytest.fixture
def valves(monkeypatch):
    monkeypatch.setattr(ev.settings, "assist_answer_verification_enabled", True)
    monkeypatch.setattr(ev.settings, "assist_answer_verification_regenerate", True)
    monkeypatch.setattr(ev.settings, "assist_answer_min_citation_score", 0.6)


@pytest.mark.asyncio
async def test_a_clean_answer_passes_untouched(no_judge, valves):
    ans = "Install 24.04 on 192.168.1.20 [1]."
    out, rep = await verify_answer(ans, sources=[{"text": "x"}], corpus=CORPUS, confirmed=CORPUS)
    assert out == ans
    assert rep["checked"] and not rep["annotated"] and not rep["regenerated"]


@pytest.mark.asyncio
async def test_a_regeneration_that_fixes_it_replaces_the_answer(no_judge, valves):
    calls = []

    async def regen(notice):
        calls.append(notice)
        return "Install 24.04 on 192.168.1.20 — check the release page for the current build."

    out, rep = await verify_answer("Install 22.04.3 on 192.168.1.20.", sources=[],
                                   corpus=CORPUS, regenerate=regen)
    assert rep["regenerated"] and not rep["annotated"]
    assert "22.04.3" not in out
    assert "`22.04.3`" in calls[0] and "GROUNDING NOTICE" in calls[0]


@pytest.mark.asyncio
async def test_a_regeneration_that_still_fails_is_annotated_not_trusted(no_judge, valves):
    async def regen(notice):
        return "Install 22.04.3 on 192.168.1.20 anyway."

    out, rep = await verify_answer("Install 22.04.3 on 192.168.1.20.", sources=[],
                                   corpus=CORPUS, regenerate=regen)
    assert rep["annotated"]
    assert "Unverified specifics" in out and "`22.04.3`" in out


@pytest.mark.asyncio
async def test_a_declined_regeneration_falls_through_to_annotation(no_judge, valves):
    async def regen(notice):
        return ""  # e.g. the fix path's integrity gate rejected the candidate

    out, rep = await verify_answer("Open port 32400.", sources=[], corpus=CORPUS,
                                   regenerate=regen)
    assert rep["annotated"] and not rep["regenerated"]
    assert "`32400`" in out


@pytest.mark.asyncio
async def test_a_strictly_better_candidate_is_kept_even_if_not_clean(no_judge, valves):
    async def regen(notice):
        return "Install 22.04.3 (verify on the release page)."  # one value left, one dropped

    out, rep = await verify_answer("Install 22.04.3 on 192.168.0.1.", sources=[],
                                   corpus=CORPUS, regenerate=regen)
    assert rep["regenerated"] and rep["annotated"]
    assert "192.168.0.1" not in out and "`22.04.3`" in out


@pytest.mark.asyncio
async def test_weak_citations_annotate_when_nothing_can_regenerate(monkeypatch, valves):
    async def judge(answer, sources):
        return {"score": 0.25, "supported": 1, "total": 4, "unsupported_citations": []}
    monkeypatch.setattr(ev, "citation_report", judge)
    out, rep = await verify_answer("Do the thing [1]. And the other [2].",
                                   sources=[{"text": "a"}, {"text": "b"}], corpus="thing other")
    assert rep["annotated"]
    assert "Weak sourcing" in out and "3 of 4" in out


@pytest.mark.asyncio
async def test_the_valve_turns_it_off(monkeypatch, no_judge):
    monkeypatch.setattr(ev.settings, "assist_answer_verification_enabled", False)
    ans = "Install 22.04.3 from memory."
    out, rep = await verify_answer(ans, sources=[], corpus="")
    assert out == ans and not rep["checked"]


# ── the research block carries dates ─────────────────────────────────────

def test_research_block_shows_publication_dates_and_the_freshness_rule():
    from app.modules.assist_research_lib import _render_research_block
    block = _render_research_block([
        _src("newer text", url="https://a.example/x", date="2025-08-10T00:00:00"),
        _src("undated text", url="https://b.example/y"),
    ])
    assert "published 2025-08-10" in block
    assert "[2] (web · https://b.example/y)" in block
    assert "prefer the newer one" in block


# ── §17.1030: the ledger round-trips through the environment deserializer ──

def test_sourced_values_round_trip_through_the_environment_reader():
    """§17.881b lesson: set_environment writes the WHOLE env dict back, so a
    key the reader drops is erased by the next fact fold."""
    from app.modules.assist_environment import _environment_from_metadata
    md = {"environment": {"facts": ["x"],
                          "sourced_values": [{"value": "7.0.4", "kind": "version", "node_key": "T2"}]}}
    env = _environment_from_metadata(md)
    assert env["sourced_values"] == [{"value": "7.0.4", "kind": "version", "node_key": "T2"}]
    assert _environment_from_metadata({"environment": {"sourced_values": "junk"}})["sourced_values"] == []
