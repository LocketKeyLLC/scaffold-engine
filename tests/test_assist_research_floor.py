"""§17.912 — research must not be skippable by the model's own confidence.

`_detect_unknowns` is an LLM judgment about whether anything needs looking up,
and `_research_prepass` returned [] whenever it declined. Live: ADD5 "Install
Ubuntu Server 22.04 on VM 106" produced **zero** queries, so the walkthrough was
written from model memory alone — while the identical retrieval stack, asked
"ubuntu 22.04 installer stalls at Downloading and installing security updates
fix", returned two useful sources.

A confident-sounding step description is not evidence that nothing needs looking
up; for an install/configure step on a real machine it is usually the opposite.
This is §17.882's medicine ("one DETERMINISTIC query, always") applied to the
GUIDE path, which never got the equivalent floor.
"""
from __future__ import annotations

import pytest

from app.modules.assist_research_lib import _floor_query as floor

pytestmark = pytest.mark.asyncio

LIVE_ADD5 = ("Install Ubuntu Server 22.04 on VM 106 (palworld-server) using the "
             "'ubuntu-22.04.3-live-server-amd64.iso'. The step is complete when "
             "the VM boots from its local disk.")


def test_live_step_yields_a_usable_query():
    assert floor(LIVE_ADD5, "Shell") == "Install Ubuntu Server 22.04 on VM 106"


def test_instance_detail_is_stripped():
    """An external engine has nothing to say about '(palworld-server)' or a
    specific ISO filename; the first cut kept them and only the local corpus
    matched."""
    q = floor(LIVE_ADD5, "Shell")
    assert "palworld-server" not in q
    assert ".iso" not in q
    assert not q.endswith(("the", "using the", "."))


@pytest.mark.parametrize("text,tool,expected", [
    ("Install NVIDIA drivers on the AI VM. Verify with nvidia-smi.", "Shell",
     "Install NVIDIA drivers on the AI VM"),
    ("Configure the Prowlarr indexers for the media stack.", "Shell",
     "Configure the Prowlarr indexers for the media stack"),
])
def test_action_steps_get_a_floor(text, tool, expected):
    assert floor(text, tool) == expected


@pytest.mark.parametrize("text,tool", [
    ("Draft the project runbook document.", "LLM"),
    ("Summarise the findings into a report.", "LLM"),
    ("", "Shell"),
    ("   \n  ", "Shell"),
])
def test_document_steps_and_empties_get_no_floor(text, tool):
    """A pure-LLM writing step legitimately needs no lookup — a floor there
    would add a retrieval round trip to every document node."""
    assert floor(text, tool) == ""


def test_context_blob_is_never_the_query():
    """`ctx.base_prompt` appends the whole project brief after the step text."""
    q = floor("Install Ubuntu on VM 106.\n\nContext: Build out a comprehensive "
              "home network and home lab on a Supermicro dual-Xeon server.", "Shell")
    assert q == "Install Ubuntu on VM 106"
    assert "Supermicro" not in q


def test_floor_is_wired_and_only_fires_when_detection_returns_nothing():
    import inspect
    from app.modules import assist_research_lib
    src = inspect.getsource(assist_research_lib._research_prepass)
    assert "if not queries:" in src
    assert "_floor_query(task_text, tool)" in src
    # the floor must not pre-empt a real detection result
    assert src.index("_detect_unknowns") < src.index("_floor_query")
