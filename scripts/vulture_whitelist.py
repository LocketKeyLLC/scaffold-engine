# §17.1057 — names vulture must treat as used: patch targets and re-exports the
# assist clusters keep on purpose (§17.856). Extend when a report is a false
# positive; `make dead-code` reads this file.
_ = None

# §17.856 re-exports (assist_agent.<name> / assist_guide.<name>) that nothing
# inside those modules calls — callers and tests reach them through the
# re-exporting module (tests/test_assist_agent_nl.py awaits
# assist_agent.drain_derive_tasks()). Dropping the import would break that
# surface; vulture cannot see the indirect use.
_.drain_derive_tasks
_.drain_consolidate_tasks
_.VERBOSITY_LEVELS
_._ERROR_SUMMARY_MAX   # job_utils re-exports job_state's cap; tests/test_job_utils.py reads it there
