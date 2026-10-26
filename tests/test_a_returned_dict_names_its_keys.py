"""§17.1834 — what a step is shown of a function that returns a dict names the dict's keys.

Live (follow-up 5b4880c0 T6, 2026-10-26): T6D's `load_batch_context(...) -> dict[str, Any]` reached the batch as its
signature and first docstring line, "Load the equity and order-count context for a daily batch run.", and attempt
after attempt read `context["equity"]` -- a key it never returns (`day_start_equity`, `orders_today`). The batch's
entry text also allowed mocking only the broker, network and clock; `propose_trades` writes its rationale with a
model, so attempts mocked it and the data functions wholesale.
"""
from __future__ import annotations

from app.modules import code_interface, engine_plan, engine_source

DATA = '''```python
# app/modules/paper_daily_signal_proposal_batch_data.py
from app.modules.paper_signal_generator import generate_signals

async def load_batch_context(*, timestamp, trading_day, db=None) -> dict:
    """Load the equity and order-count context for a daily batch run."""
    day_start_equity = None
    orders_today = 0
    return {"day_start_equity": day_start_equity, "orders_today": orders_today}

async def load_batch_signals(*, timestamp, db=None):
    """Load signals for a daily batch run."""
    return await generate_signals(timestamp, db=db)

async def count_rows(db):
    return 3
```'''


def test_the_interface_names_the_keys_a_function_returns():
    iface = code_interface.python_interface(DATA)
    assert ("load_batch_context(*, timestamp, trading_day, db=None) -> dict: ...  # returns keys: day_start_equity, "
            "orders_today -- Load the equity and order-count context for a daily batch run.") in iface
    assert "load_batch_signals(*, timestamp, db=None): ...  # Load signals for a daily batch run." in iface  # unread
    assert "def count_rows(db): ..." in iface and "count_rows(db): ...  #" not in iface                    # no dict


def test_the_carried_manifest_and_the_jobs_modules_name_them_too():
    code = DATA.split("```python\n", 1)[1].rsplit("```", 1)[0]
    api = engine_source._public_api(code)
    assert "load_batch_context(*, timestamp, trading_day, db) -> {day_start_equity, orders_today}" in api
    assert "load_batch_signals(*, timestamp, db);" in api
    defs = engine_source.sibling_definitions([{"node_key": "T6D", "output_text": DATA, "title": "",
                                               "prompt_template": ""}])
    rendered = engine_source._render_job_modules(defs, "paper_daily_signal_proposal_batch")
    assert "load_batch_context(*, timestamp, trading_day, db=…) -> {day_start_equity, orders_today}" in rendered
    assert "load_batch_signals(*, timestamp, db=…)\n" in rendered


def test_a_split_steps_entry_tests_may_mock_model_calls():
    text = engine_plan._entry_prompt("paper_b", "paper_b_data", "T6D", "Do it.")
    assert "mock only what reaches outside the engine -- the broker, the network, model calls, the clock." in text
    assert engine_plan._ENTRY_RE.match(text)                         # a re-render still recognises it
