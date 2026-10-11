"""§17.1463 — a login the engine holds FOR the operator can be shown to them; a service's key never comes back out.

Live (2026-10-11): ADD127 put the control panel behind Basic auth with the runner secret PANEL_PASSWORD (stored
2026-10-08, hint "the control panel's login password (user `panel`), operator decision 2026-10-08"). The panel
worked and nobody could log in — "the engine never gave the user the username and password it set up. How are
they suppose to log in?" — because the store was write-only by design.
"""
import asyncio
import pathlib

import pytest

from app.modules import runner_secrets as rs


class _DB:
    def __init__(self, row):
        self.row, self.sql = row, []

    async def execute(self, stmt, params=None):
        self.sql.append(str(stmt))
        row = self.row

        class R:
            def mappings(self_inner):
                class M:
                    def first(self2):
                        return row
                return M()
        return R()

    async def commit(self):
        pass


@pytest.fixture
def plain(monkeypatch):
    import app.utils.secrets as us
    monkeypatch.setattr(us, "decrypt", lambda v: "AE-test-pw" if v == "enc" else "")


def test_the_operators_login_is_revealed_and_recorded(plain):
    db = _DB({"name": "PANEL_PASSWORD", "value_enc": "enc", "hint": "the control panel's login password (user `panel`)",
              "for_operator": True})
    out = asyncio.run(rs.reveal(db, "PANEL_PASSWORD"))
    assert out == {"name": "PANEL_PASSWORD", "hint": "the control panel's login password (user `panel`)", "value": "AE-test-pw"}
    assert any("reveal_count = reveal_count + 1" in q for q in db.sql)


def test_a_services_key_never_comes_back_out(plain):
    db = _DB({"name": "RADARR_API_KEY", "value_enc": "enc", "hint": "", "for_operator": False})
    with pytest.raises(rs.NotForOperator):
        asyncio.run(rs.reveal(db, "RADARR_API_KEY"))
    assert not any("reveal_count" in q for q in db.sql)


def test_nothing_stored_is_none():
    assert asyncio.run(rs.reveal(_DB(None), "NOPE")) is None


def test_the_migration_marks_existing_logins():
    src = pathlib.Path("alembic/versions/0009_runner_secrets_for_operator.py").read_text()
    assert "ADD COLUMN IF NOT EXISTS for_operator BOOLEAN NOT NULL DEFAULT false" in src
    assert "SET for_operator = true WHERE hint ~* '\\\\mlogin\\\\M'" in src


def test_the_api_and_both_pages_offer_it():
    r = pathlib.Path("app/routers/machines.py").read_text()
    assert '@router.post("/setup/secrets/{name}/reveal")' in r and "status_code=403" in r
    assert "for_operator=body.for_operator" in r
    m = pathlib.Path("app/ui/static/views/machines.js").read_text()
    t = pathlib.Path("app/ui/static/views/theater.js").read_text()
    assert "/reveal`" in m and "this is a login I use" in m and "for_operator: mine.checked" in m
    assert '"Your logins"' in t and "x.for_operator" in t


def test_a_value_described_as_a_login_is_the_operators(monkeypatch):
    import app.utils.secrets as us
    monkeypatch.setattr(us, "encrypt", lambda v: "enc")
    seen = {}

    class DB:
        async def execute(self, stmt, params=None):
            seen.update(params or {})

        async def commit(self):
            pass
    out = asyncio.run(rs.set_secret(DB(), "PANEL_PASSWORD", "x", hint="the control panel's login password (user `panel`)"))
    assert out["for_operator"] is True and seen["f"] is True
    out2 = asyncio.run(rs.set_secret(DB(), "RADARR_API_KEY", "x", hint="Radarr's API key"))
    assert out2["for_operator"] is False
