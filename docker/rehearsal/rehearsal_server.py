"""§17.1409 — the rehearsal service: one job at a time, each in a fresh chroot.

POST /rehearse with the job JSON (see rehearse.py) -> the report JSON.

Each job gets a fresh copy of /pristine (this image's own filesystem, captured at
build time), runs `rehearse.py` chrooted into it, and is deleted afterwards along with
every process whose root is that copy. CAP_SYS_CHROOT and CAP_MKNOD are Docker
defaults: no extra privilege, no Docker socket. The engine reaches this only over an
internal network with no route out.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
JOB_TIMEOUT = int(os.environ.get("REHEARSAL_TIMEOUT_S", "240"))


def _kill_rooted_in(root: str) -> int:
    killed = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.path.realpath(f"/proc/{pid}/root") == root:
                os.kill(int(pid), signal.SIGKILL)
                killed += 1
        except (OSError, ProcessLookupError):
            continue
    return killed


def rehearse(job: dict) -> dict:
    with LOCK:                                  # jobs share loopback ports: one at a time
        base = tempfile.mkdtemp(dir="/jobs")
        root = os.path.join(base, "root")
        t0 = time.time()
        try:
            # ownership is not kept: the copy is throwaway, and keeping it would need CAP_CHOWN
            cp = subprocess.run(["cp", "-a", "--no-preserve=ownership", "/pristine", root],
                                capture_output=True, text=True)
            if cp.returncode != 0:
                raise RuntimeError(f"could not copy the pristine tree: {cp.stderr[-300:]}")
            for name, major, minor in (("null", 1, 3), ("zero", 1, 5), ("random", 1, 8), ("urandom", 1, 9)):
                path = os.path.join(root, "dev", name)
                if not os.path.exists(path):
                    os.mknod(path, 0o666 | 0o020000, os.makedev(major, minor))
            p = subprocess.run(["chroot", root, "/usr/bin/python3", "/usr/local/bin/rehearse.py"],
                               input=json.dumps(job), capture_output=True, text=True, timeout=JOB_TIMEOUT)
            try:
                report = json.loads(p.stdout)
            except ValueError:
                report = {"error": "rehearsal produced no report", "stderr": p.stderr[-800:]}
        except subprocess.TimeoutExpired:
            report = {"error": f"rehearsal exceeded {JOB_TIMEOUT}s"}
        except Exception as exc:                    # a job that cannot start says so, it does not hang up
            report = {"error": f"rehearsal could not run: {exc}"}
        finally:
            report_killed = _kill_rooted_in(os.path.realpath(root))
            # orphans re-parent to PID 1; reap whatever is ours, so no zombie outlives a job
            time.sleep(0.2)
            while True:
                try:
                    pid, _ = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
            shutil.rmtree(base, ignore_errors=True)
        report["seconds"] = round(time.time() - t0, 1)
        report["killed_after"] = report_killed
        return report


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/rehearse":
            return self._send(404, {"error": "not found"})
        try:
            job = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "body must be JSON"})
        self._send(200, rehearse(job))

    def _send(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    os.makedirs("/jobs", exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("REHEARSAL_PORT", "8799"))), Handler).serve_forever()
