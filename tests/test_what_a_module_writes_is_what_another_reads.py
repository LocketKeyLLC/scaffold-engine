"""§17.1840 — what one module writes into a JSON column carries what another reads back out of it.

Live (follow-up 5b4880c0, 2026-10-26): the EDGAR Form 4 ingester writes `sec_edgar` payloads of filing fields (cik,
accession_number, ..., document_text) and the 13F ingester `{"holdings": [...]}`; the generator gives a `sec_edgar`
signal the ticker `""`. Reddit payloads hold title/author/subreddit/... and the generator reads `ticker`/`symbol`. It
then skips every signal without a ticker -- the highest-weighted source and Reddit never reach a proposal. The shapes
below are the live modules', cut to what the check reads.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.modules import node_editor
from app.modules.engine_contracts import json_writes, payload_gaps
from app.modules.execution_retry import retry_failed_node

text = node_editor.text

EDGAR = '''import json
from sqlalchemy import text

async def ingest_form_4_filings(db, cik):
    for filing in []:
        payload = {"cik": cik, "accession_number": filing["a"], "document_text": "<xml/>"}
        await db.execute(text("INSERT INTO ingested_item (source, external_id, published_ts, fetched_ts, payload) "
                              "VALUES ('sec_edgar', :external_id, :published_ts, now(), CAST(:payload AS json)) "
                              "RETURNING id"),
                         {"external_id": filing["a"], "published_ts": None, "payload": json.dumps(payload)})
'''
THIRTEEN_F = '''from sqlalchemy import text

def _parse_13f_xml(xml_bytes):
    holdings = []
    return {"holdings": holdings}

async def _write_ingested_item(db, source, external_id, published_ts, payload):
    insert_sql = text("""
        INSERT INTO ingested_item (source, external_id, published_ts, payload)
        VALUES (:source, :external_id, :published_ts, :payload)
        ON CONFLICT (source, external_id) DO NOTHING
        """)
    await db.execute(insert_sql, {"source": source, "external_id": external_id, "published_ts": published_ts,
                                  "payload": payload})

async def ingest_13f_holdings(db, xml_bytes):
    payload = _parse_13f_xml(xml_bytes)
    await _write_ingested_item(db, source="sec_edgar", external_id="x", published_ts=None, payload=payload)
'''
REDDIT = '''from sqlalchemy import text

def _post_to_record(post):
    if not post:
        return None
    payload = {"title": post.get("title", ""), "author": post.get("author", ""), "url": post.get("url", "")}
    return {"source": "reddit", "external_id": post["id"], "published_ts": None, "payload": payload}

async def _write_ingested_item(db, record):
    await db.execute(text("INSERT INTO ingested_item (source, external_id, published_ts, payload) "
                          "VALUES (:source, :external_id, :published_ts, :payload)"),
                     {"source": record["source"], "external_id": record["external_id"],
                      "published_ts": record["published_ts"], "payload": record["payload"]})

async def ingest(db, posts):
    for post in posts:
        record = _post_to_record(post)
        await _write_ingested_item(db, record)
'''
NEWS = REDDIT.replace('"reddit"', '"news"').replace('"url": post.get("url", "")}',
                                                     '"url": post.get("url", ""), "ticker": post.get("t")}')
FINRA = '''from sqlalchemy import text

async def write(db, fields):
    await db.execute(text("INSERT INTO ingested_item (source, payload) VALUES ('finra', :payload)"),
                     {"payload": {"release": 1, **fields}})
'''
GENERATOR = '''from sqlalchemy import text

def _source_of(item):
    return str(item.get("source") or "").lower()

def _extract_ticker(payload, source):
    if source == "sec_edgar":
        return ""
    if source == "congress":
        ticker = payload.get("asset_ticker") or payload.get("ticker")
        return str(ticker or "").strip().upper()
    ticker = payload.get("ticker") or payload.get("symbol")
    return str(ticker or "").strip().upper()

def normalize_edgar_payload(payload):
    return {"source": "sec_edgar", "ticker": "", "signal_type": "form4_purchase", "rationale": "r"}

def _normalize_payload(payload, source):
    ticker = _extract_ticker(payload, source)
    return {"source": source, "ticker": ticker, "signal_type": "news", "rationale": "r"}

def _normalize_item(item):
    source = _source_of(item)
    payload = item.get("payload") or {}
    if source == "sec_edgar":
        return normalize_edgar_payload(payload)
    normalized = _normalize_payload(payload, source)
    return normalized

async def generate_signals(timestamp, *, db=None):
    rows = (await db.execute(text("SELECT id, source, payload, published_ts FROM ingested_item "
                                  "WHERE published_ts <= :ts ORDER BY id"), {"ts": timestamp})).mappings().all()
    out = []
    for item in [dict(r) for r in rows]:
        normalized = _normalize_item(item)
        if not normalized["ticker"]:
            continue
        out.append(normalized)
    return out
'''
MODULES = {"paper_edgar_form_4_ingestion": EDGAR, "paper_13f_holdings_ingestion": THIRTEEN_F,
           "paper_reddit_ingestion": REDDIT, "paper_news_ingestion": NEWS, "paper_finra_data_ingestion": FINRA,
           "paper_signal_generator": GENERATOR}


def test_each_writer_is_read_through_its_helpers_and_an_unreadable_one_is_left_out():
    got = {(w["module"], tuple(sorted(w["by"].items())), tuple(sorted(w["keys"]))) for w in json_writes(MODULES)}
    assert got == {
        ("paper_edgar_form_4_ingestion", (("source", "sec_edgar"),), ("accession_number", "cik", "document_text")),
        ("paper_13f_holdings_ingestion", (("external_id", "x"), ("source", "sec_edgar")), ("holdings",)),
        ("paper_reddit_ingestion", (("source", "reddit"),), ("author", "title", "url")),
        ("paper_news_ingestion", (("source", "news"),), ("author", "ticker", "title", "url"))}   # FINRA: `**fields`


def test_a_kind_whose_payload_never_holds_the_required_field_is_a_gap():
    gaps = payload_gaps(MODULES)
    assert [(g["kind"], g["field"], g["reader"], g["reads"], sorted(g["writers"])) for g in gaps] == [
        ("reddit", "ticker", "paper_signal_generator._normalize_item", ["asset_ticker", "symbol", "ticker"],
         ["paper_reddit_ingestion"]),
        ("sec_edgar", "ticker", "paper_signal_generator._normalize_item", [],
         ["paper_13f_holdings_ingestion", "paper_edgar_form_4_ingestion"])]                 # news carries `ticker`


def test_the_gaps_close_once_the_writers_record_it_and_the_reader_reads_it():
    fixed = {**MODULES,
             "paper_reddit_ingestion": REDDIT.replace('"url": post.get("url", "")}',
                                                      '"url": post.get("url", ""), "ticker": post.get("t")}'),
             "paper_edgar_form_4_ingestion": EDGAR.replace('"document_text": "<xml/>"}',
                                                           '"document_text": "<xml/>", "ticker": "X"}'),
             "paper_13f_holdings_ingestion": THIRTEEN_F.replace('return {"holdings": holdings}',
                                                                'return {"holdings": holdings, "ticker": "X"}'),
             "paper_signal_generator": GENERATOR.replace(
                 'return {"source": "sec_edgar", "ticker": "",', 'return {"source": "sec_edgar", '
                 '"ticker": payload.get("ticker"),')}
    assert payload_gaps(fixed) == []
    unread = {**MODULES, "paper_signal_generator": GENERATOR.replace(
        "        normalized = _normalize_item(item)\n        if not normalized[\"ticker\"]:\n            continue\n",
        "        normalized = _normalize_item(item)\n")}
    assert payload_gaps(unread) == []                                    # nothing requires the field: no claim
    whole = {**MODULES, "paper_signal_generator": GENERATOR.replace('if not normalized["ticker"]:',
                                                                    "if not normalized:")}
    assert payload_gaps(whole) == []                                     # a give-up on the item, not a field
    for test in ("if not len(normalized):", "if not normalized.items():"):
        other = {**MODULES, "paper_signal_generator": GENERATOR.replace('if not normalized["ticker"]:', test)}
        assert payload_gaps(other) == []                                 # nor on what is not one of its fields


BRIEF = {"title": "lab", "description": "Add a paper-trading research lab to the scaffold engine's own codebase."}


async def _job(db) -> str:
    jid = str(uuid.uuid4())
    await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'lab', 'running', "
                          "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(BRIEF)})
    steps = [(f"T{i}", "done", f"Python module `{m}.py`.", f"```python\n# app/modules/{m}.py\n{c}```")
             for i, (m, c) in enumerate(MODULES.items(), start=1)]
    steps.append(("T9", "failed", "Python module `paper_batch.py`. The batch.",
                  "```python\n# app/modules/paper_batch.py\nx = 1\n```"))
    for i, (k, st, prompt, out) in enumerate(steps):
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, status, depends_on, prompt_template, output_text, tool, "
            "execution_order, retry_count, max_retries) VALUES (:j, :k, :k, :s, :d, :p, :o, 'CodeGen', :n, :rc, 3)"),
            {"j": jid, "k": k, "s": st, "d": [], "p": prompt, "o": out, "n": i, "rc": 3 if k == "T9" else 0})
    await db.commit()
    return jid


async def _drop(jid: str) -> None:
    from app.database import async_session
    async with async_session() as db:
        await db.execute(text("DELETE FROM dag_node_edits WHERE job_id = :j"), {"j": jid})
        await db.execute(text("DELETE FROM execution_logs WHERE job_id = :j"), {"j": jid})
        await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
        await db.commit()


@pytest.mark.integration
async def test_an_exhausted_step_gets_the_writers_and_the_reader_fixed_once():
    from app.database import async_session
    async with async_session() as db:
        jid = await _job(db)
    try:
        async with async_session() as db:
            out = await retry_failed_node(jid, "T9", db)
        assert out["status"] == "reset" and out["upstream_fix"]["fix_steps"] == ["T9U1", "T9U2", "T9U3", "T9U4"]
        async with async_session() as db:
            rows = {r["node_key"]: r for r in (await db.execute(text(
                "SELECT node_key, depends_on, prompt_template, tool_config FROM dag_nodes WHERE job_id = :j AND "
                "node_key LIKE 'T9U%'"), {"j": jid})).mappings().all()}
        reddit, f13, edgar, gen = rows["T9U1"], rows["T9U2"], rows["T9U3"], rows["T9U4"]
        assert reddit["tool_config"]["fixes_module"] == "paper_reddit_ingestion"
        assert ("The `reddit` items it writes into `ingested_item.payload` carry `author`, `title`, `url` -- and "
                "`paper_signal_generator._normalize_item` (paper_signal_generator) takes an item's `ticker` from "
                "`asset_ticker`, `symbol`, `ticker`") in reddit["prompt_template"]
        assert "Record `ticker` in each item's payload: the ticker of the very thing the item is about" \
            in reddit["prompt_template"]
        assert reddit["tool_config"]["payload_writer"]["keys"] == ["author", "title", "url"]
        assert (f13["tool_config"]["fixes_module"], edgar["tool_config"]["fixes_module"]) == (
            "paper_13f_holdings_ingestion", "paper_edgar_form_4_ingestion")
        assert "takes an item's `ticker` from nothing" in edgar["prompt_template"]
        assert gen["tool_config"]["fixes_module"] == "paper_signal_generator"
        assert list(gen["depends_on"]) == ["T9U2", "T9U3"]                       # after its writers record it
        assert "read it from there for `sec_edgar` items" in gen["prompt_template"]
        async with async_session() as db:              # asked once: exhausted again, the same gaps add nothing
            await db.execute(text("UPDATE dag_nodes SET status = 'failed', retry_count = 3, output_text = "
                                  "'```python\n# app/modules/paper_batch.py\nx = 1\n```' WHERE job_id = :j AND "
                                  "node_key = 'T9'"), {"j": jid})
            await db.commit()
        async with async_session() as db:
            again = await retry_failed_node(jid, "T9", db)
        assert "upstream_fix" not in again, again
    finally:
        await _drop(jid)


# ── §17.1841 — a test never deletes from the job's append-only tables ────────────────────────────────────────────

MIGRATION = '''```python
# alembic/versions/0010_paper_audit_trail.py
def upgrade():
    for table in ("ingested_item", "signal"):
        op.execute(f"""
            CREATE TRIGGER trg_no_update_delete_{table}
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
        """)
    op.execute("CREATE TABLE notes (id int)")
```'''


def test_a_cleanup_delete_from_an_append_only_table_is_refused_and_a_refusal_test_is_not():
    """Live (5b4880c0 T6U7 x3): told "never DELETE from them yourself", every attempt emptied `ingested_item` in a
    fixture and every test errored at setup (`append-only table: ingested_item`)."""
    from app.modules.engine_contracts import append_only_tables, deletes_in_tests
    tables = append_only_tables([MIGRATION])
    assert tables == {"ingested_item", "signal"}
    tests = {"tests/test_r.py": '''import pytest
from sqlalchemy import text

@pytest.fixture(autouse=True)
async def clean(db):
    await db.execute(text("DELETE FROM ingested_item"))
    await db.execute(text("DELETE FROM notes"))

async def test_refused(db):
    with pytest.raises(Exception):
        await db.execute(text("DELETE FROM signal WHERE id = 1"))
'''}
    found = deletes_in_tests(tests, tables)
    assert found == ["`tests/test_r.py:6` deletes from `ingested_item`, an append-only table: with rows in it the "
                     "DELETE fails, and without them it does nothing -- the sandbox empties this job's tables before "
                     "every test. Drop it; insert only the rows the test needs"]
    assert deletes_in_tests({"app/modules/x.py": 'q = "DELETE FROM ingested_item"'}, tables) == []   # not a test


# ── §17.1842 — a payload writer's fix records the item's OWN field, and is re-rendered when asked again ────────────

LEGACY_13F = (  # T6U8's text as §17.1840 wrote it (live)
    "Python module `paper_13f_holdings_ingestion.py` -- step f00ea97f:T8's module: edit it in place (the whole file, "
    "its public names kept). The `sec_edgar` items it writes into `ingested_item.payload` carry `holdings` -- and "
    "`paper_signal_generator._normalize_item` (paper_signal_generator) takes an item's `ticker` from nothing (it gives "
    "every one an empty one), so every one of them is dropped where `ticker` is required. Record `ticker` in each "
    "item's payload, read from the item's own data (the document or record it came from) -- never made up; an item "
    "that truly has none gets none. Keep every key it writes now. In this same step also write ...")


@pytest.mark.integration
async def test_a_payload_writer_fix_asked_again_says_the_field_is_the_items_own():
    """Live (5b4880c0 T6U8): the 13F ingester recorded the filer's -- the fund manager's -- ticker on a filing about
    the securities it holds."""
    from app.database import async_session
    from app.modules.engine_plan import refresh_contract_fix
    async with async_session() as db:
        jid = await _job(db)
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, tool, tool_config, execution_order) "
            "VALUES (:j, 'T6U8', 'Fix', 'done', :p, 'CodeGen', CAST(:c AS jsonb), 20)"),
            {"j": jid, "p": LEGACY_13F, "c": json.dumps({
                "fixes_module": "paper_13f_holdings_ingestion", "found_by": "T6", "owner": "f00ea97f:T8",
                "contract": "payload:ingested_item.payload:source=sec_edgar:ticker:paper_13f_holdings_ingestion"})})
        await db.commit()
    try:
        async with async_session() as db:
            out = await refresh_contract_fix(db, jid, "T6U8", "operator")
            await db.commit()
        assert out == {"node_key": "T6U8", "refreshed": True}
        async with async_session() as db:
            r = (await db.execute(text("SELECT prompt_template, tool_config FROM dag_nodes WHERE job_id = :j AND "
                                       "node_key = 'T6U8'"), {"j": jid})).mappings().first()
        assert r["prompt_template"].startswith(LEGACY_13F.split(" Record `ticker`")[0])
        assert ("Record `ticker` in each item's payload: the ticker of the very thing the item is about, read from the "
                "item's own data (the document or record it came from) -- never one of whoever filed or published it, "
                "nor of something it merely mentions, and never made up. An item about no single one (several, or "
                "none) gets none.") in r["prompt_template"]
        assert r["tool_config"]["payload_writer"] == {
            "table": "ingested_item", "column": "payload", "kind": "sec_edgar", "field": "ticker",
            "reader": "paper_signal_generator._normalize_item", "keys": ["holdings"], "key": "ticker", "reads": []}
        async with async_session() as db:
            assert await refresh_contract_fix(db, jid, "T6U8", "operator") == {"node_key": "T6U8", "refreshed": False}
    finally:
        await _drop(jid)
