"""§17.1611 — a step is drafted with what earlier steps of its job READ on the same guest.

Live (job 0fb4ea7f, 2026-10-15): T2 measured the root filesystem on /dev/sda2; T5 "Resize ai-vm filesystem" depends only
on T3, saw nothing of T2, and drafted `growpart /dev/sda 1; resize2fs /dev/sda1`.
"""
from __future__ import annotations

import asyncio
import inspect

from app.modules import execution_agent
from app.modules import guest_evidence as ge

INV = {"cts": {}, "vms": {"110": "running", "106": "running"}, "names": {"110": "ai-vm", "106": "palworld-server"}}
T2 = {"node_key": "T2", "title": "Verify ai-vm resources", "status": "done",
      "output_text": "## Run this\n```bash\nbash /tmp/in_vm_110.sh\n```\nFilesystem Size Used\n/dev/sda2 98G 31G /"}
T3 = {"node_key": "T3", "title": "Expand ai-vm disk", "output_text": "qm resize 110 scsi0 2T"}
PAL = {"node_key": "T9", "title": "Install Palworld on palworld-server", "output_text": "steamcmd ..."}
T5 = {"node_key": "T5", "title": "Resize ai-vm filesystem", "depends_on": ["T3"]}


class _Res:
    def __init__(self, rows): self.rows = rows
    def mappings(self): return self
    def all(self): return self.rows


class _DB:
    async def execute(self, stmt, params=None): return _Res([T2, T3, PAL])


def test_the_live_measurement_reaches_the_step_that_did_not_list_it_upstream():
    b = asyncio.run(ge.earlier_reads(_DB(), "job", T5, INV))
    assert "guest 110" in b and "/dev/sda2" in b
    assert "### T3" not in b, "the declared upstream is already in the prompt"
    assert "steamcmd" not in b, "another guest's record is not this guest's evidence"


def test_a_records_own_fences_cannot_close_the_block():
    b = asyncio.run(ge.earlier_reads(_DB(), "job", T5, INV))
    assert b.count("~~~~") == 2 and "```bash" in b


def test_a_step_about_no_guest_gets_nothing():
    assert asyncio.run(ge.earlier_reads(_DB(), "job", {"node_key": "T9", "title": "Write the docs"}, INV)) == ""


def test_the_draft_path_carries_it():
    assert "_ge.earlier_reads(_db_ge, job_id, run_node, _inv)" in inspect.getsource(execution_agent)


def test_a_long_record_keeps_what_ran_and_what_it_saw():
    long = {"node_key": "T9", "title": "Install Ollama on ai-vm",
            "output_text": "## Run this\ncat > /opt/ollama/docker-compose.yml\ndocker exec ollama ollama list\n" + "x" * 6000 + "\nNAME ID SIZE"}
    b = ge.block([long], "110", set())
    assert "docker exec ollama ollama list" in b and "NAME ID SIZE" in b and "left out" in b
