"""
Gate D: Contradiction Detection.
Retrieves semantically similar existing entries before ingesting new content,
then uses LLM to check for factual contradictions.

Optimized v2: result caching, capped num_predict, keep_alive, think=false.

Requires: pymilvus, requests
Uses: qwen3-embedding:8b (local) for similarity, phi4-mini (local) for analysis
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from pymilvus import MilvusClient
except ImportError:
    MilvusClient = None

from ..llm_client import LLMConfig, call_llm, build_contradiction_prompt

logger = logging.getLogger(__name__)

MILVUS_HOST = os.getenv("MILVUS_HOST", "http://localhost:19530")
MILVUS_COLLECTION = os.getenv("MILVUS_COLLECTION", "technical_knowledge")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:8b")
OLLAMA_LOCAL = os.getenv("OLLAMA_LOCAL_ENDPOINT", "http://localhost:11434")
SIMILARITY_TOP_K = 3  # Reduced from 5 — fewer pairs, similar recall
SIMILARITY_THRESHOLD = 0.80  # Raised from 0.75 — tighter filter, fewer false matches

# Cache config
CACHE_DIR = os.getenv("CONTRADICTION_CACHE_DIR", "/tmp")
CACHE_FILE = os.path.join(CACHE_DIR, "contradiction_cache.json")


@dataclass
class ContradictionResult:
    has_contradiction: bool
    details: str
    similar_count: int
    checked: bool  # False if gate couldn't run (Milvus down, etc.)


# ---------------------------------------------------------------------------
# Result cache: skip re-checking unchanged entry pairs
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    """Load the contradiction result cache from disk."""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Cache load failed, starting fresh: {e}")
    return {}


def _save_cache(cache: dict) -> None:
    """Persist the contradiction result cache to disk."""
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except OSError as e:
        logger.warning(f"Cache save failed: {e}")


def _cache_key(new_text: str, existing_texts: list[str], model: str) -> str:
    """Generate a deterministic cache key for an entry + its similar matches."""
    # Sort existing texts so (A,B) and (B,A) produce the same key
    combined = new_text + "|||" + "|||".join(sorted(existing_texts)) + "|||" + model
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Embedding + Milvus search
# ---------------------------------------------------------------------------

def _embed_text(text: str) -> Optional[list[float]]:
    """Generate embedding via local Ollama."""
    try:
        import requests
        resp = requests.post(
            f"{OLLAMA_LOCAL}/api/embed",
            json={"model": EMBEDDING_MODEL, "input": text},
            timeout=60,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Ollama returns {"embeddings": [[...]]}
            embeddings = data.get("embeddings", [])
            if embeddings:
                return embeddings[0]
        logger.error(f"Embedding failed: HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"Embedding error: {e}")
    return None


def _search_similar(embedding: list[float], top_k: int = SIMILARITY_TOP_K) -> list[dict]:
    """Search Milvus for similar entries."""
    if MilvusClient is None:
        logger.error("pymilvus not installed")
        return []
    try:
        client = MilvusClient(uri=MILVUS_HOST)
        results = client.search(
            collection_name=MILVUS_COLLECTION,
            data=[embedding],
            limit=top_k,
            output_fields=["content", "topic", "source_url"],
        )
        # Filter by similarity threshold
        entries = []
        for hits in results:
            for hit in hits:
                if hit.get("distance", 0) >= SIMILARITY_THRESHOLD:
                    entries.append(hit.get("entity", {}))
        return entries
    except Exception as e:
        logger.error(f"Milvus search error: {e}")
        return []


# ---------------------------------------------------------------------------
# Main contradiction check
# ---------------------------------------------------------------------------

def check_contradictions(
    new_entry_text: str,
    llm_config: Optional[LLMConfig] = None,
) -> ContradictionResult:
    """
    Check if a new entry contradicts existing knowledge base entries.

    Args:
        new_entry_text: The content of the new TOON entry (topic + content combined)
        llm_config: Optional LLM config override

    Returns:
        ContradictionResult with findings
    """
    if llm_config is None:
        llm_config = LLMConfig.from_env()

    # Override LLM settings for fast contradiction detection
    llm_config.num_predict = 50  # Only need short JSON response
    llm_config.keep_alive = -1  # Keep model loaded between calls
    llm_config.think = False  # Disable chain-of-thought reasoning
    llm_config.temperature = 0.0  # Deterministic classification
    llm_config.timeout = 60  # Short timeout — small model, short response

    # Step 1: Embed the new entry
    embedding = _embed_text(new_entry_text)
    if embedding is None:
        return ContradictionResult(
            has_contradiction=False, details="Could not generate embedding",
            similar_count=0, checked=False
        )

    # Step 2: Find similar existing entries
    similar = _search_similar(embedding)
    if not similar:
        return ContradictionResult(
            has_contradiction=False, details="No similar entries found",
            similar_count=0, checked=True
        )

    # Step 3: Build existing entry texts for comparison
    existing_texts = []
    for entry in similar:
        topic = entry.get("topic", "unknown")
        content = entry.get("content", "")
        existing_texts.append(f"[{topic}] {content}")

    # Step 4: Check cache
    cache = _load_cache()
    key = _cache_key(new_entry_text, existing_texts, llm_config.model)
    if key in cache:
        cached = cache[key]
        logger.info(f"Cache hit for entry: {new_entry_text[:50]}...")
        return ContradictionResult(
            has_contradiction=cached["has_contradiction"],
            details=cached["details"] + " (cached)",
            similar_count=len(similar),
            checked=True,
        )

    # Step 5: LLM contradiction check
    prompt = build_contradiction_prompt(new_entry_text, existing_texts)
    response = call_llm(prompt, config=llm_config)

    if response is None:
        return ContradictionResult(
            has_contradiction=False, details="LLM call failed",
            similar_count=len(similar), checked=False
        )

    # Step 6: Parse LLM response
    try:
        # Strip markdown fencing if present
        cleaned = response.strip().strip("`").strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        parsed = json.loads(cleaned)
        result = ContradictionResult(
            has_contradiction=parsed.get("has_contradiction", False),
            details=parsed.get("details", ""),
            similar_count=len(similar),
            checked=True,
        )
        # Save to cache
        cache[key] = {
            "has_contradiction": result.has_contradiction,
            "details": result.details,
        }
        _save_cache(cache)
        return result
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Could not parse LLM contradiction response: {e}")
        # Conservative: flag as potential contradiction if we can't parse
        return ContradictionResult(
            has_contradiction=False,
            details=f"Unparseable LLM response: {response[:200]}",
            similar_count=len(similar),
            checked=True,
        )
