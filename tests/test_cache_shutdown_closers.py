"""§17.855 (audit B7) — the four Redis-backed caches expose a shutdown closer
that closes the lazily-opened aioredis client. Safe/no-op when none is open."""
import pytest

from app.utils.fetch_cache import close_fetch_cache
from app.utils.llm_response_cache import close_llm_response_cache
from app.utils.rag_result_cache import close_rag_result_cache
from app.utils.embedding_cache import close_embedding_cache


@pytest.mark.smoke
@pytest.mark.parametrize("closer", [
    close_fetch_cache, close_llm_response_cache,
    close_rag_result_cache, close_embedding_cache,
])
async def test_closer_is_safe_noop_when_no_client(closer):
    # No cache singleton / no open redis → must not raise.
    await closer()
    await closer()  # idempotent
