"""
Patch 5: confidence column — explicit NULL with structured TODO
Sets dag_nodes.confidence to NULL explicitly after verification,
adds TODO comment and structured log event.
"""
import sys

FILE = "/home/aedefruscio/scaffold-engine/app/modules/execution_agent.py"

with open(FILE) as f:
    src = f.read()

original = src

# --- 5. After verification, set confidence=NULL explicitly and log ---
# The verification block:
#     verified, reason, confidence = True, "skipped", 1.0
#     if not skip_verify:
#         verified, reason, confidence = await _verify_output(title, output, verifier_model)
#         if not verified:
#             logger.warning("node_verification_failed: node='%s' reason=%s", title, reason)
#
#     # 8. Persist

src = src.replace(
    '''    # 7. Verify output
    verified, reason, confidence = True, "skipped", 1.0
    if not skip_verify:
        verified, reason, confidence = await _verify_output(title, output, verifier_model)
        if not verified:
            logger.warning("node_verification_failed: node='%s' reason=%s", title, reason)

    # 8. Persist''',
    '''    # 7. Verify output
    verified, reason, confidence = True, "skipped", 1.0
    if not skip_verify:
        verified, reason, confidence = await _verify_output(title, output, verifier_model)
        if not verified:
            logger.warning("node_verification_failed: node='%s' reason=%s", title, reason)

    # TODO: Populate confidence via logprob extraction when verification escalation is implemented
    # Set confidence column to NULL explicitly — verifier confidence is not yet trusted
    await db.execute(
        text("UPDATE dag_nodes SET confidence = NULL WHERE id = :nid"),
        {"nid": str(node_id)},
    )
    await db.commit()
    logger.info(
        "verification_complete",
        extra=dict(
            event="verification_complete",
            node_key=node["node_key"],
            verified=verified,
            confidence=None,
        ),
    )

    # 8. Persist''',
)

if src == original:
    print("ERROR: No replacements applied — source text did not match.")
    sys.exit(1)

with open(FILE, "w") as f:
    f.write(src)

print("PATCH 5 APPLIED: confidence column — explicit NULL")
print("  - SET confidence = NULL after verification")
print("  - Added TODO comment for future logprob extraction")
print("  - Added verification_complete structured log event")
