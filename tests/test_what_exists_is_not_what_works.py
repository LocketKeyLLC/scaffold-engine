"""§17.1262b/1263 — a catalogue says what EXISTS; the web says what still works.

Two halves of one defect the operator named: *"this is the exact reason for the
researcher component to be wired up to fixing issues. It should find the most
current up to date and available indexers."*

  §17.1262b  the step-drafting lookup must carry the operator's SYSTEM into the
             query, not just the step's own words (the §17.1020 rule, which the
             first cut of §17.1262 broke on arrival);
  §17.1263   a rejected REQUEST and an unreachable THING are not the same
             failure. §17.1258 stops a block that repeats its own mistake; a
             list of dead third parties is not that, and stopping on the first
             corpse adds none of the ones that work.
"""
import pathlib
import re
from datetime import datetime, timezone

import pytest

from app.modules import supervised_runs as sr

_ADD115 = {
    "node_key": "ADD115",
    "title": "Add the most current working public indexers to Prowlarr",
    "description": "Find which public trackers are actually available right now and add those.",
}


# ───────────────────────────────── §17.1263 — rejected vs unreachable

def _lines(ok: int, fail: int, err: str) -> str:
    return "\n".join([f"added: Tracker{i}" for i in range(ok)]
                     + [f"failed: Thing{i} - HTTP Error {err}" for i in range(fail)])


def test_a_repeated_rejection_still_fails_the_step():
    """§17.1258's case, unchanged: 88 identical 400s is one diagnosis thrown away."""
    why = sr.repeated_identical_failures(_lines(0, 88, "400: Bad Request"))
    assert why and "88 times" in why and "RESPONSE BODY" in why


def test_a_rejection_fails_even_when_some_landed():
    """A malformed body is OURS to fix whether or not anything else worked."""
    assert sr.repeated_identical_failures(_lines(3, 10, "400: Bad Request"))


def test_dead_third_parties_do_not_fail_a_step_that_landed_things():
    """54 dead trackers after 35 were added is a finished step, not a stuck one."""
    assert sr.repeated_identical_failures(_lines(35, 54, "502: Bad Gateway")) is None


@pytest.mark.parametrize("err", ["502: Bad Gateway", "503: Service Unavailable",
                                 "504: Gateway Timeout"])
def test_every_unreachable_status_is_treated_the_same(err):
    assert sr.repeated_identical_failures(_lines(5, 9, err)) is None


def test_everything_unreachable_is_still_reported():
    """Nothing landed and all 54 timed out — that is a shared cause (DNS, live
    ADD116), and the operator must be told."""
    why = sr.repeated_identical_failures(_lines(0, 54, "502: Bad Gateway"))
    assert why and "54 times" in why


def test_the_rule_reaches_the_drafter():
    """A detector that fails a step must have told the drafter the rule first."""
    rules = sr.CHANNEL_RULES
    assert "WHAT EXISTS, NOT WHAT WORKS" in rules
    low = rules.lower()
    assert "skip it" in low and "keep going" in low


# ───────────────────────────────── §17.1262b — the query carries the system

def test_no_currency_wording_means_no_lookup():
    node = {"node_key": "ADD50", "title": "Start container 111",
            "description": "`pct start 111`."}
    assert sr.currency_question(node) == ""


@pytest.mark.asyncio
async def test_the_step_lookup_grounds_in_the_operators_machine(monkeypatch):
    """The query must name the operator's hardware, not just the step's words.

    §17.1021 measured an LLM generator dropping a model number three times when
    merely asked for it; `finalize_query` is where the request becomes a rule.
    """
    seen = {}

    async def _fake_research_one(**kw):
        seen.update(kw)
        return {"sources": [{"url": "https://example.test/a", "title": "t",
                             "snippet": "s", "authority": 0.9}]}

    import app.modules.assist_research_lib as lib
    monkeypatch.setattr(lib, "research_one", _fake_research_one)
    monkeypatch.setattr(lib, "_render_research_block", lambda s: "## Current sources\n- a")

    env = {"profile": "The Proxmox host has an RTX 3090 graphics card.\n"
                      "Prowlarr runs in container 103."}
    out = await sr.research_for_step(_ADD115, env)

    assert "Current sources" in out
    q = seen["question"]
    assert q, "nothing was searched"
    # the step's own subject survives …
    assert "indexer" in q.lower()
    # … and the environment reached the retrieval call rather than being dropped
    assert seen["prerequisite_env"] == env
    assert "Proxmox" in (seen["context_hint"] or "")


@pytest.mark.asyncio
async def test_a_failed_lookup_leaves_the_prompt_alone(monkeypatch):
    async def _boom(**kw):
        raise RuntimeError("searxng down")

    import app.modules.assist_research_lib as lib
    monkeypatch.setattr(lib, "research_one", _boom)
    assert await sr.research_for_step(_ADD115, {"profile": "x"}) == ""


def test_the_lookup_is_grounded_by_an_approved_builder():
    """The §17.1020 inventory gate enumerates the builders; this names WHICH one
    this path uses, so a refactor that drops it fails here too."""
    import inspect
    src = inspect.getsource(sr.research_for_step)
    assert "derive_need(" in src and "finalize_query(" in src
    assert re.search(r"research_one\(question=query", src), \
        "the grounded query must be what is actually searched"


# ───────────────────────────────── §17.1262c — ask a currency question

_REAL_ADD115 = {
    "node_key": "ADD115",
    "title": "Add every public indexer Prowlarr listed, and connect it to Radarr and Sonarr",
    "description": ("ADD96's output is the authoritative list: 88 public indexer definitions this "
                    "Prowlarr ships. Add as many as possible; which ones are still working is the "
                    "question the schema cannot answer."),
}


def test_the_question_is_about_the_thing_not_the_task():
    """Measured live: the query that went out was the step's title, so five
    how-to pages came back for a question about what is still alive."""
    q = sr.currency_question(_REAL_ADD115)
    assert "indexer" in q.lower() and "prowlarr" in q.lower()
    assert "still working" in q.lower()
    assert str(datetime.now(timezone.utc).year) in q
    for task_word in ("add", "connect"):
        assert task_word not in q.lower().split(), f"{task_word!r} is the work, not the subject"


def test_only_the_first_clause_is_the_subject():
    """"…, and connect it to Radarr and Sonarr" is a second job; the currency
    question belongs to the first."""
    assert "radarr" not in sr.currency_question(_REAL_ADD115).lower()


def test_a_latest_step_asks_for_a_version_not_liveness():
    q = sr.currency_question({
        "title": "Install the NVIDIA driver 580.173.02 on the Proxmox host",
        "description": "Use the latest driver available for this card."})
    assert "latest version" in q.lower() and "still working" not in q.lower()
    assert "nvidia" in q.lower()


def test_a_step_about_this_machine_still_asks_nothing():
    """§17.1262's narrowness survives §17.1262c: reading the host beats a search."""
    assert sr.currency_question({
        "title": "Give the media-stack containers working DNS (point them at Pi-hole)",
        "description": "pct set each container --nameserver 192.168.1.30"}) == ""


# ───────────────────────────────── §17.1265 — a step that cannot be checked

_CLEVER = ('curl -s http://192.168.1.21:9696/api/v1/indexer -H "X-Api-Key: $(pct exec 102 -- cat '
           '/var/lib/prowlarr/config.xml | sed -n \'s/.*<ApiKey>\\(.*\\)<\\/ApiKey>.*/\\1/p\')" '
           '| python3 -c "import sys,json; print(len(json.load(sys.stdin)))"')


def _runbook(verify_body: str) -> str:
    return ("## Run this\n\n```bash\npct exec 102 -- systemctl restart prowlarr\n```\n\n"
            f"## Verify\n\n{verify_body}\n\n## Rollback\n\n- none\n")


def test_the_draft_that_actually_parked_unverifiable_is_caught():
    """§17.1265's REAL input, saved from the live frame. The first cut of this
    detector asked whether a candidate used `$(…)` or ran long -- which was the
    first draft's reason and not this one's. This draft piped into an
    interpreter and ran a script it had just written, the detector stayed silent,
    and the step parked unverifiable a second time."""
    rb = (pathlib.Path(__file__).parent / "fixtures" / "add115_verify_section.txt").read_text(encoding="utf-8")
    assert sr.verify_commands(rb) == [], "precondition: nothing survives extraction"
    note = sr.verify_not_runnable(rb)
    assert note and "CANNOT BE RUN" in note
    assert "is not a check of anything" in note or "pipes into an interpreter" in note, \
        "the refusal must NAME why, not just say no"


def test_the_real_dropped_check_is_caught():
    """ADD115's FIRST draft: the same empty `verify`, a different reason."""
    rb = _runbook(f"- Indexer count in Prowlarr: `{_CLEVER}`")
    assert sr.verify_commands(rb) == [], "precondition: the channel does refuse this"
    note = sr.verify_not_runnable(rb)
    assert note and "CANNOT BE RUN" in note
    assert "$(...)" in note and "Split it" in note
    assert "## Verify" in note, "the redraft needs the shape it should produce"
    assert "Keep ## Run this exactly as it is" in note


def test_a_runnable_check_is_left_alone():
    rb = _runbook("- how many indexers: `curl -s http://192.168.1.21:9696/api/v1/indexer`")
    assert sr.verify_commands(rb), "precondition: this one survives extraction"
    assert sr.verify_not_runnable(rb) == ""


def test_expected_values_in_backticks_are_not_commands():
    """The false positive this must not have: a Verify section whose backticks
    hold what to EXPECT rather than what to run."""
    rb = _runbook("- `pct status 130` - expect `status: running`")
    assert sr.verify_not_runnable(rb) == ""


def test_a_verifyless_runbook_is_not_blamed_for_a_substitution():
    """No Verify section at all is a different defect (§17.1227's family); this
    check must stay silent rather than invent a reason."""
    assert sr.verify_not_runnable("## Run this\n\n```bash\npct start 130\n```\n") == ""


def test_the_verify_redraft_is_wired_and_trades_up():
    """A detector nobody calls is the §17.906 defect; and a redraft that loses
    the commands must never replace a good draft (§17.1211)."""
    import ast as _ast
    src = (pathlib.Path(sr.__file__).parent / "execution_agent.py").read_text(encoding="utf-8")
    assert "supervised_runs.verify_not_runnable(runbook)" in src
    i = src.index("verify_not_runnable(runbook)")
    window = src[i:i + 1400]
    assert '_vf.get("commands") and _vf.get("verify")' in window, "must trade UP on both"
    _ast.parse(src)


# ───────────────────── §17.1266 — whose fault, and does the caller hear it

_CLOUDFLARE = "Unable to access 16mag.net, blocked by CloudFlare Protection."


def test_the_real_cloudflare_body_is_an_availability_failure():
    """Measured on the live run: ADD115's second indexer came back with this,
    the block called it a validation failure and stopped at 2 of 89."""
    assert sr._UNREACHABLE.search(_CLOUDFLARE)


@pytest.mark.parametrize("body", [
    "'App Profile Id' must be greater than '0'",
    "Should be unique",
    "Name must not be empty",
])
def test_a_named_field_is_still_our_mistake(body):
    """The widened classes must not swallow the errors that MUST stop a step —
    those are the ones one diagnosis fixes for all 89."""
    assert not sr._UNREACHABLE.search(body)


@pytest.mark.parametrize("body", [
    "Unable to connect to indexer",
    "The request timed out",
    "502 Bad Gateway",
    "blocked by CloudFlare Protection",
    "captcha required",
    "certificate has expired",
])
def test_every_third_party_class_is_recognised(body):
    assert sr._UNREACHABLE.search(body)


def test_the_drafter_is_told_to_read_the_body_not_match_phrases():
    rules = sr.CHANNEL_RULES
    assert "WHOSE FAULT IT IS" in rules
    assert "propertyName" in rules, "the shape discriminator, not a phrase list"
    low = rules.lower()
    assert "skip it" in low and "stop at the first one" in low


def test_the_decide_response_carries_what_happened():
    """§17.1261 put the run's detail in the function; the response_model dropped
    every field of it, so a caller got `failed` and nothing else."""
    from app.schemas import DecideResult
    fields = set(DecideResult.model_fields)
    for f in ("executed", "verify", "reason", "diagnosis", "unknown_outcome"):
        assert f in fields, f"DecideResult silently drops {f!r} from resolve_run"


def test_every_run_detail_key_is_declared_on_the_surface():
    """The drift guard: a new key on either terminal path of `resolve_run` must
    reach the API, or §17.1261 regresses quietly the way it did once."""
    import ast as _ast
    from app.schemas import DecideResult
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    tree = _ast.parse(src)
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and n.name == "resolve_run")
    emitted: set[str] = set()
    for node in _ast.walk(fn):
        if isinstance(node, _ast.Return) and isinstance(node.value, _ast.Dict):
            for k in node.value.keys:
                if isinstance(k, _ast.Constant) and isinstance(k.value, str):
                    emitted.add(k.value)
    # Consumed by resolve_decision itself rather than reported onwards.
    internal = {"outcome", "node_key", "record", "node_status", "choices"}
    # A key may reach the operator either as a response field or in the route's
    # own error detail — both are surfaces; neither being true is the defect.
    route = (pathlib.Path(sr.__file__).parents[1] / "routers" / "jobs.py").read_text(encoding="utf-8")
    i = route.index("async def decide_endpoint(")
    detail = route[i:route.index("\n@router.", i + 1) if "\n@router." in route[i:] else len(route)]
    undeclared = sorted(k for k in emitted - internal - set(DecideResult.model_fields)
                        if f'"{k}"' not in detail)
    assert not undeclared, (
        "resolve_run returns these and neither DecideResult nor the route's error "
        f"detail names them, so no caller ever sees them: {undeclared}")


def test_the_spa_says_why_instead_of_pointing_elsewhere():
    """§17.1266 — the engine knew the reason and told the operator to go find it."""
    spa = (pathlib.Path(sr.__file__).parents[1] / "ui" / "static" / "views" / "theater.js")
    src = spa.read_text(encoding="utf-8")
    assert "res.reason" in src, "the reason reaches the response; the surface must use it"
    i = src.index("res.reason")
    assert "stopped:" in src[i:i + 600]


# ─────────────── §17.1267 — a budget failure, and a response that is filled

_TIMED_OUT_REASON = ("supervised run stopped — `python3 /tmp/add_indexers.py` "
                     "exited None:  (timed out after 180s)")


def test_a_timeout_is_told_apart_from_a_mistake():
    """Live ADD115: the script classified every refusal correctly and was killed
    at 180s partway through 89 indexers. Without this the next draft reads
    "exited None: (timed out)" and has no reason to write anything different."""
    fb = sr.attempt_feedback({"last_verification_reason": _TIMED_OUT_REASON, "output_text": ""})
    assert "TIME BUDGET, NOT A MISTAKE" in fb
    assert "180s" in fb, "the budget it blew must be named"
    assert "RESUMABLE" in fb and "20 items per" in fb, "the remedy must be concrete"


def test_an_ordinary_failure_gets_no_budget_lecture():
    fb = sr.attempt_feedback({
        "last_verification_reason": "supervised run stopped — `pct start 111` exited 255: already running",
        "output_text": ""})
    assert fb and "TIME BUDGET" not in fb


def test_the_drafter_knows_the_budget_before_it_writes():
    """§17.1189's lesson: tell the prompt the consumer's constraints. The drafter
    put 89 remote connection tests in one command because nothing said it had
    180 seconds."""
    rules = sr.CHANNEL_RULES
    assert "ONE COMMAND GETS 180 SECONDS" in rules
    low = rules.lower()
    assert "batches" in low and "resumable" in low


def test_the_budget_in_the_rules_matches_the_one_enforced():
    """A number written in a prompt and a number enforced in code drift; live,
    the rule would be a lie the first time the timeout changed."""
    from app.modules.assist_supervised import RUN_COMMAND_TIMEOUT_S
    assert f"ONE COMMAND GETS {int(RUN_COMMAND_TIMEOUT_S)} SECONDS" in sr.CHANNEL_RULES


def test_the_route_fills_the_response_it_declares():
    """§17.1266 declared the fields and this construction still named six, so the
    response carried `reason: null` on a run that had a reason — the same defect
    one layer up. A model cannot invent a value the handler never passes."""
    route = (pathlib.Path(sr.__file__).parents[1] / "routers" / "jobs.py").read_text(encoding="utf-8")
    i = route.index("async def decide_endpoint(")
    body = route[i:]
    j = body.index("return DecideResult(")
    construction = body[j:body.index(")", body.index("confirmed_after_drop", j)) + 1]
    for field in ("executed", "verify", "reason", "diagnosis", "unknown_outcome", "confirmed_after_drop"):
        assert f"{field}=outcome.get(" in construction, f"the route never fills {field!r}"


# ───────────── §17.1268 — work that cannot finish in the time it is given

def _fixture(name: str) -> str:
    return (pathlib.Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8").strip()


def test_the_real_unbounded_script_is_refused():
    """§17.1267 put the 180s budget in the prompt; the very next draft looped
    over all 89 indexers again AND added `time.sleep(1)` inside the loop. A rule
    the draft ignores is not a fail-safe."""
    found = sr.loops_the_network_without_a_budget([_fixture("add115_unbounded_loop.txt")])
    assert found, "the draft that would certainly time out must not be offered"
    why = found[0]["why"]
    assert "enumerate(public)" in why, "name the collection it cannot bound"
    assert "sleep inside the loop" in why, "the aggravating factor is worth saying"
    assert "[:20]" in why and "resumable" in why, "the remedy must be concrete"
    assert "180 seconds" in why


def test_a_literal_list_is_bounded_even_through_a_name():
    """The apps half of this very step: `apps = [radarr, sonarr]` then `for app
    in apps:` is two items and must keep working."""
    cmd = ("printf '%s\\n' 'import json, urllib.request' "
           "'apps = [{\"name\": \"Radarr\"}, {\"name\": \"Sonarr\"}]' "
           "'for app in apps:' '    urllib.request.urlopen(\"http://x/api\", json.dumps(app).encode())' "
           "| tee /tmp/add_apps.py")
    assert sr.loops_the_network_without_a_budget([cmd]) == []


def test_a_name_assigned_a_response_too_is_not_bounded():
    """A name that is ALSO assigned something else has whatever size that was."""
    cmd = ("printf '%s\\n' 'import urllib.request' 'items = [1, 2]' 'items = fetch()' "
           "'for i in items:' '    urllib.request.urlopen(\"http://x\")' | tee /tmp/x.py")
    assert sr.loops_the_network_without_a_budget([cmd])


def test_an_author_chosen_slice_is_allowed():
    cmd = ("printf '%s\\n' 'import urllib.request' 'public = fetch()' 'for e in public[:20]:' "
           "'    urllib.request.urlopen(\"http://x\")' | tee /tmp/b.py")
    assert sr.loops_the_network_without_a_budget([cmd]) == []


def test_a_loop_that_touches_nothing_remote_is_allowed():
    cmd = "printf '%s\\n' 'for p in paths:' '    print(open(p).read())' | tee /tmp/l.py"
    assert sr.loops_the_network_without_a_budget([cmd]) == []


def test_the_budget_is_named_once_for_prose_and_gate():
    """A number in a prompt and a number in a refusal drift; both come from one
    constant, and that constant must match what actually kills the command."""
    from app.modules.assist_supervised import RUN_COMMAND_TIMEOUT_S
    assert sr._RUN_BUDGET_S == int(RUN_COMMAND_TIMEOUT_S)
    assert f"ONE COMMAND GETS {sr._RUN_BUDGET_S} SECONDS" in sr.CHANNEL_RULES


def test_the_gate_reaches_the_frame():
    """A detector nobody calls is the §17.906 defect — it must join the refusals
    that turn Run off and trigger the §17.1196 redraft."""
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("def frame_run(")
    assert "loops_the_network_without_a_budget(cmds, shape_files)" in src[i:], \
        "the gate must be called from frame_run, not merely exist"


# ──────── §17.1269 — a refusal nobody can redraft is a dead end for the operator

def _refusal_producers() -> dict:
    """The functions `frame_run` adds to `refused` — `refused + NAME(cmds)`."""
    import ast as _ast
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    tree = _ast.parse(src)
    frame = next(n for n in _ast.walk(tree)
                 if isinstance(n, _ast.FunctionDef) and n.name == "frame_run")
    names: set[str] = set()
    for node in _ast.walk(frame):
        if isinstance(node, _ast.BinOp) and isinstance(node.op, _ast.Add) \
                and isinstance(node.right, _ast.Call):
            fn = getattr(node.right.func, "id", None)
            if fn:
                names.add(fn)
    bodies = {n.name: n for n in _ast.walk(tree)
              if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and n.name in names}
    return bodies


def test_the_refusal_producers_are_discoverable():
    """If this finds nothing, the gate below is vacuous."""
    assert len(_refusal_producers()) >= 4, sorted(_refusal_producers())


@pytest.mark.parametrize("name", sorted(_refusal_producers()))
def test_every_shape_refusal_can_be_redrafted(name):
    """§17.1269 — `shape_retry_note` matches a refusal by its TEXT against
    `_SHAPE_REFUSALS`. A producer whose wording matches nothing there gets no
    redraft, so the operator is handed a greyed-out Run and no way forward.

    Live, §17.1268's first outing: the budget refusal was correct, unregistered,
    and therefore a dead end — `suggested: myself`, options `myself, skip`.
    """
    import ast as _ast
    fn = _refusal_producers()[name]
    literals = " ".join(n.value for n in _ast.walk(fn)
                        if isinstance(n, _ast.Constant) and isinstance(n.value, str))
    assert any(s in literals for s in sr._SHAPE_REFUSALS), (
        f"{name}() refuses the engine's own block and none of its wording is in "
        f"_SHAPE_REFUSALS, so shape_retry_note cannot recognise it and no redraft "
        f"will happen. Add the distinctive phrase to the registry.")


def test_the_budget_refusal_reaches_a_redraft():
    """End to end on the real script: refused, recognised, and the note tells the
    drafter what to do instead."""
    frame = {"kind": "run", "refused": sr.loops_the_network_without_a_budget(
        [_fixture("add115_unbounded_loop.txt")])}
    assert frame["refused"], "precondition: the real script is refused"
    note = sr.shape_retry_note(frame)
    assert note, "an unregistered refusal produces no redraft — the §17.1269 dead end"
    assert "batches" in note and "[:20]" in note
    assert f"{sr._RUN_BUDGET_S} seconds" in note


def test_the_verify_redraft_never_trades_a_runnable_block_for_a_refused_one():
    """§17.1269 — my own §17.1265 redraft swapped a correct budget refusal for a
    draft of invalid Python: two unrunnable frames, the second less informative."""
    src = (pathlib.Path(sr.__file__).parent / "execution_agent.py").read_text(encoding="utf-8")
    i = src.index("verify_not_runnable(runbook)")
    assert 'frame.get("refused") else supervised_runs.verify_not_runnable' in src[i - 200:i + 60], \
        "the verify redraft must not run on an already-refused frame"
    assert '_vf.get("verify") and not _vf.get("refused")' in src[i:i + 1600], \
        "and must not accept a replacement that is itself refused"


# ───── §17.1270 — fix the provable mistake, prove the fix, and say you made it

def test_the_quoting_mistake_that_killed_three_drafts_is_repaired():
    """The REAL refused command, saved from the live frame. Three of four drafts
    for this step died on `print(f"added: {entry[\\"name\\"]}")` — §17.1257 caught
    it every time, the rule and a worked example were both in the prompt, and the
    redraft made the same mistake again."""
    cmd = _fixture("add115_escaped_quotes.txt")
    assert sr.payload_will_not_compile([cmd]), "precondition: it really does not compile"
    fixed, repairs = sr.repair_shell_quoted_payloads([cmd])
    assert repairs, "the engine must correct what is provably a mistake"
    assert not sr.payload_will_not_compile(fixed), "and the repair must be PROVEN by compiling"
    assert "single-quoted shell word" in repairs[0]["why"]


def test_a_payload_broken_some_other_way_is_left_alone():
    """The repair is only ever applied where the quoting proves the backslash
    cannot have been meant; anything else stays refused with its own reason."""
    bad = "printf '%s\\n' 'def f(' | tee /tmp/x.py"
    fixed, repairs = sr.repair_shell_quoted_payloads([bad])
    assert fixed == [bad] and repairs == []
    assert sr.payload_will_not_compile(fixed), "§17.1257 still owns this one"


def test_a_valid_command_is_untouched():
    ok = 'printf \'%s\\n\' \'print("hi")\' | tee /tmp/ok.py'
    assert sr.repair_shell_quoted_payloads([ok]) == ([ok], [])


def test_a_backslash_outside_single_quotes_is_not_touched():
    """`"\\""` in a double-quoted word IS an escape and means something."""
    cmd = 'curl -s -H "X-Api-Key: \\"abc\\"" http://x/api'
    assert sr._unescape_in_single_quotes(cmd) == cmd


def test_the_operator_is_told_the_script_was_corrected():
    """§17.1270 — an approval is for what is on screen. A repaired command is a
    changed command, so the frame carries it and the card says so."""
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("def frame_run(")
    assert '"engine_fixed": [r["why"] for r in _repairs]' in src[i:]
    spa = (pathlib.Path(sr.__file__).parents[1] / "ui" / "static" / "views" / "theater.js")
    js = spa.read_text(encoding="utf-8")
    assert "d.engine_fixed" in js and "corrected" in js


def test_the_repair_runs_before_anything_is_refused_for_it():
    """Order matters: repair, then judge. The other way round refuses a command
    the engine was about to fix."""
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("def frame_run(")
    body = src[i:]
    assert body.index("repair_shell_quoted_payloads(cmds)") < body.index("refused + payload_will_not_compile(cmds)")
