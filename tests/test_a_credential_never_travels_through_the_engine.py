"""§17.1391 — a credential the engine reads must not reach the record or the model.

Live, 2026-10-06, and this is §17.1382's capability turning on its author. The
Jellyfin read it added PRINTS the key it finds. §17.1366 carries a previous
attempt's output into the next draft's context. So the key reached the model, and
the model wrote the literal into its own check::

    pct exec 101 -- sh -c 'curl -s -H "X-Emby-Token: c9044034…" …'

Measured on the live database afterwards:

    llm_traces.request_content  carrying the key : 4
    llm_traces.response_content carrying the key : 2
    dag_nodes.output_text       carrying the key : 1

§17.1191's contract is that a secret is resolved BY THE RUNNER or not at all and
never travels through the engine. The engine's own key-reading broke it, and
`scrub_run_output` could not know: it masks values the engine SENT, and this one
was read off a machine it has never been told about.

Two halves, because one alone leaves the door open:

* `mask_credential_literals` keeps it out of the RECORD — by shape, because the
  engine cannot enumerate the credentials its machines hold.
* `a_credential_written_as_a_literal` keeps it out of the BLOCK, which is the
  thing that would send it.
"""
from __future__ import annotations

import inspect

import pytest

from app.modules import supervised_runs as sr
from app.modules.supervised_runs import (_WHOSE_GAP, a_credential_written_as_a_literal,
                                         mask_credential_literals, scrub_run_output)

#: shaped like the real one, not the real one
K = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
LIVE = (f'pct exec 101 -- sh -c \'curl -s -H "X-Emby-Token: {K}" '
        f'"http://127.0.0.1:8096/Items?Recursive=true"\'')


# ── the record ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    f'curl -H "X-Emby-Token: {K}" http://x/Items',
    f'curl -H "X-Api-Key: {K}" http://r:7878/api/v3/movie',
    f'curl "http://x:8096/Items?api_key={K}&q=1"',
    f'curl -H "Authorization: Bearer {K}"',
    f'curl -H "X-MediaBrowser-Token: {K}" http://x',
])
def test_every_credential_shape_is_masked(text):
    out = mask_credential_literals(text)
    assert K not in out and "***" in out


def test_the_header_name_survives_so_the_record_still_reads():
    out = mask_credential_literals(f'curl -H "X-Emby-Token: {K}" http://x')
    assert "X-Emby-Token" in out, "a reader must still see WHAT was sent"


@pytest.mark.parametrize("text", [
    'curl -H "X-Api-Key: $(sed -n \'s/a/b/p\' cfg)" http://x',   # the shape the engine wants
    'curl -H "X-Api-Key: <RADARR_API_KEY>" http://x',            # a placeholder
    "the file /media/movies/Sintel (2010)/x.mkv",
    'ServerId":"df37d1e5fde44e2388f31af6f88806d9"',              # a GUID is not a credential
    "RunTimeTicks:8890930000",
])
def test_what_it_must_not_touch(text):
    assert mask_credential_literals(text) == text


def test_the_scrubber_applies_it():
    """§17.1281 masked only what the engine SENT. A key read off a machine is in
    neither store, which is exactly how this one reached the record."""
    assert K not in scrub_run_output(LIVE, {})
    assert "_CREDENTIAL_LITERAL_RE" in inspect.getsource(sr._CREDENTIAL_LITERAL_RE.__class__.__module__ and sr) \
        or "mask_credential_literals(out)" in inspect.getsource(sr.scrub_run_output)


def test_it_masks_by_shape_not_by_a_list_of_known_values():
    """The engine cannot enumerate the credentials its machines hold, so a
    value-list would always be one service behind."""
    src = inspect.getsource(sr.mask_credential_literals)
    assert "_CREDENTIAL_LITERAL_RE.sub" in src
    other = "zz9y8x7w6v5u4t3s2r1q0p9o8n7m6l5k"
    assert other not in mask_credential_literals(f'X-Api-Key: {other}')


# ── the block ───────────────────────────────────────────────────────────────

def test_the_live_literal_is_refused():
    out = a_credential_written_as_a_literal([LIVE])
    assert len(out) == 1
    why = out[0]["why"]
    assert "<JELLYFIN_API_KEY>" in why and "§17.1191" in why
    assert "4 model requests" in why, "the measurement, not an opinion"


def test_the_refusal_does_not_itself_leak_the_value():
    """A gate that quotes the secret back has moved the leak, not closed it."""
    out = a_credential_written_as_a_literal([LIVE])
    assert K not in out[0]["command"] and K not in out[0]["why"]
    assert out[0]["command"].endswith("…")


def test_a_literal_in_a_check_or_a_file_is_refused_too():
    assert len(a_credential_written_as_a_literal([], None, [LIVE])) == 1
    assert len(a_credential_written_as_a_literal(
        [], [{"path": "/tmp/x.sh", "content": LIVE}])) == 1


def test_one_refusal_per_value():
    assert len(a_credential_written_as_a_literal([LIVE, LIVE, LIVE])) == 1


@pytest.mark.parametrize("text", [
    'curl -H "X-Api-Key: $(cat cfg | sed -n \'s/a/b/p\')" http://x',
    'curl -H "X-Api-Key: <RADARR_API_KEY>" http://x',
    "pct exec 101 -- find /media/movies -type f",
])
def test_the_shapes_the_engine_wants_are_left_alone(text):
    assert a_credential_written_as_a_literal([text]) == []


def test_it_is_wired_and_classified():
    assert "a_credential_written_as_a_literal(cmds, shape_files, verify)" in \
        inspect.getsource(sr.frame_run)
    assert _WHOSE_GAP["a credential into the block as a literal value"] == "drafter"


def test_the_refusal_drives_a_redraft():
    refused = a_credential_written_as_a_literal([LIVE])
    assert refused and sr.shape_retry_note({"kind": "run", "refused": refused})
