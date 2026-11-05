"""§17.1929 — an API reference page that carries its endpoint as an OpenAPI document in a JSON `<script>` block is read
from that document (method and URL, parameters, request body and response fields, a `$ref` followed, a map keyed by
symbol named), kept whole beside the page's focused prose; a page with no such document reads as before.

Live (follow-up 5b4880c0, 2026-11-05): Alpaca's "Create an Order" page (826 KB, its fields in the server-side-render
JSON) extracted to 334 characters and was dropped as a script shell; the research said the page "was not retrieved"
and the audit could not verify one order field."""
from __future__ import annotations

import json

from app.utils.openapi_embedded import SECTION_HEAD, embedded_openapi_text, render

DOC = {"openapi": "3.0.0", "servers": [{"url": "https://data.example-broker.test"}],
       "paths": {"/v2/stocks/bars": {"get": {
           "summary": "Historical bars",
           "parameters": [{"name": "symbols", "in": "query", "required": True, "schema": {"type": "string"}},
                          {"name": "timeframe", "in": "query", "required": True, "schema": {"type": "string"},
                           "description": "e.g. `1Day`"}],
           "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Bars"}}}},
                         "400": {"description": "bad"}}}},
           "/v2/orders": {"post": {
               "requestBody": {"content": {"application/json": {"schema": {
                   "type": "object", "required": ["type"],
                   "properties": {"type": {"type": "string", "enum": ["market", "limit"]},
                                  "limit_price": {"type": ["string", "null"]}}}}}}}}},
       "components": {"schemas": {
           "Bars": {"type": "object", "properties": {"bars": {"type": "object", "additionalProperties": {
               "type": "array", "items": {"$ref": "#/components/schemas/Bar"}}}}},
           "Bar": {"type": "object", "required": ["c"], "properties": {"c": {"type": "number", "description": "Close."},
                                                                         "t": {"type": "string", "format": "date-time"}}}}}}


def test_an_openapi_document_is_rendered_operation_by_operation():
    text = render(DOC)
    assert "GET https://data.example-broker.test/v2/stocks/bars -- Historical bars" in text
    assert "  - `symbols` in query string (required)" in text and "-- e.g. `1Day`" in text
    assert "  - `bars.<key>.[]c` number (required) -- Close." in text and "`bars.<key>.[]t` string (date-time)" in text
    assert "  - `type` string one of market, limit (required)" in text and "`limit_price` string or null" in text
    assert "400" not in text


def test_a_page_embedding_one_is_read_from_it_and_a_page_without_reads_as_before():
    blob = json.dumps({"document": {"api": {"schema": DOC}}, "sidebars": {}})
    page = (f'<html><body><p>Places a new order.</p><script id="ssr-props" type="application/x-ssr-props">{blob}'
            '</script><script>var x = {"openapi": 1};</script></body></html>')
    text = embedded_openapi_text(page)
    assert text.startswith("GET https://data.example-broker.test/v2/stocks/bars") and "POST" in text
    assert embedded_openapi_text("<html><p>No API here.</p></html>") == ""
    assert embedded_openapi_text('<script type="application/json">{"openapi": "3.0.0", "paths": </script>') == ""
    assert SECTION_HEAD.endswith(":\n")
