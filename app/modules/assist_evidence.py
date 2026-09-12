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
    from app.modules.assist_research_lib import _GOAL_STOPWORDS
    out: set[str] = set()
    for chunk in list(need.goal_terms) + [need.query]:
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
    kept.sort(key=lambda s: (-round(s["relevance"], 1), -source_date_key(s)))
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
# The operator's own network is not an external claim.
_LOCAL_URL_RE = re.compile(
    r"^https?://(?:localhost|127\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|"
    r"\[?::1\]?|[\w.-]+\.(?:local|lan|home|internal)(?::|/|$))", re.IGNORECASE)


def unsupported_specifics(answer: str, corpus: str) -> list[dict]:
    """Concrete values the answer states that its grounding never mentions.

    ``corpus`` is everything the generation was given: the rendered sources,
    the project context, the operator's own message and notes. A value absent
    from all of it came from the model's memory — the exact class of thing
    §17.729's CURRENCY rule asks the model not to state, and which it states
    anyway. Placeholders (``<SERVER_IP>``) are the correct way to leave a value
    open and are never flagged.
    """
    text = _PLACEHOLDER_RE.sub(" ", answer or "")
    corp = (corpus or "").lower()
    found: list[dict] = []

    def _known(v: str) -> bool:
        return v.lower() in corp

    def _known_word(v: str) -> bool:
        # Not glued to a preceding word/number ("3001" is not credited by
        # "13001"); a trailing "." is a sentence end, and "22.04" is credited
        # by "22.04.3" — a value consistent with the grounding is not from
        # memory.
        return re.search(r"(?<![\w.])" + re.escape(v.lower()) + r"(?!\w)", corp) is not None

    def _add(kind: str, value: str) -> None:
        v = value.strip(".,;:)")
        if v and not any(f["value"] == v for f in found):
            found.append({"kind": kind, "value": v})

    for u in _URL_RE.findall(text):
        u = u.rstrip(".,;:)")
        if _LOCAL_URL_RE.match(u):
            continue
        if not (_known(u) or _known(u.rstrip("/"))):
            _add("url", u)
    text = _URL_RE.sub(" ", text)
    for ip in _IPV4_RE.findall(text):
        if not _known_word(ip):
            _add("ip", ip)
    text = _IPV4_RE.sub(" ", text)
    for v in _VERSION_RE.findall(text):
        if not _known_word(v):
            _add("version", v)
    for m in _PORT_RE.finditer(text):
        p = m.group(1) or m.group(2)
        if p and int(p) >= 1024 and not _known_word(p):
            _add("port", p)
    return found


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


def grounding_notice(unsupported: list[dict], cite: Optional[dict]) -> str:
    """The regeneration directive: the exact values, named."""
    parts = ["\n\n---\nGROUNDING NOTICE:"]
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


def grounding_footer(unsupported: list[dict], cite: Optional[dict]) -> str:
    """What the operator sees when the answer still fails after regeneration."""
    lines = ["\n\n---"]
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


async def verify_answer(
    answer: str,
    *,
    sources: list,
    corpus: str,
    need: Optional[Need] = None,
    node_key: str = "?",
    label: str = "",
    regenerate: Optional[Callable[[str], Awaitable[str]]] = None,
) -> tuple[str, dict]:
    """Check an answer against its grounding; regenerate once; else annotate.

    ``regenerate`` receives the grounding notice and returns a new draft, or
    "" to decline (a caller may reject a draft that fails its own gates). The
    candidate replaces the answer when it is clean, or when it is strictly
    better (fewer unsupported values). What remains unsupported after that is
    listed under a visible warning — the operator is never silently handed a
    value from nowhere.
    """
    report: dict = {"checked": False, "unsupported": [], "citation": None,
                    "regenerated": False, "annotated": False}
    if not settings.assist_answer_verification_enabled or not (answer or "").strip():
        return answer, report
    report["checked"] = True
    unsupported = unsupported_specifics(answer, corpus)
    cite = await citation_report(answer, sources)

    def _fails(uns: list, ct: Optional[dict]) -> bool:
        return bool(uns) or _citation_weak(ct)

    if _fails(unsupported, cite) and regenerate is not None \
            and settings.assist_answer_verification_regenerate:
        logger.info(
            "assist_answer_grounding_regen node_key=%s label=%s unsupported=%r "
            "cite_score=%s", node_key, label,
            [u["value"] for u in unsupported][:6],
            (cite or {}).get("score"))
        try:
            candidate = (await regenerate(grounding_notice(unsupported, cite)) or "").strip()
        except Exception as exc:  # noqa: BLE001 — verification never breaks a turn
            logger.warning("assist_answer_grounding_regen_failed: %s", exc)
            candidate = ""
        if candidate:
            c_uns = unsupported_specifics(candidate, corpus)
            c_cite = await citation_report(candidate, sources)
            better = (not _fails(c_uns, c_cite)) or (
                len(c_uns) < len(unsupported) and not (
                    _citation_weak(c_cite) and not _citation_weak(cite)))
            if better:
                answer, unsupported, cite = candidate, c_uns, c_cite
                report["regenerated"] = True

    if _fails(unsupported, cite):
        answer = answer.rstrip() + grounding_footer(unsupported, cite)
        report["annotated"] = True
    report["unsupported"] = unsupported
    report["citation"] = cite
    logger.info(
        "assist_answer_grounding node_key=%s label=%s kind=%s unsupported=%d "
        "cite_score=%s regenerated=%s annotated=%s values=%r",
        node_key, label, getattr(need, "kind", "?"), len(unsupported),
        (cite or {}).get("score"), report["regenerated"], report["annotated"],
        [u["value"] for u in unsupported][:6])
    return answer, report
