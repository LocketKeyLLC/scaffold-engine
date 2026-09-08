"""§17.980 — the §17.950-979 arc, exercised against the real engine.

Operator: *"how do we bridge the gap of 17.950-979?"*

Everything in that arc was verified three ways — unit tests, replays of the
recorded transcript, and pty experiments — and none of it by the engine actually
running. That gap is not closable by more unit tests, because every defect in
the arc lived in WIRING: §17.951's confirm path returned the right value and was
discarded, §17.979's repairs existed and the SPA producer never called them,
§17.971's exemption re-derived intent from a string it had written itself. Unit
tests passed throughout.

So these run against the real router, the real Postgres, the real environment
store and the real turn plumbing. Only the MODEL is stubbed — the verifier's
verdict and the generator's text — because the arc is about what the engine does
with a verdict, not about producing one. That is the same line the existing
integration suite draws with ``replan_policy='disabled'``.

What this bridges, and what it does not:

  BRIDGED   the confirm path commits through the real router and mirrors to
            dag_nodes (§17.971); the file ledger survives a real jsonb
            round-trip and answers correctly afterwards (§17.965/967); the
            hypothesis ledger reads real assist_turns rows through its real SQL
            (§17.973/977).

  NOT       whether a live model, handed the new grounding, writes better
            queries or a better Diagnosis. That needs the operator, and no test
            substitutes for it. It is named here so the gap stays visible
            instead of looking closed.

Operator text below is taken VERBATIM from session 613dd1df — the messages that
produced the defects — never invented.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from unittest.mock import AsyncMock, patch

from app.database import async_session
from app.modules import assist_agent


@pytest_asyncio.fixture
async def assist_session(insert_job):
    """A real job, DAG and assist session, claimed at T1."""
    job_id = await insert_job(
        status="planning", title="live operator loop",
        refined_brief={"description": "stand up a control panel", "goals": ["g"]},
    )
    async with async_session() as db:
        await db.execute(
            text("""
                INSERT INTO dag_nodes (job_id, node_key, title, depends_on,
                                       execution_order, prompt_template, tool, domain)
                VALUES (:jid, 'T1', 'Configure the reverse proxy', '{}', 1,
                        'prompt for T1', 'LLM', 'eng')
            """), {"jid": job_id})
        await db.commit()
    async with async_session() as db:
        out = await assist_agent.start_assist_session(
            job_id=job_id, replan_policy="disabled", db=db)
        sid = out["session_id"]
        # Claim T1 so a submit has a `presented` step to act on.
        await assist_agent.get_next_step(session_id=sid, db=db)
    return {"job_id": job_id, "session_id": sid}


# ── §17.971 — the operator's word must survive the real router ───────────
#
# Live T35, 18:53:55: the confirm path submitted, the §17.890 exemption
# re-derived intent from the string it had just written, said False because that
# string carries a paste, and the verify hard-block held. The log recorded
# `completion_confirm_not_committed` and the offer was re-staged. Three times.


@pytest.mark.validate
@pytest.mark.asyncio
@pytest.mark.timeout(900)
async def test_a_verify_blocked_submit_really_does_block(assist_session):
    """The control: without the flag, an `incomplete` verdict must NOT commit.
    If this ever passes vacuously the test below proves nothing."""
    from app.routers.assist import AssistSubmitInput, assist_submit

    with patch("app.modules.assist_agent.verify_submit_outcome",
               new=AsyncMock(return_value={"outcome": "incomplete",
                                           "reason": "no evidence of TLS"})):
        async with async_session() as db:
            res = await assist_submit(
                assist_session["session_id"],
                AssistSubmitInput(node_key="T1", output="root@pve:~# cat x\nsome output"),
                db=db,
            )
    assert res["committed"] is False
    assert res["status"] == "step_incomplete"


@pytest.mark.validate
@pytest.mark.asyncio
@pytest.mark.timeout(900)
async def test_operator_affirmed_commits_through_the_real_router(assist_session):
    """§17.971 — the same verdict, with the affirmation carried as DATA."""
    from app.routers.assist import AssistSubmitInput, assist_submit

    # Verbatim from turn 1810, the message that kept being discarded.
    said = ("i think its complete due to the following:\n\n"
            'root@pve:~# pct exec 120 -- sh -c "cat /etc/caddy/Caddyfile"\n'
            "jellyfin.local {\n  reverse_proxy 192.168.1.101:8096\n}")
    with patch("app.modules.assist_agent.verify_submit_outcome",
               new=AsyncMock(return_value={"outcome": "incomplete",
                                           "reason": "no evidence of TLS"})):
        async with async_session() as db:
            res = await assist_submit(
                assist_session["session_id"],
                AssistSubmitInput(
                    node_key="T1",
                    output=f"Operator confirmed this step is complete: {said}",
                    operator_affirmed=True),
                db=db,
            )
    # A committed submit does not carry the blocked-path keys — the absence of
    # `status: step_incomplete` IS the signal, and the DB below is the proof.
    assert res.get("status") != "step_incomplete", res

    # And the §17.486 mirror invariant still holds — this is what makes every
    # downstream read (compile, digest, RAG grounding) see the step as done.
    async with async_session() as db:
        row = (await db.execute(
            text("SELECT status FROM dag_nodes WHERE job_id = :j AND node_key = 'T1'"),
            {"j": assist_session["job_id"]})).scalar()
        step = (await db.execute(
            text("SELECT status FROM assist_steps "
                 " WHERE session_id = :s AND node_key = 'T1'"),
            {"s": assist_session["session_id"]})).scalar()
    assert row == "done"
    assert step == "committed"


# ── §17.965/967 — the ledger has to survive the real store ───────────────


@pytest.mark.validate
@pytest.mark.asyncio
@pytest.mark.timeout(900)
async def test_the_file_ledger_round_trips_through_postgres(assist_session):
    """A jsonb merge that silently dropped `body` or `sha` would make §17.967
    and §17.968 answer confidently and wrongly — and both were built on the
    assumption that this survives."""
    from app.modules.assist_agent import get_environment, set_environment
    from app.modules.assist_files import (
        content_fingerprint, parse_file_writes, verified_files)

    reply = ("```bash\npct exec 111 -- bash -c \"cat > /opt/a/server.js\" <<'EOF'\n"
             "const PVE_TOKEN_ID = '<PVE_TOKEN_ID>';\napp.listen(3001);\nEOF\n```")
    written = parse_file_writes(reply)
    assert written, "the fixture reply must parse as a write"

    sid = assist_session["session_id"]
    async with async_session() as db:
        await set_environment(session_id=sid, file_writes=written, db=db)
        env = await get_environment(session_id=sid, db=db)

    rec = (env or {}).get("file_writes", {}).get("/opt/a/server.js")
    assert rec, env
    assert rec["sha"] == written["/opt/a/server.js"]["sha"]
    assert rec["body"], "§17.968 needs the text; §17.967 threw it away once already"

    # The operator pastes the file back unchanged -> VERIFIED, and §17.967's
    # gate then forbids rewriting it.
    async with async_session() as db:
        await set_environment(session_id=sid,
                              file_contents={"/opt/a/server.js": rec["body"]}, db=db)
        env2 = await get_environment(session_id=sid, db=db)
    assert verified_files(env2.get("file_writes")) == ["/opt/a/server.js"]
    assert content_fingerprint(rec["body"]) == rec["sha"]


@pytest.mark.validate
@pytest.mark.asyncio
@pytest.mark.timeout(900)
async def test_the_contract_checker_reads_the_stored_artefacts(assist_session):
    """§17.968/972 end to end: what was stored is what gets cross-checked."""
    from app.modules.assist_agent import get_environment, set_environment
    from app.modules.assist_contracts import (
        bounded_artefacts, find_contract_conflicts)
    from app.modules.assist_files import parse_file_writes

    server = ("```bash\ncat > /opt/a/server.js <<'EOF'\n"
              "const PVE_TOKEN_ID = '<PVE_TOKEN_ID>';\n"
              "app.get('/status', (req, res) => { const results = {};"
              " res.json(results); });\napp.listen(3001);\nEOF\n```")
    sid = assist_session["session_id"]
    async with async_session() as db:
        await set_environment(session_id=sid,
                              file_writes=parse_file_writes(server), db=db)
        env = await get_environment(session_id=sid, db=db)

    conflicts = find_contract_conflicts(bounded_artefacts(env.get("file_writes")))
    kinds = {c["kind"] for c in conflicts}
    assert "placeholder" in kinds, conflicts   # §17.972 — the live PVE token
    assert any("PVE_TOKEN_ID" in c["detail"] for c in conflicts)


# ── §17.973/977 — the ledger reads real turns through its real SQL ───────


@pytest.mark.validate
@pytest.mark.asyncio
@pytest.mark.timeout(900)
async def test_hypotheses_are_harvested_from_real_turn_rows(assist_session):
    """The per-step gate and the project view both derive on read. If the query
    or the row shape is wrong they return empty and fail SILENTLY — which is
    exactly how the playbook's `ruled_out` stayed at 0 for a whole session."""
    from app.modules.assist_hypotheses import (
        cross_step_eliminated, harvest, harvest_session)

    sid = assist_session["session_id"]
    turns = [
        ("T1", "## Diagnosis\nThe App.jsx file is corrupted.\n## Fix\nx"),
        ("T1", "## Diagnosis\nThe backend is missing from PM2 entirely.\n## Fix\nx"),
        ("T2", "## Diagnosis\nThe heredoc terminator never arrived intact.\n## Fix\nx"),
        # A second fix on T2 is what ELIMINATES the first — one diagnosis alone
        # is still under test (§17.973), which the first draft of this test got
        # wrong and the run corrected.
        ("T2", "## Diagnosis\nThe container has no route to the gateway.\n## Fix\nx"),
    ]
    async with async_session() as db:
        for nk, content in turns:
            await db.execute(
                text("""
                    INSERT INTO assist_turns (session_id, node_key, role, kind, content)
                    VALUES (:s, :nk, 'assistant', 'fix', :c)
                """), {"s": sid, "nk": nk, "c": content})
        await db.commit()

        rows = (await db.execute(
            text("""
                SELECT node_key, content FROM assist_turns
                 WHERE session_id = :s AND node_key IS NOT NULL
                   AND role = 'assistant' AND kind = 'fix'
                 ORDER BY created_at ASC, id ASC
            """), {"s": sid})).mappings().all()

    same_step = harvest([r["content"] for r in rows if r["node_key"] == "T1"])
    assert same_step["eliminated"] == ["The App.jsx file is corrupted."]

    ledgers = harvest_session([(r["node_key"], r["content"]) for r in rows])
    assert cross_step_eliminated(ledgers, "T1") == [
        ("T2", "The heredoc terminator never arrived intact.")]
