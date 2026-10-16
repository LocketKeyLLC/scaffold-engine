"""§17.1606 — a guest's login the operator never pinned for that guest comes from the homelab's one account.

Live (job 0fb4ea7f T2, 2026-10-15): the frame asked for `AI_USER` while the engine held `PALWORLD_USER` and the
homelab-wide `MASS_PASSWORD`; the operator: "The engine already has ALL of that information."
"""
from __future__ import annotations

from app.modules.runbook_inputs import homelab_account, suggest_inputs

ENV = {"substitutions": {"PALWORLD_USER": "operator1", "PALWORLD_IP": "10.9.8.44", "JELLYFIN_IP": "10.9.8.20"}}
INPUT = {"name": "AI_USER", "hint": "the account inside the guest that has sudo", "value": "", "secret": False}


def test_the_unpinned_guest_login_is_filled_with_the_homelab_account_and_says_why():
    out = suggest_inputs([INPUT], ENV)[0]
    assert out["value"] == "operator1"
    assert out["suggestions"][0]["confidence"] == "homelab" and "PALWORLD_USER" in out["suggestions"][0]["source"]


def test_two_different_accounts_are_a_real_question():
    env = {"substitutions": {"PALWORLD_USER": "operator1", "MEDIA_USER": "media"}}
    assert homelab_account("AI_USER", env) is None
    assert suggest_inputs([INPUT], env)[0]["value"] == ""


def test_the_guests_own_pin_wins_and_non_user_inputs_are_untouched():
    env = {"substitutions": {"PALWORLD_USER": "operator1", "AI_USER": "ml"}}
    assert suggest_inputs([INPUT], env)[0]["value"] == "ml"
    assert homelab_account("AI_IP", ENV) is None
    assert homelab_account("AI_USER", {"substitutions": {}}) is None


# §17.1607 — a job with no ledger of its own reads its owner's newest one
def test_a_job_without_a_ledger_inherits_its_owners():
    import asyncio
    from app.modules import runbook_inputs as ri

    ledger = {"environment": {"facts": ["CT 111 is the control panel"], "substitutions": {"PALWORLD_USER": "operator1"}}}

    class Res:
        def __init__(self, row): self.row = row
        def mappings(self): return self
        def first(self): return self.row
        def all(self): return []

    class DB:
        async def execute(self, stmt, params=None):
            sql = str(stmt)
            if "FROM assist_sessions WHERE job_id" in sql:
                return Res(None)                      # the job has no session of its own
            if "SELECT metadata FROM jobs" in sql:
                return Res({"metadata": {}})          # nor a ledger on its row
            if "JOIN jobs j" in sql:
                return Res({"id": "sess-1", "metadata": ledger})
            return Res(None)

    env = asyncio.run(ri._job_environment_ledger(DB(), "job-1"))
    assert env["inherited_from"] == "sess-1"
    assert env["substitutions"]["PALWORLD_USER"] == "operator1" and env["facts"]
