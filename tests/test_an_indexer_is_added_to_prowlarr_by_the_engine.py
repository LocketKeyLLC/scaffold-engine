"""§17.1672 — "add <name> to Prowlarr as a Generic Torznab indexer at <url>" is the engine's block (live, job 3cc24343
T15: the drafted script was `systemctl stop prowlarr.service` and nothing else)."""
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

from app.modules import prowlarr_torznab as pt
from app.modules import supervised_runs as sr
from app.modules.service_truth import ServiceTruth

T15 = {"node_key": "T15", "title": "Add bitmagnet to Prowlarr", "tool": "Shell",
       "prompt_template": ("Starts from T14. Add indexer in Prowlarr: Generic Torznab, URL http://192.168.1.29:3333/torznab. "
                           "Does NOT modify anything else on CT 102.")}
URL = "http://192.168.1.29:3333/torznab"


def test_the_live_step_is_claimed():
    assert pt.target(T15) == ("bitmagnet", URL)
    assert pt.target({**T15, "title": "Verify Prowlarr indexer test"}) == ("", "")
    assert pt.target({**T15, "prompt_template": "Add it."}) == ("", "")
    cmds, verify, note = pt.block(T15, lambda: "102")
    assert cmds[0].startswith("pct exec 102 -- sh -c ") and "<PROWLARR_API_KEY>" in cmds[0] and URL in cmds[0]
    assert "systemctl" not in cmds[0] and pt.block(T15, lambda: "") is None


class _Prowlarr(BaseHTTPRequestHandler):
    indexers: list = []
    posted: list = []
    refuse = False

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        assert self.headers["X-Api-Key"] == "k"
        if self.path == "/api/v1/indexer":
            return self._send(200, self.indexers)
        if self.path == "/api/v1/indexer/schema":
            return self._send(200, [{"id": 0, "implementation": "Cardigann", "name": "1337x", "fields": []},
                                    {"id": 0, "implementation": "Torznab", "definitionName": "Generic Torznab",
                                     "name": "Generic Torznab", "fields": [{"name": "baseUrl", "value": ""},
                                                                           {"name": "apiPath", "value": "/api"}]}])
        if self.path == "/api/v1/appprofile":
            return self._send(200, [{"id": 7, "name": "Standard"}])
        self._send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.refuse:
            return self._send(400, [{"errorMessage": "Unable to connect to indexer"}])
        assert "forceSave" not in self.path
        self.posted.append(body)
        self.indexers.append({**body, "id": 12})
        self._send(201, {**body, "id": 12})

    def log_message(self, *a):
        pass


def _run(port):
    prog = pt.ADD_PY.replace("127.0.0.1:9696", f"127.0.0.1:{port}")
    return subprocess.run([sys.executable, "-c", prog, "k", "bitmagnet", URL], capture_output=True, text=True, timeout=60)


def test_the_program_adds_once_from_the_catalog_template_and_tests_before_saving():
    _Prowlarr.indexers, _Prowlarr.posted, _Prowlarr.refuse = [], [], False
    srv = HTTPServer(("127.0.0.1", 0), _Prowlarr)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = _run(srv.server_port)
        assert r.returncode == 0 and "ADDED: bitmagnet as indexer 12" in r.stdout, r
        body = _Prowlarr.posted[0]
        assert body["name"] == "bitmagnet" and body["enable"] is True and body["appProfileId"] == 7 and "id" not in body
        assert {f["name"]: f["value"] for f in body["fields"]} == {"baseUrl": URL, "apiPath": "/api"}
        again = _run(srv.server_port)
        assert again.returncode == 0 and "ALREADY" in again.stdout and len(_Prowlarr.posted) == 1
        _Prowlarr.indexers, _Prowlarr.refuse = [], True
        no = _run(srv.server_port)
        assert no.returncode == 1 and "Prowlarr refused the indexer" in no.stdout and "Unable to connect" in no.stdout
    finally:
        srv.shutdown()


def test_the_frame_takes_it_and_reads_the_key_in_the_guest():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": [], "can_write_files": True}
    prowlarr = ServiceTruth(guest="102", unit="prowlarr.service", name="prowlarr", state="active", user="prowlarr",
                            argv="/opt/Prowlarr/Prowlarr -nobrowser -data=/var/lib/prowlarr", data_dir="/var/lib/prowlarr",
                            ports=("9696",))
    draft = ("## Write these files\n\n### /tmp/in_ct_102.sh\n```bash\nGID=102\nsystemctl stop prowlarr.service\n```\n\n"
             "## Run this\n\n```bash\nbash /tmp/in_ct_102.sh start\nbash /tmp/in_ct_102.sh last\n```\n")
    frame = sr.frame_run(T15, draft, spec, policy, services=[prowlarr],
                         inventory={"vms": {}, "cts": {"102": "running", "109": "running"},
                                    "names": {"102": "prowlarr", "109": "bitmagnet"}})
    assert not frame["refused"] and frame["inputs"] == [], (frame["refused"], frame["inputs"])
    joined = " ".join(frame["commands"])
    assert "systemctl stop" not in joined and "<PROWLARR_API_KEY>" not in joined and "config.xml" in joined
    assert not frame.get("files")


T16 = {"node_key": "T16", "title": "Verify Prowlarr indexer test", "tool": "Shell",
       "prompt_template": "Run Prowlarr's test on the new indexer. Confirm success."}
T16_FILE = {"path": "/tmp/in_ct_102.sh", "content": "GID=102\ncurl -s --fail http://192.168.1.21:9696/api/v1/indexer/50 | grep enable\n"}


def test_the_indexer_test_is_claimed_from_the_drafted_id():
    cmds, verify, note, extra = pt.test_block(T16, lambda: "102", ["bash /tmp/in_ct_102.sh start"], [T16_FILE])
    assert cmds[0].startswith("pct exec 102 -- sh -c ") and cmds[0].rstrip("'").endswith('"<PROWLARR_API_KEY>" "50"')
    assert extra["always_run"] and pt.block(T16, lambda: "102") is None
    assert pt.test_block(T16, lambda: "102", [], []) is None                       # which indexer? not guessed
    assert pt.test_block(T15, lambda: "102", [], [T16_FILE]) is None


class _ProwlarrT(_Prowlarr):
    tested: list = []

    def do_GET(self):
        if self.path == "/api/v1/indexer/50":
            return self._send(200, {"id": 50, "name": "bitmagnet", "enable": True, "implementation": "Torznab"})
        if self.path.startswith("/api/v1/search?"):
            assert "indexerIds=50" in self.path
            return self._send(200, [{"title": "a"}, {"title": "b"}])
        return super().do_GET()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/api/v1/indexer/test" and body["implementation"] == "Torznab"   # the whole object
        if self.refuse:
            return self._send(400, [{"errorMessage": "Unable to connect"}])
        self._send(200, {})


def test_the_test_program_both_ways():
    srv = HTTPServer(("127.0.0.1", 0), _ProwlarrT)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    prog = pt.TEST_PY.replace("127.0.0.1:9696", f"127.0.0.1:{srv.server_port}")
    try:
        _ProwlarrT.refuse = False
        r = subprocess.run([sys.executable, "-c", prog, "k", "50"], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and "TEST PASSED: Prowlarr tested bitmagnet" in r.stdout and "2 results" in r.stdout, r
        _ProwlarrT.refuse = True
        r = subprocess.run([sys.executable, "-c", prog, "k", "50"], capture_output=True, text=True, timeout=60)
        assert r.returncode == 1 and "FAILED: Prowlarr test of bitmagnet" in r.stdout
    finally:
        _ProwlarrT.refuse = False
        srv.shutdown()


def test_the_frame_takes_the_indexer_test():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": [], "can_write_files": True}
    prowlarr = ServiceTruth(guest="102", unit="prowlarr.service", name="prowlarr", state="active", user="prowlarr",
                            argv="/opt/Prowlarr/Prowlarr -nobrowser -data=/var/lib/prowlarr", data_dir="/var/lib/prowlarr",
                            ports=("9696",))
    draft = ("## Write these files\n\n### /tmp/in_ct_102.sh\n```bash\n" + T16_FILE["content"] + "```\n\n"
             "## Run this\n\n```bash\nbash /tmp/in_ct_102.sh start\nbash /tmp/in_ct_102.sh last\n```\n")
    frame = sr.frame_run(T16, draft, spec, policy, services=[prowlarr],
                         inventory={"vms": {}, "cts": {"102": "running"}, "names": {"102": "prowlarr"}})
    assert not frame["refused"] and frame["inputs"] == [], (frame["refused"], frame["inputs"])
    assert "/api/v1/indexer/test" in frame["commands"][0] and frame["always_run"]


def test_the_check_write_gate_reads_a_quoted_api_path():
    """§17.1672b — a test call by path is a test; a create by path is still a write"""
    from app.modules.check_writes import a_check_that_writes
    node = {"title": "Verify Prowlarr indexer test"}
    assert a_check_that_writes(["python3 -c \"_,e=api('/api/v1/indexer/test','POST',ix)\""], node) == []
    assert a_check_that_writes(["python3 -c \"_,e=api('/api/v1/indexer','POST',body)\""], node)
