"""§17.1694 — the drafter is shown the *arr API shapes a step reads; a draft that reads a field they rule out is refused."""
import inspect

from app.modules import arr_api_shapes as aas
from app.modules import execution_agent as ea
from app.modules import supervised_runs as sr

T4 = {"node_key": "T4", "title": "Compare Prowlarr indexer categories",
      "description": "Read-only: for each TV-capable Prowlarr indexer, compare its categories against the sync set."}
# live, job e5e22b92 T4's in-guest program (the parts that matter)
T4_FILE = {"path": "/tmp/in_ct_102.sh", "content": (
    "cat > /tmp/in_ct_102_remote.sh <<'REMOTE'\npython3 - <<'PY'\nimport urllib.request, json\n"
    "def get(path):\n    return json.load(urllib.request.urlopen(API + path))\n"
    "indexers = get(\"/api/v1/indexer\")\nfor idx in indexers:\n"
    "    if idx.get(\"enable\") and any(c in (idx.get(\"categories\") or []) for c in [5000, 5030, 5040]):\n"
    "        print(idx['name'])\nPY\nREMOTE\n")}


def test_the_live_step_is_shown_where_categories_live():
    block = aas.drafter_block(T4)
    assert "capabilities.categories" in block and "NO top-level `categories`" in block
    assert "syncCategories" in block                       # "the sync set" is the application's


def test_each_step_of_the_plan_gets_its_shapes():
    assert aas.drafter_block({"title": "Search HIMYM S6", "description": "Read-only: run interactive search for "
                              "How I Met Your Mother Season 6 in Sonarr. Do not grab."}).count("/api/v3/release?seriesId=") == 1
    assert "qualityProfileId" in aas.drafter_block({"title": "Read second quality profile",
                                                     "description": "Read Sonarr's quality profiles."})
    assert "sceneSeasonNumber" in aas.drafter_block({"title": "Map Power Rangers titles",
                                                      "description": "Map the sub-series titles to their Sonarr seasons."})
    assert aas.drafter_block({"title": "Restart nginx", "description": "Restart it."}) == ""


def test_the_live_draft_is_refused_and_the_right_one_is_not():
    hit = aas.wrong_fields(["bash /tmp/in_ct_102.sh start"], [T4_FILE])
    assert hit and "capabilities.categories" in hit[0]["why"] and 'idx.get("categories")' in hit[0]["command"]
    right = dict(T4_FILE, content=T4_FILE["content"].replace('idx.get("categories")',
                                                             '[c["id"] for c in idx["capabilities"]["categories"]]'))
    assert aas.wrong_fields(["bash /tmp/in_ct_102.sh start"], [right]) == []
    # Sonarr's own indexer has categories in `fields`; a draft that reads a different endpoint is not judged here
    other = dict(T4_FILE, content=T4_FILE["content"].replace("/api/v1/indexer", "/api/v3/indexer"))
    assert aas.wrong_fields(["x"], [other]) == []


def test_both_ends_are_wired():
    assert "_aas.drafter_block(run_node)" in inspect.getsource(ea)
    assert "_wrong_fields(cmds, shape_files)" in inspect.getsource(sr.frame_run)


def test_a_season_search_that_never_says_sonarr_gets_the_release_shape():
    """§17.1694b — live, job e5e22b92 T5 was drafted as a series list: its text never named Sonarr"""
    t5 = {"title": "Search Robot Chicken S11",
          "description": "Read-only: run interactive search for Robot Chicken Season 11 on TV-capable indexers. Do not grab."}
    assert "/api/v3/release?seriesId=" in aas.drafter_block(t5)
    assert "/api/v3/release" not in aas.drafter_block({"title": "Search the logs", "description": "grep the journal."})


def test_a_read_is_drafted_in_the_accepted_shape_from_the_first_draft():
    """§17.1694c — live, T5's draft asked for <SONARR_SERIES_ID> and checked with `"id"`"""
    t5 = {"title": "Search Robot Chicken S11",
          "description": "Read-only: run interactive search for Robot Chicken Season 11 on TV-capable indexers. Do not grab."}
    b = aas.drafter_block(t5)
    assert "python3 - <<'PY'" in b and "never ask for an id" in b
    change = {"title": "Widen TV-capable categories", "description": "Change Prowlarr's indexer categories."}
    assert aas.drafter_block(change) and "python3 - <<'PY'" not in aas.drafter_block(change)


def test_the_refusal_drives_a_redraft():
    """registered like every shape refusal (§17.1269): the redraft note recognises it"""
    hit = aas.wrong_fields(["bash /tmp/in_ct_102.sh start"], [T4_FILE])
    assert any(m in hit[0]["why"] for m in sr._SHAPE_REFUSALS)
    assert sr.shape_retry_note({"kind": "run", "refused": hit})


def test_an_indexer_change_is_shown_test_put_and_read_back():
    """§17.1694e — live, job e5e22b92 T13 tested with `POST /api/v1/indexer/test/{id}` + `{}` and saved with a POST"""
    t13 = {"title": "Re-enable healthy indexers",
           "description": "Change, operator approval first: in Prowlarr, test the disabled indexers Internet Archive and "
                          "Torrent Downloads (Prowlarr's own indexer test); re-enable each one that passes."}
    b = aas.drafter_block(t13)
    assert "POST /api/v1/indexer/test` and that WHOLE object" in b and "PUT /api/v1/indexer/{id}" in b
    assert "SEPARATE Verify" in b
    assert "python3 - <<'PY'" not in b                         # a change gets no read-only shape
    live = {"path": "/tmp/in_ct_102.sh", "content": "<<'REMOTE'\nindexers = get(\"/api/v1/indexer\")\n"
            "result = post(f\"/api/v1/indexer/test/{idx['id']}\", {})\nREMOTE\n"}
    hit = aas.wrong_fields(["bash /tmp/in_ct_102.sh start"], [live])
    assert hit and "no `/api/v1/indexer/test/{id}`" in hit[0]["why"]
    right = dict(live, content=live["content"].replace("f\"/api/v1/indexer/test/{idx['id']}\", {}", "\"/api/v1/indexer/test\", idx"))
    assert aas.wrong_fields(["x"], [right]) == []
