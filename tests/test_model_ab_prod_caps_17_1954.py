"""§17.1954 — an A/B trial runs under the output cap production gives the call.

The harness sent every task max_tokens=8192; the router's real call is capped at 512. On the server's CPU one
qwen3:4b routing draw generated for over two hours, so the A/B measured a call production never makes and the
benchmark stalled on it.
"""
from unittest.mock import AsyncMock, patch

import pytest

import scripts.model_ab as mab


@pytest.mark.asyncio
async def test_the_routing_cap_is_the_one_the_router_sends():
    from app.modules import command_guide
    fake = AsyncMock(side_effect=RuntimeError("stop"))
    with patch.object(command_guide.model_router, "tool_call", fake):
        await command_guide.classify_command(message="research the price of eggs")
    assert fake.await_args.kwargs["max_tokens"] == mab.TASKS["routing"].prod_max_tokens() == 512


def test_every_task_has_a_cap_below_the_old_blanket_where_production_is_lower():
    caps = {name: t.prod_max_tokens() for name, t in mab.TASKS.items()}
    assert caps["routing"] == 512
    assert caps["extraction"] == 4096
    assert caps["research_extract"] == 4096
    assert all(isinstance(v, int) and v > 0 for v in caps.values())


@pytest.mark.asyncio
async def test_the_library_api_runs_each_trial_under_the_task_cap(monkeypatch):
    monkeypatch.setattr(mab, "_load_goldens", lambda p: [{"id": "g1"}, {"id": "g2"}])
    seen: list[int] = []

    async def fake_avail(m, url):
        return True

    async def fake_run_one(task, model, golden, *, temperature, max_tokens):
        seen.append(max_tokens)
        return {"task": task.name, "model": model, "golden": golden["id"], "ok": True, "passed": True}

    monkeypatch.setattr(mab, "_is_available", fake_avail)
    monkeypatch.setattr(mab, "_run_one", fake_run_one)
    await mab.run_model_ab_task("routing", ["m1"])
    assert seen == [512, 512]
    seen.clear()
    await mab.run_model_ab_task("routing", ["m1"], max_tokens=100)
    assert seen == [100, 100]


@pytest.mark.asyncio
async def test_the_cli_appends_each_trial_as_it_lands(monkeypatch, tmp_path):
    out = tmp_path / "routing.jsonl"
    monkeypatch.setattr(mab, "_load_goldens", lambda p: [{"id": "g1"}, {"id": "g2"}])
    monkeypatch.setattr("sys.argv", ["model_ab.py", "--task", "routing", "--models", "m1",
                                     "--outfile", str(out)])
    seen: list[tuple[int, int]] = []

    async def fake_avail(m, url):
        return True

    async def fake_run_one(task, model, golden, *, temperature, max_tokens):
        # the rows before this trial are already on disk
        seen.append((max_tokens, len(out.read_text().splitlines())))
        return {"task": task.name, "model": model, "golden": golden["id"], "ok": True, "passed": True}

    monkeypatch.setattr(mab, "_is_available", fake_avail)
    monkeypatch.setattr(mab, "_run_one", fake_run_one)
    with patch("app.utils.http_clients.init_clients"):
        assert await mab.main() == 0
    assert seen == [(512, 0), (512, 1)]
    assert len(out.read_text().splitlines()) == 2
