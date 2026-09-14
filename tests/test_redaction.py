"""§17.1064 — secret redaction at the capture funnel."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.redaction import redact_secrets

PASTE = """root@pve:~# pct exec 111 -- cat /etc/app.env
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
password = 'hunter2secret'
token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab
OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ
curl -H "X-API-Key: 8f3a9c2d4e5b6a7f8091a2b3c4d5e6f7" localhost:8000/health
-----BEGIN RSA PRIVATE KEY-----
lrwxrwxrwx 1 root root 6 Sep 13 23:02 /opt/control-panel-backend/index.js -> server
ssh aedefruscio@192.168.1.127 nvidia-smi
CONTAINER ID 2e5a54a3623c369a582c5224769604ab66d13e05be2369efae5183215cabbb1c
"""


def test_credentials_are_masked_and_shell_lines_untouched():
    out, kinds = redact_secrets(PASTE)
    for gone in ("AKIAIOSFODNN7EXAMPLE", "hunter2secret", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab",
                 "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ", "8f3a9c2d4e5b6a7f8091a2b3c4d5e6f7"):
        assert gone not in out, gone
    assert "[REDACTED:" in out and out.count("[REDACTED:") >= 5
    # shape and the ordinary lines survive verbatim
    assert "root@pve:~# pct exec 111 -- cat /etc/app.env" in out
    assert "ssh aedefruscio@192.168.1.127 nvidia-smi" in out
    assert "2e5a54a3623c369a582c5224769604ab66d13e05be2369efae5183215cabbb1c" in out  # a container id is not a secret
    assert "AWS Access Key" in kinds and "GitHub Token" in kinds and "OpenAI-style key" in kinds and "API key header" in kinds


def test_empty_and_clean_text_pass_through():
    assert redact_secrets("") == ("", [])
    clean = "root@pve:~# pct list\n101 running jellyfin\n"
    assert redact_secrets(clean) == (clean, [])


@pytest.mark.asyncio
async def test_ingest_turn_redacts_operator_turns_only():
    from app.modules import assist_turns
    db = AsyncMock()
    res = MagicMock(); res.first.return_value = {"id": 1}; res.mappings.return_value.first.return_value = {"id": 1}
    db.execute = AsyncMock(return_value=res)
    db.commit = AsyncMock()
    from app.config import settings
    with patch.object(settings, "assist_unified_memory_enabled", True), \
         patch.object(settings, "assist_umem_capture", True, create=True), \
         patch.object(settings, "assist_redact_secrets_enabled", True, create=True), \
         patch("app.modules.assist_agent.schedule_derive_turn_memory", new=MagicMock(), create=True):
        await assist_turns.ingest_turn(session_id="s1", role="operator", kind="message",
                                       content="token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", node_key="T1", db=db)
        stored = [c.args[1].get("content") for c in db.execute.await_args_list if isinstance(c.args[1], dict) and "content" in c.args[1]]
        assert stored and all("ghp_ABCDEF" not in s and "[REDACTED:GitHub Token]" in s for s in stored)
        db.execute.reset_mock()
        await assist_turns.ingest_turn(session_id="s1", role="assistant", kind="fix",
                                       content="token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", node_key="T1", db=db)
        stored = [c.args[1].get("content") for c in db.execute.await_args_list if isinstance(c.args[1], dict) and "content" in c.args[1]]
        assert stored and all("ghp_ABCDEF" in s for s in stored)  # the engine's own words are not rewritten
