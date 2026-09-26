"""§17.1175 — the grounding layer's own false positives and false negatives.

Items 12-17 of the 2026-09-25 audit's fix order. Each of these was a check that
RAN on every turn and reported the wrong thing: values that were not values,
regenerations that were not better, gates outside the registry that was supposed
to catch them crashing, a decision resolved from a number that named nothing, a
ledger the executor could not see, and a "contradiction" detector that had never
found one.
"""
from __future__ import annotations

import pytest

from app.modules.assist_evidence import (
    extract_specifics, owned_hosts, unsupported_specifics,
)
from app.modules.plan_reconcile import choose
from app.modules.research_agent import _check_contradictions

pytestmark = pytest.mark.smoke


class TestVersionsAreNotMoney:
    """M1 — `\\b\\d{1,2}\\.\\d{2}\\b` was there for `22.04` and also matched
    prices and percentages. Those became {"kind": "version"} and were shown to
    the operator as "⚠️ Unverified specifics" — and, because
    `plan_reconcile.values_in` reuses this extractor, a pair of them became a
    correction regex-substituted into EVERY pending step's task text."""

    @pytest.mark.parametrize("text", [
        "the plan costs $49.99 per month", "95.50 percent", "£12.50 a seat",
        "50.00% done", "€9.99/mo", "uptime 99.95 percent",
    ])
    def test_money_and_percentages_are_not_versions(self, text):
        assert [f for f in extract_specifics(text) if f["kind"] == "version"] == [], text

    @pytest.mark.parametrize("text,want", [
        ("upgrade to 22.04", "22.04"), ("Ubuntu 24.04 LTS", "24.04"),
        ("version 1.2.3", "1.2.3"), ("Proxmox VE 8.10.2", "8.10.2"),
    ])
    def test_real_versions_still_extract(self, text, want):
        assert want in [f["value"] for f in extract_specifics(text) if f["kind"] == "version"]


class TestTheOperatorsOwnMachinesAreOwned:
    """M5 — `_HOST_RE` requires an alpha TLD, so a bare IPv4 could never be
    owned however many times the operator's ledger named it. On a homelab whose
    machines ARE addressed by IP, every `http://192.168.1.20:8096` in a reply
    was unverified by construction."""

    ENV = {"facts": ["Jellyfin runs in CT 102 at 192.168.1.20", "the host is pve.lan"],
           "substitutions": {"NAS": "192.168.1.50"}}

    def test_an_ip_the_ledger_names_is_owned(self):
        assert {"192.168.1.20", "192.168.1.50", "pve.lan"} <= owned_hosts(self.ENV)

    def test_a_url_on_the_operators_own_machine_is_credited(self):
        u = unsupported_specifics("Open http://192.168.1.20:8096 to check",
                                  "", owned=owned_hosts(self.ENV))
        assert [x for x in u if x["kind"] == "url"] == []

    def test_a_url_somewhere_else_is_still_unsupported(self):
        u = unsupported_specifics("Fetch http://8.8.8.8/x", "", owned=owned_hosts(self.ENV))
        assert [x["value"] for x in u if x["kind"] == "url"] == ["http://8.8.8.8/x"]

    def test_a_documentation_address_is_never_owned(self):
        """RFC 5737 addresses are examples; an example cannot credit a URL —
        the same reason `_EXAMPLE_IP_RE` excludes them on the other side."""
        for ip in ("192.0.2.5", "198.51.100.7", "203.0.113.9", "0.0.0.0"):
            assert ip not in owned_hosts({"facts": [f"see {ip}"]}), ip

    def test_the_executor_passes_the_real_ledger_not_a_synthetic_one(self):
        """The assist paths handed `owned_hosts` facts/profile/substitutions;
        the executor handed it `{"_brief_text": …}` only, so a host established
        as a session fact was not owned during autonomous execution."""
        import inspect
        from app.modules import execution_evidence as ee
        src = inspect.getsource(ee.verify_node_output)
        assert "environment or {}" in src and '"_brief_text": given' in src
        assert "environment" in inspect.signature(ee.verify_node_output).parameters


class TestADecisionNeedsAnOptionNumber:
    """M4 — the verb list was eight words wide and the number did not have to be
    an option number, so "I'll go with 2 NICs" resolved to option 2 and appended
    a decision directive to every pending downstream node."""

    OPTS = [{"n": 1, "label": "ZFS mirror", "text": "ZFS mirror - needs RAM"},
            {"n": 2, "label": "LVM-thin", "text": "LVM-thin - simpler"}]

    @pytest.mark.parametrize("record", [
        "I will go with 2 NICs on this box",
        "we need 2 disks for this",
        "lets use 5 of them",
        "pick 3 of the four",
    ])
    def test_a_number_that_names_no_option_resolves_to_nothing(self, record):
        assert choose(self.OPTS, record) is None, record

    @pytest.mark.parametrize("record,label", [
        ("go with option 2", "LVM-thin"), ("option 1", "ZFS mirror"),
        ("choice 2", "LVM-thin"), ("I choose option 1", "ZFS mirror"),
    ])
    def test_an_explicit_option_number_still_resolves(self, record, label):
        got = choose(self.OPTS, record)
        assert got and got["label"] == label, record

    def test_an_out_of_range_option_number_is_refused(self):
        assert choose(self.OPTS, "go with option 7") is None


class TestContradictionsAreContradictions:
    """H4 — the detector flagged any two TITLES sharing ≥2 words, no stopword
    filter, never looking at the content. On six realistic titles every pair
    "contradicted"; with the 5-cap, a real run showed five fabrications and no
    real one, rendered with a ⚡ in the research view."""

    NOISE = [
        {"title": "How to install Docker on Ubuntu", "content": "Run apt install docker.io"},
        {"title": "How to configure Nginx on Debian", "content": "Edit /etc/nginx/nginx.conf"},
        {"title": "Getting started with the Proxmox web UI", "content": "Browse to port 8006"},
        {"title": "Getting started with Kubernetes", "content": "Install kubectl first"},
        {"title": "A guide to the ZFS file system", "content": "ZFS needs ECC memory ideally"},
        {"title": "A guide to the Btrfs file system", "content": "Btrfs supports subvolumes"},
    ]

    def test_a_realistic_corpus_produces_no_contradictions(self):
        assert _check_contradictions(self.NOISE) == []

    def test_the_same_subject_with_different_values_is_a_contradiction(self):
        got = _check_contradictions([
            {"title": "Proxmox VE 8 firewall port", "content": "The web UI listens on port 8006"},
            {"title": "Proxmox VE 8 firewall configuration", "content": "The web UI listens on port 8007"},
        ])
        assert len(got) == 1 and "8006" in got[0]["why"] and "8007" in got[0]["why"]

    def test_the_same_subject_with_opposed_polarity_is_a_contradiction(self):
        got = _check_contradictions([
            {"title": "Tailscale exit node support", "content": "Tailscale supports exit nodes on Linux"},
            {"title": "Tailscale exit node limits", "content": "Tailscale does not support exit nodes here"},
        ])
        assert len(got) == 1 and "negative" in got[0]["why"]

    def test_the_same_subject_agreeing_is_not_a_contradiction(self):
        assert _check_contradictions([
            {"title": "Proxmox VE 8 firewall port", "content": "The web UI listens on port 8006"},
            {"title": "Proxmox VE 8 firewall notes", "content": "Reach the web UI on port 8006"},
        ]) == []


class TestTheRegenerationMustActuallyBeBetter:
    """M2 — clauses 2-5 of `better` fire when the candidate cleared a STRUCTURAL
    failure and none of them looked at the unsupported-value count, so a draft
    with 0 could be replaced by one with 5 fabricated IPs while
    report["regenerated"] read True. The docstring already promised otherwise."""

    def test_every_structural_clause_requires_no_new_unsupported_values(self):
        import inspect
        from app.modules import assist_evidence as ae
        src = inspect.getsource(ae.verify_answer)
        better = src[src.index("no_new_values = "):src.index("if better:")]
        assert better.count("no_new_values") >= 5, (
            "each structural clause of `better` must require no_new_values")


class TestEveryCheckGoesThroughTheRegistry:
    """M3 — six of verify_answer's checks ran through `run_gate`; four did not,
    so a crash in the plan-only tier, the interface check or the sourced-now
    scan escaped into the operator's turn instead of becoming
    assist_gate_crashed on /health."""

    def test_no_bare_gate_calls_remain_in_verify_answer(self):
        import inspect
        from app.modules import assist_evidence as ae
        src = inspect.getsource(ae.verify_answer)
        for fn in ("unsupported_specifics", "interface_specifics_present",
                   "max_source_authority", "sourced_now", "addresses_question",
                   "command_shape_issues"):
            for line in src.splitlines():
                stripped = line.strip()
                if f"{fn}(" in stripped and "run_gate" not in stripped and not stripped.startswith("#"):
                    pytest.fail(f"{fn} is called outside run_gate: {stripped}")

    def test_annotated_is_false_when_annotation_was_forbidden(self):
        import inspect
        from app.modules import assist_evidence as ae
        src = inspect.getsource(ae.verify_answer)
        assert 'report["annotated"] = bool(annotate and _fails(' in src
