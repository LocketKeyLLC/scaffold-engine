"""§17.981 — every key `set_environment` writes must survive being read back.

Found by the §17.980 integration test on its first run, and it had been dead in
production since the day it shipped.

`_environment_from_metadata` returns a FIXED dict of keys. `file_writes` — the
§17.965/967/968/972 file ledger: expected sizes, content hashes, retained bodies
and every cross-artefact contract check built on them — was not among them. So
`set_environment` wrote it, the next read dropped it, and the following fact fold
ERASED it, because set_environment writes the whole env dict back.

The file already carried a §17.881b comment warning about exactly this failure,
naming a live incident where the playbook was clobbered the same way. The comment
did not prevent the next occurrence, because a comment cannot enumerate.

Every unit test for §17.965-972 passed throughout: none of them round-tripped
through the real store. That is the whole argument for the integration lane.
"""
import re

import pytest

from app.modules.assist_environment import _environment_from_metadata

# Every key `set_environment` assigns into `current`, read straight out of the
# source so a new one cannot be added without this test noticing.
WRITTEN = {"banned_values", "facts", "file_writes", "missing_tools", "playbook",
           "profile", "substitutions", "substitutions_by_node", "system_state",
           "verbosity"}

# `verbosity` is the one that is NOT persisted inside the environment object: it
# is a SIBLING of `environment` in the metadata blob ("environment always,
# verbosity when given"), has its own `_verbosity_from_metadata` reader, and is
# merely decorated onto set_environment's return value for the caller's
# convenience. So it is written into `current` and correctly absent here — the
# distinction that separates it from `file_writes`, which WAS the bug.
PERSISTED = WRITTEN - {"verbosity"}

_SAMPLE = {
    "profile": "root@pve",
    "substitutions": {"HOST": "192.168.1.25"},
    "substitutions_by_node": {"T1": {"HOST": "x"}},
    "banned_values": ["radarr.video"],
    "facts": ["Proxmox host at 192.168.1.156"],
    "missing_tools": [{"tool": "sudo", "host": "pve"}],
    "system_state": {"106": {"kind": "vm", "attrs": {}, "devices": {}}},
    "playbook": {"proven": ["pct create works"], "ruled_out": ["apt.servarr.com"]},
    "file_writes": {"/opt/a/server.js": {"expected": 120, "lines": 4,
                                         "sha": "abc123", "body": "app.listen(3001);",
                                         "observed": None, "observed_sha": None}},
    "verbosity": "normal",
}


def test_the_writable_key_set_is_still_what_this_test_thinks():
    """If someone adds a key to set_environment, this fails first and tells them
    to add it to the round-trip sample rather than discovering it in production
    three features later."""
    import inspect

    from app.modules import assist_environment

    src = inspect.getsource(assist_environment)
    found = set(re.findall(r'current\["([a-z_]+)"\]', src))
    assert found == WRITTEN, {"in source": found - WRITTEN,
                              "in test only": WRITTEN - found}


@pytest.mark.parametrize("key", sorted(PERSISTED))
def test_every_persisted_key_survives_the_read(key):
    """The one that was broken: file_writes went in and never came back."""
    out = _environment_from_metadata({"environment": dict(_SAMPLE)})
    assert key in out, f"{key} was written by set_environment and dropped on read"
    assert out[key] == _SAMPLE[key], key


def test_verbosity_is_deliberately_not_in_the_environment_object():
    """Pins the distinction, so a later reader does not "fix" its absence and
    quietly change where it is stored."""
    out = _environment_from_metadata({"environment": dict(_SAMPLE)})
    assert "verbosity" not in out


def test_the_file_ledger_specifically_round_trips_whole():
    """Not just present — intact. §17.968 needs `body`, §17.967 needs `sha`, and
    a partial round-trip would let both answer confidently and wrongly."""
    out = _environment_from_metadata({"environment": dict(_SAMPLE)})
    rec = out["file_writes"]["/opt/a/server.js"]
    assert rec["sha"] == "abc123"
    assert rec["body"] == "app.listen(3001);"
    assert rec["expected"] == 120


@pytest.mark.parametrize("bad", [None, "", "not json", {}, {"environment": None},
                                {"environment": "x"}])
def test_malformed_metadata_yields_the_documented_minimal_shape(bad):
    """A missing or corrupt blob returns `{profile, substitutions, facts}` — the
    shape the docstring promises, deliberately minimal rather than full.

    Checked rather than assumed to be a bug: it is safe because there is nothing
    coherent to preserve in that case, and the NEXT read of a well-formed blob
    fills every key from its own defaults. The `file_writes` defect was
    different in kind — it dropped a key from a VALID environment."""
    out = _environment_from_metadata(bad)
    assert isinstance(out, dict)
    assert out["profile"] == "" and out["substitutions"] == {}


def test_a_wrong_type_is_replaced_not_propagated():
    out = _environment_from_metadata({"environment": {"file_writes": ["not", "a", "dict"],
                                                      "playbook": 5}})
    assert out["file_writes"] == {}
    assert out["playbook"] == {}
