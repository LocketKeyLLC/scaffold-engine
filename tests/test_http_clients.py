"""Tests for app/utils/http_clients.py (eager-init contract)."""
import pytest
from app.utils import http_clients


@pytest.fixture(autouse=True)
async def _reset_clients():
    """Each test starts with a cold registry; eager-init before use."""
    await http_clients.close_clients()
    http_clients.init_clients()
    yield
    await http_clients.close_clients()


# ---------------------------------------------------------------------------
# SearXNG client
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_searxng_client_is_initialized_eagerly():
    c = http_clients.get_searxng_client()
    assert c is not None
    assert http_clients._clients.get("searxng") is c


@pytest.mark.smoke
def test_searxng_client_follows_redirects():
    c = http_clients.get_searxng_client()
    assert c.follow_redirects is True


@pytest.mark.smoke
def test_searxng_client_is_singleton_across_calls():
    c1 = http_clients.get_searxng_client()
    c2 = http_clients.get_searxng_client()
    assert c1 is c2


# ---------------------------------------------------------------------------
# GitHub client
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_github_client_is_initialized_and_singleton():
    c1 = http_clients.get_github_client()
    c2 = http_clients.get_github_client()
    assert c1 is c2 is http_clients._clients.get("github")


@pytest.mark.smoke
def test_github_client_sets_required_headers():
    c = http_clients.get_github_client()
    assert c.headers.get("accept") == "application/vnd.github+json"
    assert c.headers.get("x-github-api-version") == "2022-11-28"


# ---------------------------------------------------------------------------
# Generic client
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_generic_client_is_initialized_and_singleton():
    c1 = http_clients.get_generic_http_client()
    c2 = http_clients.get_generic_http_client()
    assert c1 is c2 is http_clients._clients.get("generic")


@pytest.mark.smoke
def test_generic_client_follows_redirects():
    c = http_clients.get_generic_http_client()
    assert c.follow_redirects is True


# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_ollama_client_is_initialized_and_singleton():
    c1 = http_clients.get_ollama_client()
    c2 = http_clients.get_ollama_client()
    assert c1 is c2 is http_clients._clients.get("ollama")


# ---------------------------------------------------------------------------
# Lazy-init path is gone: calling a getter with empty registry must raise
# ---------------------------------------------------------------------------
@pytest.mark.smoke
async def test_getters_raise_when_not_initialized():
    await http_clients.close_clients()
    with pytest.raises(RuntimeError, match="not initialized"):
        http_clients.get_searxng_client()
    with pytest.raises(RuntimeError, match="not initialized"):
        http_clients.get_github_client()
    with pytest.raises(RuntimeError, match="not initialized"):
        http_clients.get_generic_http_client()
    with pytest.raises(RuntimeError, match="not initialized"):
        http_clients.get_ollama_client()


# ---------------------------------------------------------------------------
# close_clients
# ---------------------------------------------------------------------------
@pytest.mark.smoke
async def test_close_clients_resets_registry():
    http_clients.get_searxng_client()
    http_clients.get_github_client()
    http_clients.get_huggingface_client()
    http_clients.get_generic_http_client()
    http_clients.get_ollama_client()
    http_clients.get_openai_client()
    http_clients.get_anthropic_client()  # §17.345
    # §17.900 — HF INFERENCE, distinct from the `huggingface` client above:
    # that one talks to the Hub for ingest, this one to the OpenAI-dialect
    # inference router. Two endpoints, two timeouts, two clients.
    http_clients.get_hf_inference_client()
    http_clients.get_ngspice_client()
    http_clients.get_verilator_client()
    http_clients.get_symbiyosys_client()
    assert set(http_clients._clients.keys()) == {
        "searxng", "github", "huggingface", "generic", "ollama", "openai",
        "anthropic", "hf_inference", "ngspice", "verilator", "symbiyosys",
    }
    await http_clients.close_clients()
    assert http_clients._clients == {}


# §17.900 — a connection edited at runtime changes base_url, but a client bakes
# base_url in at construction, so it must be REBUILT or every later call keeps
# hitting the old endpoint until the next restart — the restart this feature
# exists to remove.
@pytest.mark.smoke
async def test_rebuild_client_replaces_the_instance():
    http_clients.init_clients()
    before = http_clients.get_openai_client()
    http_clients.rebuild_client("openai")
    after = http_clients.get_openai_client()
    assert after is not before
    await http_clients.close_clients()


@pytest.mark.smoke
async def test_rebuild_client_is_a_noop_for_an_unknown_name():
    """Providers without a dedicated client must not raise — a failed rebuild
    can never be allowed to fail the connection write that triggered it."""
    http_clients.init_clients()
    http_clients.rebuild_client("not-a-client")   # must not raise
    assert http_clients.get_openai_client() is not None
    await http_clients.close_clients()
