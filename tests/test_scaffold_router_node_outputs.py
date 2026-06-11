"""§17.471 — `/results <job_id> nodes` per-node output view.

Background: a completed job's `/results` returns only `compiled_output`,
which `execution_compile` Strategy 0 assembles from the DAG's
`is_output_node` leaves. On a multi-leaf job (e.g. the 10-node Proxmox
HomeLab job whose leaf-set is `{T4, T10}`) every interior node's work —
T1, T2, T3, T5..T9 — is dropped from the deliverable. Operators had no
way to pull those up from chat: `/results` gave the compiled output,
`/exec status` gave a status table with no bodies.

§17.471 adds:
  - `GET /exec/nodes/{job_id}` (orchestrator) — full output_text per node.
  - `/results <job_id> nodes` (aliases full/all/detail) — renders every
    node T1..Tn with status, output-node marker, and a capped body.
  - a discoverability hint appended to the completed-job `/results` body.

These tests pin the pipeline-side rendering + dispatch + hint.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


def _nodes_response(nodes, *, job_status="completed", title="Proxmox") -> MagicMock:
    """Build a /exec/nodes response."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "job_id": "job-1",
        "job_title": title,
        "job_status": job_status,
        "total_nodes": len(nodes),
        "nodes": nodes,
    }
    return resp


def _node(key, *, status="done", is_output=False, body="some output", order=0):
    return {
        "node_key": key,
        "title": f"task {key}",
        "status": status,
        "execution_order": order,
        "is_output_node": is_output,
        "output_text": body,
        "output_len": len(body),
    }


@pytest.mark.smoke
class TestNodeOutputView:
    def test_nodes_subcommand_renders_every_node(self, pipe):
        """`/results <id> nodes` must show interior nodes the compiled
        deliverable drops — the core gap this feature closes."""
        nodes = [
            _node("T1", is_output=False, body="storage plan", order=0),
            _node("T4", is_output=True, body="tailscale config", order=3),
            _node("T10", is_output=True, body="validation doc", order=9),
        ]
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _nodes_response(nodes)
            out = pipe._handle_results(["/results", "job-1", "nodes"])
        # Every node header + body present.
        for key, body in [("T1", "storage plan"), ("T4", "tailscale config"),
                          ("T10", "validation doc")]:
            assert f"### {key}" in out
            assert body in out
        # The compiled-from line names the output-node leaves only.
        assert "`T4`" in out and "`T10`" in out
        assert "Compiled deliverable is built from" in out

    @pytest.mark.parametrize("alias", ["nodes", "full", "all", "detail", "NODES"])
    def test_aliases_route_to_node_view(self, pipe, alias):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _nodes_response([_node("T1", body="hi")])
            out = pipe._handle_results(["/results", "job-1", alias])
        assert "### T1" in out
        assert "hi" in out

    def test_long_body_is_truncated_with_pointer(self, pipe):
        cap = Pipeline._NODE_OUTPUT_PREVIEW_CHARS
        big = "X" * (cap + 500)
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _nodes_response([_node("T1", body=big)])
            out = pipe._handle_results(["/results", "job-1", "nodes"])
        assert "more chars" in out
        assert "/web/jobs/job-1" in out
        # Body was actually capped (not emitted in full).
        assert out.count("X") <= cap + 5

    def test_empty_body_node_shows_no_output(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _nodes_response([_node("T1", body="")])
            out = pipe._handle_results(["/results", "job-1", "nodes"])
        assert "_(no output)_" in out

    def test_no_nodes_yields_planning_hint(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _nodes_response([])
            out = pipe._handle_results(["/results", "job-1", "nodes"])
        assert "no DAG nodes yet" in out

    def test_404_yields_not_found(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = MagicMock(status_code=404, text="nope")
            out = pipe._handle_results(["/results", "job-missing", "nodes"])
        assert "Job not found" in out


@pytest.mark.smoke
class TestCompletedResultsHint:
    """The default `/results` (compiled deliverable) must point operators
    at the node view so the dropped-interior-nodes gap is discoverable."""

    def test_completed_results_appends_node_hint(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "status": "completed",
                "compiled_output": "## Final deliverable\n\nbody",
                "total_nodes": 10,
            }
            mg.return_value = resp
            out = pipe._handle_results(["/results", "job-1"])
        assert "Final deliverable" in out  # compiled body preserved
        assert "/results job-1 nodes" in out
        assert "10 nodes" in out

    def test_completed_without_node_count_omits_hint(self, pipe):
        """No total_nodes (defensive) → no dangling hint."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "status": "completed",
                "compiled_output": "body only",
            }
            mg.return_value = resp
            out = pipe._handle_results(["/results", "job-1"])
        assert "body only" in out
        assert "nodes`._" not in out
