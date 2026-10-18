r"""§17.1647 — an *arr "test this download client / indexer" call sends the whole object, not just its id.

Live (job 5db1b088 T10 "Verify qBittorrent is reachable", 2026-10-18): the draft asked Radarr to test its download
client with `curl -X POST … -d "{\"id\": 1}" http://127.0.0.1:7878/api/v3/downloadclient/test`. Radarr's test endpoint
validates the body as a full resource, so it answered 400 ("'Implementation' must not be empty", "Priority must be
between 1 and 50") and the step FAILED -- reporting qBittorrent unreachable when nothing had been tested.

The engine rewrites such a call in place: GET the object by its id and pipe it into the same POST, in the same guest,
with the same key read (`curl … /<resource>/<id> | curl … -d @- /<resource>/test`). Anything else is left as drafted.
"""
from __future__ import annotations

import re

#: `-d "{\"id\": 1}"` / `-d '{"id":1}'` / `--data '{"id": 1}'` — a body that is ONLY an id
_ID_BODY_RE = re.compile(r"""\s(?:-d|--data(?:-raw)?)\s+(?:"\{\\"id\\":\s*(?P<a>\d+)\}"|'\{"id":\s*(?P<b>\d+)\}')""")
#: the test endpoint of an *arr resource on loopback
_TEST_URL_RE = re.compile(r"(?P<base>http://127\.0\.0\.1:\d{2,5}/api/v\d+/(?P<res>downloadclient|indexer|notification|"
                          r"importlist|applications|indexerproxy))/test\b")
_KEY_HDR_RE = re.compile(r"""-H\s+"X-Api-Key:\s*(?:[^"$]|\$\((?:[^()]|\([^()]*\))*\))*"|-H\s+'X-Api-Key:[^']*'""")


def _fix(line: str) -> str:
    body, url = _ID_BODY_RE.search(line), _TEST_URL_RE.search(line)
    if not body or not url or "| curl" in line:
        return line
    key = _KEY_HDR_RE.search(line)
    if not key:
        return line
    oid = body.group("a") or body.group("b")
    get = f"curl -s --fail-with-body {key.group(0)} {url.group('base')}/{oid}"
    post = (line[:body.start()] + " -d @-" + line[body.end():])
    # the POST keeps its own flags and target; its `curl` now reads the object from the GET
    start = post.rfind("curl", 0, body.start())
    if start < 0:
        return line
    return post[:start] + get + " | " + post[start:]


def send_the_whole_object(commands: list[str], verify: list[str]) -> tuple[list[str], list[str], list[dict]]:
    out_c, out_v = [_fix(str(c)) for c in commands or []], [_fix(str(v)) for v in verify or []]
    changed = out_c != [str(c) for c in commands or []] or out_v != [str(v) for v in verify or []]
    notes = [{"why": ("an *arr test call sent only the object's id, which the test endpoint validates as a whole "
                      "resource (live: Radarr answered 400 \"'Implementation' must not be empty\" and the step failed "
                      "as if the client were unreachable); the engine GETs the object by its id and posts it back to "
                      "/test, in the same guest with the same key read (§17.1647).")}] if changed else []
    return out_c, out_v, notes
