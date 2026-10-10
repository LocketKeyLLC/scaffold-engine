"""§17.1442 — every trafilatura parse runs behind ONE process-wide lock.

trafilatura and htmldate each build a module-level lxml parser (`trafilatura.utils.HTML_PARSER`,
`htmldate.utils.HTML_PARSER`) and share it across every thread that calls them. lxml forbids using one
parser from two threads at once, and this engine extracts pages from worker threads concurrently (the fetch
semaphore, several research queries in parallel). The engine died twice in lxml's etree: 2026-08-08
(`ThreadPoolExecutor … segfault … in etree.cpython-314`) and 2026-10-10 02:25 UTC (`uvloop_2 … general
protection fault … in etree.cpython-314`, exit 139) — the second mid-way through ADD4's walkthrough, which the
operator saw as "there was an error".

Extraction is CPU-bound, so serialising it costs little next to the network fetch it follows. Call these, never
`trafilatura.extract*` directly (`tests/test_html_extract_is_serialised.py` refuses a direct call).
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()


def extract_text(html: str, **kwargs):
    import trafilatura
    with _LOCK:
        return trafilatura.extract(html, **kwargs)


def extract_metadata(html: str):
    import trafilatura
    with _LOCK:
        return trafilatura.extract_metadata(html)
