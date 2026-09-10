"""§17.1007c — the operator-facing payload-field inventory.

Why this exists
---------------
``/exec/status`` has returned ``failure_reason`` per node since §17.450, sourced
from ``dag_nodes.last_verification_reason``. The CLI renders it
(``cli/scaffold_cli/main.py``). The operator SPA — which calls that exact
endpoint and reads every *other* field off each node — never did, so for three
months the answer to "why did this step fail" was on the wire, one property
away, and the console dropped it. The §17.450 and §17.480 comments say the
fields were plumbed "to the web detail page", a Jinja surface that has since
been deleted: the fields outlived their only consumer and the surface that
replaced it never picked them up.

``test_sse_event_inventory.py`` already guards this exact class of drift, but at
the level of event *names*: it scans emitters for ``_sse("name", ...)`` and
consumers for ``event_type == "name"`` and fails when either side diverges from
``ALL_EVENT_NAMES``. A field inside an event's payload sails straight past it.
This module is the same idea one level down.

The two guards, and why both are needed
---------------------------------------
Either half alone rots:

* **Producer scan** — every key a payload builder emits must appear here.
  Catches "added a field and never declared it", which would otherwise let a
  new field skip the consumer check entirely by never being known about.
* **Consumer scan** — every (field, surface) pair declared here must hold.
  Note the pair: "read by at least one surface somewhere" is too weak to be
  the rule, because the CLI rendered ``failure_reason`` throughout the three
  months the console did not. A gate asking only "does anybody read this"
  would have stayed green for the entire life of the bug it was written for.

``INTERNAL_FIELDS`` is the pressure valve. A field lands there when it is
genuinely machine-only, and every entry carries the reason — an allow-list
without justifications becomes a place to silence the gate.

Scope
-----
Deliberately starts with the ``/exec/status`` node payload, the endpoint the
original bug lived in. Extending it to another payload means adding a mapping
here plus its producer entry; it is not meant to cover every response in the
API at once, because an inventory nobody can keep accurate is worse than none.
"""

# ---------------------------------------------------------------------------
# Surfaces. A field is not "consumed" in the abstract — it is consumed BY a
# surface, and which surface matters.
# ---------------------------------------------------------------------------
SPA = "app/ui/static"   # the operator console
CLI = "cli"             # scaffold_cli
OWUI = "pipelines"      # the OWUI chat surfaces

ALL_SURFACES = (SPA, CLI, OWUI)


# ---------------------------------------------------------------------------
# GET /exec/status/{job_id} — the per-node dicts built in
# app/modules/execution_handler.py::execution_status
# ---------------------------------------------------------------------------
# field -> the surfaces REQUIRED to read it.
#
# "at least one consumer somewhere" is too weak to be the rule, and the field
# this module is named after proves it: `failure_reason` was read by the CLI
# the whole time it was missing from the console. A gate asking only "does
# anybody read this" would have gone green through the entire three months the
# bug existed. The unit of truth is (field, surface).
EXEC_STATUS_NODE_OPERATOR_FIELDS: dict[str, frozenset[str]] = {
    "node_key":        frozenset({SPA, CLI}),
    "title":           frozenset({SPA, CLI}),
    "status":          frozenset({SPA, CLI}),
    "execution_order": frozenset({SPA}),
    "depends_on":      frozenset({SPA, CLI}),
    # Rendered as the ✅/⏳ dependency marker in the OWUI node table only.
    "deps_met":        frozenset({OWUI}),
    "actionable":      frozenset({SPA, CLI}),
    "assigned_model":  frozenset({SPA, CLI}),
    # §17.450 → §17.1007. The field this whole gate exists because of: on the
    # wire since §17.450, rendered by the CLI, dropped by the SPA for three
    # months. Requiring SPA here is the assertion that would have caught it.
    "failure_reason":  frozenset({SPA, CLI}),
    "is_deliverable":  frozenset({SPA}),
    "confidence":      frozenset({SPA, CLI}),
    "tool":            frozenset({SPA, CLI}),
    "started_at":      frozenset({SPA}),
    "completed_at":    frozenset({SPA}),
}

# Machine-only fields: consumed by other server code, or structural. Each needs
# a reason, checked by the test — "we don't render it" is not one.
EXEC_STATUS_NODE_INTERNAL_FIELDS: dict[str, str] = {}


# ---------------------------------------------------------------------------
# GET /jobs/{job_id} — app/schemas.py::JobDetailResponse
# ---------------------------------------------------------------------------
# A Pydantic response model, so the producer scan reads `model_fields` rather
# than parsing a dict literal. Extending the gate here immediately found two
# more of exactly the drift it was written for: `parent_job_id` and
# `component_index` were serialised on every job-detail read and rendered by
# NOTHING — an operator looking at a component job could not see that it
# belonged to an umbrella, or which part of it this was. §17.1008 wires them.
JOB_DETAIL_OPERATOR_FIELDS: dict[str, frozenset[str]] = {
    "id":                  frozenset({SPA, CLI, OWUI}),
    "title":               frozenset({SPA, CLI, OWUI}),
    "status":              frozenset({SPA, CLI, OWUI}),
    "input_text":          frozenset({SPA}),
    "refined_brief":       frozenset({SPA, OWUI}),
    "feasibility":         frozenset({SPA, CLI, OWUI}),
    # §17.843 — the operator's gate answers, echoed back from the server.
    "user_feedback":       frozenset({SPA}),
    "deliverable_kind":    frozenset({SPA}),
    "has_compiled_output": frozenset({SPA}),
    "node_count":          frozenset({SPA, CLI, OWUI}),
    "created_at":          frozenset({SPA, CLI}),
    "updated_at":          frozenset({SPA, OWUI}),
    "completed_at":        frozenset({SPA}),
    # §17.1008 — the decomposition breadcrumb. Produced since umbrellas
    # existed; read by no surface until now.
    "parent_job_id":       frozenset({SPA}),
    "component_index":     frozenset({SPA}),
    "metadata":            frozenset({SPA, OWUI}),
}

JOB_DETAIL_INTERNAL_FIELDS: dict[str, str] = {}


# ---------------------------------------------------------------------------
# GET /assist/{session_id}/steps — assist_agent::list_steps rows
# ---------------------------------------------------------------------------
ASSIST_STEP_OPERATOR_FIELDS: dict[str, frozenset[str]] = {
    "node_key":        frozenset({SPA}),
    "title":           frozenset({SPA}),
    "step_status":     frozenset({SPA}),
    "node_status":     frozenset({SPA}),
    "execution_order": frozenset({SPA}),
    "has_guidance":    frozenset({SPA}),
    # §17.1007 — the phase chunking the walkthrough badge renders.
    "phase":           frozenset({SPA}),
    "phase_total":     frozenset({SPA}),
    "phase_pos":       frozenset({SPA}),
    "phase_size":      frozenset({SPA}),
}

ASSIST_STEP_INTERNAL_FIELDS: dict[str, str] = {
    "depends_on": (
        "Selected only to feed dag_phases.compute_phases() server-side; the "
        "client renders the derived phase numbers, never the raw edges."
    ),
}


# ---------------------------------------------------------------------------
# GET /jobs — app/schemas.py::JobSummary (list rows)
# ---------------------------------------------------------------------------
# The highest-traffic operator payload after job detail: every dashboard tile,
# every filtered list, the CLI's `jobs` table and the OWUI listing read these.
JOB_SUMMARY_OPERATOR_FIELDS: dict[str, frozenset[str]] = {
    "id":              frozenset({SPA, CLI, OWUI}),
    "title":           frozenset({SPA, CLI, OWUI}),
    "status":          frozenset({SPA, CLI, OWUI}),
    "node_count":      frozenset({SPA, CLI, OWUI}),
    "created_at":      frozenset({SPA, CLI}),
    "updated_at":      frozenset({SPA, OWUI}),
    "completed_at":    frozenset({SPA}),
    # §17.525 decomposition links, wired into the SPA by §17.1008.
    "parent_job_id":   frozenset({SPA}),
    "component_index": frozenset({SPA}),
}

JOB_SUMMARY_INTERNAL_FIELDS: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Registry consumed by tests/test_operator_field_inventory.py
# ---------------------------------------------------------------------------
# `kind` tells the test how to find the producer's field names:
#   "dict_literal" — ast-parse the named function and take the payload dict
#   "pydantic"     — read the model's declared fields
PAYLOADS: dict[str, dict] = {
    "exec_status_node": {
        "kind": "dict_literal",
        "producer": "app/modules/execution_handler.py",
        "function": "execution_status",
        "operator_fields": EXEC_STATUS_NODE_OPERATOR_FIELDS,
        "internal_fields": EXEC_STATUS_NODE_INTERNAL_FIELDS,
    },
    "job_detail": {
        "kind": "pydantic",
        "model": "JobDetailResponse",
        "operator_fields": JOB_DETAIL_OPERATOR_FIELDS,
        "internal_fields": JOB_DETAIL_INTERNAL_FIELDS,
    },
    "job_summary": {
        "kind": "pydantic",
        "model": "JobSummary",
        "operator_fields": JOB_SUMMARY_OPERATOR_FIELDS,
        "internal_fields": JOB_SUMMARY_INTERNAL_FIELDS,
    },
    "assist_step": {
        # `list_steps` projects SQL columns (`dict(r)` over a row mapping) and
        # then merges in the computed phase keys. §17.1008 left this
        # consumer-only, declining to parse SQL. §17.1009 reconsidered: a
        # GENERAL SQL parser would be brittle, but this shape is narrow — an
        # explicit column list whose output names are the alias after `AS` or
        # the bare column. A projection the scan cannot read (SELECT *, a
        # dynamic list) fails loudly instead of passing quietly, which is the
        # difference between a stated limitation and a hole.
        "kind": "sql_projection",
        "producer": "app/modules/assist_agent.py",
        "function": "list_steps",
        "operator_fields": ASSIST_STEP_OPERATOR_FIELDS,
        "internal_fields": ASSIST_STEP_INTERNAL_FIELDS,
    },
}

ALL_OPERATOR_FIELDS = frozenset(
    f for spec in PAYLOADS.values() for f in spec["operator_fields"]
)
