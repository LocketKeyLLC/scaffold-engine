"""§17.1064 — secret redaction at the capture funnel.

Every operator paste is stored verbatim in ``assist_turns``, distilled into
the facts ledger and re-injected into later prompts — including cloud
models. Nothing masked anything, so a pasted key became a durable "fact".
This runs ONCE, at ``ingest_turn`` (the §17.751 chokepoint), on operator
turns only, and replaces credential VALUES with ``[REDACTED:<kind>]`` while
keeping the paste's shape so verification and the recap still read it.

Detection is detect-secrets' keyword and known-format plugins, line by
line — the entropy plugins are deliberately OFF: on terminal output they
flag container ids, image digests and every base64-looking token, which is
the noise that would make an operator switch the whole thing off. Two
house regexes cover shapes those plugins miss on this engine's surfaces
(OpenAI-style ``sk-…`` keys, ``X-API-Key`` / ``Authorization`` header values).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("scaffold")

_PLUGINS = [{"name": n} for n in (
    "AWSKeyDetector", "GitHubTokenDetector", "GitLabTokenDetector", "SlackDetector",
    "PrivateKeyDetector", "JwtTokenDetector", "BasicAuthDetector", "KeywordDetector",
    "StripeDetector", "OpenAIDetector", "TwilioKeyDetector", "SendGridDetector",
    "DiscordBotTokenDetector", "TelegramBotTokenDetector", "AzureStorageKeyDetector",
    "MailchimpDetector", "NpmDetector", "PypiTokenDetector", "SoftlayerDetector",
    "SquareOAuthDetector", "IbmCloudIamDetector", "IbmCosHmacDetector", "ArtifactoryDetector",
    "CloudantDetector",
)]
_HOUSE = [
    ("OpenAI-style key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("API key header", re.compile(r"(?i)(x-api-key\s*[:=]\s*)([A-Za-z0-9._~+/=-]{16,})")),
    ("Bearer token", re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([A-Za-z0-9._~+/=-]{16,})")),
]
_MAX_LINE = 4000


def _scan_line(line: str) -> list[tuple[str, str]]:
    """``[(kind, secret_value)]`` for one line, via detect-secrets. Fail-soft
    to [] if the library is unavailable (the house regexes still run)."""
    try:
        from detect_secrets.core import scan
        from detect_secrets.settings import transient_settings
    except Exception:  # noqa: BLE001 — optional at import time; pinned in requirements
        return []
    out: list[tuple[str, str]] = []
    with transient_settings({"plugins_used": _PLUGINS}):
        for hit in scan.scan_line(line):
            if hit.secret_value:
                out.append((hit.type, hit.secret_value))
    return out


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Return ``(redacted_text, kinds)``. ``kinds`` lists the detector names
    that fired (never the values) for the log line and the turn's metadata."""
    if not text:
        return text, []
    kinds: list[str] = []
    out_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        tail = line[len(body):]
        if len(body) > _MAX_LINE:
            out_lines.append(line)
            continue
        new = body
        for kind, value in _scan_line(body):
            if value and value in new:
                new = new.replace(value, f"[REDACTED:{kind}]")
                kinds.append(kind)
        for kind, rx in _HOUSE:
            def _sub(m: re.Match) -> str:
                kinds.append(kind)
                return (m.group(1) if m.lastindex else "") + f"[REDACTED:{kind}]"
            new = rx.sub(_sub, new)
        out_lines.append(new + tail)
    return "".join(out_lines), kinds
