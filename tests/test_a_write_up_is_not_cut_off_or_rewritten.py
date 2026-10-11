"""§17.1483 — a write-up is neither cut off by the model's reasoning nor rewritten by the optimizer.

Live (2026-10-11): T38's fourth draw (deepseek-v4-pro:cloud) used completion_tokens 8192 of max_tokens 8192 — the
reasoning ate the budget and the document stopped mid-sentence at 1,381 characters, and passed. The detector for
exactly this (`_thinking_squeezed`, §17.877) runs only with `think_off_rescue=True`, which the node executor never
passed. And the optimizer had rewritten the task into "Document that /etc/pve/firewall/groups/ does not exist and
security groups still need creation", which outranked the inventory.
"""
import asyncio
import pathlib

from app.utils import llm_retry as lr


class _R:
    def __init__(self, text, done="stop", thinking=""):
        self.text, self.success = text, True
        self.raw = {"done_reason": done, "message": {"thinking": thinking}}


def test_a_squeezed_answer_is_redrawn_with_thinking_off():
    calls = []

    async def chat(messages, temperature, max_tokens, think=None, **kw):
        calls.append(think)
        return _R("# Homelab Documentation\n\n## Firewall … IN ACCEPT -p tcp -dport", "length", "…7000 tokens…") \
            if think is None else _R("# Homelab Documentation\n\n(complete)")
    r = asyncio.run(lr.chat_until_nonempty(chat, [], {}, temperature=0.7, max_tokens=8192, draws=1,
                                           think_off_rescue=True))
    assert r.text.endswith("(complete)") and calls == [None, False]


def test_every_node_generation_call_rescues():
    src = pathlib.Path("app/modules/execution_agent.py").read_text()
    for lab in ('label=f"node-exec {node_key}",', 'label=f"node-exec-stream-fallback {node_key}",',
                'label=f"node-exec-regen {node_key}",'):
        i = src.index(lab)
        assert "think_off_rescue=True" in src[i:i + 200], lab


def test_the_optimizer_leaves_a_step_given_the_inventory_alone():
    src = pathlib.Path("app/modules/execution_agent.py").read_text()
    assert "if not skip_optimize and settings.execution_optimize_enabled and not _inventory_text:" in src
