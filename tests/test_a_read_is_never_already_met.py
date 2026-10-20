"""§17.1692 — a step that only reads is never "already met": its output is its answer (live, job e5e22b92 T1)."""
import inspect

from app.modules import execution_agent as ea
from app.modules import supervised_runs as sr

T1 = {"node_key": "T1", "title": "Login qBittorrent list torrents",
      "description": "Read-only: authenticate with stored credentials and list all torrents. No changes."}
T1_FRAME = {"verify": ["pct exec 105 -- sh -c 'test -s /tmp/qb_cookies.txt && echo cookie-present'"]}


def test_the_live_read_is_never_already_met():
    assert sr.declares_read_only(T1)
    assert sr.may_be_already_met(T1, T1_FRAME) is False


def test_a_change_may_still_be_already_met():
    change = {"node_key": "T12", "title": "Widen TV-capable categories",
              "description": "Widen each TV-capable indexer's categories to the sync set."}
    assert sr.may_be_already_met(change, T1_FRAME) is True


def test_the_earlier_exceptions_hold():
    change = {"title": "Add authentication to the panel", "description": "Write the login check."}
    assert sr.may_be_already_met(change, {"verify": ["systemctl is-active panel"]}, developed=True) is False   # §17.1426
    assert sr.may_be_already_met(change, {"verify": ["curl -s localhost"]}, developed=True) is True
    assert sr.may_be_already_met(change, T1_FRAME, engine_check=True) is False                               # §17.1641


def test_the_pause_asks_through_it():
    src = inspect.getsource(ea)
    assert "supervised_runs.may_be_already_met(run_node, frame, _developed, _engine_check)" in src
    i = src.index("supervised_runs.may_be_already_met(")
    assert "supervised_runs.already_met(" in src[i:i + 600]
