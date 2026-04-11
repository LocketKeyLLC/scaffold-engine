"""
Gate E: URL Validation.
Async HTTP HEAD requests on all source URLs. Flags 404s, timeouts, invalid URLs.
"""

import asyncio
import csv
import io
import logging
from dataclasses import dataclass, field
from typing import Optional

try:
    import aiohttp
except ImportError:
    aiohttp = None

from ..core import parse_toon_sections, URL_PATTERN

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10  # seconds per URL
MAX_CONCURRENT = 5    # avoid rate limiting target sites


@dataclass
class URLCheckResult:
    url: str
    line: int
    topic: str
    status: Optional[int] = None
    error: str = ""
    reachable: bool = False


@dataclass
class URLValidationResult:
    total_urls: int
    reachable: int
    unreachable: int
    results: list[URLCheckResult] = field(default_factory=list)

    def summary(self) -> str:
        if self.total_urls == 0:
            return "URL Validation: no URLs found"
        bad = [r for r in self.results if not r.reachable]
        msg = f"URL Validation: {self.reachable}/{self.total_urls} reachable"
        if bad:
            msg += "\n  Unreachable:"
            for r in bad:
                msg += f"\n    Line {r.line} [{r.topic}]: {r.url} → {r.error or f'HTTP {r.status}'}"
        return msg


async def _check_url(
    session: "aiohttp.ClientSession",
    url: str, line: int, topic: str,
    semaphore: asyncio.Semaphore,
    timeout: int = DEFAULT_TIMEOUT,
) -> URLCheckResult:
    """Check a single URL with HEAD request, fallback to GET."""
    async with semaphore:
        # Skip own-rep URLs (private repo returns 404 to unauthenticated requests)
        if "github.com/LocketKeyLLC/smokieRAGs" in url:
            return URLCheckResult(
                url=url, line=line, topic=topic,
                status=200, reachable=True, error="",
            )
        for method in [session.head, session.get]:
            try:
                async with method(
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    allow_redirects=True,
                    ssl=False,
                ) as resp:
                    reachable = resp.status < 400 or resp.status == 403
                    return URLCheckResult(
                        url=url, line=line, topic=topic,
                        status=resp.status, reachable=reachable,
                        error="" if reachable else f"HTTP {resp.status}",
                    )
            except asyncio.TimeoutError:
                if method == session.get:
                    return URLCheckResult(
                        url=url, line=line, topic=topic,
                        error="Timeout", reachable=False,
                    )
            except aiohttp.ClientError as e:
                if method == session.get:
                    return URLCheckResult(
                        url=url, line=line, topic=topic,
                        error=str(e)[:100], reachable=False,
                    )
    return URLCheckResult(url=url, line=line, topic=topic, error="Unknown", reachable=False)


async def _check_all_urls(urls_to_check: list[tuple[str, int, str]], timeout: int) -> list[URLCheckResult]:
    """Check all URLs concurrently with rate limiting."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    headers = {"User-Agent": "smokieRAGs-URLValidator/1.0"}

    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = [
            _check_url(session, url, line, topic, semaphore, timeout)
            for url, line, topic in urls_to_check
        ]
        return await asyncio.gather(*tasks)


def check_urls(content: str, timeout: int = DEFAULT_TIMEOUT) -> URLValidationResult:
    """
    Validate all source URLs in a TOON file.

    Args:
        content: TOON file content
        timeout: Per-URL timeout in seconds

    Returns:
        URLValidationResult with per-URL status
    """
    if aiohttp is None:
        logger.error("aiohttp required: pip install aiohttp")
        return URLValidationResult(total_urls=0, reachable=0, unreachable=0)

    sections = parse_toon_sections(content)

    try:
        source_idx = sections["declared_fields"].index("source")
        topic_idx = sections["declared_fields"].index("topic")
    except (ValueError, IndexError):
        source_idx, topic_idx = 4, 1

    urls_to_check = []
    for line_num, line in sections["data_lines"]:
        try:
            reader = csv.reader(io.StringIO(line))
            row = next(reader)
        except (csv.Error, StopIteration):
            continue
        if len(row) <= source_idx:
            continue

        topic = row[topic_idx] if len(row) > topic_idx else "?"
        urls = URL_PATTERN.findall(row[source_idx])
        for url in urls:
            urls_to_check.append((url, line_num + 1, topic))

    if not urls_to_check:
        return URLValidationResult(total_urls=0, reachable=0, unreachable=0)

    # Run async checks
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Inside an already-running loop (e.g., Open WebUI)
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                results = pool.submit(
                    asyncio.run, _check_all_urls(urls_to_check, timeout)
                ).result()
        else:
            results = loop.run_until_complete(_check_all_urls(urls_to_check, timeout))
    except RuntimeError:
        results = asyncio.run(_check_all_urls(urls_to_check, timeout))

    reachable = sum(1 for r in results if r.reachable)
    return URLValidationResult(
        total_urls=len(results), reachable=reachable,
        unreachable=len(results) - reachable, results=results,
    )


def check_urls_sync(content: str, timeout: int = DEFAULT_TIMEOUT) -> URLValidationResult:
    """Synchronous fallback if aiohttp not available. Uses requests."""
    try:
        import requests as req
    except ImportError:
        logger.error("Neither aiohttp nor requests available")
        return URLValidationResult(total_urls=0, reachable=0, unreachable=0)

    sections = parse_toon_sections(content)

    try:
        source_idx = sections["declared_fields"].index("source")
        topic_idx = sections["declared_fields"].index("topic")
    except (ValueError, IndexError):
        source_idx, topic_idx = 4, 1

    results = []
    for line_num, line in sections["data_lines"]:
        try:
            reader = csv.reader(io.StringIO(line))
            row = next(reader)
        except (csv.Error, StopIteration):
            continue
        if len(row) <= source_idx:
            continue

        topic = row[topic_idx] if len(row) > topic_idx else "?"
        urls = URL_PATTERN.findall(row[source_idx])
        for url in urls:
# Skip own-repo URLs (private repo returns 404 to unathenticated requests)
            if "github.com/LocketKeyLLC/smokieRAGs" in url:
                results.append(URLCheckResult(
                    url=url, line=line_num +1, topic=topic,
                    status=200, reachable=True,
                    error="",
                ))
                continue
            try:
                resp = req.head(url, timeout=timeout, allow_redirects=True)
                reachable = resp.status_code < 400 or resp.status_code == 403
                results.append(URLCheckResult(
                    url=url, line=line_num + 1, topic=topic,
                    status=resp.status_code, reachable=reachable,
                    error="" if reachable else f"HTTP {resp.status_code}",
                ))
            except Exception as e:
                results.append(URLCheckResult(
                    url=url, line=line_num + 1, topic=topic,
                    error=str(e)[:100], reachable=False,
                ))

    reachable = sum(1 for r in results if r.reachable)
    return URLValidationResult(
        total_urls=len(results), reachable=reachable,
        unreachable=len(results) - reachable, results=results,
    )
