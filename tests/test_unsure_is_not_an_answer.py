"""§17.1218 — "unsure" is not an answer, and the planner must not be told it is.

Refinement asks the operator each ambiguity and stores the replies in
`user_feedback`, which `prompt_assembly` hands the planner under the heading
"Operator answers (already decided — honor, do not re-ask)". Nothing read them.

Live, three of eight ambiguities on the homelab job:

    Q: Preferred tech stack and hosting for the custom UI control panel
    A: unsure

The planner was then explicitly told not to re-ask, and emitted "Build control
panel backend" / "Build control panel frontend" for a component whose entire
shape was the thing the operator had just said they did not know.

    "no questions or assistance in making the control panel … but was never
     asked and never able to edit."
"""
from __future__ import annotations

import pytest

from app.modules import unanswered as ua

# verbatim from the operator's job
FEEDBACK = """Q: Model/type of the additional graphics card already in the server
A: I am unsure

Q: What 'outside access' should look like (VPN only, reverse proxy, or both)
A: able to access and interact with the media, video game server and research.

Q: Preferred tech stack and hosting for the custom UI control panel
A: unsure

Q: Storage layout for the 2x 6TB SAS HDDs (ZFS mirror/RAID, passthrough, etc.)
A: yes we can do that.

Q: Existing network topology: ISP router, current subnets
A: modem to Wifi Router to server.

Q: Specific AI workloads/tooling desired
A: all"""

BRIEF = {
    "user_feedback": FEEDBACK,
    "ambiguities": [
        "Model/type of the additional graphics card already in the server",
        "What 'outside access' should look like (VPN only, reverse proxy, or both)",
        "Preferred tech stack and hosting for the custom UI control panel",
        "Storage layout for the 2x 6TB SAS HDDs (ZFS mirror/RAID, passthrough, etc.)",
        "Existing network topology: ISP router, current subnets",
        "Specific AI workloads/tooling desired",
        "Whether the 600GB SSD is confirmed as the Proxmox OS disk",   # never put to them
    ],
}


@pytest.mark.parametrize("a", [
    "unsure", "Unsure", "I am unsure", "i'm not sure", "not sure", "no idea",
    "don't know", "dont know", "I do not know", "idk", "dunno", "?", "n/a",
    "none", "tbd", "whatever", "up to you", "you decide", "", "   ",
])
def test_the_ways_people_say_they_do_not_know(a):
    assert ua.is_non_answer(a) is True, a


@pytest.mark.parametrize("a", [
    "all",
    "yes we can do that.",
    "modem to Wifi Router to server.",
    "able to access and interact with the media, video game server and research.",
    "It is where proxmox os is installed.",
    "unsure of the model but it is a Tesla P40",     # starts with the word, still an answer
    "P40",
])
def test_a_short_answer_is_still_an_answer(a):
    """A false positive re-asks what the operator already settled, which is its
    own kind of not listening."""
    assert ua.is_non_answer(a) is False, a


def test_the_q_a_blob_parses_in_order():
    pairs = ua.parse_feedback(FEEDBACK)
    assert [p["answered"] for p in pairs] == [False, True, False, True, True, True]
    assert pairs[2]["question"].startswith("Preferred tech stack")


def test_the_control_panel_stays_open_and_so_does_one_never_asked():
    open_items = ua.unresolved(BRIEF)
    assert "Preferred tech stack and hosting for the custom UI control panel" in open_items
    assert "Model/type of the additional graphics card already in the server" in open_items
    # asked but never answered at all is open too
    assert "Whether the 600GB SSD is confirmed as the Proxmox OS disk" in open_items
    # the ones they DID answer are settled
    assert "Specific AI workloads/tooling desired" not in open_items
    assert "Existing network topology: ISP router, current subnets" not in open_items


def test_a_fully_answered_brief_leaves_nothing_open():
    brief = {"ambiguities": ["Specific AI workloads/tooling desired"], "user_feedback": FEEDBACK}
    assert ua.unresolved(brief) == []


# ── what the planner is told ─────────────────────────────────────────────

def test_the_note_asks_for_a_decision_node_not_a_choice():
    note = ua.decision_brief(["Preferred tech stack for the control panel"])
    assert "Do NOT choose for them" in note
    assert "`decision` node" in note and "BEFORE any step that depends" in note
    assert "cannot correct" in note, "say what a guess costs the operator"


def test_no_open_items_no_note():
    assert ua.decision_brief([]) == ""


def test_the_planner_no_longer_hears_a_non_answer_as_decided():
    import inspect
    from app.modules import prompt_assembly as pa
    src = inspect.getsource(pa)
    i = src.index('feedback = (brief.get("user_feedback") or "").strip()')
    blk = src[i:i + 1400]
    assert "unanswered import" in blk, blk
    assert 'p["answered"]' in blk, "only real answers may go under 'already decided'"
    assert "decision_brief(unresolved(brief))" in blk, "the rest must be named as still owed"


def test_a_blob_in_an_unknown_shape_is_passed_through_whole():
    """Never silently drop feedback the parser does not recognise."""
    import inspect
    from app.modules import prompt_assembly as pa
    assert "elif not pairs:" in inspect.getsource(pa)


# ── §17.1220 — enforced, not requested ───────────────────────────────────

PLAN = [
    {"node_key": "T6", "title": "Configure Proxmox firewall rules", "node_type": "task"},
    {"node_key": "T33", "title": "Build control panel backend", "node_type": "task"},
    {"node_key": "T34", "title": "Build control panel frontend", "node_type": "task"},
    {"node_key": "T28", "title": "Install NVIDIA drivers", "node_type": "task"},
]
BRIEF_OPEN = {
    "ambiguities": ["Preferred tech stack and hosting for the custom UI control panel"],
    "user_feedback": "Q: Preferred tech stack and hosting for the custom UI control panel\nA: unsure",
}


def test_no_node_is_inserted_when_a_step_will_ask_it():
    """§17.1221 — the operator's design: the plan stays about the work, and the
    step that needs the answer asks for it on arrival."""
    assert ua.ensure_decisions(PLAN, BRIEF_OPEN) == []


def test_a_question_no_step_touches_still_gets_a_node():
    """The backstop. Undecided AND unraised would be decided by silence."""
    brief = {"ambiguities": ["Whether backups should go offsite"],
             "user_feedback": "Q: Whether backups should go offsite\nA: unsure"}
    added = ua.ensure_decisions(PLAN, brief)
    assert len(added) == 1 and added[0]["node_type"] == "decision"
    assert added[0]["_waiters"] == []


def test_the_steps_that_build_the_undecided_thing_are_found():
    assert set(ua.dependent_steps(PLAN, ua._keywords(
        "Preferred tech stack and hosting for the custom UI control panel"))) == {"T33", "T34"}


def test_unrelated_steps_are_not_matched():
    keys = ua._keywords("Preferred tech stack and hosting for the custom UI control panel")
    assert "T6" not in ua.dependent_steps(PLAN, keys)
    assert "T28" not in ua.dependent_steps(PLAN, keys)


def test_a_decision_the_planner_already_wrote_is_not_duplicated():
    plan = PLAN + [{"node_key": "D1", "node_type": "decision",
                    "title": "Decide the control panel stack and hosting"}]
    assert ua.ensure_decisions(plan, BRIEF_OPEN) == []


def test_an_answered_brief_inserts_nothing():
    brief = {"ambiguities": ["Preferred tech stack for the control panel"],
             "user_feedback": "Q: Preferred tech stack for the control panel\nA: a simple web page"}
    assert ua.ensure_decisions(PLAN, brief) == []


def test_the_inserted_node_guides_rather_than_demanding_a_spec():
    # the orphan case — a question no step touches, so a node IS inserted
    brief = {"ambiguities": ["Whether backups should go offsite"],
             "user_feedback": "Q: Whether backups should go offsite\nA: unsure"}
    d = ua.ensure_decisions(PLAN, brief)[0]["description"]
    assert "not expected to know the technology" in d
    assert "Do NOT ask them to supply a specification" in d and "do NOT pick for them" in d
    assert "2-4 concrete options" in d and "everyday" in d
    assert "what they want to be able to DO" in d, "narrow it rather than re-asking the same thing"


def test_the_generator_inserts_and_rewires_before_validating():
    import inspect
    from app.modules import dag_generator as dg
    src = inspect.getsource(dg)
    assert "ensure_decisions(normalized, brief or {})" in src
    assert 'normalized.insert(0, _a)' in src, "a decision must come before what waits on it"
    assert src.index("ensure_decisions(normalized") < src.index("plan_coverage import uncovered"), \
        "rewire before the coverage warning reads the plan"


# ── §17.1221 — the question arrives with the step ────────────────────────

def test_the_control_panel_step_carries_its_own_question():
    qs = ua.questions_for_step("Build control panel backend", BRIEF_OPEN)
    assert qs == ["Preferred tech stack and hosting for the custom UI control panel"]


def test_an_unrelated_step_carries_none():
    assert ua.questions_for_step("Configure Proxmox firewall rules", BRIEF_OPEN) == []
    assert ua.questions_for_step("Install NVIDIA drivers", BRIEF_OPEN) == []


def test_an_answered_brief_puts_no_question_on_any_step():
    brief = {"ambiguities": ["Preferred tech stack for the control panel"],
             "user_feedback": "Q: Preferred tech stack for the control panel\nA: a simple web page"}
    assert ua.questions_for_step("Build control panel backend", brief) == []


def test_the_ask_block_opens_with_the_question_not_the_work():
    b = ua.ask_first_block(["Preferred tech stack for the control panel"])
    assert "ASK BEFORE YOU BUILD" in b and "Open with the question, not the work" in b
    assert "NOT expected to know the technology" in b
    assert "2-4 concrete options" in b and "what they want to be able to DO" in b
    assert "cannot correct" in b


def test_no_question_no_block():
    assert ua.ask_first_block([]) == ""


def test_every_step_prompt_goes_through_the_one_place_that_asks():
    import inspect
    from app.modules import prompt_assembly as pa
    src = inspect.getsource(pa.build_base_prompt)
    assert "ask_first_block(questions_for_step(" in src, src
    assert "except Exception:" in src, "a prompt must never fail to assemble over this"


# ── §17.1251: a passing mention must not attach a question to a step ──────


ROUTER_AMB = "Proxmox VE version and current storage/network configuration"
PANEL_AMB = "Preferred tech stack and hosting for the custom UI control panel"


def _brief(amb):
    return {"ambiguities": [amb], "user_feedback": f"Q: {amb}\nA: Unsure"}


def test_a_question_does_not_attach_to_a_step_it_has_nothing_to_do_with():
    """Live ADD112 ("Point the Spectrum router's DNS at Pi-hole"): the title
    shares ZERO words with the Proxmox-configuration ambiguity. The three that
    matched — proxmox, network, configuration — all came from a description
    sentence saying that anything needing the Proxmox host belongs in a DIFFERENT
    step. The step then opened by asking it, and flagged the mismatch itself:
    "even though it appears unrelated to the router-configuration step"."""
    title = "Point the Spectrum router's DNS at Pi-hole so every device uses it"
    desc = ("If something genuinely has to be checked on the Proxmox host, that belongs in its "
            "own step. network configuration")
    assert ua.questions_for_step(title, _brief(ROUTER_AMB), description=desc) == []


def test_the_case_this_was_built_for_still_works():
    """§17.1221's whole purpose — the control-panel question reaching the
    control-panel step — must survive the tightening."""
    assert ua.questions_for_step("Rebuild the control panel to do what was chosen in ADD99",
                              _brief(PANEL_AMB))
    assert ua.questions_for_step("Build control panel backend", _brief(PANEL_AMB),
                              description="the control panel stack")


def test_a_step_whose_subject_IS_the_question_still_gets_it():
    assert ua.questions_for_step("Reconcile the Proxmox storage and network configuration",
                              _brief(ROUTER_AMB))


def test_one_title_word_alone_is_not_enough():
    """The threshold is still two shared words overall — the title just has to
    contribute at least one of them."""
    assert ua.questions_for_step("Configure Proxmox firewall rules", _brief(ROUTER_AMB)) == []


def test_the_description_can_corroborate_but_not_carry():
    title = "Install the control panel stack"
    # title gives 'control'/'panel'; description adds nothing it needs
    assert ua.questions_for_step(title, _brief(PANEL_AMB))
    # a title with no shared word cannot be rescued by the description
    assert ua.questions_for_step("Install Jellyfin", _brief(PANEL_AMB),
                              description="tech stack hosting custom UI control panel") == []
