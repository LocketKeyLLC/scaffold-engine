"""§17.1604 — the runner install line never names an address only the engine's Docker network resolves.

Live, 2026-10-15: the engine had remembered `http://scaffold-orchestrator:8000` (a request from the pipelines container,
over the compose network), and the Machines page told the Proxmox host to `curl` it: "Could not resolve host:
scaffold-orchestrator". The helper-23 update could not be installed.
"""
from __future__ import annotations

import pytest

from app.modules import engine_setup as es


@pytest.mark.parametrize("url", ["http://scaffold-orchestrator:8000", "http://orchestrator", "http://localhost:8000",
                                 "http://127.0.0.1:8000", "http://0.0.0.0:8000", "http://169.254.1.2:8000", ""])
def test_addresses_the_lan_cannot_reach_are_local(url):
    assert es._url_is_local(url)


@pytest.mark.parametrize("url", ["http://10.9.8.43:8000", "http://engine.home.lan:8000", "https://scaffold.example.org"])
def test_lan_addresses_and_dotted_names_are_not(url, monkeypatch):
    monkeypatch.setattr(es, "_own_subnets", lambda: [])
    assert not es._url_is_local(url)


def test_the_containers_own_bridge_subnet_is_local(monkeypatch):
    import ipaddress
    monkeypatch.setattr(es, "_own_subnets", lambda: [ipaddress.ip_network("172.18.0.0/16")])
    assert es._url_is_local("http://172.18.0.13:8000")
    assert not es._url_is_local("http://10.9.8.43:8000")


def test_a_docker_name_is_never_remembered(monkeypatch):
    import asyncio
    calls = []

    class DB:
        async def execute(self, *a, **k):
            calls.append(a)

        async def commit(self):
            pass
    asyncio.run(es.remember_engine_url(DB(), "sid", "http://scaffold-orchestrator:8000/"))
    assert calls == []
