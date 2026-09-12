"""§17.1025 — retrieval logic must not carry the answer to one operator's problem.

The operator's objection, verbatim: *"This sounds like giving it the answers not
fixing the actual problem."* They were right. §17.1023 shipped:

    phrases = [ph for ph in ("port forwarding", "dns record", "dynamic dns",
                             "reverse proxy", "static ip", "bridge mode",
                             "nat loopback", "double nat") if ph in low]

assembled by reading the one transcript it was written for. It made THEIR query
better and did nothing for anyone whose problem is a kernel panic, a boot order
or a printer driver — and it hid the real defect (§17.1024: the prompt put 30 KB
of the project's own prior answers ahead of the freshly fetched sources).

Phrases are now DERIVED from the text. This gate keeps them derived.

The line it draws: a LANGUAGE-level list (articles, modals, pronouns) describes
English and generalises to every subject. A DOMAIN-level list names one
operator's technology stack. The first is fine here; the second is the answer
key.
"""
import pathlib
import re

import pytest

# Modules whose job is to turn an operator's words into a retrieval query.
QUERY_MODULES = [
    "app/modules/assist_research_lib.py",
    "app/modules/assist_render.py",
    "app/modules/assist_guide.py",
    "app/modules/assist_agent.py",
    "app/modules/assist_policy.py",
    "app/modules/assist_evidence.py",  # §17.1027 — need / evidence / verification
]

# §17.1026 — literal phrase collections that are NOT answer keys, each with its
# reason. Adding an entry is a deliberate act that belongs in review. This
# registry is the point of the gate: the alternative — predicting which words a
# future answer key will contain — is the mistake it exists to prevent.
EXEMPT_PHRASE_LISTS = {
    ("assist_guide.py", 4201): (
        "destructive-command patterns (`| sh`, `dpkg -i`). A security scanner is "
        "inherently a list of known-dangerous shapes; it encodes no operator's "
        "problem, and deriving it from operator text would be strictly worse."
    ),
}

# Vocabulary that names a specific technology rather than a grammatical class.
# If a query builder is matching on these, it is recognising one operator's
# problem instead of parsing any operator's sentence.
DOMAIN_TERMS = [
    "port forwarding", "port forward", "dynamic dns", "dns record",
    "bridge mode", "nat loopback", "double nat", "reverse proxy",
    "kernel panic", "boot order", "disk passthrough", "acme challenge",
    "static ip", "vlan", "wireguard", "tailscale", "jellyfin", "caddy",
    "proxmox", "spectrum", "sax1v1k", "es2251",
]

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _code_lines(path: pathlib.Path):
    """Source lines with comments and docstrings stripped — a term may be
    DISCUSSED in a comment (that is how these defects get explained) but must
    not be MATCHED on in code."""
    out, in_doc, doc_q = [], False, ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw
        if in_doc:
            if doc_q in line:
                in_doc = False
                line = line.split(doc_q, 1)[1]
            else:
                continue
        for q in ('"""', "'''"):
            if q in line:
                before, _, after = line.partition(q)
                if q in after:               # single-line docstring
                    line = before + after.split(q, 1)[1]
                else:
                    in_doc, doc_q = True, q
                    line = before
                break
        line = re.sub(r"#.*$", "", line)
        if line.strip():
            out.append(line)
    return out


def test_the_gate_can_see_code():
    """If the stripper eats everything the gate is vacuous."""
    lines = _code_lines(ROOT / QUERY_MODULES[0])
    assert len(lines) > 200, f"only {len(lines)} code lines survived stripping"


# §17.1026 — pre-existing domain vocabulary, BASELINED not blessed.
#
# Widening the gate from two modules to five surfaced 18 pre-existing hits —
# almost all one operator's hypervisor named inside prompt examples ("latest
# Proxmox version", "how do I connect the Proxmox server") plus two
# shell-context regexes. Same class as the §17.1023 phrase table at lower dose:
# it tunes the engine for one homelab.
#
# NOT cleaned here. Those strings sit in prompts tuned against live behaviour,
# and rewriting them blind at the end of a session whose whole lesson is that I
# verify the wrong thing would be the very move that caused the damage.
# Baselined instead: the count may go DOWN freely and may not go UP.
DOMAIN_VOCAB_BASELINE = {
    "assist_research_lib.py": 0,
    "assist_render.py": 0,
    "assist_guide.py": 17,
    "assist_agent.py": 0,
    "assist_policy.py": 1,
    "assist_evidence.py": 0,
}


@pytest.mark.parametrize("rel", QUERY_MODULES)
def test_domain_vocabulary_does_not_grow(rel):
    path = ROOT / rel
    hits = []
    for line in _code_lines(path):
        low = line.lower()
        for term in DOMAIN_TERMS:
            if term in low:
                hits.append(f"{term!r} — {line.strip()[:90]}")
    baseline = DOMAIN_VOCAB_BASELINE[path.name]
    assert len(hits) <= baseline, (
        f"{rel}: domain vocabulary in query logic grew {baseline} -> {len(hits)}. "
        "Naming one operator's technology here tunes the engine for one homelab "
        "(§17.1025). Derive it from their text instead.\n  "
        + "\n  ".join(hits[baseline:])
    )
    if len(hits) < baseline:
        pytest.fail(
            f"{rel}: {baseline - len(hits)} fewer than baseline — good. Lower "
            f"DOMAIN_VOCAB_BASELINE['{path.name}'] to {len(hits)} so the "
            "ratchet holds."
        )


# ── the structural gate: the SHAPE of an answer key, not its vocabulary ──
def _phrase_collections(path: pathlib.Path):
    """Literal tuples/lists/sets that are mostly MULTI-WORD strings.

    Single-word collections are ordinary (stopwords, enums, field names). A
    literal collection of multi-word PHRASES, in a module that reads operator
    text, is the shape of a lookup table built by reading one transcript —
    exactly what §17.1023 did and §17.1025 removed.

    Vocabulary-free by construction: the first version of this gate could only
    catch terms I had thought of, which is the same failure in a different coat.
    """
    import ast
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            continue
        strs = [e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if len(strs) < 3:
            continue
        multi = [x for x in strs if " " in x.strip() and 2 <= len(x.split()) <= 4]
        if len(multi) >= 2 and len(multi) >= len(strs) * 0.6:
            out.append((node.lineno, multi[:4]))
    return out


@pytest.mark.parametrize("rel", QUERY_MODULES)
def test_no_unregistered_phrase_lookup_tables(rel):
    path = ROOT / rel
    unregistered = [
        f"{rel}:{ln} -> {sample}"
        for ln, sample in _phrase_collections(path)
        if (path.name, ln) not in EXEMPT_PHRASE_LISTS
    ]
    assert not unregistered, (
        "a literal table of multi-word phrases in query/routing logic is the "
        "shape of an answer key — a list built by reading ONE transcript "
        "(§17.1023/§17.1025):\n  " + "\n  ".join(unregistered)
        + "\n\nDerive the phrases from the operator's text, or register the "
          "collection in EXEMPT_PHRASE_LISTS with the reason it is not one."
    )


def test_the_structural_gate_is_not_vacuous():
    """It must find the one registered collection; a detector that finds
    nothing would pass every module for the wrong reason."""
    found = _phrase_collections(ROOT / "app/modules/assist_guide.py")
    assert any(ln == 4201 for ln, _ in found), "the detector has gone blind"


def test_every_exemption_states_a_reason():
    for key, reason in EXEMPT_PHRASE_LISTS.items():
        assert len(reason.split()) >= 12, f"{key} is exempted without an argument"


def test_phrases_are_derived_and_generalise():
    """The proof that derivation replaced recognition: domains this codebase
    has never mentioned still yield their own technical phrase.

    §17.1026 — skipped in the `make ci-tier-0` static lane, which runs host
    pytest with no app dependencies installed. The file-scanning gates above
    are the ones that must hold there; this behavioural check runs in the
    dev-image lane. Skipping rather than deleting, because the alternative is
    a gate file that cannot be run in the fast lane at all."""
    pytest.importorskip("asyncpg", reason="static lane has no app deps")
    from app.modules.assist_research_lib import _goal_keywords
    for text, expected in [
        ("VM fails to boot: kernel panic not syncing after the last apt upgrade",
         "kernel panic"),
        ("The zfs pool will not import; disk passthrough was never configured",
         "disk passthrough"),
        ("The label printer will not appear in CUPS after the driver install",
         "label printer"),
        ("Caddy cannot obtain a certificate; the acme challenge times out",
         "acme challenge"),
    ]:
        assert expected in _goal_keywords(text), (text, _goal_keywords(text))


def test_the_stopword_list_stays_language_level():
    pytest.importorskip("asyncpg", reason="static lane has no app deps")
    from app.modules.assist_research_lib import _GOAL_STOPWORDS
    for w in _GOAL_STOPWORDS:
        assert " " not in w, f"multi-word entry {w!r} is a phrase, not a stopword"
        for term in DOMAIN_TERMS:
            assert w != term, f"{w!r} names a technology"
