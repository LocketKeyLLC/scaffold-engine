"""Tests for app/utils/http_clients.py (#9.28)."""
import pytest

from app.utils import http_clients


@pytest.fixture(autouse=True)
async def _reset_clients():
    """Ensure each test starts with no cached clients."""
    await http_clients.close_clients()
    yield
    await http_clients.close_clients()


# ---------------------------------------------------------------------------
# SearXNG client (#7.5 — follow_redirects=True)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_searxng_client_is_lazy():
    assert http_clients._searxng_client is None
    c = http_clients.get_searxng_client()
    assert c is not None
    assert http_clients._searxng_client is c


@pytest.mark.smoke
def test_searxng_client_follows_redirects():
    c = http_clients.get_searxng_client()
    assert c.follow_redirects is True


@pytest.mark.smoke
def test_searxng_client_is_cached_across_calls():
    c1 = http_clients.get_searxng_client()
    c2 = http_clients.get_searxng_client()
    assert c1 is c2


# ---------------------------------------------------------------------------
# GitHub client
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_github_client_is_lazy_and_cached():
    assert http_clients._github_client is None
    c1 = http_clients.get_github_client()
    c2 = http_clients.get_github_client()
    assert c1 is c2 is http_clients._github_client


@pytest.mark.smoke
def test_github_client_sets_required_headers():
    c = http_clients.get_github_client()
    assert c.headers.get("accept") == "application/vnd.github+json"
    assert c.headers.get("x-github-api-version") == "2022-11-28"


# ---------------------------------------------------------------------------
# Generic client (added for #76)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_generic_client_is_lazy_and_cached():
    assert http_clients._generic_client is None
    c1 = http_clients.get_generic_http_client()
    c2 = http_clients.get_generic_http_client()
    assert c1 is c2 is http_clients._generic_client


@pytest.mark.smoke
def test_generic_client_follows_redirects():
    c = http_clients.get_generic_http_client()
    assert c.follow_redirects is True


# ---------------------------------------------------------------------------
# close_clients
# ---------------------------------------------------------------------------
@pytest.mark.smoke
async def test_close_clients_resets_all_three():
    http_clients.get_searxng_client()
    http_clients.get_github_client()
    http_clients.get_generic_http_client()
    assert http_clients._searxng_client is not None
    assert http_clients._github_client is not None
    assert http_clients._generic_client is not None

    await http_clients.close_clients()

    assert http_clients._searxng_client is None
    assert http_clients._github_client is None
    assert http_clients._generic_client is None
