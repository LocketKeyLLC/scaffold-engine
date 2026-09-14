# Known-hit fixture (§17.1057): two violations, two clean calls.
async def f(session_id, req, db, agent):
    await agent.capture_assistant_reply(session_id=session_id, kind="fix", content="x", db=db)   # HIT: no node_key
    await agent.capture_assistant_reply(session_id=session_id, node_key="T1", kind="fix", content="x", db=db)
    await add_step(session_id=session_id, request=req, db=db)                                    # HIT: no before_node_key
    await add_step(session_id=session_id, request=req, before_node_key="T1", db=db)
