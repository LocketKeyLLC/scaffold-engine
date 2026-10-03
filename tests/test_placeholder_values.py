"""§17.1306 — a placeholder is not a value, and the template draw sees the plan.

Live, 2026-10-03 10:06 UTC: ADD88 "Install Caddy and write the Caddyfile inside
LXC 120" rendered from the container template with the model's content
`{ email admin@example.com } :80 { respond "Caddy is running" }` -- while the
step specified five reverse-proxy blocks and the facts named the operator's
DuckDNS domain. The free-parameter draw had neither the upstream outputs nor
the facts; the model path has both."""
from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest

from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

FX = pathlib.Path(__file__).parent / "fixtures"
FRAME = json.loads((FX / "add88_frame_template_2026_10_03.json").read_text(encoding="utf-8"))
ENV = {"profile": "root@pve", "facts": [
    "pct exec 101 -- getent hosts defrusciohomelab.duckdns.org returns 67.240.32.243 (DNS resolution works).",
    "LXC container 120 (caddy-proxy) is currently stopped."], "substitutions": {"JELLYFIN_IP": "192.168.1.20"}}
POLICY = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
NODE = {"node_key": "ADD88", "title": "Install Caddy and write the Caddyfile inside LXC 120",
        "description": "Install Caddy inside container 120 and create /etc/caddy/Caddyfile with the 17-line/406-byte content."}


def _frame(rb, env=ENV):
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    return sr.frame_run(NODE, rb, spec, POLICY, env=env)


def test_the_live_stub_caddyfile_is_refused_and_the_real_domain_is_named():
    assert "admin@example.com" in FRAME["files"][0]["content"]
    frame = _frame(FRAME["runbook"])
    hits = [r for r in frame["refused"] if "is a placeholder, not a value" in r["why"]]
    assert len(hits) == 1, [r["why"][:100] for r in frame["refused"]]
    assert "admin@example.com" in hits[0]["why"] and "defrusciohomelab.duckdns.org" in hits[0]["why"]
    assert "/tmp/in_ct_120.sh" in hits[0]["command"]
    assert sr.shape_retry_note({"refused": hits, "commands": frame["commands"]}), "registered: the chain redrafts"


def test_without_a_known_domain_the_remedy_is_a_placeholder_the_operator_fills():
    out = sr.placeholder_values_in_files([], [{"path": "/etc/x.conf", "content": "server_name example.com;\n"}], {"facts": []})
    assert len(out) == 1 and "<DOMAIN>" in out[0]["why"] and "example.com" in out[0]["why"]


def test_real_values_and_operator_placeholders_are_not_placeholders():
    files = [{"path": "/etc/caddy/Caddyfile", "content": "defrusciohomelab.duckdns.org {\n  handle_path /jellyfin/* {\n    reverse_proxy 192.168.1.20:8096\n  }\n}\n"}]
    assert sr.placeholder_values_in_files(["pct exec 120 -- caddy validate"], files, ENV) == []
    assert sr.placeholder_values_in_files([], [{"path": "/etc/x", "content": "email <ADMIN_EMAIL>\n"}], ENV) == []
    assert sr.placeholder_values_in_files(["curl https://example.com/docs"], [], ENV), "a sample host in a command is one too"


def test_the_template_draw_sees_the_upstream_outputs_and_the_facts():
    ctx = rt.plan_context("ADD56 wrote: defrusciohomelab.duckdns.org { handle_path /jellyfin/* { reverse_proxy 192.168.1.20:8096 } }", ENV)
    assert "WHAT EARLIER STEPS ESTABLISHED" in ctx and "handle_path /jellyfin" in ctx
    assert "KNOWN FACTS" in ctx and "defrusciohomelab.duckdns.org" in ctx
    assert "PINNED VALUES" in ctx and "JELLYFIN_IP = 192.168.1.20" in ctx
    assert rt.plan_context("", None) == ""
    assert "example.com" in rt.FREE_PARAM_SYSTEM and "truncating write" in rt.FREE_PARAM_SYSTEM


@pytest.mark.asyncio
async def test_the_draw_carries_the_context_into_the_prompt(monkeypatch):
    import app.utils.llm_retry as lr
    seen = []

    async def fake(gen, prompt, params, *, system, **kw):
        seen.append(prompt)
        return type("R", (), {"text": "```bash\napt-get install -y caddy\n```"})()
    monkeypatch.setattr(lr, "generate_until_nonempty", fake)
    await rt.fill_free_params(rt.RUN_IN_CONTAINER, NODE, "brief", upstream="ADD56: the 406-byte Caddyfile …", environment=ENV)
    assert len(seen) == 1 and "ADD56: the 406-byte Caddyfile" in seen[0] and "defrusciohomelab.duckdns.org" in seen[0]
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    assert "upstream=upstream, environment=environment)   # §17.1306" in src, "the drafter hands the draw what it hands the model path"
