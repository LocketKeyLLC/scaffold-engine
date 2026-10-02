"""§17.1219 — a service installed and never configured is not a plan.

The operator, after living with a finished homelab build:

    "at no point did the engine assist in setting up where and how to retrieve
     movies and media for the homelab."

The plan had installed the entire stack for exactly that:

    T13/T14  Create + Install Prowlarr        (an indexer manager)
    T15/T16  Create + Install Radarr          (finds and fetches films)
    T17/T18  Create + Install Sonarr          (the same for TV)
    T19/T20  Create + Install download client

and not one step added an indexer, connected Radarr to Prowlarr, pointed either
at the download client, set a root folder, or chose a quality. Four services
whose whole purpose is the configuration, installed and left at their defaults.
The planner prompt already says to emit a node for EACH install / configure /
integrate / verify outcome (§17.686); it simply did not.

So the plan is checked against itself: a step that INSTALLS something, with no
later step that CONFIGURES or CONNECTS that same thing, is a hole the operator
will find at the end. Deterministic — the check reads the plan's own titles, not
a model — and it names the service so the gap is actionable rather than a score.

Deliberately narrow. It only fires when a step's verb is unmistakably an install
and nothing downstream mentions that subject with a configuring verb. A plan that
installs `curl` is not a finding; a plan that installs Radarr and never mentions
Radarr again is.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("scaffold")

_INSTALL_RE = re.compile(r"\b(install|deploy|set\s?up|create|provision|add)\b", re.I)
_CONFIGURE_RE = re.compile(
    r"\b(configur\w*|connect\w*|integrat\w*|link\w*|point\w*|register\w*|enrol\w*|"
    r"set\s+(?:the\s+)?(?:root|path|folder|profile|quality|indexer|api|key|url)|"
    r"add\s+(?:an?\s+)?(?:indexer|tracker|source|library|profile|root)|"
    r"credential\w*|api\s?key|token)\b", re.I)

#: Words that are never the SUBJECT of a coverage finding — packages and
#: primitives an operator does not configure to taste.
_NOT_A_SERVICE = frozenset({
    "curl", "wget", "git", "gnupg", "ca-certificates", "sudo", "vim", "nano",
    "python", "python3", "pip", "node", "npm", "docker", "package", "packages",
    "dependencies", "deps", "repository", "repo", "key", "keys", "driver",
    "drivers", "agent", "kernel", "module", "modules", "firmware", "tools",
    # §17.1219 — measured against the real plan: without these the check named
    # "step", "host", "ssh" and "hostpci0" as unconfigured services. A finding
    # nobody can act on trains the reader to skip the list.
    "step", "steps", "host", "hosts", "ssh", "hostpci0", "helper", "server",
    "dataset", "tooling", "stack", "lxc", "vm", "container", "containers",
    "disk", "disks", "storage", "pool", "bridge", "port", "ports", "rule",
    "rules", "firewall", "public", "target", "machine", "engine", "local",
    "this", "that", "into", "from", "with", "onto", "their", "there",
})

#: Function words. `_WORD_RE` takes any 3+ letter token, so without these "and"
#: and "for" were reported as unconfigured services ("Install curl and
#: ca-certificates" -> subject "and").
_STOPWORDS = frozenset({
    "and", "the", "for", "are", "was", "has", "had", "not", "but", "you",
    "your", "our", "its", "his", "her", "them", "then", "than", "who", "why",
    "how", "what", "when", "where", "which", "also", "only", "each", "some",
    "any", "all", "one", "two", "three", "new", "old", "via", "per", "out",
    "off", "now", "yet", "own", "use", "used", "using", "make", "made", "back",
    "over", "under", "after", "before", "inside", "again", "both", "same",
})

#: A step title this thin is bookkeeping, not an install worth checking.
_MIN_TITLE_WORDS = 3

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


def subjects(title: str) -> set[str]:
    """The nouns a step is about, lowercased."""
    return {w.lower() for w in _WORD_RE.findall(title or "")
            if w.lower() not in _NOT_A_SERVICE and w.lower() not in _STOPWORDS
            and not _INSTALL_RE.fullmatch(w) and not _CONFIGURE_RE.fullmatch(w)}


def uncovered(nodes: list[dict]) -> list[dict]:
    """``[{node_key, title, subject}]`` for each install with no configuration.

    `nodes` are the plan's steps in execution order.
    """
    rows = [n for n in (nodes or []) if (n.get("title") or "").strip()]
    configured: set[str] = set()
    for n in rows:
        t = n.get("title") or ""
        if _CONFIGURE_RE.search(t):
            configured |= subjects(t)

    out: list[dict] = []
    seen: set[str] = set()
    for n in rows:
        t = n.get("title") or ""
        if not _INSTALL_RE.search(t) or len(t.split()) < _MIN_TITLE_WORDS:
            continue
        subs = subjects(t)
        # the service is the subject that no configuring step ever mentions
        orphan = sorted(s for s in subs if s not in configured)
        if not orphan or not subs:
            continue
        key = orphan[0]
        if key in seen:
            continue
        seen.add(key)
        out.append({"node_key": n.get("node_key"), "title": t[:120], "subject": key})
    if out:
        logger.warning("plan_coverage_gaps n=%d first=%s", len(out), out[0]["subject"])
    return out


def retry_note(gaps: list[dict]) -> str:
    """What goes back to the planner. Names the service, not a score."""
    if not gaps:
        return ""
    lines = "\n".join(f"  - {g['subject']} (installed by {g['node_key']}: {g['title']})"
                      for g in gaps[:10])
    return (
        "\n\nINSTALLED BUT NEVER CONFIGURED — this plan installs these and then "
        "never sets them up:\n" + lines + "\n\nFor each one add the steps that make it "
        "actually usable: connect it to the things it needs, set the paths and "
        "folders, add the sources or credentials it cannot work without, and verify "
        "it does its job end to end. Where the right setting is the operator's taste "
        "rather than a fact, emit a `decision` node that asks them in plain words "
        "(§17.1219) instead of choosing a default. A service left at its defaults is "
        "a service the operator has to finish themselves, which is the work they "
        "asked the engine to do."
    )
