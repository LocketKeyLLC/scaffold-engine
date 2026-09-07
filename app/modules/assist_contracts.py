"""§17.968 — check the artefacts the engine authored against each other.

Operator: *"did you again give it the answer instead of fixing the problem? …it
feels as if you have not addressed the root cause."*

They were right. §17.965 recorded a file's SIZE, §17.967 recorded its CONTENT
HASH and proved the file on disk was correct — and then the engine still had no
way to find out why the page was blank, because the actual defect lived in the
relationship between two files rather than inside either one:

    server.js   app.get('/status', …)  →  const results = {};  res.json(results)
    App.jsx     fetch(`${API_BASE}/status`) → setServices(data)
                services.find(item => item.vmid === s.id)

`.find` is an Array method. The response is an object. React throws during
render and the page is blank. **The engine wrote both files.** Both sat in its
own transcript for hours while it rewrote a third file that was already right.

§17.967 shipped one sentence of prose about this — "look at the OTHER files and
services this session set up" — and this codebase has said repeatedly, since
§17.882, that a prompt rule is not a mechanism. Worse, §17.967 threw the file
content away the moment it had hashed it (`rec.pop("body")`), so the engine
could not have compared them even if it wanted to.

So: keep what was written, and check the pairs deterministically. Nothing here
calls a model — every finding below is a contradiction between two strings the
engine itself emitted.

The three checks are ordered by how certain they are:

* PORT     — a client pointed at a port nothing listens on. Certain.
* ENDPOINT — a client calling a path no server serves. Certain.
* SHAPE    — a server returning an object where the client applies an Array
             method. This is the live bug, and the one that needs a chain of
             inference; it is deliberately conservative at every link.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("scaffold")

# Server side ────────────────────────────────────────────────────────────────
_ROUTE_RE = re.compile(
    r"""(?:app|router)\.(?:get|post|put|patch|delete)\(\s*['"`]([^'"`]+)['"`]""")
_ROUTE_BLOCK_RE = re.compile(
    r"""(?:app|router)\.(?:get|post|put|patch|delete)\(\s*['"`]([^'"`]+)['"`]"""
    r"""(.*?)(?=(?:app|router)\.(?:get|post|put|patch|delete)\(|\Z)""", re.S)
_LISTEN_RE = re.compile(r"\.listen\(\s*(\d{2,5})")
_RES_JSON_VAR_RE = re.compile(r"res\.json\(\s*([A-Za-z_$][\w$]*)\s*\)")
_RES_JSON_LITERAL_RE = re.compile(r"res\.json\(\s*([\[{])")
# Python/FastAPI and Flask, so this is not purely a Node checker.
_PY_ROUTE_RE = re.compile(
    r"""@(?:app|router)\.(?:get|post|put|patch|delete)\(\s*['"]([^'"]+)['"]""")

# Client side ────────────────────────────────────────────────────────────────
_FETCH_RE = re.compile(r"""(?:fetch|axios\.\w+|axios)\(\s*\\?[`'"]([^`'"]*)""")
_BASE_URL_RE = re.compile(
    r"""(?:API_BASE|BASE_URL|baseURL|API_URL)\s*[:=]\s*\\?[`'"]([^`'"]+)""")
_USESTATE_RE = re.compile(r"""(?:const|let)\s*\[\s*(\w+)\s*,\s*(set\w+)\s*\]\s*=\s*useState""")
_SETTER_CALL_RE = re.compile(r"\b(set\w+)\(\s*([A-Za-z_$][\w$]*)\s*\)")

_ARRAY_ONLY_METHODS = ("find", "map", "filter", "forEach", "reduce",
                       "some", "every", "flatMap", "findIndex")
_MAX_BODY = 20000


def _url_path(raw: str) -> str:
    """The path portion of whatever the client wrote, template markers dropped."""
    s = re.sub(r"\$\{[^}]*\}", "", raw or "")          # ${API_BASE}
    s = re.sub(r"^\\?https?://[^/]*", "", s)           # scheme + host
    s = s.split("?")[0].rstrip("`'\"\\ ")
    return s if s.startswith("/") else ("/" + s if s else "")


def _ports_in(text: str) -> set[str]:
    return set(_LISTEN_RE.findall(text or ""))


def _client_ports(text: str) -> set[str]:
    out = set()
    for base in _BASE_URL_RE.findall(text or ""):
        m = re.search(r":(\d{2,5})", base)
        if m:
            out.add(m.group(1))
    return out


def server_endpoints(text: str) -> set[str]:
    return {p for p in _ROUTE_RE.findall(text or "")} | {
        p for p in _PY_ROUTE_RE.findall(text or "")}


def client_endpoints(text: str) -> set[str]:
    out = set()
    for raw in _FETCH_RE.findall(text or ""):
        p = _url_path(raw)
        if p and p != "/":
            out.add(p)
    return out


def response_shapes(text: str) -> dict[str, str]:
    """endpoint -> 'object' | 'array', for handlers whose shape is unambiguous."""
    shapes: dict[str, str] = {}
    for path, handler in _ROUTE_BLOCK_RE.findall(text or ""):
        lit = _RES_JSON_LITERAL_RE.search(handler)
        if lit:
            shapes[path] = "array" if lit.group(1) == "[" else "object"
            continue
        var = _RES_JSON_VAR_RE.search(handler)
        if not var:
            continue
        decl = re.search(
            rf"(?:const|let|var)\s+{re.escape(var.group(1))}\s*=\s*([\[{{])", handler)
        if decl:
            shapes[path] = "array" if decl.group(1) == "[" else "object"
    return shapes


def state_sources(text: str) -> dict[str, str]:
    """react state var -> the endpoint whose response is stored in it.

    Conservative: the setter call must appear shortly after the fetch, and the
    value must be the bound `.then` argument rather than a literal.
    """
    setters = {s: st for st, s in _USESTATE_RE.findall(text or "")}
    links: dict[str, str] = {}
    for m in re.finditer(r"(?:fetch|axios\.\w+|axios)\(", text or ""):
        after = text[m.start():m.start() + 600]
        path = ""
        pm = _FETCH_RE.search(after)
        if pm:
            path = _url_path(pm.group(1))
        if not path:
            continue
        for sm in _SETTER_CALL_RE.finditer(after):
            st = setters.get(sm.group(1))
            if st and sm.group(2) not in ("true", "false", "null", "undefined"):
                links.setdefault(st, path)
    return links


def array_ops_on(text: str, var: str) -> set[str]:
    return {m.group(1) for m in re.finditer(
        rf"\b{re.escape(var)}\.({'|'.join(_ARRAY_ONLY_METHODS)})\(", text or "")}


# §17.972 — a placeholder the engine wrote and never came back for.
#
# Live, T33. The engine composed `server.js` containing
#
#     const PVE_TOKEN_ID = '<PVE_TOKEN_ID>';
#     const PVE_TOKEN_SECRET = '<PVE_TOKEN_SECRET>';
#
# and the operator pasted that file back, placeholders intact, across EIGHT
# turns (1725-1741). Nothing ever resolved them. The backend authenticates to
# Proxmox with the literal string `<PVE_TOKEN_ID>`, every call fails, `/status`
# returns its error payload, the frontend `.catch` leaves `services` empty, and
# the panel renders every service as "unknown" — which is exactly what the
# operator reported, twenty turns and one whole step later:
#
#   "the control panel page is loaded now … though i'm unsure why if its all
#    connected its unsure what is currently on."
#
# §17.851b resolves placeholders in COMMANDS the engine emits. A placeholder
# written into a FILE escapes that entirely: it is syntactically valid, nothing
# errors at write time, and it fails silently at runtime as a permission or
# auth problem several steps downstream — the hardest possible thing to trace
# back to its cause.
#
# The engine wrote the file and put the marker there itself. Finding it needs no
# model and no research; it needs someone to look.

_PLACEHOLDER_RE = re.compile(r"<([A-Z][A-Z0-9_]{2,63})>")
# Uppercase HTML/JSX tags are the only realistic collision; `<div>`, `<Button>`,
# `Array<String>` and `a < B` already miss on case or length.
_TAG_WORDS = frozenset({
    "HTML", "HEAD", "BODY", "DIV", "SPAN", "TABLE", "THEAD", "TBODY", "FORM",
    "INPUT", "SCRIPT", "STYLE", "TITLE", "META", "LINK", "PRE", "CODE", "MAIN",
    "HEADER", "FOOTER", "SECTION", "ARTICLE", "NAV", "BUTTON", "LABEL", "IMG",
    "DOCTYPE", "BLOCKQUOTE", "TEXTAREA", "SELECT", "OPTION", "IFRAME", "SVG",
})


def find_unresolved_placeholders(artefacts: dict[str, str]) -> list[dict]:
    """Files the engine wrote that still carry a `<PLACEHOLDER>` marker."""
    out: list[dict] = []
    for path, body in (artefacts or {}).items():
        names = sorted({
            n for n in _PLACEHOLDER_RE.findall(body or "")
            if n not in _TAG_WORDS
        })
        if names:
            out.append({
                "kind": "placeholder", "client": path, "names": names,
                "detail": (
                    f"`{path}` still contains the placeholder"
                    + ("s " if len(names) > 1 else " ")
                    + ", ".join(f"`<{n}>`" for n in names)
                    + " — the literal marker, not a real value. Whatever reads "
                      "that file is using the text `<" + names[0] + ">` as if it "
                      "were the setting, so it fails at RUNTIME, silently, and "
                      "usually looks like an auth or permission problem "
                      "somewhere else entirely. Ask the operator for the value "
                      "and write it in.")})
    return out


def find_contract_conflicts(artefacts: dict[str, str]) -> list[dict]:
    """Contradictions between files the engine wrote. Deterministic; no model.

    ``artefacts`` is ``{path: body}``. Findings carry the two paths involved so
    the operator is told which pair disagrees, not merely that something is off.
    """
    if not artefacts:
        return []
    out: list[dict] = []
    if len(artefacts) < 2:
        # Cross-checks need a pair; a placeholder does not.
        return find_unresolved_placeholders(artefacts)

    served: dict[str, str] = {}          # endpoint -> file that serves it
    shapes: dict[str, tuple[str, str]] = {}   # endpoint -> (shape, file)
    listen: dict[str, str] = {}          # port -> file
    for path, body in artefacts.items():
        for ep in server_endpoints(body):
            served.setdefault(ep, path)
        for ep, shape in response_shapes(body).items():
            shapes.setdefault(ep, (shape, path))
        for port in _ports_in(body):
            listen.setdefault(port, path)

    # §17.972 — highest priority: a marker the engine left in its own file.
    out.extend(find_unresolved_placeholders(artefacts))

    for path, body in artefacts.items():
        calls = client_endpoints(body)
        if not calls:
            continue
        # PORT — pointed at something nothing listens on.
        if listen:
            for port in _client_ports(body):
                if port not in listen:
                    out.append({
                        "kind": "port", "client": path,
                        "detail": (f"`{path}` calls port {port}, but the only "
                                   f"port anything in this session listens on is "
                                   + ", ".join(sorted(listen)))})
        # ENDPOINT — calling a path nothing serves.
        if served:
            for ep in sorted(calls):
                if ep not in served:
                    near = ", ".join(sorted(served)) or "none"
                    out.append({
                        "kind": "endpoint", "client": path, "endpoint": ep,
                        "detail": (f"`{path}` calls `{ep}`, which no server file "
                                   f"in this session defines (served: {near})")})
        # SHAPE — an Array method on something the server sends as an object.
        for var, ep in state_sources(body).items():
            shape = shapes.get(ep)
            if not shape or shape[0] != "object":
                continue
            ops = array_ops_on(body, var)
            if ops:
                out.append({
                    "kind": "shape", "client": path, "server": shape[1],
                    "endpoint": ep, "var": var, "ops": sorted(ops),
                    "detail": (
                        f"`{shape[1]}` returns an OBJECT from `{ep}`, but "
                        f"`{path}` stores that response in `{var}` and calls "
                        + ", ".join(f"`{o}()`" for o in sorted(ops))
                        + " on it. Those are Array methods; on an object they "
                          "throw, which for a UI means a blank page rather than "
                          "an error message.")})
    return out


def render_contract_conflicts(conflicts: list[dict] | None) -> str:
    """The prompt block. Ground-truth tier — these are the engine's own files."""
    if not conflicts:
        return ""
    lines = ["### TWO FILES THIS SESSION WROTE CONTRADICT EACH OTHER "
             "(deterministic — read from their actual contents, not inferred "
             "from the symptom). This is the most likely cause of whatever is "
             "not working, and it will not be fixed by rewriting either file "
             "unchanged:"]
    for c in conflicts[:5]:
        lines.append(f"- {c['detail']}")
    lines.append("Fix the DISAGREEMENT — change one side to match the other — "
                 "and say which side you changed and why.")
    return "\n".join(lines)


def bounded_artefacts(state: dict | None) -> dict[str, str]:
    """The retained bodies from the §17.965 ledger, ready to cross-check."""
    out: dict[str, str] = {}
    for path, rec in (state or {}).items():
        if isinstance(rec, dict) and isinstance(rec.get("body"), str):
            out[path] = rec["body"][:_MAX_BODY]
    return out
