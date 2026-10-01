"""§17.1272 — a page that has forgotten the machine must not advise about it.

One root cause, two symptoms, found by an operator asking a plain question:
*"is it the first copy or the second copy?"* — about the two install lines the
Machines page offers.

`/setup/machines` reads the runner's permissions from a cache and never fills
it, and that cache is empty after every engine restart. With no policy:

  * ``trust_mode`` fell through to ``"list"``, so a runner set to ANY was
    reported as untrusted and the page offered the "turn trust on" line for
    trust the operator already had;
  * the install line silently became a DOWNGRADE. §17.1198 and §17.1204 already
    stop it taking permissions away, by unioning in whatever the machine
    already allows — read from that same missing policy. With no policy the
    union is empty, so the very mechanism written to prevent a downgrade
    produced one: `ANY`, `pct create` and `lvremove` all dropped off a trusted
    runner's line.

The second is the one that matters: §17.1204's rule is that an install line must
not disarm a runner, and this disarmed it while looking exactly like the line
that does not.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTER = (ROOT / "app" / "routers" / "machines.py").read_text(encoding="utf-8")
VIEW = (ROOT / "app" / "ui" / "static" / "views" / "machines.js").read_text(encoding="utf-8")


def test_an_unknown_machine_is_reported_as_unknown():
    """"list" was asserted whenever the policy was MISSING. Unknown is its own
    answer and the only honest one."""
    i = ROUTER.index('"trust_mode"')
    expr = ROUTER[i:i + 240]
    assert '"unknown" if not known_policy' in expr
    assert '"approve" if _sw.ANY in' in expr, "a known trusted runner must still read as approve"


def test_the_install_line_is_not_offered_from_ignorance():
    """A line built with an empty "already allowed" set takes permissions away."""
    assert '"install_ready": bool(known_policy)' in ROUTER


def test_the_union_that_prevents_a_downgrade_still_exists():
    """§17.1198/§17.1204 are the mechanism; this fix feeds them. If the union
    itself were ever dropped, `install_ready` would be guarding nothing."""
    assert 'already = list((policy or {}).get("allow") or [])' in ROUTER
    assert '"already allowed on this runner"' in ROUTER


def test_both_copy_buttons_are_gated_on_knowing():
    """Neither the enumerated line nor the trusted line may be one click from
    the clipboard while the engine does not know what it would replace."""
    assert "const ready = d.install_ready !== false;" in VIEW
    assert "ready ? install : null" in VIEW
    assert "(w.trusted || !ready) ? null : trustedInstall" in VIEW


def test_the_operator_is_told_why_the_line_is_withheld():
    assert "has not read this machine's current permissions yet" in VIEW
    # the sentence is built by concatenation, so assert its halves
    assert "built by ADDING to them" in VIEW
    assert "take away something" in VIEW and "already allowed there" in VIEW


def test_the_page_finds_out_instead_of_asking():
    """"Press Test first" would move the trap, not remove it: the cold cache is
    guaranteed after every restart. The probe is the same one the Test button
    runs, and it fills the cache for every other consumer."""
    assert 'api.post("/setup/machines/probe"' in VIEW
    i = VIEW.index('api.post("/setup/machines/probe"')
    window = VIEW[max(0, i - 500):i]
    assert 'write_channel.checked === false' in window, "only when the state is unknown"


def test_the_probe_runs_once_per_mount():
    """A page that probes on every render is a loop against the operator's
    machine."""
    assert "let probed = false;" in VIEW
    i = VIEW.index('api.post("/setup/machines/probe"')
    assert "probed = true;" in VIEW[max(0, i - 300):i]


def test_the_probe_actually_fills_the_cache_this_reads():
    """The fix only works if that endpoint refreshes the policy — otherwise the
    page probes, re-reads the same empty cache and renders the same wrong thing."""
    setup = (ROOT / "app" / "modules" / "engine_setup.py").read_text(encoding="utf-8")
    i = setup.index("async def probe_local_runner")
    body = setup[i:i + 3000]
    assert "write_policy(spec, use_cache=False)" in body
    assert "runner_setup(spec)" in body


def test_a_failed_probe_is_reported_rather_than_swallowed():
    """The operator is on this page because something is already wrong with the
    machine; a silent catch there is the least useful thing it could do."""
    assert "probeError" in VIEW
    i = VIEW.index('api.post("/setup/machines/probe"')
    assert "probeError =" in VIEW[i:i + 700], "the catch must record why"
    assert "${probeError}" in VIEW, "and the card must show it"


def test_a_trust_claim_is_not_made_on_an_unknown_machine():
    """The summary sentence states what the machine is set to; with no policy it
    would state it wrongly."""
    assert "!ready ? null : el(\"p\", { class: \"cap-summary\"" in VIEW


@pytest.mark.parametrize("flag", ["install_ready", "trust_mode"])
def test_the_page_consumes_what_the_router_sends(flag):
    """§17.1007c's lesson in miniature: a field nobody renders is a field that
    silently stops working."""
    assert flag in ROUTER and re.search(rf"\bd\.{flag}\b", VIEW), f"{flag} is sent but not read"
