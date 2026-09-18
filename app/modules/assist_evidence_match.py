"""§17.1101 — match pasted evidence to the step it actually completes.

The operator's plan cursor drifts (steps reopen, they jump around), so a paste
often proves a DIFFERENT pending step than the one in focus — and the verifier,
judging against the wrong step, returns "I couldn't verify". Live: `qm config
106 | grep scsi0 … size=40G` was pasted while the cursor sat on ADD54
(palworld.service); it actually completes ADD15 (set scsi0 to 40G).

This module finds the right step. Precision over recall, by construction:
- a cheap DETERMINISTIC pre-filter (shared concrete tokens — commands, ids,
  values) shortlists candidates, so the model never judges unrelated steps;
- each shortlisted candidate is verified against ITS OWN 'Done when' bar;
- only a CONFIDENT, UNIQUE 'succeeded' auto-commits — two matches is ambiguous
  (ask), zero falls through to the normal block.

Pure functions here; the model-in-the-loop match + the commit live in
assist_agent, gated by `assist_evidence_step_match_enabled`.
"""
from __future__ import annotations

import re

# Concrete tokens worth matching on — the things that tie a paste to a step.
_CMD_RE = re.compile(r"\b(?:pct|qm|systemctl|ip|zpool|caddy|docker|nvidia-smi|apt|apt-get)\b", re.I)
_ID_RE = re.compile(r"\b(?:ct|lxc|vm)?\s*#?\s*(\d{2,4})\b", re.I)          # 106, CT 120
_SIZE_RE = re.compile(r"\b\d+\s?(?:g|gb|gib|t|tb|m|mb)\b", re.I)           # 40G, 512M
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")                        # 192.168.1.50
_PATH_RE = re.compile(r"/[\w./-]{3,}")                                     # /etc/caddy/Caddyfile
_SVC_RE = re.compile(r"\b[\w-]+\.service\b", re.I)                         # palworld.service
_WORD_RE = re.compile(r"\b[a-z][a-z0-9_-]{3,}\b", re.I)                    # generic identifiers

_STOP = {
    "this", "that", "step", "done", "when", "with", "from", "your", "will", "have",
    "into", "then", "show", "shows", "should", "config", "output", "command", "root",
    "true", "false", "next", "here", "which", "what", "each", "make", "sure", "check",
}


def evidence_tokens(text: str) -> set[str]:
    """Concrete, matchable tokens from a paste or a step's text — ids, sizes,
    IPs, paths, service names, known commands, and salient words."""
    t = text or ""
    toks: set[str] = set()
    toks |= {m.lower() for m in _CMD_RE.findall(t)}
    toks |= {m for m in _ID_RE.findall(t)}
    toks |= {m.lower().replace(" ", "") for m in _SIZE_RE.findall(t)}
    toks |= {m for m in _IP_RE.findall(t)}
    toks |= {m.lower() for m in _PATH_RE.findall(t)}
    toks |= {m.lower() for m in _SVC_RE.findall(t)}
    toks |= {w.lower() for w in _WORD_RE.findall(t) if w.lower() not in _STOP}
    return toks


# ids / sizes / ips / paths / services are STRONG signals (a step is about a
# specific machine + value); a shared generic word is weak.
_STRONG_RE = re.compile(r"^(?:\d{2,4}|\d+(?:g|gb|gib|t|tb|m|mb)|/[\w./-]+|[\w-]+\.service|\d{1,3}(?:\.\d{1,3}){3})$", re.I)


def score_candidate(evidence_toks: set[str], step_text: str) -> int:
    """Overlap score between a paste's tokens and one step's text. A shared
    STRONG token (id, size, ip, path, service) counts 3; a shared word counts 1."""
    cand = evidence_tokens(step_text)
    shared = evidence_toks & cand
    return sum(3 if _STRONG_RE.match(tok) else 1 for tok in shared)


def shortlist_candidates(evidence: str, candidates: list[dict], *, top_k: int = 3,
                         min_score: int = 3) -> list[dict]:
    """Rank pending steps by concrete-token overlap; keep the top_k that clear
    min_score (default 3 = at least one strong shared token). Each candidate is
    a dict with at least ``node_key`` and a ``match_text`` (title + done-when).
    Returns the survivors with a ``score`` added, best first."""
    ev = evidence_tokens(evidence)
    scored = []
    for c in candidates:
        s = score_candidate(ev, c.get("match_text") or "")
        if s >= min_score:
            scored.append({**c, "score": s})
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:top_k]


# ── §17.1102 — near-duplicate pending steps (plan hygiene) ──────────────────
_ID_ONLY_RE = re.compile(r"^\d{2,4}$")   # a bare machine/container id


def _same_work(ti: set[str], tj: set[str]) -> bool:
    """Two steps are the SAME work when they share the same MACHINE (an id) AND
    the same DELIVERABLE — either a shared non-id strong token (size / ip / path
    / service), or a high word overlap. A shared id ALONE is not enough: many
    different steps act on the same machine (measured on 39 real pending steps,
    where id-only merging chained SSH-key + static-IP + NVIDIA + PM2 into one
    false cluster)."""
    shared = ti & tj
    ids = {t for t in shared if _ID_ONLY_RE.match(t)}
    if not ids:
        return False
    value_strong = {t for t in shared if _STRONG_RE.match(t) and not _ID_ONLY_RE.match(t)}
    if value_strong:
        return True
    # no shared value token — fall back to a HIGH word-overlap ratio (same
    # phrasing, e.g. two "Set static IP and gateway on LXC 111's net0" steps).
    wi = {t for t in ti if not _STRONG_RE.match(t)}
    wj = {t for t in tj if not _STRONG_RE.match(t)}
    if not wi or not wj:
        return False
    jac = len(wi & wj) / len(wi | wj)
    return jac >= 0.6


def near_duplicate_clusters(steps: list[dict]) -> list[list[dict]]:
    """Group pending steps that are the SAME work (see `_same_work`). Clustering
    is transitive over pairs that DIRECTLY satisfy `_same_work`, which — because
    the pair test needs a shared deliverable, not just a shared machine — no
    longer chains unrelated same-machine steps. Each step needs ``node_key`` and
    ``match_text``. Returns clusters of size >= 2."""
    n = len(steps)
    toks = [evidence_tokens(s.get("match_text") or "") for s in steps]
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if _same_work(toks[i], toks[j]):
                parent[find(i)] = find(j)

    groups: dict[int, list[dict]] = {}
    for i, s in enumerate(steps):
        groups.setdefault(find(i), []).append(s)
    return [g for g in groups.values() if len(g) >= 2]
