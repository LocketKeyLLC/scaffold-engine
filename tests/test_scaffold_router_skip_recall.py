"""§17.316 — `/skip` adopts §17.307 with tiered confirmation-friction.

§17.316 is the sixth and final cohort member to adopt active-job
memory. /skip is unique in the cohort because it has DUAL
semantics:

  - bare `/skip <job_id>` lists candidate nodes (informational,
    §17.215 E1)
  - `/skip <job_id> <node_key>` performs skip (state-altering)

This maps cleanly onto the §17.314/§17.315 tiered model:

  /skip                  (0 args)           → recall list (informational)
  /skip <UUID>           (1 arg, UUID)      → list candidates (existing)
  /skip <node_key>       (1 arg, non-UUID)  → auto-skip on recall (§17.315)
  /skip <job_id> <node>  (2 args)           → explicit skip (unchanged)

The 0-args informational path is the §17.316 contribution — it
distinguishes /skip from §17.314's /execute (0 args state-altering)
and §17.315's /exec retry (0 args needs node_key to disambiguate).
/skip's 0-args is safe to auto-recall because list-candidates is
read-only.

These tests pin each tier + cross-test with §17.314/§17.315
patterns + source-shape guards.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


_SAMPLE_JOB_ID = "abc1234e-d5f6-7890-abcd-ef1234567890"
_OTHER_JOB_ID = "ffff1111-2222-3333-4444-555566667777"
_CHAT_A = "chat-aaa-111"


def _candidates_response(nodes: list | None = None) -> MagicMock:
    """Mock /exec/status/<id> for _render_skip_candidates."""
    r = MagicMock(status_code=200, text="")
    r.json.return_value = {
        "nodes": nodes or [
            {"node_key": "T1", "status": "failed", "title": "Build artifact"},
            {"node_key": "T2", "status": "pending", "title": "Run tests"},
        ],
    }
    return r


def _ok_skip_response() -> MagicMock:
    r = MagicMock(status_code=200, text="")
    r.json.return_value = {"job_id": _SAMPLE_JOB_ID, "node_key": "T3",
                           "status": "skipped"}
    return r


# ---------------------------------------------------------------------------
# Tier 1: 0 args + recall = list candidates from recalled job
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestBareSkipWithRecall:
    """§17.316 — `/skip` (no args) with recall hit auto-lists
    candidates from the recalled job. INFORMATIONAL path — no
    friction needed (list-candidates isn't state-altering)."""

    def test_bare_skip_recall_hit_lists_candidates(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="my CLI")
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _candidates_response()
            out = pipe._handle_skip(["/skip"], chat_id=_CHAT_A)
        # 📌 hint surfaced.
        assert "📌" in out
        # _render_skip_candidates fired with the recalled id.
        called_url = mg.call_args[0][0]
        assert _SAMPLE_JOB_ID in called_url
        # Candidate rows present (from _render_skip_candidates output).
        assert "T1" in out
        assert "T2" in out

    def test_bare_skip_no_recall_returns_usage(self, pipe):
        """Cold cache + bare /skip = pre-§17.316 Usage error
        (with §17.316's added hint pointing at bare /skip <id>)."""
        out = pipe._handle_skip(["/skip"], chat_id=_CHAT_A)
        assert "Usage:" in out
        assert "/skip <job_id> <node_key>" in out
        # No 📌 because nothing was recalled.
        assert "📌" not in out

    def test_bare_skip_no_chat_id_returns_usage(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        out = pipe._handle_skip(["/skip"], chat_id=None)
        assert "Usage:" in out
        assert "📌" not in out


# ---------------------------------------------------------------------------
# Tier 2: 1 arg UUID = list candidates (existing §17.215 E1 behavior)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSingleUUIDListsCandidates:
    """§17.316 — `/skip <UUID>` preserves §17.215 E1's behavior:
    list candidates for that specific job_id. Existing path."""

    def test_single_uuid_lists_candidates(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _candidates_response()
            out = pipe._handle_skip(
                ["/skip", _OTHER_JOB_ID], chat_id=_CHAT_A,
            )
        # Candidates listed for the EXPLICIT id, not recalled.
        called_url = mg.call_args[0][0]
        assert _OTHER_JOB_ID in called_url
        # No 📌 (explicit path, no recall consulted).
        assert "📌" not in out
        # Candidates rendered.
        assert "T1" in out

    def test_single_uuid_overrides_recall(self, pipe):
        """Explicit UUID never consults the cache — operator typed
        the id they meant; honor it."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _candidates_response()
            pipe._handle_skip(
                ["/skip", _OTHER_JOB_ID], chat_id=_CHAT_A,
            )
        called_url = mg.call_args[0][0]
        assert _OTHER_JOB_ID in called_url
        assert _SAMPLE_JOB_ID not in called_url


# ---------------------------------------------------------------------------
# Tier 3: 1 arg non-UUID = auto-skip via recall (§17.315 pattern)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSingleNodeKeyAutoSkip:
    """§17.316 — `/skip <node_key>` with recall hit auto-fires the
    skip on the recalled job_id. Mirror of §17.315's /exec retry
    single-non-UUID-arg pattern."""

    def test_single_node_recall_hit_auto_fires(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="my CLI")
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            mp.return_value = _ok_skip_response()
            out = pipe._handle_skip(
                ["/skip", "T3"], chat_id=_CHAT_A,
            )
        # POST fired with recalled job_id + typed node_key.
        mp.assert_called_once()
        sent_json = mp.call_args[1]["json"]
        assert sent_json["job_id"] == _SAMPLE_JOB_ID
        assert sent_json["node_key"] == "T3"
        # 📌 hint surfaced.
        assert "📌" in out

    def test_single_node_no_recall_returns_error(self, pipe):
        """Cold cache + single node_key = friendly error pre-filling
        the typed node_key in 2-arg suggestion."""
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            out = pipe._handle_skip(
                ["/skip", "T3"], chat_id=_CHAT_A,
            )
        mp.assert_not_called()
        assert "No active job" in out
        # 2-arg suggestion pre-fills node_key.
        assert "/skip <job_id> T3" in out

    def test_single_node_no_chat_id_returns_error(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            out = pipe._handle_skip(
                ["/skip", "T3"], chat_id=None,
            )
        mp.assert_not_called()
        assert "No active job" in out

    def test_single_node_error_mentions_bare_skip_option(self, pipe):
        """The cold-cache single-arg error must mention that bare
        /skip <id> lists candidates — operators who typed a node_key
        without knowing the job_id need to discover the listing path."""
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            out = pipe._handle_skip(
                ["/skip", "T3"], chat_id=_CHAT_A,
            )
        # Points at the listing affordance.
        assert "list candidate nodes" in out or "candidate nodes" in out


# ---------------------------------------------------------------------------
# Tier 4: 2 args explicit (existing path, unchanged)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestExplicitTwoArgSkip:

    def test_two_arg_explicit_fires(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            mp.return_value = _ok_skip_response()
            out = pipe._handle_skip(
                ["/skip", _OTHER_JOB_ID, "T7"], chat_id=_CHAT_A,
            )
        mp.assert_called_once()
        sent_json = mp.call_args[1]["json"]
        assert sent_json["job_id"] == _OTHER_JOB_ID
        assert sent_json["node_key"] == "T7"
        # No 📌 — explicit path doesn't consult recall.
        assert "📌" not in out

    def test_two_arg_overrides_recall(self, pipe):
        """Explicit 2-arg never consults the cache (mirror of §17.314/
        §17.315 contract)."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="cached")
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            mp.return_value = _ok_skip_response()
            pipe._handle_skip(
                ["/skip", _OTHER_JOB_ID, "T7"], chat_id=_CHAT_A,
            )
        sent_json = mp.call_args[1]["json"]
        assert sent_json["job_id"] == _OTHER_JOB_ID

    def test_two_arg_job_id_placeholder_rejected(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            out = pipe._handle_skip(
                ["/skip", "<job_id>", "T2"], chat_id=_CHAT_A,
            )
        mp.assert_not_called()
        assert "placeholder" in out.lower()

    def test_two_arg_node_key_placeholder_rejected(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            out = pipe._handle_skip(
                ["/skip", _OTHER_JOB_ID, "<node_key>"], chat_id=_CHAT_A,
            )
        mp.assert_not_called()
        assert "placeholder" in out.lower()


# ---------------------------------------------------------------------------
# Dispatch plumbing
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestDispatchPlumbing:

    def test_handle_skip_accepts_chat_id(self, pipe):
        import inspect
        sig = inspect.signature(pipe._handle_skip)
        assert "chat_id" in sig.parameters

    def test_dispatch_routes_to_handle_skip(self, pipe):
        """The /skip dispatch in _handle_command must route to the
        new _handle_skip method (rather than the pre-§17.316 inline
        block which is now removed)."""
        # Calling _handle_command with the chat_id param triggers
        # the new dispatch path; we just verify the method handles
        # a bare /skip without raising.
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _candidates_response()
            pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="x")
            out = pipe._handle_command("/skip", chat_id=_CHAT_A)
        # The 📌 marker proves we reached the _handle_skip recall path.
        assert "📌" in out


# ---------------------------------------------------------------------------
# Source-shape regression guards
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_handle_skip_extracted(self):
        """The §17.316 refactor extracted /skip's inline block into a
        dedicated method. Anchor the method definition."""
        src = self._src()
        assert "def _handle_skip" in src

    def test_dispatch_routes_through_handle_skip(self):
        src = self._src()
        assert "return self._handle_skip(parts, chat_id=chat_id)" in src

    def test_zero_args_recall_lists_candidates(self):
        """Pin the contract: 0 args + recall → render_skip_candidates
        (informational, not state-altering)."""
        src = self._src()
        # The recall-then-render pattern unique to /skip's 0-args path.
        assert "hint + self._render_skip_candidates(rid)" in src

    def test_job_id_token_one_arg_branch_lists_candidates(self):
        """The 1-arg job_id-shaped branch (full UUID OR 8-hex short_id)
        keeps §17.215 E1's existing behavior (no recall consulted)."""
        src = self._src()
        # The detector — pins the §17.316 short_id support.
        assert "_JOB_ID_TOKEN_RE.match(only)" in src
        # Returns _render_skip_candidates for the typed id.
        assert "return self._render_skip_candidates(only)" in src

    def test_job_id_token_detector_anchored(self):
        """The _JOB_ID_TOKEN_RE regex must accept both shapes. Pin
        the regex pattern fragment so a refactor that drops short_id
        support trips here."""
        src = self._src()
        assert "_JOB_ID_TOKEN_RE = re.compile" in src
        # Pattern accepts 8-hex prefix WITH OR WITHOUT the rest of UUID.
        assert "(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?" in src

    def test_non_uuid_one_arg_auto_substitutes(self):
        """The 1-arg non-UUID branch fires POST with recalled job_id +
        typed node_key. Pin the auto-substitute shape."""
        src = self._src()
        assert '"node_key": only' in src
