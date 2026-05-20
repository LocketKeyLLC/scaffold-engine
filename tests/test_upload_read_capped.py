"""§17.180 — unit tests for app.utils.upload.read_capped.

Validates the bounded streaming reader used by the /research/pdf endpoint.
The fix replaces a single ``await file.read()`` (which buffers the entire
upload before any cap check) with a chunked accumulator that aborts mid-
stream once the cap is exceeded. The tests below cover:

  * under-cap reads return the full payload (no behavior regression),
  * over-cap reads raise HTTPException(413),
  * over-cap reads stop reading shortly after the cap (RSS-bound guarantee),
  * argument validation rejects nonpositive caps and chunk sizes,
  * empty uploads return b"" without raising.
"""
from __future__ import annotations

from io import BytesIO

import pytest
from fastapi import HTTPException

from app.utils.upload import read_capped


class _TrackingUploadFile:
    """Async-readable stand-in that records total bytes read.

    Mimics enough of starlette.datastructures.UploadFile for read_capped
    (which only needs ``async read(size) -> bytes``). Tracking the byte
    count lets us assert the streaming-abort guarantee directly rather
    than trust the implementation.
    """

    def __init__(self, data: bytes):
        self._buf = BytesIO(data)
        self.bytes_read = 0

    async def read(self, size: int = -1) -> bytes:
        chunk = self._buf.read(size if size > 0 else -1)
        self.bytes_read += len(chunk)
        return chunk


async def test_under_cap_returns_full_payload():
    data = b"X" * 1000
    file = _TrackingUploadFile(data)
    result = await read_capped(file, cap_bytes=2000, chunk_bytes=256)
    assert result == data
    assert file.bytes_read == len(data)


async def test_exactly_at_cap_is_accepted():
    cap = 4096
    data = b"Z" * cap
    file = _TrackingUploadFile(data)
    result = await read_capped(file, cap_bytes=cap, chunk_bytes=512)
    assert result == data


async def test_one_byte_over_cap_raises_413():
    cap = 4096
    data = b"Y" * (cap + 1)
    file = _TrackingUploadFile(data)
    with pytest.raises(HTTPException) as exc:
        await read_capped(file, cap_bytes=cap, chunk_bytes=512, label="PDF")
    assert exc.value.status_code == 413
    assert "PDF" in exc.value.detail
    assert "cap" in exc.value.detail.lower()


async def test_huge_upload_bounded_by_cap_plus_one_chunk():
    """RSS-bound guarantee — the whole point of §17.180.

    A 10 MB upload against a 512 KB cap with 64 KB chunks should stop
    reading shortly after the cap, NOT after the full payload."""
    cap = 512 * 1024
    chunk = 64 * 1024
    data = b"A" * (10 * 1024 * 1024)
    file = _TrackingUploadFile(data)
    with pytest.raises(HTTPException) as exc:
        await read_capped(file, cap_bytes=cap, chunk_bytes=chunk)
    assert exc.value.status_code == 413
    # Strict bound: at most cap + one chunk worth of bytes was read.
    assert file.bytes_read <= cap + chunk, (
        f"streaming abort failed — read {file.bytes_read} bytes against "
        f"cap+chunk = {cap + chunk}"
    )
    # And critically, far less than the full payload was buffered.
    assert file.bytes_read < len(data)


async def test_empty_upload_returns_empty_bytes_without_raising():
    file = _TrackingUploadFile(b"")
    result = await read_capped(file, cap_bytes=1024, chunk_bytes=256)
    assert result == b""


async def test_rejects_nonpositive_cap():
    file = _TrackingUploadFile(b"x")
    with pytest.raises(ValueError):
        await read_capped(file, cap_bytes=0, chunk_bytes=256)
    with pytest.raises(ValueError):
        await read_capped(file, cap_bytes=-1, chunk_bytes=256)


async def test_rejects_nonpositive_chunk():
    file = _TrackingUploadFile(b"x")
    with pytest.raises(ValueError):
        await read_capped(file, cap_bytes=1024, chunk_bytes=0)


async def test_label_appears_in_413_detail():
    """Detail message should identify which upload-class exceeded the cap."""
    file = _TrackingUploadFile(b"q" * 100)
    with pytest.raises(HTTPException) as exc:
        await read_capped(file, cap_bytes=10, chunk_bytes=4, label="manifest")
    assert "manifest" in exc.value.detail
