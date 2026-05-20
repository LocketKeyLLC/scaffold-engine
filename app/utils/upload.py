"""§17.180 — bounded streaming reader for UploadFile.

The default ``await UploadFile.read()`` consumes the entire upload before
the caller can decide whether to keep it. For endpoints that cap upload
size (e.g. ``/research/pdf`` with ``research_max_pdf_bytes``), this means
a hostile uploader can transiently inflate orchestrator RSS by the full
upload payload before being rejected by a post-read length check.

``read_capped`` reads in fixed-size chunks and short-circuits as soon as
the accumulated byte count exceeds the cap, bounding the peak inflation
to one chunk past the cap regardless of how large the actual upload is.
"""
from __future__ import annotations

from typing import Protocol

from fastapi import HTTPException


_DEFAULT_CHUNK_BYTES = 1 << 20  # 1 MiB


class _AsyncReadable(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


async def read_capped(
    file: _AsyncReadable,
    cap_bytes: int,
    *,
    chunk_bytes: int = _DEFAULT_CHUNK_BYTES,
    label: str = "upload",
) -> bytes:
    """Read ``file`` into memory, raising ``HTTPException(413)`` past ``cap_bytes``.

    Peak memory is bounded by ``cap_bytes + chunk_bytes`` — the chunk that
    tips the accumulator past the cap is discarded along with the rest
    before the exception propagates. Within the cap, the return value is
    byte-identical to ``await file.read()``.

    ``label`` appears in the 413 detail message ("PDF exceeds 20MB cap…").
    """
    if cap_bytes <= 0:
        raise ValueError(f"cap_bytes must be positive, got {cap_bytes}")
    if chunk_bytes <= 0:
        raise ValueError(f"chunk_bytes must be positive, got {chunk_bytes}")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(chunk_bytes)
        if not chunk:
            break
        total += len(chunk)
        if total > cap_bytes:
            cap_mb = cap_bytes // (1024 * 1024)
            raise HTTPException(
                status_code=413,
                detail=(
                    f"{label} exceeds {cap_mb}MB cap "
                    f"(stopped reading at {total} bytes)"
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)
