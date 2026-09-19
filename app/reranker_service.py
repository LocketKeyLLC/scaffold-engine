"""§17.1065 — the reranker sidecar: one endpoint, the engine's own model.

Run from the engine image as its own container (compose profile
``reranker``): ``uvicorn app.reranker_service:app --port 80``. Speaks the
text-embeddings-inference ``POST /rerank`` contract so the orchestrator's
``rerank_http`` client works against either. Exists because the in-process
CrossEncoder saturates the orchestrator's CPU (§17.704) and TEI cannot load
this engine's reranker architecture; moving the same model into a sibling
process is the change that helps without changing a single score.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.rerankers import _get_cross_encoder, predict_raw

logger = logging.getLogger("scaffold")
app = FastAPI(title="scaffold reranker sidecar", docs_url=None, redoc_url=None)


class RerankRequest(BaseModel):
    query: str
    texts: list[str] = Field(default_factory=list)
    raw_scores: bool = True
    truncate: bool = True


@app.on_event("startup")
async def _warm() -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _get_cross_encoder)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok" if _get_cross_encoder() is not None else "loading"}


@app.post("/rerank")
async def rerank(req: RerankRequest) -> list[dict]:
    model = _get_cross_encoder()
    if model is None:
        raise HTTPException(status_code=503, detail="reranker model not loaded")
    if not req.texts:
        return []
    pairs = [[req.query, t] for t in req.texts]
    loop = asyncio.get_running_loop()
    t0 = time.monotonic()
    # §17.1124 — honour ``raw_scores`` the way TEI does: hand back the raw
    # logit so the client's single sigmoid is the only activation applied.
    scorer = (lambda: predict_raw(model, pairs)) if req.raw_scores else (lambda: model.predict(pairs))
    scores = await loop.run_in_executor(None, scorer)
    logger.info("reranker_sidecar_scored: docs=%d elapsed_ms=%.0f", len(pairs), (time.monotonic() - t0) * 1000)
    return [{"index": i, "score": float(s)} for i, s in enumerate(scores)]
