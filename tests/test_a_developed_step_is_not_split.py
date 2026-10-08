"""§17.1423 — a developed step is never split.

Live, 2026-10-08: ADD126 (the panel's single page) developed to score 0 -- and then `too_large` split it
("the step asks for 2 separate deliverables in one block (a frontend, a page)") into ADD140–144, and
ADD140 again into ADD145–148: runbook steps writing `index.html` by heredoc into a directory the backend
does not serve. The splitter's premise is "the engine carries one block of commands per step"; a developed
step's one delivery carries any number of whole files, so the premise does not hold there.
"""
from __future__ import annotations

import inspect
import re

from app.modules import execution_agent


def test_the_split_is_guarded_by_not_developed():
    src = inspect.getsource(execution_agent._pause_for_decision)
    i = src.index("_sd.too_large(frame, run_node)")
    guard = src.rfind("if _depth < 6", 0, i)
    assert guard != -1 and re.match(r"if _depth < 6 and not _developed:", src[guard:guard + 40])


def test_developed_is_known_before_the_split():
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert src.index("_developed = _dev_host is not None") < src.index("_sd.too_large(frame, run_node)")
