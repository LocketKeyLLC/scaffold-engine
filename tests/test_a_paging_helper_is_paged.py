"""§17.1719 — a call through the program's own paging helper is paged.

Live (job e5e22b92 MA1, 2026-10-21, a read of every missing episode and its download): the draft defined
`get_paged(path)`, which appends `page={page}&pageSize={page_size}` and reads until `totalRecords`, and called
`get_paged('/wanted/missing?includeSeries=true')` and `get_paged('/queue')`; §17.1713's rule saw no `pageSize` at the
call and refused the right program.
"""
from app.modules import arr_api_shapes as aas

# live, MA1's in-guest program (the parts that matter)
HELPERS = (
    "python3 - <<'PY'\nimport urllib.request, json\n"
    "def get(path, timeout=180):\n    req = urllib.request.Request(base + path, headers=headers)\n"
    "    with urllib.request.urlopen(req, timeout=timeout) as r:\n        return json.load(r)\n"
    "def get_paged(path, timeout=180):\n    records = []\n    page = 1\n    page_size = 1000\n    while True:\n"
    "        sep = '&' if '?' in path else '?'\n"
    "        url = f\"{path}{sep}page={page}&pageSize={page_size}\"\n"
    "        data = get(url, timeout)\n        records.extend(data.get('records', []))\n"
    "        if page * page_size >= data.get('totalRecords', 0):\n            break\n        page += 1\n"
    "    return records\n"
    "series_list = get('/series')\n")
CALLS = "missing_records = get_paged('/wanted/missing?includeSeries=true')\nqueue_records = get_paged('/queue')\nPY\n"


def _file(prog):
    return {"path": "/tmp/in_ct_104.sh", "content": "cat > /tmp/in_ct_104_remote.sh <<'REMOTE'\n" + prog + "REMOTE\n"}


def test_the_live_draft_is_not_refused():
    assert aas.wrong_fields(["bash /tmp/in_ct_104.sh start"], [_file(HELPERS + CALLS)]) == []


def test_the_same_call_through_the_plain_get_still_is():
    hit = aas.wrong_fields(["x"], [_file(HELPERS + CALLS.replace("get_paged('/queue')", "get('/queue')"))])
    assert len(hit) == 1 and hit[0]["command"] == "queue_records = get('/queue')"


def test_a_helper_that_does_not_page_is_not_a_pass():
    unpaged = HELPERS.replace("&pageSize={page_size}", "").replace("page_size = 1000\n", "").replace(
        "page * page_size", "page * 10")
    assert aas.wrong_fields(["x"], [_file(unpaged + CALLS)])


def test_base_plus_and_fstring_calls_through_the_helper():
    calls = "q = get_paged(base + '/queue')\nm = get_paged(f'/wanted/missing?includeSeries={1}')\nPY\n"
    assert aas.wrong_fields(["x"], [_file(HELPERS + calls)]) == []
