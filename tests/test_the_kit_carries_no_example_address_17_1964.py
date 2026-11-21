"""§17.1964 — the engine's own develop kit carries no address literal: every developed delivery ships the kit, and the
unsourced-address gate (§17.1312) refused all of them for the kit's usage comment `{ address: '10.0.0.6', … }`."""
import pathlib

from app.modules.supervised_runs import unsourced_addresses_in_files

KIT = pathlib.Path(__file__).resolve().parents[1] / "app" / "modules" / "develop_kit"


def test_every_kit_file_passes_the_unsourced_address_gate():
    files = [{"path": "/opt/x/scaffold-kit/" + p.name, "content": p.read_text(encoding="utf-8")}
             for p in KIT.rglob("*") if p.is_file()]
    assert files, "precondition: the kit has files"
    env = {"facts": [{"text": "the panel is CT 111 at 192.168.1.25"}]}           # a ledger, so the gate is live
    assert unsourced_addresses_in_files([], files, env, {"title": "CT 111: fix a route"}) == []
