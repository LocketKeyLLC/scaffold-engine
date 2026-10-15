"""§17.1599 — a page section must read the fields its route answers; §17.1600 — the develop flow reads the page whole.

Live, 2026-10-15 (c2792d4b, after §17.1598 made every route answer): the control panel's Network View read
Pi-hole v5's flat `data.dns_queries_today`, … while `/api/network-view` answers `{status, service, host, port,
response}` with v6's numbers nested -- "No stats available" with the route up. Measuring that showed the engine
had never read the page whole: the runner returns at most 20,000 characters of output, the page is 48,897 bytes,
and the 40-file budget ran out on images before the page was read at all.

Shapes are the live files' (trimmed); addresses made up.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import re
import textwrap

from app.modules import develop as dv
from app.modules import execution_agent
from app.modules import rehearsal

ROUTE_VIEW = textwrap.dedent("""\
    module.exports = function registerNetworkViewRoute(app) {
      app.get('/api/network-view', async (req, res) => {
        try {
          const summary = await httpRequest(addr, 80, '/api/stats/summary', 'GET');
          res.json({ status: 'ok', service: 'pihole', host: addr, port: 80, response: summary });
        } catch (err) {
          res.status(503).json({ error: 'Pi-hole service is not available', detail: err.message });
        }
      });
    };
    """)
ROUTE_LIBRARY = textwrap.dedent("""\
    module.exports = (app) => {
      app.get('/api/media-library', async (req, res) => {
        const movies = await a(); const series = await b();
        res.json({ movies, series });
      });
      app.get('/api/passthrough', async (req, res) => { res.json(await upstream()); });
    };
    """)
PAGE = textwrap.dedent("""\
    <script>
    function escapeHtml(str) { return String(str).replace(/'/g, '&#039;').replace(/"/g, '&quot;'); }
    async function loadNetworkView() {
      const container = document.getElementById('network-stats');
      try {
        const res = await fetch(`${API_BASE}/api/network-view`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed');
        const stats = [
          { label: 'DNS Queries Today', value: data.dns_queries_today },
          { label: 'Ads Blocked Today', value: data.ads_blocked_today },
          { label: 'Domains Being Blocked', value: data.domains_being_blocked },
        ];
      } catch (err) {}
    }
    async function loadMediaLibrary() {
      const res = await fetch(`${API_BASE}/api/media-library`);
      const data = await res.json();
      render(data.movies, data.series);
    }
    async function loadPassthrough() {
      const res = await fetch('/api/passthrough');
      const data = await res.json();
      render(data.anything);
    }
    </script>
    """)
WS = {"/opt/panel-backend/network_view_route.js": ROUTE_VIEW,
      "/opt/panel-backend/media_library_route.js": ROUTE_LIBRARY,
      "/opt/panel-ui/index.html": PAGE}


def test_the_route_shapes_are_read_off_the_handlers_success_answers():
    sh = dv.route_shapes(WS)
    assert sh["/api/network-view"] == {"status", "service", "host", "port", "response"}, "the 503 answer is not a shape"
    assert sh["/api/media-library"] == {"movies", "series"}, "shorthand keys"
    assert "/api/passthrough" not in sh, "an upstream body passed through: nothing to judge"


def test_the_page_reads_survive_a_quote_in_a_regex_literal():
    pr = dv.page_reads(WS)
    assert pr["/api/network-view"][1] == ["dns_queries_today", "ads_blocked_today", "domains_being_blocked"]
    assert pr["/api/media-library"][1] == ["movies", "series"]


def test_bites_on_the_live_mismatch_and_passes_the_sections_that_agree():
    out = dv.page_reads_what_no_route_returns({}, WS)
    assert [o["command"].split(": ", 1)[1].split(" ->")[0] for o in out] == ["/api/network-view"]
    assert "`dns_queries_today`" in out[0]["why"] and "`response`" in out[0]["why"]


def test_a_version_that_reads_the_nested_fields_passes():
    fixed = PAGE.replace("data.dns_queries_today", "data.response.queries.total") \
                .replace("data.ads_blocked_today", "data.response.queries.blocked") \
                .replace("data.domains_being_blocked", "data.response.gravity.domains_being_blocked")
    assert dv.page_reads_what_no_route_returns({"/opt/panel-ui/index.html": fixed}, WS) == []


def test_a_route_changed_to_answer_the_pages_fields_passes():
    flat = ROUTE_VIEW.replace("res.json({ status: 'ok', service: 'pihole', host: addr, port: 80, response: summary });",
                              "res.json({ dns_queries_today: 1, ads_blocked_today: 2, domains_being_blocked: 3 });")
    assert dv.page_reads_what_no_route_returns({"/opt/panel-backend/network_view_route.js": flat}, WS) == []


def test_a_spread_answer_judges_nothing():
    spread = ROUTE_VIEW.replace("res.json({ status: 'ok',", "res.json({ ...summary, status: 'ok',")
    assert dv.page_reads_what_no_route_returns({"/opt/panel-backend/network_view_route.js": spread}, WS) == []


def test_the_develop_loop_runs_it():
    assert "develop.page_reads_what_no_route_returns(files, workspace)" in inspect.getsource(execution_agent)


# ── §17.1600: a file over the runner's output cap is read whole ──

def _fake_runner(files: dict[str, bytes], calls: list):
    async def probe(spec, cmd):
        calls.append(cmd)
        m = re.search(r" cat '?([^' ]+)'?$", cmd)
        if m and "base64" not in cmd:
            return True, files[m.group(1)].decode()[:rehearsal.RUNNER_OUTPUT_CAP]
        m = re.search(r"stat -c %s '?([^' ]+)'?$", cmd)
        if m:
            return True, f"{len(files[m.group(1)])}\n"
        m = re.search(r"base64 -w0 '?([^' ]+?)'? \| cut -c (\d+)-(\d+)", cmd)
        if m:
            b = base64.b64encode(files[m.group(1)]).decode()
            return True, b[int(m.group(2)) - 1:int(m.group(3))] + "\n"
        return False, ""
    return probe


def test_a_file_the_runner_cuts_is_reread_in_chunks_and_comes_back_whole(monkeypatch):
    from app.modules import service_truth as st
    page = ("<p>é</p>\n" * 6000).encode()          # 60 KB, multi-byte characters across chunk boundaries
    calls: list = []
    monkeypatch.setattr(st, "_probe", _fake_runner({"/opt/ui/index.html": page}, calls))
    got = asyncio.run(rehearsal._cat(None, "111", False, "/opt/ui/index.html"))
    assert got == page.decode()
    assert sum("cut -c" in c for c in calls) == -(-len(base64.b64encode(page)) // rehearsal._B64_CHUNK)


def test_a_small_file_is_one_read(monkeypatch):
    from app.modules import service_truth as st
    calls: list = []
    monkeypatch.setattr(st, "_probe", _fake_runner({"/opt/ui/a.js": b"x = 1\n"}, calls))
    assert asyncio.run(rehearsal._cat(None, "111", False, "/opt/ui/a.js")) == "x = 1\n"
    assert len(calls) == 1


def test_a_chunk_that_fails_returns_nothing_not_a_cut_file(monkeypatch):
    from app.modules import service_truth as st
    page = b"a" * 50000
    inner = _fake_runner({"/opt/ui/index.html": page}, [])

    async def flaky(spec, cmd):
        if "cut -c 16001-" in cmd:
            return True, "(timed out after 20 s)"
        return await inner(spec, cmd)
    monkeypatch.setattr(st, "_probe", flaky)
    assert asyncio.run(rehearsal._cat(None, "111", False, "/opt/ui/index.html")) is None


def test_the_chunk_reads_pass_the_read_only_classifier():
    from app.modules.assist_state_check import read_only_command
    assert read_only_command("pct exec 111 -- stat -c %s /opt/ui/index.html")
    assert read_only_command('pct exec 111 -- sh -c "base64 -w0 /opt/ui/index.html | cut -c 1-16000"')


def test_code_is_read_before_assets_and_shallow_before_deep(monkeypatch):
    from app.modules import service_truth as st
    ui = ["/opt/ui/src/assets/hero.png", "/opt/ui/public/icons.svg", "/opt/ui/src/App.jsx", "/opt/ui/index.html"]
    read: list = []

    async def probe(spec, cmd):
        if cmd.startswith("pct exec 111 -- find /opt/ui "):
            return True, "\n".join(ui)
        if cmd.startswith("pct exec 111 -- find "):
            return True, ""
        read.append(cmd.rsplit(" ", 1)[-1])
        return True, "x"
    monkeypatch.setattr(st, "_probe", probe)
    monkeypatch.setattr(rehearsal, "MAX_SEED_FILES", 2)
    host = dv.Host(guest="111", vm=False, workdir="/opt/be", unit="be.service", extra_dirs=("/opt/ui",))
    ws = asyncio.run(dv.read_workspace(None, host))
    assert list(ws)[:2] == ["/opt/ui/index.html", "/opt/ui/src/App.jsx"]
