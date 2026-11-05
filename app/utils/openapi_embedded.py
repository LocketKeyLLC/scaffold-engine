"""§17.1929 — an API reference page that carries its endpoint as an OpenAPI document in the HTML, rendered as text.

A reference site built on an OpenAPI renderer ships each endpoint's method, path, parameters, request body and response
fields as JSON in a `<script>` block (a server-side-render payload), and draws the tables in the browser: text
extraction keeps the one-line summary and nothing else. Live (follow-up 5b4880c0, 2026-11-05): Alpaca's "Create an
Order" page is 826 KB with `time_in_force` 30 times in that JSON; trafilatura kept 334 characters -- under the 400 the
research keeps -- so the research said "the Create an Order reference page was not retrieved", and the audit could not
verify one order field the code sends. Pure, bounded, fail-soft: a page with no such document gives "".
"""
from __future__ import annotations

import html as _html
import json
import re

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
_JSONISH_RE = re.compile(r"""type\s*=\s*["'](?:application/(?:[\w.+-]*json|x-ssr-props)|text/json)["']""", re.I)
_DATA_JSON_RE = re.compile(r"""data-json\s*=\s*"([^"]*)\"""", re.I)
#: the heading the rendered reference goes under in a page's extracted text
SECTION_HEAD = "API reference (from the page's embedded OpenAPI document):\n"
_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
_MAX_DEPTH = 4
_MAX_FIELDS = 80


def _docs(obj, found: list[dict], depth: int = 0) -> None:
    if depth > 12 or len(found) >= 4:
        return
    if isinstance(obj, dict):
        if isinstance(obj.get("paths"), dict) and ("openapi" in obj or "swagger" in obj):
            found.append(obj)
            return
        for v in obj.values():
            _docs(v, found, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:200]:
            _docs(v, found, depth + 1)


def _ref(doc: dict, node, seen: frozenset = frozenset()):
    """`node` with a local `$ref` (`#/components/schemas/Order`) followed; the target itself when it is one."""
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and node["$ref"].startswith("#/"):
        ref = node["$ref"]
        if ref in seen:
            return {}
        seen = seen | {ref}
        cur = doc
        for part in ref[2:].split("/"):
            cur = cur.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(cur, dict) else None
        node = cur if isinstance(cur, dict) else {}
    return node if isinstance(node, dict) else {}


def _kind(doc: dict, s: dict) -> str:
    s = _ref(doc, s)
    t = s.get("type") or ("object" if "properties" in s or "additionalProperties" in s else "")
    if isinstance(t, list):
        t = " or ".join(str(x) for x in t)
    if t == "array":
        return f"array of {_kind(doc, s.get('items') or {}) or 'items'}"
    out = str(t or "")
    if s.get("format"):
        out += f" ({s['format']})"
    if s.get("enum"):
        out += " one of " + ", ".join(str(e) for e in s["enum"][:12])
    return out


def _desc(s: dict) -> str:
    d = re.sub(r"\s+", " ", str(s.get("description") or "")).strip()
    return f" -- {d[:200]}" if d else ""


def _fields(doc: dict, schema, prefix: str, out: list[str], depth: int = 0) -> None:
    s = _ref(doc, schema)
    for key in ("allOf", "oneOf", "anyOf"):
        for part in s.get(key) or []:
            _fields(doc, part, prefix, out, depth)
    if s.get("type") == "array" or "items" in s:
        _fields(doc, s.get("items") or {}, prefix + "[]", out, depth)
        return
    props = s.get("properties") or {}
    req = set(s.get("required") or [])
    for name, sub in props.items():
        if len(out) >= _MAX_FIELDS:
            return
        sub = _ref(doc, sub)
        out.append(f"  - `{prefix}{name}` {_kind(doc, sub)}{' (required)' if name in req else ''}{_desc(sub)}")
        if depth < _MAX_DEPTH and (sub.get("properties") or sub.get("items") or sub.get("allOf")
                                   or isinstance(sub.get("additionalProperties"), dict)):
            _fields(doc, sub, f"{prefix}{name}.", out, depth + 1)
    extra = s.get("additionalProperties")
    if isinstance(extra, dict) and depth < _MAX_DEPTH:
        _fields(doc, extra, prefix + "<key>.", out, depth + 1)


def render(doc: dict) -> str:
    """Each operation of an OpenAPI document as text: method and URL, parameters, request body, response fields."""
    servers = [str(s.get("url") or "") for s in doc.get("servers") or [] if isinstance(s, dict)]
    lines: list[str] = []
    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in _METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            lines.append(f"{method.upper()} {(servers[0] if servers else '')}{path}"
                         + (f" -- {op['summary']}" if op.get("summary") else ""))
            params = [_ref(doc, p) for p in [*shared, *(op.get("parameters") or [])]]
            if params:
                lines.append("Parameters:")
                for p in params[:_MAX_FIELDS]:
                    lines.append(f"  - `{p.get('name')}` in {p.get('in')} {_kind(doc, p.get('schema') or {})}"
                                 f"{' (required)' if p.get('required') else ''}{_desc(p)}")
            body = _ref(doc, op.get("requestBody") or {})
            for ctype, media in (body.get("content") or {}).items():
                out: list[str] = []
                _fields(doc, (media or {}).get("schema") or {}, "", out)
                if out:
                    lines.append(f"Request body ({ctype}):")
                    lines += out
                break
            for code, resp in (op.get("responses") or {}).items():
                resp = _ref(doc, resp)
                if not str(code).startswith("2"):
                    continue
                for ctype, media in (resp.get("content") or {}).items():
                    out = []
                    _fields(doc, (media or {}).get("schema") or {}, "", out)
                    if out:
                        lines.append(f"Response {code} ({ctype}) fields:")
                        lines += out
                    break
            lines.append("")
    return "\n".join(lines).strip()


def embedded_openapi_text(page_html: str, limit: int = 20000) -> str:
    """The OpenAPI documents a page embeds in its JSON `<script>` blocks, rendered (`render`); "" when there are none."""
    if not page_html or ('"openapi"' not in page_html and "&quot;openapi&quot;" not in page_html
                         and '"swagger"' not in page_html):
        return ""
    found: list[dict] = []
    try:
        for m in _SCRIPT_RE.finditer(page_html):
            attrs, body = m.group(1), m.group(2).strip()
            blobs = [body] if _JSONISH_RE.search(attrs) or attrs.strip().startswith('id="__NEXT_DATA__"') else []
            blobs += [_html.unescape(d) for d in _DATA_JSON_RE.findall(attrs)]
            for blob in blobs:
                if "openapi" not in blob and "swagger" not in blob:
                    continue
                try:
                    _docs(json.loads(blob), found)
                except (ValueError, RecursionError):
                    continue
        text = "\n\n".join(t for t in (render(d) for d in found) if t)
    except Exception:
        return ""
    return text[:limit]
