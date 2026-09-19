"""§17.1123 — the fix gate's contradicted-facts detector, re-measured on the real
corpus and narrowed.

Read-only replay over 517 stored fix/guide replies: the old detector flagged
24 (4.6 %) and every one was false; the new one flags 1 (0.2 %). Three of five
scratch fix turns had regenerated (a second ~20 s model call each) on a draft
that AGREED with a negative fact. The no-hit fixtures below are the real
false-positive shapes from that replay; the hit fixture is a genuine
contradiction.
"""
from __future__ import annotations

from app.modules.assist_guide import find_contradicted_facts

FACTS_CERT = {"facts": [
    "nginx on pve is configured to load a TLS certificate at /etc/letsencrypt/live/home.lan/fullchain.pem, but that file does not exist, causing 'nginx -t' to fail",
    "The certificate domain in use is home.lan",
]}
FACTS_LAB = {"facts": [
    "Operator runs commands as root@pve in ONE interactive shell",
    "The Proxmox bridge is vmbr0",
    "The management VM's address is 192.168.1.21",
    "Storage 'local-lvm' is the LVM-thin pool; 'local' is the directory storage",
    "Container tooling: pct is available on the host",
    "The firewall config lives under /etc/pve/firewall/",
]}


# ── the hit: a genuine contradiction ─────────────────────────────────────────

def test_disowning_a_confirmed_value_is_a_hit():
    draft = "## Diagnosis\nThe bridge `vmbr0` does not exist on this host, so the VM has no network. Create it first."
    hits = find_contradicted_facts(draft, FACTS_LAB)
    assert [h["value"] for h in hits] == ["vmbr0"]
    assert "does not exist" in hits[0]["claim"]


def test_subject_after_the_claim_phrase_is_also_a_hit():
    draft = "This fails because the following storage does not exist: `local-lvm`. Use another."
    assert [h["value"] for h in find_contradicted_facts(draft, FACTS_LAB)] == ["local-lvm"]


# ── agreement is not contradiction (the scratch-turn regenerations) ──────────

def test_saying_a_file_is_missing_when_the_fact_says_it_is_missing_is_agreement():
    draft = ("## Diagnosis\n`/etc/letsencrypt/live/home.lan/fullchain.pem` does not exist, which is why "
             "`nginx -t` fails. ## Fix\n1. Issue a certificate for `home.lan` …")
    assert find_contradicted_facts(draft, FACTS_CERT) == []


# ── the real false-positive shapes, from the corpus replay ───────────────────

def test_placeholder_instructions_are_not_claims():
    draft = "## Inputs needed\nReplace each `<PLACEHOLDER>` below with the actual value for your network. Run as `root@pve`."
    assert find_contradicted_facts(draft, FACTS_LAB) == []


def test_a_value_far_from_the_claim_is_not_its_subject():
    draft = ("Edit `/etc/pve/firewall/cluster.fw` as before. Then the download: it tried to fetch a template "
             "named `system` — which does not exist. The Debian 12 template is available.")
    assert find_contradicted_facts(draft, FACTS_LAB) == []


def test_quoted_or_conditional_error_text_is_not_the_drafts_claim():
    draft = "## If that fails\nIf the second command says `storage 'local-lvm' does not exist`, your installation uses a different name."
    assert find_contradicted_facts(draft, FACTS_LAB) == []
    draft2 = "The error message `Unit file prowlarr.service does not exist` means the package did not install; `apt-get update` first."
    assert find_contradicted_facts(draft2, FACTS_LAB) == []


def test_short_tokens_and_substrings_do_not_match_the_ledger():
    draft = "`pct` is the host tool; it does not exist inside the container itself."
    assert find_contradicted_facts(draft, FACTS_LAB) == []
    draft2 = "The name `loc` does not exist as a storage id."
    assert find_contradicted_facts(draft2, FACTS_LAB) == [], "`loc` is a substring of local, not a fact"


def test_no_facts_or_no_text_is_quiet():
    assert find_contradicted_facts("`vmbr0` does not exist", {"facts": []}) == []
    assert find_contradicted_facts("", FACTS_LAB) == []
