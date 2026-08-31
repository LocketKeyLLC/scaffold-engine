"""§17.851 — code-enforced placeholder resolution on generated guidance.

The §17.850 prompt rules alone did not stick (live: facts held the concrete
URL, the walkthrough still emitted <PROXMOX_HOST_IP>) — the §17.668 lesson:
LLMs ignore prompt rules, so enforce in code. Layer 1 substitutes pinned
values deterministically; Layer 2 maps leftovers against the facts ledger via
one tool-call; resolutions auto-pin; everything fails soft to the original.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import assist_guide

pytestmark = pytest.mark.asyncio


def _env(subs=None, facts=None, profile=""):
    return {"substitutions": subs or {}, "facts": facts or [], "profile": profile}


async def test_layer1_pinned_values_substitute_without_model():
    text = "Open https://<PROXMOX_HOST_IP>:8006 and ssh root@<PROXMOX_HOST_IP>."
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()) as tc:
        out, applied = await assist_guide.resolve_placeholders(
            text=text, session_id="s1",
            environment=_env(subs={"PROXMOX_HOST_IP": "192.168.1.156"}),
        )
    tc.assert_not_awaited()  # fully deterministic — no model call
    assert "192.168.1.156:8006" in out and "<PROXMOX_HOST_IP>" not in out
    assert applied["PROXMOX_HOST_IP"]["kind"] == "known"
    assert "Values filled in" in out


async def test_layer2_maps_against_facts_and_suggests_names():
    text = "qm create <VMID> --name <VM_NAME> --bridge <MGMT_BRIDGE>"
    resp = SimpleNamespace(success=True, tool_calls=[SimpleNamespace(arguments={
        "resolutions": [
            {"token": "MGMT_BRIDGE", "value": "vmbr0", "kind": "known"},
            {"token": "VM_NAME", "value": "jellyfin", "kind": "suggested"},
            {"token": "VMID", "value": "", "kind": "unknown"},
        ],
    })])
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock(return_value=resp)):
        out, applied = await assist_guide.resolve_placeholders(
            text=text, session_id="s1",
            environment=_env(facts=["vmbr0 is static 192.168.1.156/24"]),
        )
    assert "--name jellyfin" in out and "--bridge vmbr0" in out
    assert "<VMID>" in out  # unknown stays a placeholder
    assert applied["VM_NAME"]["kind"] == "suggested"
    assert "rename if you like" in out


async def test_bad_model_values_are_rejected():
    text = "use <KEY_A> and <KEY_B>"
    resp = SimpleNamespace(success=True, tool_calls=[SimpleNamespace(arguments={
        "resolutions": [
            {"token": "KEY_A", "value": "evil\ninjection", "kind": "known"},
            {"token": "KEY_B", "value": "<nested>", "kind": "known"},
            {"token": "NOT_IN_TEXT", "value": "x", "kind": "known"},
        ],
    })])
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock(return_value=resp)):
        out, applied = await assist_guide.resolve_placeholders(
            text=text, session_id="s1", environment=_env(facts=["f"]),
        )
    assert out == text and applied == {}


async def test_shell_metachar_values_rejected():
    """§17.854 (audit C3) — a value carrying a shell metacharacter must not be
    substituted (it would land verbatim in a command block and auto-pin forever)."""
    text = "storage: <POOL>"
    resp = SimpleNamespace(success=True, tool_calls=[SimpleNamespace(arguments={
        "resolutions": [
            {"token": "POOL", "value": "local-lvm; wipefs -a /dev/sda", "kind": "suggested"},
        ],
    })])
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock(return_value=resp)):
        out, applied = await assist_guide.resolve_placeholders(
            text=text, session_id="s1", environment=_env(facts=["f"]),
        )
    assert out == text and "POOL" not in applied  # rejected, placeholder stays


async def test_resolved_values_carry_source_tag():
    """§17.854 (audit C3) — operator (Layer 1) vs model (Layer 2) provenance so
    the SPA can flag model-suggested pins."""
    text = "ip <HOST_IP> vm <VM_NAME>"
    resp = SimpleNamespace(success=True, tool_calls=[SimpleNamespace(arguments={
        "resolutions": [{"token": "VM_NAME", "value": "jellyfin", "kind": "suggested"}],
    })])
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock(return_value=resp)):
        out, applied = await assist_guide.resolve_placeholders(
            text=text, session_id="s1",
            environment=_env(subs={"HOST_IP": "10.0.0.5"}, facts=["f"]),
        )
    assert applied["HOST_IP"]["source"] == "operator"   # pinned/deterministic
    assert applied["VM_NAME"]["source"] == "model"       # model-suggested


async def test_fail_soft_returns_original_on_error():
    text = "use <SOME_KEY>"
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock(side_effect=RuntimeError("boom"))):
        out, applied = await assist_guide.resolve_placeholders(
            text=text, session_id="s1", environment=_env(facts=["f"]),
        )
    assert out == text and applied == {}


async def test_no_tokens_is_a_noop():
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()) as tc:
        out, applied = await assist_guide.resolve_placeholders(
            text="all concrete already", session_id="s1", environment=_env(),
        )
    tc.assert_not_awaited()
    assert out == "all concrete already" and applied == {}


async def test_autopin_persists_resolutions_node_scoped():
    """§17.892 — auto-pins land under substitutions_by_node[node_key], never
    the global map (the DarthSidious incident: a switch hostname auto-pinned
    globally deterministically named the PalWorld VM)."""
    text = "ssh root@<HOST_IP>"
    pinned = {}
    async def fake_set_env(**kw):
        pinned.update(kw.get("substitutions_by_node") or {})
        assert not kw.get("substitutions")  # never the global map
    resp = SimpleNamespace(success=True, tool_calls=[SimpleNamespace(arguments={
        "resolutions": [{"token": "HOST_IP", "value": "192.168.1.156", "kind": "known"}],
    })])
    from app.modules import assist_agent
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock(return_value=resp)), \
         patch.object(assist_agent, "set_environment", new=AsyncMock(side_effect=fake_set_env)):
        out, applied = await assist_guide.resolve_placeholders(
            text=text, session_id="s1", environment=_env(facts=["host is 192.168.1.156"]),
            db=object(), node_key="T7",
        )
    assert pinned == {"T7": {"HOST_IP": "192.168.1.156"}}
    assert "root@192.168.1.156" in out


async def test_autopin_skipped_without_node_key():
    """§17.892 — no node_key → no honest scope → nothing pins."""
    resp = SimpleNamespace(success=True, tool_calls=[SimpleNamespace(arguments={
        "resolutions": [{"token": "HOST_IP", "value": "10.0.0.9", "kind": "known"}],
    })])
    from app.modules import assist_agent
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock(return_value=resp)), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as se:
        out, _ = await assist_guide.resolve_placeholders(
            text="ssh <HOST_IP>", session_id="s1", environment=_env(facts=["f"]),
            db=object(),
        )
    se.assert_not_awaited()
    assert "10.0.0.9" in out  # resolution itself still happens


async def test_layer1_node_scoped_pin_applies_only_to_its_node():
    """§17.892 — the incident, pinned as a test: T4's learned HOSTNAME must
    not deterministically fill another step's <HOSTNAME>."""
    env = _env(subs={"STORAGE": "local-lvm"})
    env["substitutions_by_node"] = {"T4": {"HOSTNAME": "DarthSidious"}}
    text = "pct create --hostname <HOSTNAME> --storage <STORAGE>"
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()) as tc:
        out, applied = await assist_guide.resolve_placeholders(
            text=text, session_id="s1", environment=env, node_key="T22",
        )
    tc.assert_not_awaited()  # no facts/profile → Layer 2 skipped
    assert "local-lvm" in out            # operator global still applies
    assert "<HOSTNAME>" in out           # another node's auto-pin does NOT
    assert "DarthSidious" not in out
    assert "HOSTNAME" not in applied


async def test_layer1_node_scoped_pin_applies_on_its_own_node():
    env = _env()
    env["substitutions_by_node"] = {"T4": {"HOSTNAME": "DarthSidious"}}
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()):
        out, applied = await assist_guide.resolve_placeholders(
            text="config <HOSTNAME>", session_id="s1", environment=env, node_key="T4",
        )
    assert "config DarthSidious" in out
    assert applied["HOSTNAME"]["source"] == "learned"


async def test_layer1_operator_global_outranks_node_pin():
    env = _env(subs={"HOSTNAME": "operator-choice"})
    env["substitutions_by_node"] = {"T4": {"HOSTNAME": "DarthSidious"}}
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()):
        out, applied = await assist_guide.resolve_placeholders(
            text="config <HOSTNAME>", session_id="s1", environment=env, node_key="T4",
        )
    assert "operator-choice" in out
    assert applied["HOSTNAME"]["source"] == "operator"
