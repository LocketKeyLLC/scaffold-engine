"""Config discovery — flag > env > user toml > walked .env > default."""
from __future__ import annotations


import pytest

from scaffold_cli import config as cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def clean_env(monkeypatch):
    """Strip env vars + XDG so each test sees a known baseline."""
    for v in ("SCAFFOLD_API_URL", "SCAFFOLD_API_KEY", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(v, raising=False)
    yield monkeypatch


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point HOME at a tmp dir so the user-config-toml lookup is sandboxed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------
def test_flag_beats_env(clean_env, isolated_home, tmp_path):
    clean_env.setenv("SCAFFOLD_API_URL", "http://from-env:8000")
    clean_env.setenv("SCAFFOLD_API_KEY", "key-env")
    out = cfg.resolve_config(
        flag_url="http://from-flag:9000",
        flag_key="key-flag",
        cwd=tmp_path,
    )
    assert out.api_url == "http://from-flag:9000"
    assert out.api_key == "key-flag"
    assert out.source == "flag"


def test_env_beats_user_config(clean_env, isolated_home, tmp_path):
    user_cfg = isolated_home / ".scaffold" / "config.toml"
    user_cfg.parent.mkdir()
    user_cfg.write_text('api_url = "http://from-toml:7000"\napi_key = "key-toml"\n')
    clean_env.setenv("SCAFFOLD_API_URL", "http://from-env:8000")
    clean_env.setenv("SCAFFOLD_API_KEY", "key-env")
    out = cfg.resolve_config(cwd=tmp_path)
    assert out.api_url == "http://from-env:8000"
    assert out.api_key == "key-env"
    assert "env" in out.source.lower()


def test_user_config_beats_walked_dotenv(clean_env, isolated_home, tmp_path):
    user_cfg = isolated_home / ".scaffold" / "config.toml"
    user_cfg.parent.mkdir()
    user_cfg.write_text('api_url = "http://from-toml:7000"\napi_key = "key-toml"\n')
    (tmp_path / ".env").write_text(
        "SCAFFOLD_API_URL=http://from-dotenv:6000\nSCAFFOLD_API_KEY=key-dotenv\n"
    )
    out = cfg.resolve_config(cwd=tmp_path)
    assert out.api_url == "http://from-toml:7000"
    assert out.api_key == "key-toml"
    assert "user config" in out.source


def test_walked_dotenv_beats_default(clean_env, isolated_home, tmp_path):
    (tmp_path / ".env").write_text(
        "SCAFFOLD_API_URL=http://from-dotenv:6000\nSCAFFOLD_API_KEY=key-dotenv\n"
    )
    out = cfg.resolve_config(cwd=tmp_path)
    assert out.api_url == "http://from-dotenv:6000"
    assert out.api_key == "key-dotenv"
    assert ".env" in out.source


def test_walked_dotenv_searches_parents(clean_env, isolated_home, tmp_path):
    """Running from a nested subdir should find a .env at the project root."""
    (tmp_path / ".env").write_text("SCAFFOLD_API_URL=http://parent-env:5000\n")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    out = cfg.resolve_config(cwd=nested)
    assert out.api_url == "http://parent-env:5000"


def test_default_when_nothing_else_present(clean_env, isolated_home, tmp_path):
    out = cfg.resolve_config(cwd=tmp_path)
    assert out.api_url == cfg.DEFAULT_API_URL
    assert out.api_key is None
    assert out.source == "default"


def test_xdg_config_home_overrides_dot_scaffold(clean_env, tmp_path, monkeypatch):
    """When XDG_CONFIG_HOME is set, the lookup uses ``$XDG/scaffold/config.toml``
    instead of ``~/.scaffold/config.toml``. This is the convention more careful
    Linux users follow; we honor it without forcing it."""
    xdg = tmp_path / "xdg"
    (xdg / "scaffold").mkdir(parents=True)
    (xdg / "scaffold" / "config.toml").write_text('api_url = "http://from-xdg:1000"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    out = cfg.resolve_config(cwd=tmp_path)
    assert out.api_url == "http://from-xdg:1000"


def test_malformed_toml_falls_through(clean_env, isolated_home, tmp_path):
    """A broken config.toml shouldn't crash — fall through to next source."""
    user_cfg = isolated_home / ".scaffold" / "config.toml"
    user_cfg.parent.mkdir()
    user_cfg.write_text("this is not valid toml = = =\n")
    out = cfg.resolve_config(cwd=tmp_path)
    assert out.api_url == cfg.DEFAULT_API_URL  # fell through cleanly


# ---------------------------------------------------------------------------
# §17.420 — walked-.env provenance security note
# ---------------------------------------------------------------------------

def test_provenance_note_fires_on_key_url_mismatch(clean_env, isolated_home, tmp_path):
    """key from a trusted source (env) + url discovered from a walked .env →
    the key would be sent to that walked-.env URL, so the note fires."""
    clean_env.setenv("SCAFFOLD_API_KEY", "key-env")  # trusted source
    (tmp_path / ".env").write_text("SCAFFOLD_API_URL=http://untrusted:6000\n")
    out = cfg.resolve_config(cwd=tmp_path)
    assert out.api_key == "key-env"
    assert out.key_source == "env SCAFFOLD_API_KEY"
    assert out.source.startswith("walked .env")
    note = cfg.provenance_security_note(out)
    assert note is not None
    assert "walked-up .env" in note
    assert "API key WILL be sent" in note


def test_no_note_when_key_and_url_from_same_dotenv(clean_env, isolated_home, tmp_path):
    """The intended 'run from the repo' case: both key + url from the same
    .env → no provenance mismatch, so no note."""
    (tmp_path / ".env").write_text(
        "SCAFFOLD_API_URL=http://repo:6000\nSCAFFOLD_API_KEY=key-dotenv\n"
    )
    out = cfg.resolve_config(cwd=tmp_path)
    assert out.source.startswith("walked .env")
    assert out.key_source.startswith("walked .env")
    assert cfg.provenance_security_note(out) is None


def test_no_note_when_no_key(clean_env, isolated_home, tmp_path):
    (tmp_path / ".env").write_text("SCAFFOLD_API_URL=http://repo:6000\n")
    out = cfg.resolve_config(cwd=tmp_path)
    assert out.api_key is None
    assert cfg.provenance_security_note(out) is None


def test_no_note_when_url_not_from_walked_dotenv(clean_env, isolated_home, tmp_path):
    """key from env + url from env → no walked .env involved → no note."""
    clean_env.setenv("SCAFFOLD_API_KEY", "key-env")
    clean_env.setenv("SCAFFOLD_API_URL", "http://trusted:8000")
    out = cfg.resolve_config(cwd=tmp_path)
    assert not out.source.startswith("walked .env")
    assert cfg.provenance_security_note(out) is None
