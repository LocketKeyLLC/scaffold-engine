"""§17.285 — gt_browser domain validation + Milvus expression safety.

§17.280-🟡-4 audit-tail concern: ``gt_browser.gt_list`` and
``gt_browser.gt_search`` interpolate the ``domain`` argument into a
Milvus expression via f-string after a ``validate_domain`` pass.
Pre-§17.285 the validator was a regex sanitizer that rejected control
chars + ``"`` + ``\\``. Injection-proofness depended on the regex
catching every character the Milvus expression parser would
mis-interpret — a fragile contract.

§17.285 switches the validator to an allowlist membership check against
``VALID_DOMAINS`` (the same frozenset that drives partition-key
fan-out). Anything not in the 7-element set is a hard 400. Plus the
expression composition now goes through ``_domain_expr_clause(d)``
which re-validates at the formatter boundary — belt-and-braces against
a future refactor that bypasses ``validate_domain``.

These tests pin both layers: the validator's allowlist semantics and
the formatter-boundary helper's defensive recheck.
"""
import pytest
from fastapi import HTTPException

from app.config import VALID_DOMAINS
from app.modules import gt_browser


@pytest.mark.smoke
class TestValidateDomainAllowlist:
    """§17.285 — strict-allowlist semantics."""

    def test_none_passes_through(self):
        """None is the "fan out across all partitions" signal — must
        survive validation unchanged."""
        assert gt_browser.validate_domain(None) is None

    @pytest.mark.parametrize("d", sorted(VALID_DOMAINS))
    def test_every_allowlist_value_accepted(self, d):
        """Each of the 7 known partition names round-trips."""
        assert gt_browser.validate_domain(d) == d

    def test_empty_string_rejected(self):
        """`""` is not in VALID_DOMAINS — 400."""
        with pytest.raises(HTTPException) as excinfo:
            gt_browser.validate_domain("")
        assert excinfo.value.status_code == 400
        assert "invalid domain" in str(excinfo.value.detail).lower()

    def test_non_string_type_rejected(self):
        with pytest.raises(HTTPException) as excinfo:
            gt_browser.validate_domain(42)  # type: ignore[arg-type]
        assert excinfo.value.status_code == 400

    def test_unknown_domain_rejected(self):
        """A plausible-looking but unlisted partition name → 400.
        Pre-§17.285 this would have passed (no control chars, no
        quote, no backslash) and reached the Milvus expression as-is.
        """
        with pytest.raises(HTTPException) as excinfo:
            gt_browser.validate_domain("hypothetical")
        assert excinfo.value.status_code == 400

    def test_case_sensitive_rejection(self):
        """`Eng` and `ENG` differ from `eng` — case sensitivity matches
        the frozenset's literal values. A case-insensitive accept would
        let attackers probe with case permutations to bypass intent
        checks downstream.
        """
        with pytest.raises(HTTPException):
            gt_browser.validate_domain("Eng")
        with pytest.raises(HTTPException):
            gt_browser.validate_domain("ENG")

    def test_whitespace_rejected(self):
        """`"eng "` is structurally distinct from `"eng"` — reject; no
        silent strip. Trimming at the validator would hide caller bugs."""
        with pytest.raises(HTTPException):
            gt_browser.validate_domain("eng ")
        with pytest.raises(HTTPException):
            gt_browser.validate_domain(" eng")


@pytest.mark.smoke
class TestValidateDomainRejectsInjection:
    """§17.285 — injection attempts via the validator's input.

    Pre-§17.285 each of these would have either:
      (a) passed cleanly into the f-string (no quote/backslash → regex OK), or
      (b) been rejected by the regex sanitizer.
    Post-§17.285 every one is rejected because none match the allowlist
    — the validator no longer reasons about characters at all.
    """

    @pytest.mark.parametrize(
        "payload",
        [
            'eng" or domain == "rag',
            'eng\\"',
            'eng" and 1=1 --',
            "eng' or ''='",
            'eng" || true',
            "eng\x00rag",          # NUL byte
            "eng\nrag",            # newline
            "eng;DROP TABLE",      # SQL-shaped attempt (Milvus uses different grammar but still)
            'eng" UNION SELECT *',
            "../../../etc/passwd", # path traversal nonsense, irrelevant but pinned
            "x" * 200,             # long enough to exceed the old 128-cap
        ],
    )
    def test_injection_payload_rejected(self, payload):
        with pytest.raises(HTTPException) as excinfo:
            gt_browser.validate_domain(payload)
        assert excinfo.value.status_code == 400


@pytest.mark.smoke
class TestDomainExprClauseFormatterBoundary:
    """§17.285 — ``_domain_expr_clause`` re-validates at the formatter
    boundary so a refactor that bypasses ``validate_domain`` still can't
    emit a Milvus expression with an untrusted literal.
    """

    @pytest.mark.parametrize("d", sorted(VALID_DOMAINS))
    def test_allowlist_value_produces_expected_clause(self, d):
        clause = gt_browser._domain_expr_clause(d)
        assert clause == f'domain == "{d}"'

    def test_unknown_domain_raises_at_formatter(self):
        """If a future caller forgets to run ``validate_domain`` first,
        the formatter still refuses to emit the clause. Pinned because
        the gt_list path used to interpolate via raw f-string — the
        helper is what makes that no longer possible."""
        with pytest.raises(HTTPException) as excinfo:
            gt_browser._domain_expr_clause('eng" or 1=1')
        assert excinfo.value.status_code == 400

    def test_quote_in_input_rejected_at_formatter(self):
        with pytest.raises(HTTPException):
            gt_browser._domain_expr_clause('eng"')

    def test_backslash_in_input_rejected_at_formatter(self):
        with pytest.raises(HTTPException):
            gt_browser._domain_expr_clause("eng\\")

    def test_clause_output_contains_no_user_chars_outside_allowlist(self):
        """Anchor on the clause shape — any drift that lets caller
        characters leak past the allowlist would change this regex."""
        import re
        for d in sorted(VALID_DOMAINS):
            clause = gt_browser._domain_expr_clause(d)
            # Allowlist-domain values are all `[a-z]{2,6}` so this regex
            # is a tight superset; any future drift would need to either
            # change VALID_DOMAINS (caught by test_every_allowlist_value_accepted)
            # or change the clause format itself.
            assert re.fullmatch(r'domain == "[a-z]{2,6}"', clause), (
                f"§17.285 regression: clause shape drifted for domain={d!r}: {clause!r}"
            )


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.285 — guard against drive-by reverts that re-introduce
    raw f-string Milvus expression composition.
    """

    def test_gt_browser_does_not_inline_domain_fstring(self):
        """The two original sites (gt_list, gt_search) both went through
        ``_domain_expr_clause`` post-§17.285. A regression that reverts
        to ``f'domain == "{domain}"'`` would re-open the audit concern.
        """
        with open(gt_browser.__file__, encoding="utf-8") as f:
            src = f.read()

        # The literal pre-§17.285 f-string shape.
        assert 'f\'domain == "{domain}"\'' not in src, (
            "§17.285 regression: a raw f-string `f'domain == \"{domain}\"'` "
            "has reappeared in app/modules/gt_browser.py. The interpolation "
            "must go through `_domain_expr_clause(d)` so the formatter "
            "re-validates against VALID_DOMAINS before emitting the literal."
        )
        # The pre-§17.285 string-concat shape from gt_search.
        assert "'domain == \"' + d + '\"'" not in src, (
            "§17.285 regression: a `'domain == \"' + d + '\"'` concat has "
            "reappeared. Use `_domain_expr_clause(d)` — both gt_list and "
            "gt_search share the helper."
        )

    def test_validate_domain_anchored_to_valid_domains_import(self):
        """The validator must check membership in VALID_DOMAINS. Drift
        to a regex-based sanitizer would lose the allowlist guarantee.
        """
        with open(gt_browser.__file__, encoding="utf-8") as f:
            src = f.read()

        # Definition shape — the membership check is the load-bearing line.
        assert "s not in VALID_DOMAINS" in src, (
            "§17.285 regression: validate_domain no longer checks "
            "membership in VALID_DOMAINS. A regex-based sanitizer ("
            "the pre-§17.285 pattern) makes injection-proofness depend "
            "on the regex catching every char Milvus might mis-parse."
        )
