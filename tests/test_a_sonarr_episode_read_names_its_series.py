"""§17.1718 — Sonarr's `GET /api/v3/episode` needs `seriesId`.

Live (job e5e22b92 HI1, 2026-10-21, after §17.1717): the draft listed `get('/episode', timeout=180)` and filtered by
`seriesId` afterwards; Sonarr 4.0.20 answered `HTTP Error 400: Bad Request` and the read stopped after its first part.
"""
from app.modules import arr_api_shapes as aas

STEP = {"node_key": "HI1", "title": "Read the HIMYM S6 import in Sonarr",
        "description": "Read-only, change nothing. Inside Sonarr's container (CT 104), season 6: the episode numbers of "
                       "season 6 episodes that still have no file."}
# live, HI1's third draft (the lines that matter)
LIVE = ("python3 - <<'PY'\nbase = 'http://localhost:8989/api/v3'\nheaders = {'X-Api-Key': api_key}\n"
        "    missing_episodes = []\n"
        "    for ep in get('/episode', timeout=180):\n"
        "        if ep.get('seriesId') == series_id and ep.get('seasonNumber') == 6 and not ep.get('hasFile', False):\n"
        "            missing_episodes.append(ep.get('episodeNumber'))\nPY\n")
FILE = {"path": "/tmp/in_ct_104.sh", "content": "cat > /tmp/in_ct_104_remote.sh <<'REMOTE'\n" + LIVE + "REMOTE\n"}


def _with(line: str) -> dict:
    return dict(FILE, content=FILE["content"].replace("get('/episode', timeout=180)", line))


def test_the_live_draft_is_refused():
    hit = aas.wrong_fields(["bash /tmp/in_ct_104.sh start"], [FILE])
    assert len(hit) == 1 and "NEEDS `seriesId`" in hit[0]["why"] and "/episode'" in hit[0]["command"]
    assert aas.wrong_fields(["x"], [_with("get('/episode?seasonNumber=6')")])     # a season alone is not enough
    assert aas.wrong_fields(["x"], [_with("json.load(urllib.request.urlopen(base + '/episode'))")])


def test_the_named_reads_are_not():
    for ok in ("get(f'/episode?seriesId={series_id}&seasonNumber=6')", "get(f'/episode/{eid}')",
               "get(f'/episodefile?seriesId={series_id}')", "get('/episode?episodeIds=' + ','.join(ids))",
               # live, jobs e5e22b92/c2518102 (SC1, PR4, PR5, RC4, …): the series passed as the params dict
               "get('/api/v3/episode', {'seriesId': series_id, 'seasonNumber': 1})",
               "get(f'/api/v3/episode', {'seriesId': sid})"):
        assert aas.wrong_fields(["x"], [_with(ok)]) == [], ok


def test_the_drafter_is_shown_the_shape():
    block = aas.drafter_block(STEP)
    assert "/api/v3/episode?seriesId=N" in block and "400 Bad Request" in block
