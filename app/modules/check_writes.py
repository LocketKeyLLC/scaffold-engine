r"""§17.1659 — a CHECK step does not change what it checks.

Live (job 5db1b088 T18 "Verify qBittorrent's network interface is wg0", 2026-10-18): the draft logged in and POSTed
`/api/v2/app/setPreferences` with `{"outgoing_interface":"wg0"}` -- a verify that would have WRITTEN the setting it was
asked to confirm (and with a key qBittorrent does not have), so a pass would prove nothing and a wrong value would have
been written to the download client behind the VPN kill-switch.

A step whose title leads with verify / check / confirm / test / validate is refused when a command sends a changing
request (POST/PUT/PATCH/DELETE, a request body). Requests that only ask the service to test itself or open a session
are not changes: `…/test`, `…/testall`, `…/auth/login`, `…/login`.
"""
from __future__ import annotations

import re
from typing import Optional

_EXEMPT_URL_RE = re.compile(r"/(?:test|testall|auth/login|login)\b|/api/v\d+/command\b", re.I)


def a_check_that_writes(commands: list[str], node: Optional[dict]) -> list[dict]:
    from app.modules.step_classify import _LEADING_OBSERVE_RE
    from app.modules.supervised_runs import _SENDS_A_CHANGE_RE, _strip_comment_shell
    title = str((node or {}).get("title") or "")
    if not _LEADING_OBSERVE_RE.match(title):
        return []
    for c in commands or []:
        for ln in re.sub(r"\\\r?\n[ \t]*", " ", str(c)).splitlines():
            code = _strip_comment_shell(ln)
            m = _SENDS_A_CHANGE_RE.search(code)
            if not m:
                continue
            urls = re.findall(r"https?://[^\s'\"]+", code)      # the TARGET is the last URL (a Referer header comes first)
            if urls and _EXEMPT_URL_RE.search(urls[-1]):
                continue
            return [{"command": str(c)[:200], "why": (
                f"\"{title}\" is a CHECK, and this command CHANGES what it checks (it sends "
                f"{(m.group(0) or 'a request body').strip()[:40]}): a pass would only prove the step's own write, and "
                "a wrong value would land on the machine (live, T18's verify POSTed qBittorrent's setPreferences). "
                "Read the setting back instead -- the config file, or the API's GET (§17.1659).")}]
    return []
