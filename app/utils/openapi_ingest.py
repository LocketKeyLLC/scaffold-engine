"""OpenAPI/Swagger ingestion for /research openapi:<url>.

Fetches an OpenAPI 3.0 or Swagger 2.0 spec, validates it, then emits one
entry per endpoint with path + method + description + params.
"""
import json
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


class OpenAPIFetchError(Exception):
    """Spec URL unreachable or returned non-2xx."""


class OpenAPIParseError(Exception):
    """Spec is not valid JSON/YAML or not a recognizable OpenAPI/Swagger doc."""


class OpenAPIValidationError(Exception):
    """Spec parsed but failed schema validation."""


async def _fetch_spec(url: str) -> dict[str, Any]:
    """Fetch spec URL and decode as JSON or YAML."""
    try:
        async with httpx.AsyncClient(
            timeout=float(settings.openapi_timeout),
            follow_redirects=True,
        ) as client:
            r = await client.get(url)
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
        import yaml
        return yaml.safe_load(text)
    except Exception as e:
        raise OpenAPIParseError(f"Spec is neither JSON nor YAML: {e}") from e


def _validate_spec(spec: dict) -> str:
    """Validate and return spec version: 'openapi-3' or 'swagger-2'."""
    from openapi_spec_validator import validate
    from openapi_spec_validator.validation.exceptions import OpenAPIValidationError as _ValErr

    if "openapi" in spec and str(spec["openapi"]).startswith("3."):
        version = "openapi-3"
    elif str(spec.get("swagger", "")).startswith("2."):
        version = "swagger-2"
    else:
        raise OpenAPIParseError(
            "Spec missing 'openapi: 3.x' or 'swagger: 2.0' top-level field"
        )

    try:
        validate(spec)
    except _ValErr as e:
        raise OpenAPIValidationError(f"Spec validation failed: {e}") from e
    except Exception as e:
        # validate() can raise various refresolver/jsonschema errors for malformed specs
        raise OpenAPIValidationError(f"Spec validation error: {e}") from e

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
            line += f" — {desc[:200]}"
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
        lines.append(f"  - {code}: {desc[:200]}" if desc else f"  - {code}")
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

    # Request body (OpenAPI 3.x only)
    req_body = operation.get("requestBody") or {}
    req_body_desc = ""
    if isinstance(req_body, dict):
        rb_desc = (req_body.get("description") or "").strip()
        content_types = list((req_body.get("content") or {}).keys())
        if rb_desc or content_types:
            parts = ["Request body:"]
            if rb_desc:
                parts.append(f"  {rb_desc[:300]}")
            if content_types:
                parts.append(f"  Content-Type(s): {', '.join(content_types)}")
            req_body_desc = "\n".join(parts)

    content_parts = [f"{method.upper()} {path}"]
    if op_id:
        content_parts.append(f"operationId: {op_id}")
    if summary:
        content_parts.append(f"Summary: {summary}")
    if description:
        content_parts.append(f"Description: {description[:800]}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")

    params_text = _format_parameters(all_params)
    if params_text:
        content_parts.append(params_text)

    if req_body_desc:
        content_parts.append(req_body_desc)

    resp_text = _format_responses(operation.get("responses") or {})
    if resp_text:
        content_parts.append(resp_text)

    title = summary or op_id or f"{method.upper()} {path}"
    title = f"{method.upper()} {path} — {title}" if title != f"{method.upper()} {path}" else f"{method.upper()} {path}"

    return {
        "title": title[:200],
        "content": "\n\n".join(content_parts),
        "tags": tags,
        "path": path,
        "method": method.upper(),
    }


def _walk_paths(spec: dict) -> list[dict[str, Any]]:
    """Extract one entry per (path, method) in spec['paths']."""
    entries: list[dict[str, Any]] = []
    paths = spec.get("paths") or {}

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        path_level_params = path_item.get("parameters") or []

        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            entries.append(_build_entry(path, method.lower(), operation, path_level_params))

    return entries


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

    version = _validate_spec(spec)

    info = spec.get("info") or {}
    entries = _walk_paths(spec)
    total = len(entries)

    cap = settings.openapi_max_endpoints
    truncated = False
    if total > cap:
        logger.warning("OpenAPI spec has %d endpoints, capping at %d", total, cap)
        entries = entries[:cap]
        truncated = True

    metadata = {
        "version": version,
        "title": (info.get("title") or url)[:200],
        "spec_version": str(info.get("version", "")),
        "total_endpoints": total,
        "ingested_endpoints": len(entries),
        "truncated": truncated,
    }

    logger.info(
        "OpenAPI fetch: url=%s version=%s endpoints=%d (total=%d truncated=%s)",
        url, version, len(entries), total, truncated,
    )

    return entries, metadata
