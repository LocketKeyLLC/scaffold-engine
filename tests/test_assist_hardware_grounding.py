"""§17.1018 — the operator's own hardware must reach the research query.

Live (T37, 2026-09-11 15:19). The operator wrote:

    "i am in the spectrum app under the router, which is where the DNS server
     forwarding is but it only lists Primary DNS Server and Secondary DNS server."

and got generic advice back: "try 192.168.1.1, look for Port Forwarding /
Advanced / Firewall / NAT, default admin/admin, check the sticker on the
router". Their router is a **Spectrum SAX1V1K**, and that string exists in
exactly ONE place in the session — the NOTES. `render_research_grounding` is
built from `environment` (facts, profile, substitutions, playbook) and has
never read notes, so the single token that makes the question answerable never
reached a query.

The logged query for that turn was the operator's sentence, verbatim:

    assist_fix_research node_key=T37 queries=['i am in the spectrum app under
      the router, which is where the DNS server forwarding is but it only lists
      Primary DNS Server and Secondary DNS server', ...]
"""
import pytest

from app.modules.assist_guide import _error_focus_query
from app.modules.assist_render import hardware_identifiers, render_research_grounding

# Verbatim from the live session's note ledger.
REAL_NOTE = ("Decision (established in conversation, 2026-08-29): abandon the VLAN "
             "segmentation approach — the Spectrum SAX1V1K router cannot trunk "
             "tagged VLANs and the ES2251 modem has no usable LAN port.")
REAL_MSG = ("i am in the spectrum app under the router, which is where the DNS "
            "server forwarding is but it only lists Primary DNS Server and "
            "Secondary DNS server.")


def _notes(*texts):
    return [{"text": t} for t in texts]


# ── the models reach the grounding ───────────────────────────────────────
def test_the_live_router_model_is_extracted():
    assert hardware_identifiers(_notes(REAL_NOTE)) == ["SAX1V1K", "ES2251"]


def test_the_grounding_names_the_exact_model():
    out = render_research_grounding({"facts": ["Proxmox VE 8.2 on the host."]},
                                    _notes(REAL_NOTE))
    assert "SAX1V1K" in out
    assert "search the EXACT model" in out


def test_notes_are_optional_and_backward_compatible():
    """Every existing caller passed one argument; none may break."""
    out = render_research_grounding({"facts": ["Proxmox VE 8.2 on the host."]})
    assert "Proxmox VE 8.2" in out
    assert "Hardware the operator has named" not in out


@pytest.mark.parametrize("text", [
    "Operator updated the palworld.service unit to include RestartSec=30.",
    "See apt-secure for repository signing.",
    "Operator has decided to use Ubuntu 22.04 LTS as the guest OS.",
    "Proxmox host IP is 192.168.1.25.",
])
def test_software_and_addresses_are_not_hardware(text):
    """Measured on the live 44-note ledger: 5 model-shaped tokens overall, of
    which hardware-adjacency keeps only the 2 real ones. Versions and IPs carry
    dots and are excluded by shape; software names lack a hardware noun."""
    assert hardware_identifiers(_notes(text)) == []


def test_a_model_only_counts_next_to_the_kind_of_thing_it_is():
    assert hardware_identifiers(_notes("The X9DRD-7LN4F motherboard has 2 NICs.")) \
        == ["X9DRD-7LN4F"]
    # Same token, no hardware noun in the note → not claimed as hardware.
    assert hardware_identifiers(_notes("Ran benchmark X9DRD-7LN4F last night.")) == []


def test_extraction_is_bounded_and_deduped():
    many = _notes(*[f"The router model RT{i:04d}X is in the rack." for i in range(20)])
    got = hardware_identifiers(many)
    assert len(got) <= 8
    assert len(got) == len(set(got))


# ── and the deterministic fallback query stops being prose ───────────────
def test_the_operators_sentence_is_no_longer_the_query():
    q = _error_focus_query("Validate entire build", REAL_MSG)
    low = q.lower()
    assert not low.startswith("i am in"), f"first-person narration leads the query: {q}"
    for framing in ("i am in", "which is where", "but it only lists"):
        assert framing not in low, f"narration survived: {framing!r} in {q!r}"


def test_volatile_tokens_are_stripped():
    """A timestamp, a unix epoch and a PID match no document, and crowd out the
    terms that would."""
    q = _error_focus_query("Validate entire build",
                           'caddy-proxy Sep 11 14:41:01 caddy[1797]: '
                           '{"ts":1789137661.7748446,"msg":"could not get certificate"}')
    assert "14:41:01" not in q and "1789137661" not in q and "[1797]" not in q
    assert "could not get certificate" in q


def test_a_shell_prompt_is_not_the_subject_of_a_search():
    q = _error_focus_query("Validate entire build", 'root@pve:~# echo "=== Firewall ==="')
    assert not q.startswith("root@pve")


def test_the_query_does_not_repeat_its_own_opening():
    """`lead` is drawn FROM the line, so prefixing it produced
    "caddy-proxy level error caddy-proxy level error Sep 11 ..."."""
    q = _error_focus_query("Validate entire build",
                           "caddy-proxy level error could not get certificate")
    assert q.lower().count("caddy-proxy level error") <= 1, q
