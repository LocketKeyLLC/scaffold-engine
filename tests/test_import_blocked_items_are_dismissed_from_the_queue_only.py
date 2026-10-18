"""§17.1650 — "dismiss the import-blocked queue items" is the engine's own block (job 5db1b088 IB1, 2026-10-18).

82 of Sonarr's 107 queue items were finished season packs held importBlocked. §17.1508's per-series search claimed the
step (title "queue", text "missing episodes") and would have searched instead of dismissing."""
from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

from app.modules import supervised_runs as sr
from app.modules.service_truth import ServiceTruth

IB1 = {"node_key": "IB1", "tool": "Shell", "title": "CT 104: dismiss Sonarr's import-blocked queue items",
       "description": ("82 are finished downloads with trackedDownloadState importBlocked ... dismiss them from Sonarr's "
                       "queue ONLY -- DELETE /api/v3/queue/bulk?removeFromClient=false&blocklist=false -- then queue "
                       "Sonarr's MissingEpisodeSearch so the missing episodes are searched again. Done when Sonarr's "
                       "queue holds 0 items whose trackedDownloadState is importBlocked.")}
SERVICES = [ServiceTruth(guest="104", unit="sonarr.service", name="sonarr", ports=("8989",))]
INV = {"vms": {}, "cts": {"104": "running"}, "names": {"104": "sonarr"}}


def test_the_dismissal_claims_it_and_the_search_does_not():
    cmds, verify, notes = sr.rewrite_dismiss_import_blocked(IB1, ["x"], [], SERVICES, INV)
    assert notes and cmds[0].startswith("pct exec 104 -- sh -c ") and "removeFromClient=false&blocklist=false" in cmds[0]
    assert "DELETE" not in verify[0] and "grep -o importBlocked" in verify[0]
    assert sr.rewrite_queue_missing(IB1, ["x"], [], SERVICES, INV)[2] == []
    # a search step is still the search's, and a stalled clear is still the clear's
    assert sr.rewrite_dismiss_import_blocked({"title": "Queue missing episodes for Scrubs"}, ["x"], [], SERVICES, INV)[2] == []
    assert sr.rewrite_dismiss_import_blocked({"title": "Remove stalled downloads from the queue"}, ["x"], [], SERVICES, INV)[2] == []


def test_the_frame_takes_it_with_the_key_read_in_the_guest():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": []}
    frame = sr.frame_run(IB1, "## Run this\n\n```bash\necho draft\n```\n", spec, policy, services=SERVICES, inventory=INV)
    assert not frame["refused"] and frame["inputs"] == [], (frame["refused"], frame["inputs"])
    assert "<SONARR_API_KEY>" not in " ".join(frame["commands"] + frame["verify"])


class _Sonarr(BaseHTTPRequestHandler):
    queue = [{"id": 1, "trackedDownloadState": "importBlocked"}, {"id": 2, "trackedDownloadState": "downloading"},
             {"id": 3, "trackedDownloadState": "importBlocked"}]
    calls: list = []

    def _send(self, body):
        b = json.dumps(body).encode(); self.send_response(200); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        self._send({"records": self.queue, "totalRecords": len(self.queue)})

    def do_DELETE(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append(("DELETE", self.path, body))
        type(self).queue = [r for r in self.queue if r["id"] not in body["ids"]]
        self._send({})

    def do_POST(self):
        type(self).calls.append(("POST", self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        self._send({"id": 9})

    def log_message(self, *a):
        pass


def test_the_program_dismisses_only_the_blocked_ones_from_the_queue_only():
    srv = HTTPServer(("127.0.0.1", 0), _Sonarr); threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = subprocess.run(["python3", "-c", sr._DISMISS_BLOCKED_PY, str(srv.server_port), "MissingEpisodeSearch", "k"],
                           capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()
    assert r.returncode == 0 and "IMPORT-BLOCKED: 2 of 3" in r.stdout and "STATE-OK" in r.stdout, r.stdout + r.stderr
    (d_method, d_path, d_body), (p_method, _p, p_body) = _Sonarr.calls
    assert d_path.startswith("/api/v3/queue/bulk?removeFromClient=false&blocklist=false") and d_body == {"ids": [1, 3]}
    assert p_body == {"name": "MissingEpisodeSearch"}
