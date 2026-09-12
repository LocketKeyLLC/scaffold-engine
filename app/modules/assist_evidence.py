"""§17.1027 — the evidence layer: information need → ranked evidence → verified answer.

Why this module exists. Between §17.1018 and §17.1026 the same subsystem was
patched thirteen times in two days, each patch improving ONE input to ONE
retrieval path (the hint, the query, the prompt order, a phrase table), and
each verified on the component just edited. The live log for the day after
shows what all of that left untouched — the fix path's research queries,
verbatim::

    assist_fix_research queries=['qm config 106', 'qm config 106']
    assist_fix_research queries=['pct exec 111 -- ls -la /opt', ...]
    assist_fix_research queries=['tailscale ip -4', ...]
    assist_fix_research queries=['no changes', 'no changes']

The engine searched the open web for the command line the operator had just
typed. Nothing between the operator's message and the search engine asked the
only question that matters: *what does this turn actually need to know?* And
nothing between the model's answer and the operator asked the second one:
*is what it just said supported by anything it was given?*

Three stages, shared by every path that answers an operator:

1. **Need** — `derive_need` classifies the operator's text deterministically
   (a pasted command line is not a question; an error line is; when there is
   neither, the step's recorded OPEN item is the thing to research; when there
   is nothing at all, the honest answer is "no research", not a junk query)
   and `finalize_query` enforces the operator's named hardware and the goal's
   own terms into whatever query an LLM generator produced.
2. **Evidence** — `rank_evidence` scores every retrieved source against the
   need (a fetched page that shares no term with the question is dropped, the
   way §17.729 already drops such SNIPPETS) and orders the rest by relevance,
   then by publication date, newest first. Dates ride on each source from the
   search engine or the page's own metadata, so "the most up-to-date answer"
   is something the prompt can actually express.
3. **Verification** — `verify_answer` checks the produced answer AGAINST its
   own grounding: every version number, IP address, external URL and
   non-standard port it states must appear in a source, a fact, a note or the
   operator's own words; every `[n]` citation is judged against the source it
   cites (§17.798's scorer, which had only ever run on the topic-research
   summary). A failing answer is regenerated ONCE with the exact unsupported
   values named; if it still fails, the unsupported values are listed under a
   visible warning rather than handed over as fact.

The house rule applies throughout: prompts are guidance, this is enforcement.
Nothing in this module names a technology — the §17.1025/1026 gates scan it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from app.config import settings

logger = logging.getLogger("scaffold")


# ───────────────────────────────────────────── Stage 1: the information need

# A line the operator TYPED at a shell, as opposed to what the shell printed.
# `user@host:path$ cmd`, `[user@host dir]# cmd`, `PS C:\> cmd`, or a bare `$ cmd`.
_PROMPT_LINE_RE = re.compile(
    r"^\s*(?:\[?[\w.-]+@[\w.-]+[^$#>\n]*\]?\s*[$#>]|PS\s+[A-Z]:\\[^>\n]*>|\$)\s")

# A heredoc opener on a command line: the body that follows is FILE CONTENT the
# operator pasted, not shell output, and must not be mined for symptoms.
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")

# Language-level: how a person opens a request for help. No technology here.
_QUESTION_LEAD_RE = re.compile(
    r"^\s*(?:how|what|where|why|which|when|who|can|could|should|would|will|"
    r"is|are|does|do|did|has|have|am|may|might|explain|help|walk|show|tell|"
    r"please|any\s+idea|i\s+(?:need|want|would\s+like|am\s+trying|am\s+not\s+"
    r"sure|can'?t|cannot|don'?t\s+(?:know|understand|see)))\b",
    re.IGNORECASE,
)

# The operator asked for the CURRENT state of something — the answer's date
# matters as much as its content.
_WANTS_LATEST_RE = re.compile(
    r"\b(?:latest|newest|current(?:ly)?|up[- ]to[- ]date|most\s+recent|"
    r"recent(?:ly)?|20\d\d)\b", re.IGNORECASE)

# Queries are held short — keyword engines degrade with length (§17.1023).
_QUERY_MAX_WORDS = 12


@dataclass
class Need:
    """What one operator turn needs from retrieval.

    ``kind``: ``question`` (they asked something), ``error`` (their paste
    carries a failure line), ``goal`` (no error and no question — research the
    step's recorded OPEN item), ``none`` (nothing a lookup could answer).
    """
    kind: str
    subject: str = ""
    query: str = ""
    hardware: list = field(default_factory=list)
    goal_terms: list = field(default_factory=list)
    wants_latest: bool = False
    reason: str = ""

    @property
    def researchable(self) -> bool:
        return self.kind != "none" and bool((self.query or self.subject).strip())


def split_paste(text: str) -> tuple[list[str], list[str]]:
    """Separate the lines the operator TYPED from the lines the machine PRINTED.

    Heredoc bodies are attributed to the command that opened them and dropped
    from both lists — a config file's contents are neither a command nor a
    symptom, and "error" is a common word in config files.
    """
    commands: list[str] = []
    output: list[str] = []
    terminator: Optional[str] = None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        if not line.strip():
            continue
        if _PROMPT_LINE_RE.match(line + " "):
            commands.append(line)
            m = _HEREDOC_RE.search(line)
            if m:
                terminator = m.group(1)
            continue
        output.append(line)
    return commands, output


def recap_open_lines(step_recap: Optional[str]) -> list[str]:
    """The OPEN bullets of a step recap (§17.738 shape), then GOAL as fallback.

    DONE is excluded on purpose: finished work is not something to research.
    """
    if not (step_recap or "").strip():
        return []
    buckets: dict[str, list[str]] = {"OPEN": [], "GOAL": []}
    cur: Optional[str] = None
    for ln in step_recap.splitlines():
        st = ln.strip()
        if not st:
            continue
        head = st.split(":", 1)[0].strip().upper()
        if head in ("GOAL", "OPEN", "DONE", "CONSTRAINTS", "NEXT", "CONTEXT"):
            cur = head if head in buckets else None
            st = st.split(":", 1)[1].strip() if ":" in st else ""
            if not st:
                continue
        if cur:
            item = st.lstrip("-•* ").strip()
            if item:
                buckets[cur].append(item)
    return buckets["OPEN"] + buckets["GOAL"]


def _symptom_line(output_lines: list[str]) -> str:
    """The first PRINTED line that reads as a failure. Uses the fix path's own
    symptom vocabulary so the two cannot disagree about what an error is."""
    from app.modules.assist_guide import _SYMPTOM_LINE_RE
    for ln in output_lines:
        if re.search(_SYMPTOM_LINE_RE, ln, re.IGNORECASE):
            return ln.strip()
    return ""


def _keywords(text: str, limit: int) -> list[str]:
    from app.modules.assist_research_lib import _goal_keywords
    return _goal_keywords(text, limit=limit)


def _cap(q: str) -> str:
    return " ".join((q or "").split()[:_QUERY_MAX_WORDS])


def _shares_a_word(text: str, other: Optional[str]) -> bool:
    """True when two texts share a content word (≥4 chars, not a stopword)."""
    if not (text or "").strip() or not (other or "").strip():
        return False
    from app.modules.assist_research_lib import _GOAL_STOPWORDS

    def _words(t: str) -> set[str]:
        return {w for w in re.findall(r"[a-z][a-z0-9-]{3,}", t.lower())
                if w not in _GOAL_STOPWORDS}
    return bool(_words(text) & _words(other))


def derive_need(
    text: str,
    *,
    title: str = "",
    step_recap: Optional[str] = None,
    operator_notes: Optional[list] = None,
    goal_terms: Optional[str] = None,
    assume_question: bool = False,
) -> Need:
    """Classify one operator turn into the thing retrieval should look up.

    Deterministic, in priority order:

    1. a PRINTED line that reads as a failure → ``error``; the query is the
       fix path's symptom query (§17.882/909/1020), fed only the printed lines
       so a prompt prefix or a typed command can never become the query;
    2. prose that opens like a request (or ``assume_question``, for the ask
       path, where routing has already decided it is one) → ``question``;
    3. otherwise the step's recorded OPEN item (or the caller's ``goal_terms``)
       → ``goal``: the paste is context; the live blocker is the subject;
    4. otherwise ``none`` — a successful command's output with no open item is
       not a research question, and searching it is worse than not searching.
    """
    raw = (text or "").strip()
    commands, output = split_paste(raw)
    from app.modules.assist_render import hardware_for_text
    hardware = list(hardware_for_text(f"{title} {raw}", operator_notes))

    symptom = _symptom_line(output)
    if symptom:
        from app.modules.assist_guide import _error_focus_query
        q = _error_focus_query(title, "\n".join(output), operator_notes)
        return Need(
            kind="error", subject=symptom, query=_cap(q), hardware=hardware,
            goal_terms=_keywords(symptom, 4),
            wants_latest=bool(_WANTS_LATEST_RE.search(symptom)),
            reason="printed line reads as a failure",
        )

    is_prose = not commands and len(output) <= 4
    if assume_question or (is_prose and raw and (
            raw.endswith("?") or _QUESTION_LEAD_RE.match(raw))):
        # The step's OPEN item aims the search (§17.1022) ONLY when the
        # question is about it. Measured live: a router question asked while
        # the step's open item had moved on to a backend service got
        # "control-panel backend attempted start" appended to its query. Same
        # rule as the hardware: a term earns its place by sharing a
        # distinctive word with the operator's text.
        terms = _keywords(goal_terms or "", 4) if _shares_a_word(raw, goal_terms) else []
        q = " ".join(hardware + _keywords(raw, 6) + [t for t in terms if t not in raw.lower()])
        return Need(
            kind="question", subject=raw, query=_cap(q), hardware=hardware,
            goal_terms=terms,
            wants_latest=bool(_WANTS_LATEST_RE.search(raw)),
            reason="operator asked",
        )

    open_items = recap_open_lines(step_recap)
    if not open_items and (goal_terms or "").strip():
        open_items = [goal_terms.strip()]
    if open_items:
        subject = open_items[0]
        # The OPEN line names the live blocker; the hardware only earns a place
        # when the blocker is about that device (same rule as every other query).
        hw = list(hardware_for_text(f"{title} {subject}", operator_notes))
        terms = _keywords(subject, 6)
        return Need(
            kind="goal", subject=subject, query=_cap(" ".join(hw + terms)),
            hardware=hw, goal_terms=terms,
            wants_latest=bool(_WANTS_LATEST_RE.search(subject)),
            reason="no error and no question; researching the step's open item",
        )

    return Need(
        kind="none", hardware=hardware,
        reason=("paste carries no failure line, no question, and the step "
                "records no open item" if commands or output
                else "empty message"),
    )


def finalize_query(need: Need, generated: Optional[str] = None,
                   *, node_key: str = "?") -> str:
    """ENFORCE the need into a query — the operator's hardware and the goal's
    terms are appended to whatever an LLM generator produced, then capped.

    A hint is a request the generator may decline (§17.1021: it dropped the
    model number three times). This is where the request becomes a rule.
    """
    base = (generated or "").strip() or need.query or need.subject
    if not base:
        return ""
    have = base.lower()
    hw_add = [m for m in need.hardware if m.lower() not in have]
    goal_add = [w for w in need.goal_terms if w.lower() not in have]
    if hw_add:
        logger.info("assist_web_query_hardware_enforced node_key=%s models=%s",
                    node_key, hw_add)
    if goal_add:
        logger.info("assist_web_query_goal_enforced node_key=%s terms=%s",
                    node_key, goal_add)
    return _cap(" ".join(hw_add + [base] + goal_add))


# ───────────────────────────────────────────── Stage 2: the evidence

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


# §17.1036 — how authoritative a source is for a PRODUCT question. The
# research extractor already scores well-known hosts; on top of that, a URL
# whose host or path is documentation-shaped (docs., wiki., help., support.,
# /docs/, /documentation/, /manual/, /wiki/, /admin-guide/) counts as
# documentation. Language-level shapes, no product names.
_DOC_HOST_RE = re.compile(r"^(?:docs?|wiki|help|support|manual|kb)\.", re.IGNORECASE)
# §17.1037f — vendor SUPPORT articles are documentation too: live, the ISP's
# own `/support/internet/advanced-wifi-advanced-settings` page was fetched and
# the "no documentation retrieved" note still fired.
_DOC_PATH_RE = re.compile(
    r"/(?:docs?|documentation|manual|manuals|wiki|guide|admin-guide|reference|support|help|faq|kb|"
    r"knowledge-?base|user-guide)(?:/|$)", re.IGNORECASE)
_DOC_AUTHORITY = 0.85


def source_authority(url: str) -> float:
    if not url:
        return 0.0
    try:
        from app.modules.research_extractors import _score_source
        base = float(_score_source(url))
    except Exception:  # noqa: BLE001
        base = 0.5
    m = re.match(r"https?://([^/?#]+)([^?#]*)", url.lower())
    if m and (_DOC_HOST_RE.match(m.group(1)) or _DOC_PATH_RE.search(m.group(2) or "")):
        # §17.1037c — an encyclopedia's `/wiki/` path is not a product's
        # documentation; live it satisfied the authority test with a page
        # about the word "documentation" itself.
        if not m.group(1).endswith("wikipedia.org"):
            base = max(base, _DOC_AUTHORITY)
    return base


def max_source_authority(sources: list) -> float:
    best = 0.0
    for s in sources or []:
        if isinstance(s, dict) and s.get("kind") in ("web", "searxng") and s.get("url"):
            best = max(best, source_authority(s["url"]))
    return best


# The shapes of an interface instruction: language-level words about screens
# and controls, not any product's names.
_INTERFACE_RE = re.compile(
    r"\b(?:click|tap|tab|menu|button|checkbox|dialog|toggle|dropdown|sidebar|"
    r"screen|panel|wizard|setting|settings|option|options|field|enable|disable)\b",
    re.IGNORECASE)


def interface_specifics_present(answer: str) -> bool:
    return len(_INTERFACE_RE.findall(answer or "")) >= 3


def source_date_key(source: dict) -> int:
    """``YYYYMMDD`` as an int for sorting; 0 when the source carries no date."""
    m = _DATE_RE.search(str(source.get("date") or ""))
    if not m:
        return 0
    try:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
    except ValueError:  # pragma: no cover
        return 0


def content_terms(need: Optional[Need]) -> set[str]:
    """The tokens a relevant source must share with the need. Phrases are split
    into their words; hardware models count; stopwords do not."""
    if need is None:
        return set()
    from app.modules.assist_research_lib import _GOAL_STOPWORDS, _goal_keywords
    out: set[str] = set()
    chunks = list(need.goal_terms) + [need.query]
    if need.kind == "question":
        # §17.1037c — a long question's own nouns are the relevance terms;
        # the query keeps six of them and the live UniFi question judged
        # relevance on "exact click current" while "port forward firewall"
        # were in the question and not in the set.
        chunks += _goal_keywords(need.subject, limit=12)
    for chunk in chunks:
        for tok in re.findall(r"[a-z0-9][a-z0-9-]{2,}", (chunk or "").lower()):
            if len(tok) >= 4 and tok not in _GOAL_STOPWORDS:
                out.add(tok)
    for m in need.hardware:
        out.add(m.lower())
    return out


def rank_evidence(sources: list[dict], need: Optional[Need] = None,
                  *, node_key: str = "?") -> list[dict]:
    """Drop off-topic sources, dedupe by URL, and order the rest by relevance
    to the need and then by date, newest first.

    Conservative like §17.729's snippet filter: with fewer than two content
    terms there is nothing to judge on and every source is kept. A source is
    dropped ONLY when it shares no term at all with the need — a fetched page
    can pass the snippet filter on its title and still be about something else.
    """
    terms = content_terms(need)
    kept: list[dict] = []
    seen_urls: set[str] = set()
    dropped = 0
    for s in sources or []:
        url = (s.get("url") or "").strip()
        if url and url in seen_urls:
            continue
        hay = set(re.findall(r"[a-z0-9][a-z0-9-]{2,}", (s.get("text") or "").lower()))
        overlap = len(terms & hay)
        if len(terms) >= 2 and overlap == 0:
            dropped += 1
            continue
        rel = (overlap / len(terms)) if terms else 0.0
        kept.append(dict(s, relevance=round(rel, 2)))
        if url:
            seen_urls.add(url)
    # §17.1036 — at equal relevance, official documentation outranks a forum
    # thread, and only then does recency decide.
    for s in kept:
        s["authority"] = round(source_authority(s.get("url") or ""), 2) if s.get("url") else 0.0
    kept.sort(key=lambda s: (-round(s["relevance"], 1), -s["authority"], -source_date_key(s)))
    if dropped:
        logger.info("assist_evidence_dropped_offtopic node_key=%s dropped=%d kept=%d",
                    node_key, dropped, len(kept))
    return kept


# ───────────────────────────────────────────── Stage 3: the verified answer

_URL_RE = re.compile(r"https?://[^\s\"'`<>\)\]]+")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Two-or-more dots (1.2.3), or a two-digit minor (22.04) — the shapes a release
# number takes. A single-dot decimal like 8.2 is too ambiguous to police.
_VERSION_RE = re.compile(r"\b\d{1,4}(?:\.\d{1,4}){2,}\b|\b\d{1,2}\.\d{2}\b")
# `port 8096` or `host:8096`. Ports below 1024 are protocol constants that any
# answer may state; registered/ephemeral ports are instance-specific.
_PORT_RE = re.compile(r"(?i)\bport\s*(\d{4,5})\b|(?<=[A-Za-z0-9\]}]):(\d{4,5})\b")
_PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]*>")
# §17.1029 — measured on 43 stored walkthroughs: two classes of value that are
# not claims. A URL carrying a shell variable (`https://${PVE_HOST}:8006/…`) is
# a template the operator fills in; an RFC 5737 documentation address
# (192.0.2.x, 198.51.100.x, 203.0.113.x), the wildcard 0.0.0.0 and loopback are
# examples by definition and cannot be "confirmed" against anything.
_EXAMPLE_IP_RE = re.compile(
    r"^(?:192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|0\.0\.0\.0$|127\.)")
# The operator's own network is not an external claim.
_LOCAL_URL_RE = re.compile(
    r"^https?://(?:localhost|127\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|"
    r"\[?::1\]?|[\w.-]+\.(?:local|lan|home|internal)(?::|/|$))", re.IGNORECASE)


# §17.1028 — the footer this module writes, parsed back: a value the engine
# itself flagged as unverified stays unverified until a SOURCE or the OPERATOR
# states it. Without this, the flagged reply is captured as a turn, the next
# turn's conversation block contains it, and the corpus credits the engine's
# own guess as known — observed live one turn after the footer first fired.
_FLAG_FOOTER_RE = re.compile(r"Unverified specifics\*{0,2}[^\n]*?:\s*([^\n]+)")
_FLAG_ITEM_RE = re.compile(r"`([^`\n]+)`\s*\((?:url|ip|version|port)\)")


def flagged_values(replies: Optional[list]) -> set[str]:
    """Every value a previous engine reply carried under an Unverified
    specifics footer. Derived on read from the replies themselves — the footer
    is the durable record, so every existing session recovers its ledger."""
    out: set[str] = set()
    for r in replies or []:
        text = (r.get("content") if isinstance(r, dict) else r) or ""
        for m in _FLAG_FOOTER_RE.finditer(str(text)):
            out.update(v.strip() for v in _FLAG_ITEM_RE.findall(m.group(1)))
    return out


def operator_text(history: Optional[list]) -> str:
    """The OPERATOR-authored half of a dialogue history (``{role, content}``
    items, role ``user``/``operator``). Engine replies are not provenance for
    the engine's next reply."""
    return "\n".join(
        (m.get("content") or "") for m in (history or [])
        if isinstance(m, dict) and (m.get("role") or "").strip().lower() in ("user", "operator")
    )


def sourced_values_from_environment(environment: Optional[dict]) -> set[str]:
    """§17.1030 — the session's ledger of source-confirmed values, as a set."""
    out: set[str] = set()
    for v in ((environment or {}).get("sourced_values") or []):
        val = v.get("value") if isinstance(v, dict) else v
        if val and str(val).strip():
            out.add(str(val).strip())
    return out


def extract_specifics(answer: str) -> list[dict]:
    """Every concrete value the answer states — ``{kind, value}`` — before any
    crediting. Placeholders, templated URLs, local URLs and example addresses
    are not values (see the notes on each pattern above)."""
    text = _PLACEHOLDER_RE.sub(" ", answer or "")
    found: list[dict] = []

    def _add(kind: str, value: str) -> None:
        v = value.strip(".,;:)")
        if v and not any(f["value"] == v for f in found):
            found.append({"kind": kind, "value": v})

    for u in _URL_RE.findall(text):
        u = u.rstrip(".,;:)")
        if _LOCAL_URL_RE.match(u) or "$" in u or "{" in u:
            continue
        _add("url", u)
    text = _URL_RE.sub(" ", text)
    for ip in _IPV4_RE.findall(text):
        if not _EXAMPLE_IP_RE.match(ip):
            _add("ip", ip)
    text = _IPV4_RE.sub(" ", text)
    for v in _VERSION_RE.findall(text):
        _add("version", v)
    for m in _PORT_RE.finditer(text):
        p = m.group(1) or m.group(2)
        if p and int(p) >= 1024:
            _add("port", p)
    return found


def _in_word(v: str, hay: str) -> bool:
    # Not glued to a preceding word/number ("3001" is not credited by
    # "13001"); a trailing "." is a sentence end, and "22.04" is credited by
    # "22.04.3" — a value consistent with the grounding is not from memory.
    return re.search(r"(?<![\w.])" + re.escape(v.lower()) + r"(?!\w)", hay) is not None


_HOST_RE = re.compile(r"\b(?=[a-z0-9-]{1,63}\.)(?:[a-z0-9-]{1,63}\.)+[a-z]{2,24}\b")


def _url_host(url: str) -> str:
    m = re.match(r"https?://([^/:?#]+)", (url or "").lower())
    return m.group(1) if m else ""


def ledger_text(environment: Optional[dict], operator_notes: Optional[list] = None) -> str:
    """§17.1034 — the CONFIRMED tier of provenance: what the operator's own
    ledger records. Facts (distilled from their output), notes (their words),
    substitution values and profile (their pins), system state (read from
    their own command output). NOT the plan, the digest or a recap — those are
    what the engine assumed or distilled, and a value that traces only to them
    is an assumption the operator has not confirmed on their system."""
    env = environment or {}
    parts: list[str] = [str(f) for f in (env.get("facts") or [])]
    parts.append(str(env.get("profile") or ""))
    subs = env.get("substitutions") or {}
    if isinstance(subs, dict):
        parts.extend(f"{k}={v}" for k, v in subs.items())
    st = env.get("system_state") or {}
    if isinstance(st, dict) and st:
        import json as _json
        try:
            parts.append(_json.dumps(st))
        except Exception:  # noqa: BLE001
            parts.append(str(st))
    for n in (operator_notes or []):
        parts.append(str((n.get("text") if isinstance(n, dict) else n) or ""))
    return "\n".join(p for p in parts if p)


def owned_hosts(environment: Optional[dict], operator_notes: Optional[list] = None) -> set[str]:
    """§17.1032 — hostnames the OPERATOR'S ledger names: facts (distilled from
    their own output), notes (their own words), substitution values (their
    own pins), profile. A URL on one of these hosts is a path on a machine or
    domain that is theirs, not a product URL from memory. Deliberately NOT the
    whole corpus: a source citing github.com must not make every github URL
    credited — §17.883's invented-download-URL class lives there."""
    env = environment or {}
    parts: list[str] = []
    parts.extend(str(f) for f in (env.get("facts") or []))
    parts.append(str(env.get("profile") or ""))
    subs = env.get("substitutions") or {}
    if isinstance(subs, dict):
        parts.extend(str(v) for v in subs.values())
    for n in (operator_notes or []):
        parts.append(str((n.get("text") if isinstance(n, dict) else n) or ""))
    # §17.1037 — the brief is the operator's own statement of the project.
    parts.append(str(env.get("_brief_text") or ""))
    text = "\n".join(parts).lower()
    return {h for h in _HOST_RE.findall(text) if not re.fullmatch(r"[\d.]+", h)}


def _credited(item: dict, hay: str, owned: Optional[set] = None) -> bool:
    v = item["value"].lower()
    if item["kind"] == "url":
        if v in hay or v.rstrip("/") in hay:
            return True
        host = _url_host(v)
        return bool(host) and host in (owned or set())
    return _in_word(v, hay)


def unsupported_specifics(answer: str, corpus: str, *, trusted: str = "",
                          flagged: Optional[set] = None,
                          sourced: Optional[set] = None,
                          owned: Optional[set] = None) -> list[dict]:
    """Concrete values the answer states that its grounding never mentions.

    ``corpus`` is what the generation was given that counts as provenance:
    the rendered sources, the project ledgers (facts, notes, digest, recap)
    and the operator's own words — NOT the engine's earlier replies (§17.1028).
    A value absent from all of it came from the model's memory — the exact
    class of thing §17.729's CURRENCY rule asks the model not to state, and
    which it states anyway. Placeholders (``<SERVER_IP>``) are the correct way
    to leave a value open and are never flagged.

    ``flagged`` values (§17.1028: what earlier replies already carried under
    the footer) are credited ONLY by ``trusted`` text — the retrieved sources
    and the operator's current message — never by the wider corpus, which by
    then contains the flagged reply's echoes (recaps, digests).

    ``sourced`` values (§17.1030: what a retrieved source confirmed on an
    EARLIER turn, from the session ledger) are credited outright — a value a
    source established does not become a guess because this turn's research
    did not refetch that source.

    ``owned`` hosts (§17.1032: from the operator's own ledger) credit any URL
    on them — a path on the operator's own domain is not a product claim.
    """
    trust = (trusted or "").lower()
    # Trusted text is provenance by definition (§17.1029): a value the task or
    # a source states is credited even if a caller's corpus omitted it.
    corp = (corpus or "").lower() + "\n" + trust
    flagged_l = {v.lower() for v in (flagged or ())}
    sourced_l = {v.lower() for v in (sourced or ())}
    out: list[dict] = []
    for item in extract_specifics(answer):
        v = item["value"].lower()
        if v in sourced_l:
            continue
        hay = trust if v in flagged_l else corp
        if not _credited(item, hay, owned):
            out.append(item)
    return out


def sourced_now(answer: str, sources: list) -> list[dict]:
    """§17.1030 — the values in ``answer`` that THIS turn's retrieved sources
    state, i.e. what the next turn may credit without refetching."""
    if not (answer or "").strip() or not sources:
        return []
    hay = "\n".join(
        str((s.get("text") or s.get("content") or "") if isinstance(s, dict) else s)
        for s in sources).lower()
    if not hay.strip():
        return []
    return [it for it in extract_specifics(answer) if _credited(it, hay)]


# §17.1031 — does the answer address the question that was ASKED?
#
# Live (scratch session, run d4b2cdb9): the operator asked for the current
# Node.js LTS version; the research ran on Node; the reply told them to run
# `pm2 web` and discussed the pm2 web port — the PREVIOUS turn's question,
# which sat last in the prompt inside the conversation block. Nothing checked
# that the answer and the question shared a single word.
_QUESTION_TERM_STOP = frozenset({
    "what", "which", "where", "when", "why", "how", "does", "did", "should",
    "could", "would", "please", "help", "there", "their", "this", "that",
    "with", "from", "into", "about", "right", "now", "know", "want", "need",
    "make", "sure", "just", "also", "still", "again", "here", "then",
})


_MIN_JUDGED_WORDS = 25


def question_terms(need: Optional["Need"]) -> set[str]:
    """The distinctive words of the operator's QUESTION itself — not the goal
    terms or hardware appended to the query, which come from the step recap
    and would credit an answer about the step's other business."""
    if need is None or need.kind != "question":
        return set()
    from app.modules.assist_research_lib import _GOAL_STOPWORDS
    out: set[str] = set()
    # No dots in a token: "nodejs.org" is the two words "nodejs" and "org", and
    # an answer that says "nodejs" has addressed it.
    for tok in re.findall(r"[a-z][a-z0-9-]{2,}", (need.subject or "").lower()):
        tok = tok.strip("-")
        if len(tok) >= 4 and tok not in _GOAL_STOPWORDS and tok not in _QUESTION_TERM_STOP:
            out.add(tok)
    return out


def addresses_question(answer: str, need: Optional["Need"]) -> bool:
    """True unless the question has distinctive terms and the answer shares
    NONE of them. Deliberately lenient — one shared term passes, and a short
    answer (under ``_MIN_JUDGED_WORDS``) is never judged: "Use 10.0.0.1" to
    "which address do I connect to" shares no word and is the right answer —
    so it catches an answer to a DIFFERENT question, not a terse right one."""
    terms = question_terms(need)
    if len(terms) < 2 or len((answer or "").split()) < _MIN_JUDGED_WORDS:
        return True
    hay = (answer or "").lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", hay) for t in terms)


# §17.1033 — command SHAPE: an interpreter invoked on a file of another
# language. Live (operator-session replay, fix path): the draft proposed
# `nohup python3 /opt/control-panel-backend/server.js` — hedged, after a
# discovery step, but a command that cannot work as written. No value check
# can see it (no version, address, URL or port), and the shell gates check
# syntax, not meaning. This is language-level knowledge (which interpreter runs
# which file kind), not one operator's problem.
_INTERPRETER_EXTS = {
    "python": {"py", "pyw"}, "python3": {"py", "pyw"}, "python2": {"py", "pyw"},
    "pypy": {"py"}, "pypy3": {"py"},
    "node": {"js", "mjs", "cjs"}, "nodejs": {"js", "mjs", "cjs"},
    "bash": {"sh", "bash"}, "sh": {"sh"}, "zsh": {"sh", "zsh"}, "dash": {"sh"},
    "perl": {"pl", "pm"}, "ruby": {"rb"}, "php": {"php"},
}
_ALL_SCRIPT_EXTS = frozenset(e for exts in _INTERPRETER_EXTS.values() for e in exts)
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_INTERP_TOKEN_RE = re.compile(r"^(?:[\w./-]*/)?(python(?:[23](?:\.\d+)?)?|pypy3?|node(?:js)?|bash|sh|zsh|dash|perl|ruby|php)$")


def _interp_family(tok: str) -> Optional[str]:
    m = _INTERP_TOKEN_RE.match(tok)
    if not m:
        return None
    name = m.group(1)
    if name.startswith("python"):
        return "python3" if name != "python2" else "python2"
    return name


def command_shape_issues(answer: str) -> list[dict]:
    """Commands in the answer's code (fenced blocks and inline spans) that
    invoke an interpreter on a file whose extension belongs to a different
    interpreter family. ``python3 -m mod``, ``python3 -c '…'`` and files with
    no known script extension are never flagged."""
    issues: list[dict] = []
    snippets = _FENCE_RE.findall(answer or "") + _INLINE_CODE_RE.findall(answer or "")
    seen: set[str] = set()
    for snippet in snippets:
        for line in snippet.splitlines():
            toks = line.strip().split()
            for i, tok in enumerate(toks[:-1]):
                fam = _interp_family(tok)
                if not fam:
                    continue
                nxt = toks[i + 1]
                if nxt.startswith("-"):
                    continue  # -m module, -c code, -e expr, flags
                name = nxt.strip("'\"`;&|()").rsplit("/", 1)[-1]
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if not ext or ext not in _ALL_SCRIPT_EXTS:
                    continue
                if ext in _INTERPRETER_EXTS.get(fam, set()):
                    continue
                cmd = " ".join(toks[i:i + 2])
                if cmd in seen:
                    continue
                seen.add(cmd)
                issues.append({"command": cmd, "interpreter": tok, "file": nxt,
                               "expects": sorted(_INTERPRETER_EXTS.get(fam, set()))})
    return issues


async def citation_report(answer: str, sources: list) -> Optional[dict]:
    """§17.798's per-citation judge, fail-soft. ``None`` when the answer cites
    nothing (attribution undefined) or the judge could not run."""
    if not (answer or "").strip() or not sources:
        return None
    try:
        from app.modules.citation_faithfulness import parse_citations, score_citation_faithfulness
        if not parse_citations(answer, len(sources)):
            return None
        return await score_citation_faithfulness(answer, sources)
    except Exception as exc:  # noqa: BLE001 — verification never breaks a turn
        logger.warning("assist_citation_report_failed: %s", exc)
        return None


def _citation_weak(cite: Optional[dict]) -> bool:
    if not cite or cite.get("score") is None:
        return False
    return float(cite["score"]) < float(settings.assist_answer_min_citation_score)


def grounding_notice(unsupported: list[dict], cite: Optional[dict],
                     *, off_question: Optional[str] = None,
                     shape: Optional[list] = None) -> str:
    """The regeneration directive: the exact values, named."""
    parts = ["\n\n---\nGROUNDING NOTICE:"]
    if shape:
        parts.append(
            "Your previous answer contained command(s) that CANNOT work as "
            "written — an interpreter run on a file of a different language:\n"
            + "\n".join(
                f"- `{s['command']}` — `{s['interpreter']}` runs "
                + "/".join("." + e for e in s["expects"]) + " files, not `"
                + s["file"].rsplit("/", 1)[-1] + "`"
                for s in shape[:5])
            + "\nEither the interpreter or the file is wrong. If you do not know "
              "which file is the entry point, give a DISCOVERY command that "
              "prints it and stop there; never pair an interpreter with a file "
              "it cannot run.")
    if off_question:
        parts.append(
            "Your previous answer did NOT address the question the operator just "
            "asked — it answered something else (most likely an earlier message "
            "in the conversation). The question to answer, and the ONLY one, is:\n"
            f"    {off_question}\n"
            "Answer that. If the sources and project context do not let you, say "
            "so plainly for THAT question; do not answer a different one.")
    if unsupported:
        parts.append(
            "Your previous answer stated specific values that appear in NONE of "
            "the sources, the project's confirmed facts, or the operator's own "
            "words — they came from memory and may be stale or wrong:\n"
            + "\n".join(f"- `{u['value']}` ({u['kind']})" for u in unsupported[:8])
            + "\nFor each one: use a value a source or fact above actually "
              "states; or replace it with the way to FIND the current one (a "
              "command that prints it, the page to open); or drop it. Do not "
              "restate the same value from memory.")
    if _citation_weak(cite):
        bad = cite.get("unsupported_citations") or []
        parts.append(
            f"{cite.get('total', 0) - cite.get('supported', 0)} of "
            f"{cite.get('total', 0)} cited statements are NOT supported by the "
            "source they cite. Cite only what a source actually says; if no "
            "source says it, say plainly that it is unverified."
            + ("".join(f"\n- \"{str(b.get('claim') or b)[:160]}\" cites [{b.get('source_id', '?')}]"
                        for b in bad[:5] if isinstance(b, dict)) if bad else ""))
    return "\n".join(parts)


def grounding_footer(unsupported: list[dict], cite: Optional[dict],
                     *, off_question: Optional[str] = None,
                     shape: Optional[list] = None,
                     plan_only: Optional[list] = None,
                     unsourced_interface: bool = False) -> str:
    """What the operator sees when the answer still fails after regeneration."""
    lines = ["\n\n---"]
    if unsourced_interface:
        # §17.1036 — F3/F4 in the §17.1035 run: screen labels and setting
        # levels stated with no documentation retrieved, reading as sourced.
        lines.append(
            "ℹ️ **No official documentation was retrieved for this** — the "
            "screen labels, menu paths and setting levels above come from "
            "general knowledge, not from a page fetched for this question. "
            "Verify them against the screen in front of you.")
    if plan_only:
        # §17.1034 — softer than the warning: the value is not from memory, it
        # is from the plan — but nothing the operator has reported confirms it.
        lines.append(
            "ℹ️ **From the plan, not yet confirmed on your system** — "
            + ", ".join(f"`{u['value']}` ({u['kind']})" for u in plan_only[:8])
            + ". If any of these is not what your system actually shows, tell me.")
    if shape:
        lines.append(
            "⚠️ **Command mismatch** — these pair an interpreter with a file it "
            "cannot run; fix the interpreter or the filename before running: "
            + ", ".join(f"`{s['command']}`" for s in shape[:5]))
    if off_question:
        lines.append(
            "⚠️ **This may not answer what you asked** — the reply above does not "
            f"mention any of the specifics of your question (*{off_question[:160]}*). "
            "Ask again in one sentence if it missed the point.")
    if unsupported:
        lines.append(
            "⚠️ **Unverified specifics** — these values appear in no source, "
            "fact, or note for this step; confirm each before relying on it: "
            + ", ".join(f"`{u['value']}` ({u['kind']})" for u in unsupported[:8]))
    if _citation_weak(cite):
        lines.append(
            f"⚠️ **Weak sourcing** — {cite.get('total', 0) - cite.get('supported', 0)} "
            f"of {cite.get('total', 0)} cited statements are not backed by the "
            "source they cite.")
    return "\n".join(lines)


# §17.1037d — the verifier OWNS the footer namespace. Earlier replies carry
# these footers; they sit in the conversation block, and live the model copied
# "ℹ️ No official documentation was retrieved…" into a fresh answer whose
# retrieval HAD found documentation — the verifier's own log showed no such
# flag. A footer line in a draft is therefore never the model's to write:
# strip every footer-shaped line first, re-add only what is true now.
_FOOTER_LINE_RE = re.compile(
    r"^\s*(?:ℹ️|⚠️)\s*\*\*(?:No official documentation was retrieved|Unverified specifics|"
    r"Weak sourcing|From the plan, not yet confirmed|Command mismatch|This may not answer what you asked)\b.*$",
    re.MULTILINE)


def strip_verifier_footers(answer: str) -> str:
    """Remove footer lines the verifier itself writes (and a separator left
    dangling right above one), so a draft that echoed an earlier reply's footer
    is judged on its own content."""
    if not answer or not _FOOTER_LINE_RE.search(answer):
        return answer
    out = _FOOTER_LINE_RE.sub("", answer)
    # a `---` that now has nothing but whitespace after it, or is duplicated
    out = re.sub(r"(?:\n\s*---\s*)+(?=\n\s*---\s*$|\s*$)", "", out.rstrip())
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.rstrip()


async def verify_answer(
    answer: str,
    *,
    sources: list,
    corpus: str,
    need: Optional[Need] = None,
    node_key: str = "?",
    label: str = "",
    regenerate: Optional[Callable[[str], Awaitable[str]]] = None,
    trusted: str = "",
    flagged: Optional[set] = None,
    sourced: Optional[set] = None,
    owned_hosts: Optional[set] = None,
    confirmed: str = "",
    annotate: bool = True,
) -> tuple[str, dict]:
    """Check an answer against its grounding; regenerate once; else annotate.

    ``annotate=False`` (§17.1039, the executor): the footer is returned in
    ``report["footer"]`` and the text is left as written — a footer inside a
    node's output would be parroted downstream and compiled into the deliverable.

    ``trusted`` / ``flagged`` — §17.1028: values a previous reply already
    flagged are credited only by the sources and the operator's current
    message (``trusted``), never by the wider corpus.

    ``regenerate`` receives the grounding notice and returns a new draft, or
    "" to decline (a caller may reject a draft that fails its own gates). The
    candidate replaces the answer when it is clean, or when it is strictly
    better (fewer unsupported values). What remains unsupported after that is
    listed under a visible warning — the operator is never silently handed a
    value from nowhere.
    """
    report: dict = {"checked": False, "unsupported": [], "citation": None,
                    "regenerated": False, "annotated": False, "sourced_now": [],
                    "off_question": False, "command_shape": [], "plan_only": [],
                    "unsourced_interface": False, "footer": ""}
    if not settings.assist_answer_verification_enabled or not (answer or "").strip():
        return answer, report
    report["checked"] = True
    answer = strip_verifier_footers(answer)  # §17.1037d
    # §17.1030 — the sources this call is handed are trusted text by
    # definition; a caller cannot forget to render them into the corpus.
    trusted = (trusted or "") + "\n" + "\n".join(
        str((s.get("text") or s.get("content") or "") if isinstance(s, dict) else s)
        for s in (sources or []))
    unsupported = unsupported_specifics(answer, corpus, trusted=trusted, flagged=flagged,
                                        sourced=sourced, owned=owned_hosts)
    cite = await citation_report(answer, sources)
    off = not addresses_question(answer, need)  # §17.1031
    if off:
        logger.warning("assist_answer_offtopic node_key=%s label=%s question_terms=%r",
                       node_key, label, sorted(question_terms(need))[:8])
    shape = command_shape_issues(answer)  # §17.1033
    if shape:
        logger.warning("assist_answer_command_shape node_key=%s label=%s commands=%r",
                       node_key, label, [s["command"] for s in shape][:4])
    _carried = [u["value"] for u in unsupported if u["value"].lower() in {v.lower() for v in (flagged or ())}]
    if _carried:
        logger.info("assist_answer_grounding_flag_carried node_key=%s label=%s values=%r",
                    node_key, label, _carried[:6])

    def _fails(uns: list, ct: Optional[dict], off_: bool = False,
               shape_: Optional[list] = None) -> bool:
        return bool(uns) or _citation_weak(ct) or off_ or bool(shape_)

    _q = (need.subject if (need is not None and need.kind == "question") else "") or ""
    if _fails(unsupported, cite, off, shape) and regenerate is not None \
            and settings.assist_answer_verification_regenerate:
        logger.info(
            "assist_answer_grounding_regen node_key=%s label=%s unsupported=%r "
            "cite_score=%s off_question=%s command_shape=%d", node_key, label,
            [u["value"] for u in unsupported][:6],
            (cite or {}).get("score"), off, len(shape))
        try:
            candidate = (await regenerate(grounding_notice(
                unsupported, cite, off_question=_q if off else None,
                shape=shape)) or "").strip()
        except Exception as exc:  # noqa: BLE001 — verification never breaks a turn
            logger.warning("assist_answer_grounding_regen_failed: %s", exc)
            candidate = ""
        if candidate:
            candidate = strip_verifier_footers(candidate)  # §17.1037d
            c_uns = unsupported_specifics(candidate, corpus, trusted=trusted, flagged=flagged,
                                          sourced=sourced, owned=owned_hosts)
            c_cite = await citation_report(candidate, sources)
            c_off = not addresses_question(candidate, need)
            c_shape = command_shape_issues(candidate)
            better = (not _fails(c_uns, c_cite, c_off, c_shape)) or (
                not c_off and off and not c_shape) or (
                not c_shape and shape and not c_off) or (
                not c_off and not c_shape and len(c_uns) < len(unsupported) and not (
                    _citation_weak(c_cite) and not _citation_weak(cite)))
            if better:
                answer, unsupported, cite, off, shape = candidate, c_uns, c_cite, c_off, c_shape
                report["regenerated"] = True

    # §17.1034 — the plan-only tier: credited by the corpus (task text, digest,
    # recaps) but by NOTHING the operator has confirmed — not the sources, not
    # their message, not their ledger. Not wrong, so no regeneration; noted.
    _unconfirmed = unsupported_specifics(answer, confirmed or "", trusted=trusted,
                                         flagged=flagged, sourced=sourced, owned=owned_hosts)
    _uns_vals = {u["value"].lower() for u in unsupported}
    plan_only = [u for u in _unconfirmed if u["value"].lower() not in _uns_vals]
    # §17.1036 — interface specifics with no documentation behind them.
    unsourced_iface = bool(
        need is not None and need.kind == "question"
        and interface_specifics_present(answer)
        and max_source_authority(sources) < _DOC_AUTHORITY)
    if unsourced_iface:
        logger.info("assist_answer_unsourced_interface node_key=%s label=%s max_authority=%.2f",
                    node_key, label, max_source_authority(sources))
    if _fails(unsupported, cite, off, shape) or plan_only or unsourced_iface:
        footer = grounding_footer(
            unsupported, cite, off_question=_q if off else None, shape=shape,
            plan_only=plan_only, unsourced_interface=unsourced_iface)
        report["footer"] = footer
        if annotate:
            answer = answer.rstrip() + footer
        report["annotated"] = bool(_fails(unsupported, cite, off, shape))
    report["unsourced_interface"] = unsourced_iface
    report["off_question"] = off
    report["command_shape"] = shape
    report["plan_only"] = plan_only
    report["unsupported"] = unsupported
    report["citation"] = cite
    # §17.1030 — what THIS turn's sources confirmed, for the session ledger.
    # Computed on the final text, excluding anything still flagged.
    _still = {u["value"].lower() for u in unsupported}
    report["sourced_now"] = [it for it in sourced_now(answer, sources)
                             if it["value"].lower() not in _still]
    logger.info(
        "assist_answer_grounding node_key=%s label=%s kind=%s unsupported=%d "
        "cite_score=%s regenerated=%s annotated=%s values=%r sourced_now=%r off_question=%s "
        "command_shape=%r",
        node_key, label, getattr(need, "kind", "?"), len(unsupported),
        (cite or {}).get("score"), report["regenerated"], report["annotated"],
        [u["value"] for u in unsupported][:6],
        [it["value"] for it in report["sourced_now"]][:6], report["off_question"],
        [s["command"] for s in shape][:4])
    if report["plan_only"]:
        logger.info("assist_answer_plan_only node_key=%s label=%s values=%r",
                    node_key, label, [u["value"] for u in report["plan_only"]][:6])
    return answer, report
