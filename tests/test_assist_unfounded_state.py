"""§17.1015 — the engine must not assert state it has no grounding for.

Two live defects from session 613dd1df, T35, 2026-09-11 02:09, in one message:

  1. "**Values filled in:** `YOUR_DOMAIN` → `home.local` (from your
     environment)". `home.local` appears 0 times in the session's 40 facts.
     The resolver's own system prompt says "Never invent a kind=known value
     that is not literally supported by the facts" — a request the model
     declined. The message's own Diagnosis said a PUBLIC domain was required
     for Let's Encrypt, and then proposed `curl -I https://jellyfin.home.local`.

  2. "the `dmz` group was created earlier in the project" — asserted to the
     operator who had, that minute, said they did not know where dmz came
     from. The fact ledger recorded their doubt correctly, verbatim:
     "A security group was added (operator is unsure where the 'dmz' security
     group came from)."  The grounding header already said to "treat anything
     marked unknown/unverified as still open"; the model read past it.
"""
import pytest

from app.modules.assist_placeholders import _supported_by_facts
from app.modules.assist_render import render_environment_block

# The real fact from the live session's ledger.
REAL_CADDY_FACT = (
    "Inside LXC container 120 (caddy-proxy), the /etc/caddy/Caddyfile reverse "
    "proxies: http://jellyfin.local -> 192.168.1.20:8096, http://prowlarr.local "
    "-> 192.168.1.21:9696, and http://panel.local -> 192.168.1.25:3000."
)
REAL_DMZ_FACT = ("A security group was added (operator is unsure where the "
                 "'dmz' security group came from).")
REAL_ENV = {"profile": "Proxmox VE host pve at 192.168.1.25",
            "substitutions": {"JELLYFIN_IP": "192.168.1.20"}}


# ── 1. a "known" value must actually be in the grounding ─────────────────
def test_the_live_fabrication_is_rejected():
    """`home.local` resembles the facts without appearing in them."""
    assert _supported_by_facts("home.local", [REAL_CADDY_FACT], REAL_ENV) is False


@pytest.mark.parametrize("value", [
    "jellyfin.local", "panel.local", "192.168.1.20", "192.168.1.25", "caddy-proxy",
])
def test_values_actually_in_the_facts_are_supported(value):
    assert _supported_by_facts(value, [REAL_CADDY_FACT], REAL_ENV) is True


def test_the_profile_and_pins_also_count_as_grounding():
    assert _supported_by_facts("pve", [], REAL_ENV) is True
    assert _supported_by_facts("192.168.1.20", [], REAL_ENV) is True


@pytest.mark.parametrize("value", ["example.com", "myhomelab.duckdns.org", "vmbr9"])
def test_plausible_but_absent_values_are_not_supported(value):
    assert _supported_by_facts(value, [REAL_CADDY_FACT], REAL_ENV) is False


def test_matching_is_case_insensitive_but_otherwise_literal():
    assert _supported_by_facts("CADDY-PROXY", [REAL_CADDY_FACT], REAL_ENV) is True
    # A near-miss must NOT pass — this is the whole point.
    assert _supported_by_facts("caddyproxy", [REAL_CADDY_FACT], REAL_ENV) is False


def test_empty_value_is_never_supported():
    assert _supported_by_facts("", [REAL_CADDY_FACT], REAL_ENV) is False
    assert _supported_by_facts("   ", [REAL_CADDY_FACT], REAL_ENV) is False


def test_the_resolver_enforces_it():
    """Prompt rules are requests; this one is now checked."""
    import inspect
    from app.modules import assist_placeholders
    src = inspect.getsource(assist_placeholders.resolve_placeholders)
    assert "_supported_by_facts(" in src
    assert 'kind = "suggested"' in src, (
        "an unfounded known value must be relabelled, not shipped as observed"
    )


# ── 2. uncertain facts are structurally separated ────────────────────────
def test_an_uncertain_fact_does_not_render_as_established():
    out = render_environment_block({
        "facts": [REAL_CADDY_FACT, REAL_DMZ_FACT],
    })
    assert "### OPEN — recorded as uncertain, NOT established" in out
    open_part = out.split("### OPEN")[1]
    assert "dmz" in open_part, "the uncertain fact must be under OPEN"
    # ...and must NOT be in the settled list.
    settled_part = out.split("### OPEN")[0]
    assert "dmz" not in settled_part
    assert "jellyfin.local" in settled_part


def test_the_open_section_forbids_the_exact_failure():
    out = render_environment_block({"facts": [REAL_DMZ_FACT]})
    low = out.lower()
    assert "do not state them as settled" in low
    assert "where they came from" in low      # the dmz answer, specifically
    assert "settles it" in low                # and says to CHECK instead


@pytest.mark.parametrize("fact", [
    "The Caddyfile write could not be verified; 21 lines on disk vs 15 written.",
    "It is unclear whether the dmz group exists at datacenter level.",
    "The reboot outcome was inconclusive.",
    "Port forwarding has not been confirmed.",
])
def test_other_doubt_wordings_also_land_under_open(fact):
    out = render_environment_block({"facts": [fact]})
    assert "### OPEN" in out
    assert fact in out.split("### OPEN")[1]


def test_settled_facts_keep_their_own_heading():
    out = render_environment_block({"facts": [REAL_CADDY_FACT]})
    assert "### Known facts about the operator's system" in out
    assert "### OPEN" not in out, "no open items → no empty OPEN heading"


def test_all_facts_uncertain_emits_no_empty_known_heading():
    out = render_environment_block({"facts": [REAL_DMZ_FACT]})
    assert "### OPEN" in out
    assert "### Known facts about the operator's system" not in out
