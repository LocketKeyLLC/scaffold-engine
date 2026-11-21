"""§17.1977 — a step that tests, reads or writes up builds nothing, whatever nouns ("route") its text carries.

Live (job 7cc2a7ac, 2026-11-20): T16 "Proxmox host: test the control panel game-server controls through its API"
was split into eight children -- inventory the routes, read their handlers, baseline them through Caddy, baseline them
directly, exercise two controls, confirm the UI, write the report. Every one names the game-server ROUTES, so
`builds_a_capability` called each a build and §17.1408 rewrote its done-condition to "Done when it WORKS: … call the
route and check the answer carries the real data", calling `cat /tmp/t16-caddy-baseline.txt` -- the read the step
exists to make -- "only shows the code is there, which is not done".
"""
from __future__ import annotations

import pytest

from app.modules.step_decomposition import builds_a_capability, builds_nothing_by_title, children_from

WORKS = "Done when it WORKS"

#: the live T16 children's shape (made-up hosts and files)
T16 = "Proxmox host: test the control panel game-server controls through its API"
T16_CHILDREN = [
    {"title": "Proxmox host: inventory the panel's game-server API routes on CT 140",
     "description": "On the Proxmox host shell, read-only. List the backend's route definitions for the game-server controls "
                    "and record them in /tmp/t9-routes.txt.",
     "check": "pct exec 140 -- grep -rniE 'gamesrv' /opt/panel-backend --include='*.js'"},
    {"title": "Proxmox host: baseline the game-server routes through the Caddy front door",
     "description": "For each game-server route recorded in /tmp/t9-routes.txt, issue a read-only GET through Caddy: "
                    "`curl -sS -m 10 -o /tmp/t9-body-<n>.json https://panel.lab-sample.internal/<route>`. Record status, time "
                    "and body for each into /tmp/t9-caddy-baseline.txt. Do not POST anything that changes state in this step.",
     "check": "cat /tmp/t9-caddy-baseline.txt"},
    {"title": "Proxmox host: baseline the same game-server routes directly on the backend",
     "description": "Repeat the read-only calls against the backend, bypassing Caddy, and append status, time and body to "
                    "/tmp/t9-direct-baseline.txt. Change nothing.",
     "check": "cat /tmp/t9-direct-baseline.txt"},
    {"title": "Proxmox host: write the game-server control test report",
     "description": "Consolidate the /tmp/t9-* files into /tmp/t9-report.md: one row per test with route, method, status and "
                    "an explicit PASS/FAIL. List any defect found and leave it unfixed for the repair step. Change nothing on any service.",
     "check": "grep -cE 'PASS|FAIL' /tmp/t9-report.md"},
]


def test_the_children_of_a_test_are_not_told_to_work_like_a_build():
    out = children_from(T16_CHILDREN, parent_key="T9", parent_deps=[], keys=["ADD1", "ADD2", "ADD3", "ADD4"],
                        parent_title=T16)
    assert len(out) == 4
    for spec, st in zip(out, T16_CHILDREN, strict=True):
        assert WORKS not in spec["description"], spec["title"]
        assert f"Done when `{st['check']}` shows it." in spec["description"]


def test_each_child_alone_builds_nothing_by_its_own_title():
    # the child's own title decides even when the parent is not named a test
    out = children_from(T16_CHILDREN, parent_key="P", parent_deps=[], keys=["A1", "A2", "A3", "A4"])
    assert [WORKS in s["description"] for s in out] == [False, False, False, False]


def test_a_real_route_build_still_gets_the_rewrite():
    # the correct miss: "add a route to Caddy" builds a capability; its grep shows the line is there, not that it works
    steps = [
        {"title": "CT 140: add a route to Caddy for the panel's game-server API",
         "description": "Add a reverse_proxy route for /api/gamesrv/* to the Caddyfile and reload Caddy.",
         "check": "pct exec 140 -- grep -n 'gamesrv' /etc/caddy/Caddyfile"},
        T16_CHILDREN[1],
    ]
    out = children_from(steps, parent_key="P", parent_deps=[], keys=["A1", "A2"], parent_title="Publish the game-server API")
    assert WORKS in out[0]["description"]
    assert WORKS not in out[1]["description"]


@pytest.mark.parametrize("title,text,reads", [
    ("Proxmox host: baseline the game-server routes through the Caddy front door", T16_CHILDREN[1]["description"], True),
    ("Proxmox host: exercise the Palworld settings control and confirm the effect", "call the settings route", True),
    ("Proxmox host: confirm the start actually took effect on container 140", "verify the start", True),
    ("Proxmox host: write the game-server control test report", "consolidate the files", True),
    ("Audit the code that calls the sample API against its research", "write one bullet per mismatch", True),
    # a read title whose text names a change stays a build (§17.1962c's T15)
    ("Read the host's guests through the Proxmox API",
     "Change the control panel's /api/network-ui handler on container 140 to read the host through its API", False),
    # a report PAGE is a build
    ("CT 140: write the status report page for the panel", "add a /report route rendering the results", False),
    ("CT 140: add a route to Caddy", "reverse_proxy /api/* to the backend", False),
])
def test_what_tests_or_reads(title, text, reads):
    assert builds_nothing_by_title(title, text) is reads


def test_a_test_parent_decides_where_the_childs_own_title_does_not():
    # live ADD9/ADD10: "recover … from step 2's record" / "capture … baseline status" name a change word in their
    # text, so their own titles do not settle it -- that they are parts of T16's TEST does
    step = {"title": "Proxmox host: prepare the game-server control call from step 2's record",
            "description": "Pull the control route, its body and the front-door URL from /tmp/t9-routes.txt and write them "
                           "as METHOD=/PATH=/BODY= lines to /tmp/t9-control.txt. Do NOT invent any of these.",
            "check": "grep -E '^(METHOD|PATH|BODY)=' /tmp/t9-control.txt"}
    under_test = children_from([step, T16_CHILDREN[0]], parent_key="T9", parent_deps=[], keys=["A1", "A2"], parent_title=T16)
    under_build = children_from([step, T16_CHILDREN[0]], parent_key="P", parent_deps=[], keys=["A1", "A2"],
                                parent_title="CT 140: build the game-server controls")
    assert WORKS not in under_test[0]["description"]
    assert WORKS in under_build[0]["description"]


def test_builds_a_capability_without_a_title_is_unchanged():
    text = "Proxmox host: baseline the game-server routes through the Caddy front door"
    assert builds_a_capability(text) is True
    assert builds_a_capability(text, title=text) is False
