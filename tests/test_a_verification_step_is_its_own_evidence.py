"""§17.1695 — a verification step whose block only reads is its own evidence, like a declared read-only step."""
from app.modules import supervised_runs as sr

T11 = {"node_key": "T11", "title": "Verify Sonarr indexers",
       "description": "List Sonarr's indexers to confirm all TV-capable enabled indexers are present."}   # live, job e5e22b92
PROG = {"path": "/tmp/in_ct_104.sh", "content": (
    "cat > /tmp/in_ct_104_remote.sh <<'REMOTE'\nset -e\nexport DEBIAN_FRONTEND=noninteractive\npython3 - <<'PY'\n"
    "import urllib.request, json\nreq = urllib.request.Request('http://localhost:8989/api/v3/indexer', headers={'X-Api-Key': K})\n"
    "print(len(json.load(urllib.request.urlopen(req))))\nPY\nREMOTE\n")}


def test_the_live_verification_step_only_reads():
    assert not sr.declares_read_only(T11) and sr.only_reads(T11)
    assert sr.a_read_only_program(T11, [PROG])


def test_a_step_that_changes_is_not_one():
    for t, d in (("Widen TV-capable categories", "Widen TV-capable indexer categories as specified in Group C1."),
                 ("Check and restart nginx", "Restart nginx if it is down."),
                 ("Verify and fix the sync", "Fix the Prowlarr sync, then list the indexers."),
                 ("Search season gaps smallest first", "Search season-by-season, smallest gaps first.")):
        assert not sr.only_reads({"title": t, "description": d}), t
    assert not sr.a_read_only_program({"title": "Check and restart nginx", "description": "Restart it."}, [PROG])


def test_its_cut_output_fails_it_too():
    out = "[CUT: the step printed 900 lines; only the last 300 are kept]\n"
    assert sr.a_read_was_cut(T11, [{"command": "bash /tmp/in_ct_104.sh start", "output": out}])
