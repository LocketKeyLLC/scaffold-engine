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
    out, rep = await verify_answer(ans, sources=[{"text": "x"}], corpus=CORPUS)
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
