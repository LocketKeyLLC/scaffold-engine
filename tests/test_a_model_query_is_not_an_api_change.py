"""§17.1619 — asking a model (chat/completions, generate, embeddings) is not changing an API.

Live (job 0fb4ea7f T17, 2026-10-16): the end-to-end test of the server engine -- one POST to its
/v1/chat/completions -- was refused by §17.1362 as "CHANGES something through the API on port 8000".
"""
from __future__ import annotations

from app.modules.supervised_runs import api_ports_changed

LIVE = ("curl -sS --fail --max-time 600 -H @/root/.scaffold_hdr -H 'Content-Type: application/json' -d "
        "'{\"model\":\"scaffold\",\"messages\":[]}' http://localhost:8000/v1/chat/completions > /root/out.json")


def test_the_live_query_changes_nothing():
    assert api_ports_changed([LIVE]) == {}


def test_ollama_and_embedding_queries_change_nothing():
    for c in ("curl -s http://localhost:11434/api/generate -d '{\"model\":\"m\"}'",
              "curl -s -X POST http://localhost:11434/api/chat -d '{}'",
              "curl -s http://localhost:8000/v1/embeddings -d '{}'"):
        assert api_ports_changed([c]) == {}, c


def test_a_real_change_still_counts():
    out = api_ports_changed(["curl -s -X POST http://localhost:7878/api/v3/rootfolder -d '{\"path\":\"/media\"}'"])
    assert "7878" in out
