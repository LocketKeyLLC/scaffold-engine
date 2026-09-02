"""§17.900 — HuggingFace Inference provider.

HF's Inference Router (``https://router.huggingface.co/v1``) serves the OpenAI
chat-completions dialect, so this is a thin subclass of ``OpenAIProvider``:
every wire concern — streaming SSE shape, native tool-call translation, the
error envelope, reasoning-model parameter quirks — is already solved there and
must not be forked. Only three things differ, and they are exactly the three
overridden below: the credential, the HTTP client (different base URL and
timeout), and the provider name.

Two HuggingFace paths exist in this engine and they are NOT the same thing:

  * **Downloaded** — ``ollama pull hf.co/<user>/<repo>`` pulls a GGUF straight
    from the Hub into Ollama. The result is an ordinary Ollama tag, served by
    OllamaProvider, listed by ``/models/available`` like any other local model.
    No credential, no new runtime, and it works on this CPU-only host.
  * **Hosted** — this provider. Inference runs on HF's infrastructure against
    your token; nothing is downloaded.

``supports_embeddings`` is False on purpose. The router's embeddings coverage is
model-dependent, and the embedder role is a locked 512-dim singleton
(§17.483) — advertising support we cannot guarantee would let an operator bind
the embedder to a provider that silently returns a different dim and corrupt the
index. Chat/streaming/tools are the supported surface.
"""
from __future__ import annotations

import logging

import httpx

from app.providers import register
from app.providers.base import ProviderUnavailableError
from app.providers.openai import OpenAIProvider

logger = logging.getLogger("scaffold.providers.huggingface")


class HuggingFaceProvider(OpenAIProvider):
    """HuggingFace Inference Router (OpenAI-compatible)."""

    name = "huggingface"
    supports_chat = True
    supports_streaming = True
    supports_native_tools = True
    # See module docstring — deliberately not advertised.
    supports_embeddings = False
    # The router does not implement OpenAI's json_schema response_format across
    # models; §17.773's gate is capability-keyed, so declaring False routes
    # these calls through the post-hoc json_repair path instead of sending a
    # constraint the backend will ignore.
    supports_structured_outputs = False

    @staticmethod
    def _auth_headers() -> dict[str, str]:
        from app.config import settings
        key = settings.huggingface_api_key.get_secret_value()
        if not key:
            raise ProviderUnavailableError(
                "huggingface_api_key is empty. Add a HuggingFace token in "
                "Settings → Connections (or set HUGGINGFACE_API_KEY), OR point "
                "the role's provider away from 'huggingface'. To run a HF model "
                "LOCALLY instead, pull its GGUF into Ollama: "
                "`ollama pull hf.co/<user>/<repo>`."
            )
        return {"Authorization": f"Bearer {key}"}

    @staticmethod
    def _client() -> httpx.AsyncClient:
        from app.utils.http_clients import get_hf_inference_client
        return get_hf_inference_client()


register("huggingface", HuggingFaceProvider())
