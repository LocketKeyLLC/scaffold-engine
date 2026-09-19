"""§17.1065 — the HTTP reranker backend (sidecar): same normalisation, honest fallback."""
from unittest.mock import MagicMock, patch

from app import rerankers
from app.config import settings


def _client_returning(rows, status=200):
    resp = MagicMock(); resp.status_code = status; resp.json.return_value = rows
    resp.raise_for_status = MagicMock(side_effect=None if status < 400 else RuntimeError(f"HTTP {status}"))
    client = MagicMock(); client.__enter__.return_value = client; client.__exit__.return_value = False
    client.post.return_value = resp
    return client


def test_tei_scores_are_normalised_and_ordered():
    rows = [{"index": 2, "score": 4.0}, {"index": 0, "score": -3.0}, {"index": 1, "score": 0.5}]
    with patch("httpx.Client", return_value=_client_returning(rows)), \
         patch.object(settings, "reranker_url", "http://tei:80"):
        out = rerankers.rerank_http("q", ["a", "b", "c"], top_k=2)
    assert out is not None and out.backend == "HTTP"
    assert [i.index for i in out.items] == [2, 1]
    # the SAME normaliser as the in-process path for the configured model,
    # so a backend swap cannot move the confidence threshold
    _, normalize = rerankers.get_score_range_info(settings.model_reranker)
    expected = normalize([-3.0, 0.5, 4.0])
    assert [i.score for i in out.items] == [expected[2], expected[1]]


def test_dispatch_prefers_tei_then_falls_back():
    with patch.object(settings, "reranker_backend", "http"), \
         patch.object(rerankers, "rerank_http", return_value=None) as tei, \
         patch.object(rerankers, "rerank_cross_encoder", return_value=None) as ce:
        out = rerankers.rerank("q", ["a", "b"], top_k=2)
    assert tei.called and ce.called and out.backend == "RRF"
    with patch.object(settings, "reranker_backend", "local"), \
         patch.object(rerankers, "rerank_http") as tei, \
         patch.object(rerankers, "rerank_cross_encoder", return_value=None):
        rerankers.rerank("q", ["a"], top_k=1)
    assert not tei.called


def test_tei_failure_returns_none_not_raise():
    with patch("httpx.Client", return_value=_client_returning([], status=503)), \
         patch.object(settings, "reranker_url", "http://tei:80"):
        assert rerankers.rerank_http("q", ["a"]) is None
    assert rerankers.rerank_http("q", []).items == []


def test_http_payload_uses_the_shared_pair_template(monkeypatch):
    """§17.1124 — the sidecar sees the SAME pair shape as the in-process path:
    instruct template for Qwen3, plain pair for every other family."""
    rows = [{"index": 0, "score": 1.0}]
    for name, wrapped in (("tomaarsen/Qwen3-Reranker-0.6B-seq-cls", True), ("BAAI/bge-reranker-base", False)):
        client = _client_returning(rows)
        with patch("httpx.Client", return_value=client), \
             patch.object(settings, "reranker_url", "http://tei:80"), \
             patch.object(settings, "model_reranker", name):
            rerankers.rerank_http("q", ["a"], top_k=1)
        payload = client.post.call_args.kwargs["json"]
        assert payload["raw_scores"] is True
        assert ("<Document>: a" in payload["texts"][0]) is wrapped, name
        assert ("<Query>: q" in payload["query"]) is wrapped, name


def test_sidecar_honours_raw_scores():
    """The bundled sidecar must hand back RAW logits when asked (TEI contract),
    or the client's sigmoid lands on already-sigmoided scores (double sigmoid)."""
    import asyncio
    import torch
    from app import reranker_service
    model = MagicMock(); model.predict.return_value = [3.0]
    with patch.object(reranker_service, "_get_cross_encoder", return_value=model):
        out = asyncio.run(reranker_service.rerank(reranker_service.RerankRequest(query="q", texts=["a"], raw_scores=True)))
    assert out == [{"index": 0, "score": 3.0}]
    assert isinstance(model.predict.call_args.kwargs.get("activation_fn"), torch.nn.Identity)
    with patch.object(reranker_service, "_get_cross_encoder", return_value=model):
        asyncio.run(reranker_service.rerank(reranker_service.RerankRequest(query="q", texts=["a"], raw_scores=False)))
    assert "activation_fn" not in model.predict.call_args.kwargs
