"""§17.968 — the engine's own artefacts, checked against each other.

Operator: *"did you again give it the answer instead of fixing the problem?"*

They had. §17.965 recorded a file's size, §17.967 its content hash — and the
live defect was in neither file, it was BETWEEN two of them:

    server.js  app.get('/status', …) → const results = {}; res.json(results)
    App.jsx    fetch(`${API_BASE}/status`) → setServices(data)
               services.find(item => item.vmid === s.id)

The engine wrote both. Both sat in its transcript for hours while it rewrote a
third file that was already correct.
"""
import inspect

import pytest

from app.modules.assist_contracts import (
    bounded_artefacts,
    client_endpoints,
    find_contract_conflicts,
    render_contract_conflicts,
    response_shapes,
    server_endpoints,
    state_sources,
)
from app.modules.assist_directives import apply_contract_conflict

# Reduced from the live files, preserving the exact shapes that matter.
_SERVER = """
const app = express();
app.get('/status', async (req, res) => {
  try {
    const results = {};
    for (const [name, id] of Object.entries(SERVICES)) { results[name] = 'running'; }
    res.json(results);
  } catch (error) { res.status(500).json({ error: 'Failed' }); }
});
app.post('/power', async (req, res) => { res.json({ ok: true }); });
app.listen(3001, () => console.log('Backend API running on port 3001'));
"""

_CLIENT = """
function App() {
  const [services, setServices] = useState([]);
  const [loading, setLoading] = useState(true);
  const API_BASE = 'http://192.168.1.25:3001';
  useEffect(() => {
    fetch(`${API_BASE}/status`)
      .then(res => res.json())
      .then(data => { setServices(data); setLoading(false); });
  }, []);
  return (<div>{SERVICES.map(s => {
    const status = services.find(item => item.vmid === s.id)?.status || 'unknown';
    return <span>{status}</span>;
  })}</div>);
}
"""

_BE = "/opt/control-panel-backend/server.js"
_FE = "/opt/control-panel-ui/src/App.jsx"


# ── extraction ────────────────────────────────────────────────────────────


def test_server_endpoints_and_shapes_are_read():
    assert server_endpoints(_SERVER) == {"/status", "/power"}
    assert response_shapes(_SERVER)["/status"] == "object"


def test_client_endpoints_survive_template_interpolation():
    """`fetch(`${API_BASE}/status`)` has to resolve to `/status`."""
    assert client_endpoints(_CLIENT) == {"/status", "/power"} - {"/power"}


def test_state_is_linked_back_to_the_endpoint_that_filled_it():
    assert state_sources(_CLIENT) == {"services": "/status"}


# ── the live conflict ─────────────────────────────────────────────────────


def test_the_live_blank_page_is_found():
    conflicts = find_contract_conflicts({_BE: _SERVER, _FE: _CLIENT})
    shape = [c for c in conflicts if c["kind"] == "shape"]
    assert shape, conflicts
    c = shape[0]
    assert c["endpoint"] == "/status" and c["var"] == "services"
    assert c["ops"] == ["find"]
    assert c["server"] == _BE and c["client"] == _FE
    assert "Array methods" in c["detail"]


def test_matching_shapes_are_silent():
    """The same code with the server returning an array is not a conflict."""
    ok = _SERVER.replace("const results = {};", "const results = [];")
    assert [c for c in find_contract_conflicts({_BE: ok, _FE: _CLIENT})
            if c["kind"] == "shape"] == []


def test_an_object_response_without_array_ops_is_silent():
    plain = _CLIENT.replace("services.find(item => item.vmid === s.id)?.status",
                            "services[s.name.toLowerCase()]")
    assert [c for c in find_contract_conflicts({_BE: _SERVER, _FE: plain})
            if c["kind"] == "shape"] == []


# ── the other two checks ──────────────────────────────────────────────────


def test_a_call_to_an_endpoint_nobody_serves():
    client = _CLIENT.replace("/status", "/state")
    hits = [c for c in find_contract_conflicts({_BE: _SERVER, _FE: client})
            if c["kind"] == "endpoint"]
    assert hits and hits[0]["endpoint"] == "/state"


def test_a_call_to_a_port_nothing_listens_on():
    client = _CLIENT.replace(":3001", ":3002")
    hits = [c for c in find_contract_conflicts({_BE: _SERVER, _FE: client})
            if c["kind"] == "port"]
    assert hits and "3002" in hits[0]["detail"]


def test_the_live_pair_has_no_port_or_endpoint_conflict():
    """Both genuinely match — so parity checks alone would NOT have caught this
    bug, which is why the shape check exists."""
    conflicts = find_contract_conflicts({_BE: _SERVER, _FE: _CLIENT})
    assert [c for c in conflicts if c["kind"] in ("port", "endpoint")] == []


# ── safety ────────────────────────────────────────────────────────────────


def test_a_single_artefact_is_never_a_conflict():
    assert find_contract_conflicts({_BE: _SERVER}) == []
    assert find_contract_conflicts({}) == []
    assert find_contract_conflicts(None) == []


def test_unrelated_files_produce_nothing():
    assert find_contract_conflicts({
        "/etc/hosts": "127.0.0.1 localhost",
        "/opt/notes.md": "# just some notes\nnothing to see"}) == []


def test_only_retained_bodies_are_checked():
    state = {_BE: {"expected": 10, "sha": "x"},          # no body kept
             _FE: {"expected": 10, "sha": "y", "body": _CLIENT}}
    assert bounded_artefacts(state) == {_FE: _CLIENT}
    assert find_contract_conflicts(bounded_artefacts(state)) == []


# ── surfaced and acted on ─────────────────────────────────────────────────


def test_the_block_names_both_files_and_the_remedy():
    block = render_contract_conflicts(
        find_contract_conflicts({_BE: _SERVER, _FE: _CLIENT}))
    assert "CONTRADICT EACH OTHER" in block
    assert _BE in block and _FE in block
    assert "Fix the DISAGREEMENT" in block
    assert render_contract_conflicts([]) == ""


def test_the_directive_forbids_rewriting_unchanged():
    out = apply_contract_conflict("SYS", conflicts=find_contract_conflicts(
        {_BE: _SERVER, _FE: _CLIENT}))
    assert "CONTRADICT EACH OTHER" in out
    assert "Rewriting either file unchanged cannot fix it" in out
    assert "the disagreement is yours to resolve" in out
    assert apply_contract_conflict("SYS", conflicts=None) == "SYS"


def test_conflicts_reach_the_prompt_and_the_fix_path():
    from app.modules import assist_guide, assist_render

    assert "find_contract_conflicts" in inspect.getsource(assist_render)
    fix = inspect.getsource(assist_guide.generate_fix)
    assert "find_contract_conflicts" in fix and "apply_contract_conflict" in fix


def test_the_ledger_actually_keeps_the_text_now():
    """§17.967 hashed the body and discarded it, which made every check above
    impossible. That regression must not come back."""
    from app.modules.assist_files import parse_file_writes

    got = parse_file_writes(
        "```bash\ncat > /opt/a/server.js <<'EOF'\napp.listen(3001);\nEOF\n```")
    assert got["/opt/a/server.js"].get("body")
