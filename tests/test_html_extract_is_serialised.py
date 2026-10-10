"""§17.1442 — trafilatura is only ever called through `app.utils.html_extract` (one process-wide lock).

trafilatura/htmldate share a module-level lxml parser across threads; concurrent use segfaulted the engine in
lxml's etree on 2026-08-08 and 2026-10-10 (exit 139, mid-way through ADD4's walkthrough).
"""
import pathlib
import re
import threading
import time
from unittest.mock import patch

from app.utils import html_extract

DIRECT = re.compile(r"\btrafilatura\.(?:extract|extract_metadata|bare_extraction|baseline|html2txt)\b")


def test_no_direct_trafilatura_call_outside_the_helper():
    offenders = []
    for p in pathlib.Path("app").rglob("*.py"):
        if p.as_posix() == "app/utils/html_extract.py":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if DIRECT.search(code) and '"""' not in line:
                offenders.append(f"{p}:{i}: {line.strip()}")
    assert not offenders, "call app.utils.html_extract instead:\n" + "\n".join(offenders)


def test_two_threads_never_parse_at_once():
    inside, peak = [0], [0]
    gate = threading.Lock()

    def slow(*a, **k):
        with gate:
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
        time.sleep(0.02)
        with gate:
            inside[0] -= 1
        return "x"

    with patch("trafilatura.extract", side_effect=slow), patch("trafilatura.extract_metadata", side_effect=slow):
        ts = [threading.Thread(target=html_extract.extract_text, args=("<p>x</p>",)) for _ in range(6)] + \
             [threading.Thread(target=html_extract.extract_metadata, args=("<p>x</p>",)) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert peak[0] == 1


def test_the_extract_paths_use_the_helper():
    ra = pathlib.Path("app/modules/research_agent.py").read_text()
    assert "extract_text(html, output_format=\"txt\", with_metadata=False)" in ra
    assert "extract_text, html," in ra
    assert "extract_text, html," in pathlib.Path("app/utils/hf_ingest.py").read_text()
    assert "to_thread(extract_metadata, html)" in pathlib.Path("app/modules/research_extractors.py").read_text()
