"""OpenAPI/Swagger ingestion for /research openapi:<url>.

Fetches an OpenAPI 3.0 or Swagger 2.0 spec, validates it, then emits one
entry per endpoint with path + method + description + params.
"""
import asyncio
import json
import logging
from typing import Any, Literal

import httpx
import yaml
from openapi_spec_validator import validate as _osv_validate
from openapi_spec_validator import (
    OpenAPIV2SpecValidator,
    OpenAPIV30SpecValidator,
    OpenAPIV31SpecValidator,
)
from openapi_spec_validator.validation.exceptions import (
    OpenAPIValidationError as _OSVValidationError,
)

try:
    from prance import ResolvingParser
    _PRANCE_AVAILABLE = True
except ImportError:  # pragma: no cover
    ResolvingParser = None  # type: ignore[assignment]
    _PRANCE_AVAILABLE = False

from app.config import settings

logger = logging.getLogger(__name__)

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}

# --- Content-truncation caps (#74) ------------------------------------------
# Per-field character caps keep each endpoint entry bounded so a single verbose
# spec doesn't blow the Milvus canonical_text field (65535 chars) or dominate
# RAG retrieval. Increase with care — larger caps mean fewer endpoints fit.
_DESC_CAP = 800        # operation description
_REQ_BODY_CAP = 300    # requestBody description
_PARAM_DESC_CAP = 200  # per-parameter description
_RESPONSE_CAP = 200    # per-response description
_TITLE_CAP = 200       # entry title


class OpenAPIFetchError(Exception):
    """Spec URL unreachable or returned non-2xx."""


class OpenAPIParseError(Exception):
    """Spec is not valid JSON/YAML or not a recognizable OpenAPI/Swagger doc."""


class OpenAPIValidationError(Exception):
    """Spec parsed but failed schema validation."""


async def _fetch_spec(url: str) -> dict[str, Any]:
    """Fetch spec URL and decode as JSON or YAML.

    Assumes the response body is UTF-8 (or httpx.Response.text-decodable) text.
    Binary spec encodings (e.g. Protobuf, MessagePack) are NOT supported —
    they would surface as ``OpenAPIParseError`` after failing both JSON and
    YAML parsing. If such encodings become relevant, add a dedicated decoder
    branch before the JSON/YAML attempts (#152).
    """
    # #76 — use shared pooled client (was ephemeral AsyncClient per call)
    from app.utils.http_clients import get_generic_http_client
    try:
        client = get_generic_http_client()
        r = await client.get(url, timeout=float(settings.openapi_timeout))
        r.raise_for_status()
        text = r.text
    except httpx.HTTPError as e:
        raise OpenAPIFetchError(f"Failed to fetch {url}: {e}") from e

    # Try JSON first (faster); fall back to YAML
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise OpenAPIParseError(f"Spec is neither JSON nor YAML: {e}") from e


def _validate_spec(spec: dict) -> Literal["openapi-3.0", "openapi-3.1", "swagger-2"]:
    """Detect spec version and validate with the matching backend.

    Returns one of: 'openapi-3.0', 'openapi-3.1', 'swagger-2'. Raises
    OpenAPIParseError if the spec has no recognizable version field, or
    OpenAPIValidationError if validation fails.
    """
    oa = str(spec.get("openapi", ""))
    sw = str(spec.get("swagger", ""))

    if oa.startswith("3.1"):
        version: Literal["openapi-3.0", "openapi-3.1", "swagger-2"] = "openapi-3.1"
        validator_cls = OpenAPIV31SpecValidator
    elif oa.startswith("3.0"):
        version = "openapi-3.0"
        validator_cls = OpenAPIV30SpecValidator
    elif sw.startswith("2."):
        version = "swagger-2"
        validator_cls = OpenAPIV2SpecValidator
    else:
        raise OpenAPIParseError(
            "Spec missing recognizable 'openapi: 3.x' or 'swagger: 2.0' top-level field"
        )

    try:
        _osv_validate(spec, cls=validator_cls)
    except _OSVValidationError as e:
        raise OpenAPIValidationError(f"Spec validation failed ({version}): {e}") from e
    except (TypeError, KeyError, ValueError) as e:
        # #72 — narrowed from bare Exception. Malformed specs commonly raise
        # these on mis-shaped dicts; anything else is a real bug we want surfaced.
        raise OpenAPIValidationError(f"Spec validation error ({version}): {e}") from e

    return version


def _format_parameters(params: list[dict]) -> str:
    """Format parameter list as readable text."""
    if not params:
        return ""
    lines = ["Parameters:"]
    for p in params:
        name = p.get("name", "?")
        loc = p.get("in", "?")
        required = " (required)" if p.get("required") else ""
        desc = p.get("description", "").strip().replace("\n", " ")
        schema = p.get("schema", {}) or {}
        type_ = schema.get("type") or p.get("type", "")
        type_str = f" [{type_}]" if type_ else ""
        line = f"  - {name} (in: {loc}){type_str}{required}"
        if desc:
            line += f" — {desc[:_PARAM_DESC_CAP]}"
        lines.append(line)
    return "\n".join(lines)


def _format_responses(responses: dict) -> str:
    """Format responses dict as readable text."""
    if not responses:
        return ""
    lines = ["Responses:"]
    for code, resp in responses.items():
        if not isinstance(resp, dict):
            continue
        desc = resp.get("description", "").strip().replace("\n", " ")
        lines.append(f"  - {code}: {desc[:_RESPONSE_CAP]}" if desc else f"  - {code}")
    return "\n".join(lines)


def _build_entry(
    path: str,
    method: str,
    operation: dict,
    path_level_params: list[dict],
) -> dict[str, Any]:
    """Build one {title, content, tags, source_suffix} dict for an endpoint."""
    summary = (operation.get("summary") or "").strip()
    description = (operation.get("description") or "").strip()
    op_id = operation.get("operationId", "")
    tags = operation.get("tags") or []

    # Merge path-level + operation-level parameters
    op_params = operation.get("parameters") or []
    all_params = list(path_level_params) + list(op_params)
    # Per-endpoint param cap (#74) — keeps a single huge endpoint from blowing
    # the canonical_text budget. Overflow surfaced as a footer line.
    _param_cap = settings.openapi_max_params_per_endpoint
    _params_overflow = max(0, len(all_params) - _param_cap)
    if _params_overflow:
        all_params = all_params[:_param_cap]

    # Request body (OpenAPI 3.x only)
    req_body = operation.get("requestBody") or {}
    req_body_desc = ""
    if isinstance(req_body, dict):
        rb_desc = (req_body.get("description") or "").strip()
        content_types = list((req_body.get("content") or {}).keys())
        if rb_desc or content_types:
            parts = ["Request body:"]
            if rb_desc:
                parts.append(f"  {rb_desc[:_REQ_BODY_CAP]}")
            if content_types:
                parts.append(f"  Content-Type(s): {', '.join(content_types)}")
            req_body_desc = "\n".join(parts)

    content_parts = [f"{method.upper()} {path}"]
    if op_id:
        content_parts.append(f"operationId: {op_id}")
    if summary:
        content_parts.append(f"Summary: {summary}")
    if description:
        content_parts.append(f"Description: {description[:_DESC_CAP]}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")

    params_text = _format_parameters(all_params)
    if params_text:
        if _params_overflow:
            params_text = params_text + "\n  ... (" + str(_params_overflow) + " more)"
        content_parts.append(params_text)

    if req_body_desc:
        content_parts.append(req_body_desc)

    resp_text = _format_responses(operation.get("responses") or {})
    if resp_text:
        content_parts.append(resp_text)

    # #73 — prefer summary/operationId, else fall back to just the METHOD+path.
    route = f"{method.upper()} {path}"
    label = summary or op_id
    title = f"{route} — {label}" if label else route

    return {
        "title": title[:_TITLE_CAP],
        "content": "\n\n".join(content_parts),
        "tags": tags,
        "path": path,
        "method": method.upper(),
    }


def _walk_paths(spec: dict) -> tuple[list[dict[str, Any]], int]:
    """Extract one entry per (path, method) in spec['paths'].

    Returns (entries, skipped_param_refs). Parameter dicts that still contain
    a raw ``$ref`` (i.e. unresolved) are filtered out and counted, since
    downstream formatters cannot render them and including them would emit
    misleading "name=?  in=?" lines.
    """
    entries: list[dict[str, Any]] = []
    skipped_refs = 0
    paths = spec.get("paths") or {}

    def _filter_param_refs(params: list) -> list[dict]:
        nonlocal skipped_refs
        out: list[dict] = []
        for p in params:
            if isinstance(p, dict) and "$ref" in p:
                skipped_refs += 1
                continue
            if isinstance(p, dict):
                out.append(p)
        return out

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        path_level_params = _filter_param_refs(path_item.get("parameters") or [])

        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            op_clean = dict(operation)
            op_clean["parameters"] = _filter_param_refs(operation.get("parameters") or [])
            entries.append(_build_entry(path, method.lower(), op_clean, path_level_params))

    if skipped_refs:
        logger.warning(
            "_walk_paths: skipped %d parameter entries containing unresolved $ref",
            skipped_refs,
        )
    return entries, skipped_refs


async def _resolve_refs(spec: dict, url: str) -> tuple[dict, bool]:
    """Resolve $refs via prance. Returns (spec, refs_resolved).

    Passes the already-fetched spec to prance via ``spec_string`` so we don't
    re-fetch the URL. Remote refs still resolve relative to ``url``. On any
    failure, returns the unresolved spec with refs_resolved=False (#75).
    """
    if not _PRANCE_AVAILABLE:
        logger.warning("prance not installed — skipping $ref resolution")
        return spec, False

    spec_string = json.dumps(spec)

    def _do_resolve() -> dict:
        # NOTE: passing both url= and spec_string= breaks in prance >=25 (it
        # tries to use the parsed URL as a filesystem path). With spec_string
        # alone, internal $refs resolve but relative *external* refs cannot —
        # those are an unsupported edge case for inline ingestion.
        parser = ResolvingParser(
            spec_string=spec_string,
            lazy=False,
            strict=False,
            backend="openapi-spec-validator",
        )
        return parser.specification

    loop = asyncio.get_running_loop()
    try:
        resolved = await loop.run_in_executor(None, _do_resolve)
        return resolved, True
    except Exception as e:
        logger.warning(
            "prance $ref resolution failed: %s — falling back to unresolved spec", e
        )
        return spec, False


async def fetch_and_parse_spec(url: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch, validate, and parse an OpenAPI/Swagger spec.

    Returns (entries, metadata) where:
        entries: list of endpoint dicts, capped at settings.openapi_max_endpoints
        metadata: {version, title, spec_version, total_endpoints, truncated}

    Raises:
        OpenAPIFetchError: URL unreachable
        OpenAPIParseError: not JSON/YAML or missing version field
        OpenAPIValidationError: failed schema validation
    """
    spec = await _fetch_spec(url)
    if not isinstance(spec, dict):
        raise OpenAPIParseError("Spec root is not an object")

    # #75 — resolve $refs FIRST so validation runs against the inlined spec.
    # Validating before resolution can reject legitimate specs whose $refs
    # only resolve at runtime, and lets unresolved-ref bugs slip past validation.
    spec, refs_resolved = await _resolve_refs(spec, url)

    version = _validate_spec(spec)

    info = spec.get("info") or {}
    entries, skipped_param_refs = _walk_paths(spec)

    total = len(entries)
    cap = settings.openapi_max_endpoints
    truncated = False
    if total > cap:
        logger.warning("OpenAPI spec has %d endpoints, capping at %d", total, cap)
        entries = entries[:cap]
        truncated = True

    # Tag every entry with refs_resolved so downstream consumers can flag
    # endpoints whose schemas may still contain raw $ref objects.
    for e in entries:
        e["refs_resolved"] = refs_resolved

    metadata = {
        "version": version,
        "title": (info.get("title") or url)[:200],
        "spec_version": str(info.get("version", "")),
        "total_endpoints": total,
        "ingested_endpoints": len(entries),
        "truncated": truncated,
        "refs_resolved": refs_resolved,
        "skipped_param_refs": skipped_param_refs,
    }
    logger.info(
        "OpenAPI fetch: url=%s version=%s endpoints=%d (total=%d truncated=%s refs_resolved=%s)",
        url, version, len(entries), total, truncated, refs_resolved,
    )
    return entries, metadata
