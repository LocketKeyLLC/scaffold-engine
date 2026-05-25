"""§17.294 — execute_next_node's node-timeout message is operator-actionable.

§17.280-UX-8 audit-tail concern: the timeout branch returned

    {"status": "failed", "node_key": ..., "title": ...,
     "error": timeout_msg,  # had node_key + elapsed + limit
     "reason": "timeout",
     "message": "Node timed out. Review timeout settings or retry."}

The ``error`` field carried context (built from ``timeout_msg``); but
the ``message`` field — the surface chat / CLI typically renders inline
— was a generic stub. Operator saw "Node timed out. Review timeout
settings or retry." with no node_key, no actual timeout value, no
retry command.

§17.294 enriches ``message`` to mirror what ``error`` carries plus a
copy-pasteable recovery line:

    "Node `<key>` timed out after <N>s. Retry with `/exec retry <job_id>
    <node_key>` or raise `node_timeout_seconds`."

Source-shape regression guards pin the format string against drive-by
reverts; an end-to-end behavioral test through `execute_next_node`'s
timeout branch would require mocking the full Phase 1 + 2 + 3 DB
choreography for a one-line message change. The audit asked for
wording — pin the wording.
"""
import pytest

from app.modules import execution_agent
from app.config import settings


@pytest.mark.smoke
class TestTimeoutMessageContent:
    """§17.294 — every operator-facing token is present in source."""

    def _src(self) -> str:
        with open(execution_agent.__file__, encoding="utf-8") as f:
            return f.read()

    def test_message_includes_node_key(self):
        """The literal `<key>` substitution must be in the message
        template — pre-§17.294 the bare 'Node timed out.' carried no
        identifier and the operator had to dig the error field."""
        src = self._src()
        # The exact f-string fragment that builds the new message.
        assert "Node `{node_key}` timed out after " in src, (
            "§17.294 regression: the timeout message no longer names "
            "the node_key. Operator-facing UX requires the identifier "
            "for ad-hoc recovery."
        )

    def test_message_includes_actual_timeout_value(self):
        """The configured timeout value (`node_timeout_seconds`) must be
        interpolated into the message — pre-§17.294 it was implied by
        "Review timeout settings", which left the operator hunting."""
        src = self._src()
        assert "{settings.node_timeout_seconds}s." in src, (
            "§17.294 regression: the actual timeout value is no longer "
            "interpolated. Operator can't tell from the message alone "
            "whether the limit is 60s or 600s."
        )

    def test_message_includes_retry_command(self):
        """The copy-pasteable retry command — using the real chat-form
        `/exec retry <job_id> <node_key>` (NOT the audit's `/exec/retry/
        <job>/<key>` path-style typo). The chat-form is what an operator
        types in OWUI; the REST equivalent is `POST /exec/retry` with
        a JSON body, also acceptable but less actionable from chat."""
        src = self._src()
        assert "/exec retry {job_id} {node_key}" in src, (
            "§17.294 regression: the retry command template has changed "
            "or been removed. Chat operators need the literal command "
            "they can copy-paste from the failure message."
        )

    def test_message_names_node_timeout_seconds_setting(self):
        """The setting name is what an operator runs `make doctor` /
        env-grep against. Naming `execution_node_timeout_seconds` (the
        audit's typo) would send them looking for a non-existent knob.
        Pin the real name from `app/config.py`."""
        src = self._src()
        assert "`node_timeout_seconds`" in src, (
            "§17.294 regression: the setting name in the recovery hint "
            "no longer matches `app/config.py::node_timeout_seconds`. "
            "Operator runs grep / `make doctor` against the wrong knob."
        )

    def test_real_setting_exists(self):
        """Defensive — verify `node_timeout_seconds` is actually the
        setting name. If a future config refactor renames it, this test
        forces a coordinated fix to the timeout message above."""
        assert hasattr(settings, "node_timeout_seconds"), (
            "§17.294: `node_timeout_seconds` no longer exists on "
            "settings — the timeout message in execution_agent.py "
            "references a stale knob name. Coordinate the rename."
        )

    def test_pre_fix_generic_message_removed(self):
        """The pre-§17.294 generic message string must NOT reappear in
        the timeout branch. A drive-by 'simplify' that re-collapses the
        f-string back to the literal 'Node timed out. Review timeout
        settings or retry.' would slip past behavioural tests that only
        check the dict shape."""
        src = self._src()
        # The exact pre-§17.294 string. Other branches may use the word
        # "timeout" in unrelated messages; this is the canonical pre-fix
        # literal we guard against.
        assert (
            "Node timed out. Review timeout settings or retry."
            not in src
        ), (
            "§17.294 regression: the generic pre-fix `message` string "
            "has reappeared in execution_agent.py. The audit-fix shape "
            "interpolates node_key, the configured timeout, the retry "
            "command, and the setting name."
        )

    def test_audit_comment_anchored(self):
        """The §17.294 inline comment naming the audit reasoning must
        stay — future readers should see why this message is verbose
        rather than the previous one-liner."""
        src = self._src()
        assert "§17.294" in src, (
            "§17.294 regression: the audit citation has been removed "
            "from execution_agent.py. Keep the comment so the next "
            "reader sees why the message string is more verbose than "
            "the pre-fix stub."
        )
