r"""§17.1594 — the engine-deployed proxies are probed from the host with their latency; a shorter timeout is refused.

Live (c2792d4b T7/T11): Byparr's /health answers 200 in 8.3 s (measured from the host; `/` 301 and `/docs` 200 in
3 ms); the panel's byparr route gave it less and answered `{"error":"Byparr service is not available","detail":"Request
timeout"}` on every call — Byparr shown down while up and slow — and the verify step that demands 200s failed on it.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace as NS

from app.modules import develop as dv

BYPARR = NS(name="byparr", guest="108", unit="", ports=("8191",), address="192.168.1.27", workdir="", vm=False)
FLARE = NS(name="flaresolverr", guest="107", unit="", ports=("8191",), address="192.168.1.26", workdir="", vm=False)
PIHOLE = NS(name="pihole", guest="130", unit="pihole-FTL.service", ports=("80",), address="192.168.1.30", workdir="", vm=False)


def test_the_proxies_are_probed_from_the_host_with_their_latency():
    reads = dv.status_reads([BYPARR, FLARE, PIHOLE])
    assert ("byparr", "/health", 'curl -s -m 20 -o /dev/null -w "%{http_code} %{time_total}" "http://192.168.1.27:8191/health"') in reads
    assert ("flaresolverr", "/", 'curl -s -m 20 -o /dev/null -w "%{http_code} %{time_total}" "http://192.168.1.26:8191/"') in reads
    assert any(r[0] == "pihole" and r[2].startswith("pct exec 130 -- curl") and "%{time_total}" in r[2] for r in reads)   # in-guest, timed too
    assert dv.parse_status_answer("200 8.302806") == ("200", 8.3) and dv.parse_status_answer("301") == ("301", None)
    assert dv.parse_status_answer("ipcc_send_rec[1] failed") == ("", None) and dv.parse_status_answer("") == ("", None)


def test_a_timeout_shorter_than_the_measured_latency_is_refused_and_a_generous_one_passes():
    status = [{"service": "byparr", "path": "/health", "code": "200", "address": "192.168.1.27", "seconds": 8.3},
              {"service": "flaresolverr", "path": "/", "code": "200", "address": "192.168.1.26", "seconds": 0.0}]
    short = {"/opt/control-panel-backend/byparr_route.js": "const r = await httpRequest('192.168.1.27', 8191, '/health', { timeout: 5000 });\n"}
    out = dv.timeouts_shorter_than_measured(short, status)
    assert len(out) == 1 and "5000 ms" in out[0]["command"] and "8.3 s" in out[0]["why"] and "12450" in out[0]["why"]
    ok = {"/opt/control-panel-backend/byparr_route.js": "req.setTimeout(15000); // byparr is slow\n"}
    assert dv.timeouts_shorter_than_measured(ok, status) == []
    other = {"/opt/control-panel-backend/palworld_route.js": "fetch(url, { signal: AbortSignal.timeout(3000) })\n"}     # names no slow service
    assert dv.timeouts_shorter_than_measured(other, status) == []
    assert dv.timeouts_shorter_than_measured(short, [{"service": "byparr", "path": "/health", "code": "200", "seconds": None}]) == []
    secs = {"/opt/control-panel-backend/byparr.sh": "curl --max-time 5 http://192.168.1.27:8191/health\n"}
    assert dv.timeouts_shorter_than_measured(secs, status)
    from app.modules import execution_agent as ea
    assert "+ develop.timeouts_shorter_than_measured(files, status)" in inspect.getsource(ea._pause_for_decision)


def test_a_redirect_is_a_live_answer_not_a_dead_path():
    """§17.1595 — Byparr's `/` answers 301 → /docs in 3 ms: the fast, live path a health check may use."""
    status = [{"service": "byparr", "path": "/", "code": "301", "address": "192.168.1.27", "seconds": 0.0},
              {"service": "byparr", "path": "/health", "code": "200", "address": "192.168.1.27", "seconds": 8.3},
              {"service": "pihole", "path": "/admin/api.php?summary", "code": "400", "address": "192.168.1.30", "seconds": 0.0}]
    files = {"/opt/control-panel-backend/byparr_route.js": "const r = await httpRequest('192.168.1.27', 8191, '/', {});\n",
             "/opt/control-panel-backend/network_route.js": "fetch('http://192.168.1.30/admin/api.php?summary')\n"}
    out = dv.calls_a_dead_path(files, status)
    assert len(out) == 1 and out[0]["command"].endswith(": /admin/api.php") and "HTTP 400" in out[0]["why"]      # the 400 is dead; the 301 is not
