"""§17.2005 — the lab's pages as the running engine serves them, read by the engine itself for a check after delivery.

Live (69fe61c4 T13 "Verify dashboard renders", 2026-11-24): after the operator answered the delivery question and the
engine confirmed the code was running, the check -- an LLM step -- refused three times: "this step requires live
verification against a running engine -- HTTP requests to /labs/paper/dashboard … -- but it was routed to an LLM node
with no shell/execution channel. I cannot run the checks and will not fabricate results." It was right: nothing had
read the page. The engine IS the running engine, so it reads its own lab's GET views over its loopback, with and
without the API key, plus the assets each page loads and the same-page links it offers (`?span=1w`), and hands the
check what it saw -- statuses, sizes, the page's data arrays and its visible text.
"""
from __future__ import annotations

import hashlib
import html
import json
import re

from app.modules.engine_contracts import view_routes

_MAX_ROUTES = 6
_MAX_LINKS = 8
_MAX_ASSETS = 6
_TEXT_CHARS = 900

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_ASSET_RE = re.compile(r"""(?:src|href)=["'](/[^"'?#]+)["']""", re.I)
_QUERY_LINK_RE = re.compile(r"""href=["'](\?[^"'#]*)["']""", re.I)
_ARRAY_RE = re.compile(r'"(\w+)"\s*:\s*\[')
_STRIP_RE = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)


def _arrays(body: str) -> dict[str, int]:
    """The JSON arrays a page carries (`"dates": [...]`) and how many items each holds."""
    out: dict[str, int] = {}
    for m in _ARRAY_RE.finditer(body):
        start, depth = m.end() - 1, 0
        for i in range(start, min(len(body), start + 200_000)):
            depth += {"[": 1, "]": -1}.get(body[i], 0)
            if depth == 0:
                try:
                    v = json.loads(body[start:i + 1])
                except ValueError:
                    break
                if isinstance(v, list):
                    out.setdefault(m.group(1), len(v))
                break
    return out


def _text(body: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_STRIP_RE.sub(" ", body))).strip()


def lab_paths(entry_code: str, served: list[str]) -> list[str]:
    """The engine's served paths (`GET /openapi.json`) of the entry module's GET views, without path parameters."""
    rel = [r for r, _h, _ln in view_routes(entry_code) if "{" not in r]
    return [p for p in served if p.startswith("/labs/") and "{" not in p and any(p.endswith(r) for r in rel)][:_MAX_ROUTES]


async def read_lab_pages(entry_code: str, base_url: str, api_key: str, client=None) -> str:
    """What the running engine serves for each GET view of the lab's entry, as a block for a check step."""
    import httpx
    own = client is None
    client = client or httpx.AsyncClient(base_url=base_url, timeout=30.0)
    hdr = {"X-API-Key": api_key}
    lines: list[str] = []
    try:
        spec = (await client.get("/openapi.json")).json()
        paths = lab_paths(entry_code, [p for p, ops in (spec.get("paths") or {}).items() if "get" in ops])
        for path in paths:
            r = await client.get(path, headers=hdr)
            body = r.text
            ctype = r.headers.get("content-type", "").split(";")[0]
            noauth = (await client.get(path)).status_code
            lines.append(f"### GET {path}")
            lines.append(f"- with the API key: {r.status_code} {ctype}, {len(r.content)} bytes; without it: {noauth}")
            if ctype == "text/html":
                t = _TITLE_RE.search(body)
                if t:
                    lines.append(f"- title: {html.unescape(t.group(1)).strip()}")
                for a in list(dict.fromkeys(_ASSET_RE.findall(body)))[:_MAX_ASSETS]:
                    ar = await client.get(a, headers=hdr)
                    lines.append(f"- loads `{a}`: {ar.status_code}, {len(ar.content)} bytes")
                base = hashlib.sha256(r.content).hexdigest()
                for q in list(dict.fromkeys(_QUERY_LINK_RE.findall(body)))[:_MAX_LINKS]:
                    lr = await client.get(path + q, headers=hdr)
                    same = "same page as the default" if hashlib.sha256(lr.content).hexdigest() == base else "differs"
                    lines.append(f"- link `{q}`: {lr.status_code}, {len(lr.content)} bytes ({same}); arrays "
                                 + (json.dumps(_arrays(lr.text)) if _arrays(lr.text) else "none"))
                arr = _arrays(body)
                lines.append("- data arrays in the page (name: items): " + (json.dumps(arr) if arr else "none"))
                lines.append(f"- visible text: {_text(body)[:_TEXT_CHARS]}")
            else:
                lines.append(f"- body: {body[:_TEXT_CHARS]}")
    except Exception as exc:                                   # the evidence says it could not be read; never invented
        lines.append(f"- the engine could not read its own pages: {type(exc).__name__}: {str(exc)[:200]}")
    finally:
        if own:
            await client.aclose()
    if not lines:
        return ""
    return ("\n\n## The running engine's lab pages, read by the engine just now (§17.2005)\n"
            "The engine requested each page of this lab's entry from itself (the code the operator merged and "
            "redeployed). Judge the check from this; it is the live page, not a description of it.\n"
            + "\n".join(lines) + "\n")


_CHECK_TITLE_RE = re.compile(r"^\s*(?:verify|check|confirm|validate|test)\b", re.I)


def after_delivery(nodes: list[dict], node_key: str) -> bool:
    """A step stands after the job's delivery question, answered: an ancestor (through `depends_on`) is the
    "Is this job's code merged into the engine and running?" question, done."""
    from app.modules.engine_plan import _DELIVER_QUESTION_RE
    by = {str(n["node_key"]): n for n in nodes}
    seen: set[str] = set()
    todo = list((by.get(node_key) or {}).get("depends_on") or [])
    while todo:
        k = str(todo.pop())
        if k in seen or k not in by:
            continue
        seen.add(k)
        n = by[k]
        if _DELIVER_QUESTION_RE.match(str(n.get("title") or "")) and n.get("status") == "done":
            return True
        todo.extend(n.get("depends_on") or [])
    return False


def is_check(node: dict) -> bool:
    return (str(node.get("node_type") or "") == "checkpoint"
            or bool(_CHECK_TITLE_RE.match(str(node.get("title") or ""))))


async def block_for_step(db, job_id: str, node: dict) -> str:
    """§17.2005 — for an LLM check that stands after the job's answered delivery question: the lab's pages as the
    running engine serves them (the job's entry module, `engine_plan.wiring_modules`). Empty for any other step."""
    from sqlalchemy import text

    from app.config import settings
    from app.modules import engine_plan, engine_source
    if str(node.get("tool") or "").lower() != "llm" or not is_check(node):
        return ""
    rows = (await db.execute(text("SELECT node_key, title, status, depends_on FROM dag_nodes WHERE job_id = :j"),
                             {"j": job_id})).mappings().all()
    if not after_delivery([dict(r) for r in rows], str(node.get("node_key") or "")):
        return ""
    files = engine_source.job_files(await engine_source.code_rows(db, job_id))
    entries = engine_plan.wiring_modules(files)
    if not entries:
        return ""
    return await read_lab_pages(files[f"app/modules/{entries[0]}.py"][0], settings.web_loopback_url,
                                settings.scaffold_api_key.get_secret_value())
