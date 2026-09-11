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
]

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


@pytest.mark.parametrize("rel", QUERY_MODULES)
def test_no_domain_vocabulary_in_query_logic(rel):
    path = ROOT / rel
    hits = []
    for i, line in enumerate(_code_lines(path), 1):
        low = line.lower()
        for term in DOMAIN_TERMS:
            if term in low:
                hits.append(f"{rel}: {term!r} in code — {line.strip()[:90]}")
    assert not hits, (
        "query logic is matching on one operator's technology instead of "
        "parsing any operator's sentence (§17.1025):\n  " + "\n  ".join(hits)
        + "\n\nDerive the terms from the text. A language-level stopword list "
          "is fine; a technology list is the answer key."
    )


def test_phrases_are_derived_and_generalise():
    """The proof that derivation replaced recognition: domains this codebase
    has never mentioned still yield their own technical phrase."""
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
    from app.modules.assist_research_lib import _GOAL_STOPWORDS
    for w in _GOAL_STOPWORDS:
        assert " " not in w, f"multi-word entry {w!r} is a phrase, not a stopword"
        for term in DOMAIN_TERMS:
            assert w != term, f"{w!r} names a technology"
