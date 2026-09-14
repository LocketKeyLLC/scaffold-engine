# Known-hit fixture (§17.1057): one violation, one clean call.
async def g(model_router, messages, tool):
    await model_router.tool_call(messages, [tool], role="model_general", max_tokens=768)          # HIT: no think=False
    await model_router.tool_call(messages, [tool], role="model_general", max_tokens=768, think=False)
