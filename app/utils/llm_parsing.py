"""Shared LLM output parsing utilities.

Regexes are compiled once at import time and reused across all calls.
"""
import json
import logging
import re

from json_repair import repair_json

logger = logging.getLogger("scaffold.parsing")

# Two compiled patterns cover both <think> and <thinking>:
#   - CLOSED: well-formed <tag>...</tag>
#   - OPEN:   stray <tag> with no closing tag (truncated mid-stream)
# Both branches are non-greedy for the closed case and anchored to end-of-text
# for the open case, matching the semantics of the prior four regexes.
_THINK_CLOSED_RE = re.compile(r"<(?:think|thinking)>.*?</(?:think|thinking)>", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<(?:think|thinking)>.*", re.DOTALL)

# Markdown fence stripper: match ``` optionally followed by a language tag,
# anywhere in the string. Previously only stripped leading fences; now also
# catches trailing ``` and fences embedded around prose.
_FENCE_RE = re.compile(r"^[ \t]*```[A-Za-z0-9_+-]*[ \t]*\r?\n?|\r?\n?[ \t]*```[ \t]*$|```", re.MULTILINE)


#: §17.1642 — a CLOSING tag with no opener: the model's chat template opened the block, so the output starts
#: inside it and the reasoning runs up to the first `</think>`.
_THINK_ORPHAN_CLOSE_RE = re.compile(r"^.*?</(?:think|thinking)>", re.DOTALL)


def strip_think_tags(text: str) -> str:
    """Remove <think>/<thinking> reasoning blocks (closed, open, or opened by the template) from LLM output.

    §17.1642 — live (research d4b7ce68, 2026-10-18): the summary began "Let me analyze the task…" and ran 80
    lines of reasoning to a lone `</think>` before the answer; the opening tag was in the model's template, not
    in its output, so neither pattern above matched and the reasoning was stored as the summary."""
    cleaned = _THINK_CLOSED_RE.sub("", text)
    cleaned = _THINK_OPEN_RE.sub("", cleaned)
    if re.search(r"</(?:think|thinking)>", cleaned):
        cleaned = _THINK_ORPHAN_CLOSE_RE.sub("", cleaned, count=1)
    return cleaned.strip()


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown fences from text, wherever they appear."""
    return _FENCE_RE.sub("", text).strip()


def _extract_by_brackets(text: str, open_b: str, close_b: str):
    """Pull the outermost bracket-delimited substring from text.

    Counts nesting depth from the first `open_b` and returns the slice up
    to its matching `close_b`. Trailing junk after the matched close
    bracket is dropped, which matters for malformed LLM output like
    `{"k": "v"}} extra` — `rfind` would have included the second `}`.
    Falls back to `rfind` semantics if no balanced match is found, so
    the downstream `repair_json` still has something to chew on.
    """
    start = text.find(open_b)
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == open_b:
            depth += 1
        elif ch == close_b:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    end = text.rfind(close_b)
    if end > start:
        return text[start : end + 1]
    return None


def parse_json_object(raw: str):
    """Parse a JSON object from raw LLM output (4-step chain)."""
    # §17.601 — try the raw text verbatim FIRST. The think-tag OPEN regex
    # (deletes from a literal "<think>" to end-of-text) and the markdown-fence
    # stripper both mutate content that can legitimately appear INSIDE a JSON
    # string value, so pre-stripping can corrupt already-valid JSON.
    try:
        result = json.loads(raw)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    cleaned = strip_think_tags(raw)
    cleaned = _strip_markdown_fences(cleaned)
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    try:
        repaired = repair_json(cleaned, return_objects=True)
        if isinstance(repaired, dict):
            logger.info("json_repaired: method=full_text target=object")
            return repaired
    except Exception:
        pass
    fragment = _extract_by_brackets(cleaned, "{", "}")
    if fragment:
        try:
            result = json.loads(fragment)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
        try:
            repaired = repair_json(fragment, return_objects=True)
            if isinstance(repaired, dict):
                logger.info("json_repaired: method=brace_extract target=object")
                return repaired
        except Exception:
            pass
    return None


def _balanced_object_spans(text: str):
    """Every balanced ``{...}`` span of ``text`` as ``(start, end)``, nested ones included (one pass, a stack)."""
    stack: list[int] = []
    for i, ch in enumerate(text):
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            yield stack.pop(), i + 1


def parse_json_object_for_schema(raw: str, schema: dict | None):
    """§17.1824 — the JSON object in ``raw`` that answers ``schema``: one that carries the schema's own keys.

    ``parse_json_object`` takes the first object it can build, wherever it sits. A coaxed reply that is prose
    quoting code hands it a stray literal: live 2026-10-26 (job 5b4880c0 T6), the verifier wrote 51k chars of
    analysis quoting the step's ``metrics={"rejected": True}``, that became its verdict, the "missing 'pass' key"
    counted as a failed verification and the redraw that was left never ran. Here an object counts only if it
    has a key of ``schema["properties"]`` and either every key of it is one, or it holds every required key (a
    quoted ``{"operator_id": .., "reason": ..}`` shares one key with a verdict and is still not one); among those
    the one with the most required keys, then the most properties, then the last (the answer follows the
    reasoning) wins. ``None`` when nothing in ``raw`` answers
    the schema. A schema without properties keeps ``parse_json_object``.
    """
    props = set(((schema or {}).get("properties") or {}).keys())
    if not props:
        return parse_json_object(raw)
    required = set((schema or {}).get("required") or ()) & props

    def rank(obj, pos: int):
        keys = props & obj.keys()
        if not keys or not (obj.keys() <= props or required <= obj.keys()):
            return None
        return (len(required & keys), len(keys), pos)

    whole = parse_json_object(raw)
    best = None
    if isinstance(whole, dict) and (r := rank(whole, -1)) is not None:
        if required <= whole.keys():
            return whole
        best = (r, whole)
    text = raw or ""
    quoted = tuple(f'"{k}"' for k in props)
    for start, end in _balanced_object_spans(text):
        span = text[start:end]
        if not any(q in span for q in quoted):
            continue
        try:
            obj = json.loads(span)
        except json.JSONDecodeError:
            try:
                obj = repair_json(span, return_objects=True)
            except Exception:
                continue
        if not isinstance(obj, dict) or (r := rank(obj, end)) is None:
            continue
        if best is None or r > best[0]:
            best = (r, obj)
    return best[1] if best else None


_CALL_NAMES = {"true": True, "false": False, "null": None, "True": True, "False": False, "None": None}


def _call_span(text: str, open_at: int) -> int | None:
    """The index just past the ``)`` that closes the ``(`` at ``open_at``, skipping quoted strings."""
    depth, quote, i = 0, "", open_at
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


_KW_PREFIX = "kw__"


def _unkeyword_arg_names(call: str) -> str:
    """``pass=false`` -> ``kw__pass=false`` at the call's own argument positions (not inside strings), so a
    keyword-named argument -- the verifier's ``pass`` -- parses as Python."""
    import keyword
    out, quote, depth, i, at_arg = [], "", 0, 0, False
    while i < len(call):
        ch = call[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < len(call):
                out.append(call[i + 1]); i += 1
            elif ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote, at_arg = ch, False
        elif ch in "([{":
            depth += 1
            at_arg = depth == 1 and ch == "("
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 1:
            at_arg = True
        elif at_arg and (ch.isalpha() or ch == "_"):
            j = i
            while j < len(call) and (call[j].isalnum() or call[j] == "_"):
                j += 1
            word, rest = call[i:j], call[j:].lstrip()
            if keyword.iskeyword(word) and rest.startswith("=") and not rest.startswith("=="):
                word = _KW_PREFIX + word
            out.append(word); i, at_arg = j, False
            continue
        elif not ch.isspace():
            at_arg = False
        out.append(ch)
        i += 1
    return "".join(out)


def _literal(node):
    import ast
    if isinstance(node, ast.Name) and node.id in _CALL_NAMES:
        return _CALL_NAMES[node.id]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_literal(n) for n in node.elts]
    if isinstance(node, ast.Dict):
        return {_literal(k): _literal(v) for k, v in zip(node.keys, node.values, strict=True)}
    return ast.literal_eval(node)


def parse_tool_call_text(raw: str, name: str, schema: dict | None):
    """§17.1827 — the arguments of a tool call the model wrote as TEXT: ``name(k=v, ...)`` or ``name({...})``.

    Live 2026-10-26 (job 5b4880c0 T6): glm-5.3-flash ended 41k chars of reasoning with
    ``</think>record_verification(pass=false, reason="...", confidence=0.8)`` -- no native call, no JSON -- and the
    attempt failed on "verifier produced no tool call", its real reason (a batch that always skips) lost; 12 such
    verdicts in 14 days. The LAST call of ``name`` in ``raw`` wins; its arguments must answer ``schema`` the way
    ``parse_json_object_for_schema`` asks (a key of the schema, no foreign key or every required key)."""
    import ast
    text = raw or ""
    at = text.rfind(name + "(")
    while at >= 0:
        if at == 0 or not (text[at - 1].isalnum() or text[at - 1] == "_"):
            end = _call_span(text, at + len(name))
            if end:
                try:
                    call = ast.parse(_unkeyword_arg_names(text[at:end].strip()), mode="eval").body
                    if isinstance(call, ast.Call) and not call.args:
                        args = {kw.arg.removeprefix(_KW_PREFIX): _literal(kw.value) for kw in call.keywords if kw.arg}
                    elif isinstance(call, ast.Call) and len(call.args) == 1 and not call.keywords:
                        args = _literal(call.args[0])
                    else:
                        args = None
                except (SyntaxError, ValueError, TypeError):
                    args = None
                if isinstance(args, dict) and args:
                    return parse_json_object_for_schema(json.dumps(args, default=str), schema)
        at = text.rfind(name + "(", 0, at)
    return None


def diagnose_json_object_parse(raw: str) -> dict | None:
    """§17.293 — return diagnostic context for the first ``JSONDecodeError``
    encountered while parsing ``raw`` as a JSON object.

    Mirrors the first step of ``parse_json_object``'s pipeline (strip
    think-tags + markdown fences, then ``json.loads``). Returns a dict
    with ``lineno`` / ``colno`` / ``msg`` / ``pos`` from the exception
    when that first parse fails; returns ``None`` when the first parse
    succeeds (meaning ``parse_json_object`` would also have succeeded).

    Operator-facing usage: when ``parse_json_object`` returns ``None``,
    callers can attach this diagnostic to the failure dict so the
    operator sees ``line 3, col 12, "Expecting ',' delimiter"`` next to
    the truncated raw output, instead of having to scan the snippet by
    eye. The deeper repair / fragment-extract steps in the pipeline
    swallow further errors silently; this helper deliberately reports
    only the first one, which is the most diagnostic for the operator
    (it pinpoints where the LLM first deviated from valid JSON).
    """
    # §17.601 — mirror parse_json_object's raw-first fast path: if the raw
    # text parses verbatim, parse_json_object would too, so report no error.
    try:
        if isinstance(json.loads(raw), dict):
            return None
    except json.JSONDecodeError:
        pass
    cleaned = strip_think_tags(raw)
    cleaned = _strip_markdown_fences(cleaned)
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return None
        # Parsed cleanly but wrong shape (e.g., an array) — that's a
        # caller-side concern, not a JSONDecodeError. Report None so the
        # parse_error block doesn't lie about the failure mode.
        return None
    except json.JSONDecodeError as e:
        return {
            "lineno": e.lineno,
            "colno": e.colno,
            "msg": e.msg,
            "pos": e.pos,
        }


def parse_json_array(raw: str):
    """Parse a JSON array from raw LLM output (4-step chain)."""
    # §17.601 — raw verbatim first (see parse_json_object).
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    cleaned = strip_think_tags(raw)
    cleaned = _strip_markdown_fences(cleaned)
    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    try:
        repaired = repair_json(cleaned, return_objects=True)
        if isinstance(repaired, list):
            logger.info("json_repaired: method=full_text target=array")
            return repaired
    except Exception:
        pass
    fragment = _extract_by_brackets(cleaned, "[", "]")
    if fragment:
        try:
            result = json.loads(fragment)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass
        try:
            repaired = repair_json(fragment, return_objects=True)
            if isinstance(repaired, list):
                logger.info("json_repaired: method=bracket_extract target=array")
                return repaired
        except Exception:
            pass
    return None
