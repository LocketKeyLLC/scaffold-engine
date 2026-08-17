"""Native chat dispatcher for the OpenAI surface (§17.790+).

Ports the OWUI pipeline's intelligence into the engine so ``/v1/chat/completions``
routes a plain message through NL command routing / confirm-cards (§17.790) and,
in later phases, conversational triage + the ``/go``→``/confirm`` auto-chain.

The public entry point is :func:`app.native_chat.dispatch.route`.
"""
from app.native_chat.dispatch import route

__all__ = ["route"]
