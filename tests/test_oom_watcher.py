"""§17.161 — unit tests for scripts/oom_watcher.py.

Targets the three pure helpers + the build_emit_argv composition. The
subprocess loop is exercised via ``--test-event`` in an integration
smoke (out of unit-test scope).
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "oom_watcher",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "oom_watcher.py",
)
oom_watcher = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(oom_watcher)


def _flags(argv: list[str]) -> dict[str, str]:
    """Return the --flag → value mapping from the alerts-CLI tail of argv."""
    i = argv.index("emit") + 1
    tail = argv[i:]
    return dict(zip(tail[::2], tail[1::2]))


def _make_event(
    *,
    typ: str = "container",
    action: str = "oom",
    name: str = "scaffold-orchestrator",
    project: str | None = "scaffold-engine",
    cid: str = "abcdef0123456789",
    image: str = "scaffold-engine:local",
    t: int = 1_700_000_000,
) -> dict:
    attrs: dict = {"name": name, "image": image}
    if project is not None:
        attrs["com.docker.compose.project"] = project
    return {
        "Type": typ,
        "Action": action,
        "Actor": {"ID": cid, "Attributes": attrs},
        "time": t,
    }


class TestParseEvent:
    def test_returns_dict_on_valid_json(self):
        ev = _make_event()
        out = oom_watcher.parse_event(json.dumps(ev))
        assert out == ev

    def test_returns_none_on_blank_line(self):
        assert oom_watcher.parse_event("") is None
        assert oom_watcher.parse_event("   \n") is None

    def test_returns_none_on_malformed_json(self):
        assert oom_watcher.parse_event("not json") is None
        assert oom_watcher.parse_event("{unterminated") is None

    def test_returns_none_on_non_object_json(self):
        # docker events should never emit a top-level array, but be defensive.
        assert oom_watcher.parse_event("[1,2,3]") is None
        assert oom_watcher.parse_event('"a string"') is None


class TestIsComposeManagedOom:
    def test_accepts_compose_managed_oom(self):
        ev = _make_event()
        assert oom_watcher.is_compose_managed_oom(ev, "scaffold-engine")

    def test_rejects_non_container_type(self):
        ev = _make_event(typ="image")
        assert not oom_watcher.is_compose_managed_oom(ev, "scaffold-engine")

    def test_rejects_non_oom_action(self):
        for act in ("die", "start", "kill", "restart", "destroy"):
            ev = _make_event(action=act)
            assert not oom_watcher.is_compose_managed_oom(ev, "scaffold-engine")

    def test_rejects_unlabelled_container(self):
        ev = _make_event(project=None)
        assert not oom_watcher.is_compose_managed_oom(ev, "scaffold-engine")

    def test_rejects_wrong_project(self):
        ev = _make_event(project="some-other-project")
        assert not oom_watcher.is_compose_managed_oom(ev, "scaffold-engine")

    def test_handles_missing_actor(self):
        ev = {"Type": "container", "Action": "oom"}
        assert not oom_watcher.is_compose_managed_oom(ev, "scaffold-engine")


class TestBuildEmitArgv:
    def test_carries_kind_severity_message_and_dedup_key(self):
        ev = _make_event(name="milvus-standalone")
        argv = oom_watcher.build_emit_argv(ev, orchestrator="scaffold-orchestrator")
        assert argv[0:3] == ["docker", "exec", "scaffold-orchestrator"]
        assert argv[3:6] == ["python", "-m", "app.observability.alerts"]
        assert "emit" in argv
        kv = _flags(argv)
        assert kv["--kind"] == "container.oom_killed"
        assert kv["--severity"] == "critical"
        assert "milvus-standalone" in kv["--message"]
        assert kv["--dedup-key"] == "container.oom_killed:milvus-standalone"

    def test_payload_is_json_with_expected_fields(self):
        ev = _make_event(
            name="scaffold-postgres",
            cid="0123456789abcdef0123",
            image="postgres:16",
            t=1_700_000_000,
        )
        argv = oom_watcher.build_emit_argv(ev, orchestrator="scaffold-orchestrator")
        payload_idx = argv.index("--payload") + 1
        payload = json.loads(argv[payload_idx])
        assert payload["container_name"] == "scaffold-postgres"
        assert payload["container_id"] == "0123456789ab"  # short 12 chars
        assert payload["image"] == "postgres:16"
        assert payload["event_time_utc"].startswith("2023-11-")  # 1.7e9 epoch

    def test_dedup_key_stable_across_container_id_changes(self):
        # Restart-on-OOM gives the container a new ID but keeps the name —
        # the dedup key must be name-stable so cooldown still applies.
        a = _make_event(name="open-webui", cid="aaaaaaaaaaaa")
        b = _make_event(name="open-webui", cid="bbbbbbbbbbbb")
        argv_a = oom_watcher.build_emit_argv(a, orchestrator="x")
        argv_b = oom_watcher.build_emit_argv(b, orchestrator="x")
        dedup_a = argv_a[argv_a.index("--dedup-key") + 1]
        dedup_b = argv_b[argv_b.index("--dedup-key") + 1]
        assert dedup_a == dedup_b

    def test_orchestrator_target_is_configurable(self):
        ev = _make_event()
        argv = oom_watcher.build_emit_argv(ev, orchestrator="custom-orchestrator")
        assert argv[2] == "custom-orchestrator"

    def test_handles_missing_attributes(self):
        ev = {"Type": "container", "Action": "oom", "Actor": {}, "time": 1}
        argv = oom_watcher.build_emit_argv(ev, orchestrator="x")
        kv = _flags(argv)
        assert kv["--dedup-key"] == "container.oom_killed:<unknown>"


class TestEventTimeIso:
    def test_converts_epoch_to_utc_iso(self):
        out = oom_watcher._event_time_iso({"time": 1_700_000_000})
        assert out.startswith("2023-11-14T")
        assert out.endswith("+00:00")

    def test_falls_back_to_now_on_missing_time(self):
        out = oom_watcher._event_time_iso({})
        # Should be a valid ISO string with timezone; specific time isn't pinned.
        assert "T" in out
        assert out.endswith("+00:00")

    def test_falls_back_to_now_on_non_numeric_time(self):
        out = oom_watcher._event_time_iso({"time": "not a number"})
        assert "T" in out
