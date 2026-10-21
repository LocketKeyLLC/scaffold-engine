"""§17.1716 — Sonarr's `/history/series` is a plain list; `/wanted/missing` ignores `seriesId`.

Live (job e5e22b92 HI1, a read of the HIMYM S6 import, 2026-10-21): the draft read
`get('/history/series?…&pageSize=1000')` and then `history.get('records', [])` -- the queue shape's "Sonarr's lists are
PAGED" taught it -- and crashed with `'list' object has no attribute 'get'`; the same draft's
`/wanted/missing?seriesId=…&seasonNumber=6` listed season 6 of every series.
"""
from app.modules import arr_api_shapes as aas

HI1 = {"node_key": "HI1", "title": "Read the HIMYM S6 import in Sonarr",
       "description": "Read-only, change nothing. Inside Sonarr's container (CT 104), How I Met Your Mother, season 6: "
                      "the season's have N of M, the queue records, and the last events from "
                      "/api/v3/history/series?seriesId=<id>&seasonNumber=6."}
# live, job e5e22b92 HI1's in-guest program (the parts that matter)
HI1_PY = (
    "python3 - <<'PY'\nimport urllib.request, json\n"
    "def get(path, timeout=180):\n    req = urllib.request.Request(base + path, headers=headers)\n"
    "    with urllib.request.urlopen(req, timeout=timeout) as r:\n        return json.load(r)\n"
    "queue = get('/queue?pageSize=1000')\n"
    "for rec in queue['records']:\n    print(rec.get('title'))\n"
    "history = get(f'/history/series?seriesId={series_id}&seasonNumber=6&pageSize=1000')\n"
    "cutoff = datetime(2026, 10, 21, 0, 0, 0, tzinfo=timezone.utc)\nevents = []\n"
    "for rec in history.get('records', []):\n    date_str = rec.get('date', '')\nPY\n")
FILE = {"path": "/tmp/in_ct_104.sh", "content": "cat > /tmp/in_ct_104_remote.sh <<'REMOTE'\n" + HI1_PY + "REMOTE\n"}


def test_the_live_draft_is_refused():
    hit = aas.wrong_fields(["bash /tmp/in_ct_104.sh start"], [FILE])
    assert len(hit) == 1 and "plain LIST" in hit[0]["why"]


def test_the_list_read_and_the_queue_records_are_not():
    right = dict(FILE, content=FILE["content"].replace("&pageSize=1000')", "')")
                 .replace("history.get('records', [])", "history"))
    assert aas.wrong_fields(["x"], [right]) == []
    # the queue IS paged: its `records` in the same program is not the history's
    assert "queue['records']" in right["content"]


def test_an_inline_records_read_is_refused():
    inline = dict(FILE, content=FILE["content"].replace(
        "for rec in history.get('records', []):", "for rec in get('/history/series?seriesId=1').get('records', []):"))
    assert aas.wrong_fields(["x"], [inline])


def test_the_drafter_is_shown_the_list_and_the_series_filter():
    block = aas.drafter_block(HI1)
    assert "/history/series" in block and "plain LIST" in block and "not paged" in block
    assert "does NOT filter by series" in block                   # the queue/missing shape, also named by the step
    assert "Sonarr's lists are PAGED" not in block                  # the wording that taught the crash
