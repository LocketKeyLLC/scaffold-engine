"""Config discovery for the scaffold CLI.

Resolution order (first non-empty wins):
  1. CLI flag             — ``--api-url`` / ``--api-key`` on the command
  2. Environment vars     — ``SCAFFOLD_API_URL`` / ``SCAFFOLD_API_KEY``
  3. User config file     — ``~/.scaffold/config.toml`` (or
                             ``$XDG_CONFIG_HOME/scaffold/config.toml``)
  4. Walked-up ``.env``   — first ``.env`` found by walking up from cwd
                             (lets users run from inside the scaffold-engine
                             repo without re-typing the orchestrator URL/key)
  5. Defaults             — ``api_url = http://localhost:8000``, no key

Returning ``None`` for a key signals "no value available" — the caller
decides whether that's fatal (e.g. authenticated endpoints) or fine
(e.g. ``scaffold doctor`` against a key-less ``/health``).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


DEFAULT_API_URL = "http://localhost:8000"


@dataclass
class CLIConfig:
    api_url: str
    api_key: str | None
    source: str  # human-readable: where the api_url came from (for `scaffold doctor`)
    key_source: str = ""  # §17.420 — where the api_key came from (provenance note)


def _user_config_path() -> Path:
    """``$XDG_CONFIG_HOME/scaffold/config.toml`` if XDG is set, else
    ``~/.scaffold/config.toml``. Both are common conventions; we accept
    either to match how the rest of the user's tooling is laid out."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "scaffold" / "config.toml"
    return Path.home() / ".scaffold" / "config.toml"


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except Exception:
        # A malformed config file shouldn't crash the CLI — skip and
        # fall through to the next source. The caller can re-create it
        # via ``scaffold init`` (or hand-edit). Silent here is correct
        # because we have a fallback chain.
        return {}


def _walk_for_dotenv(start: Path, max_levels: int = 6) -> Path | None:
    """Walk up from ``start`` looking for a ``.env`` file. Caps at
    ``max_levels`` to avoid an unbounded scan when the user runs the
    CLI from somewhere unrelated."""
    cur = start.resolve()
    for _ in range(max_levels):
        candidate = cur / ".env"
        if candidate.is_file():
            return candidate
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


def _read_dotenv(path: Path) -> dict[str, str]:
    """Trivial KEY=VALUE parser. Doesn't try to be a full dotenv impl —
    quotes, exports, multi-line values, and interpolation are NOT
    supported. The CLI only cares about ``SCAFFOLD_API_URL`` and
    ``SCAFFOLD_API_KEY``, both of which are simple strings in practice."""
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            out[key.strip()] = val.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def resolve_config(
    flag_url: str | None = None,
    flag_key: str | None = None,
    *,
    cwd: Path | None = None,
) -> CLIConfig:
    """Apply the resolution order. ``cwd`` is overridable for testing."""
    cwd = cwd or Path.cwd()

    # 1. Flags
    if flag_url:
        api_url = flag_url
        url_source = "flag"
    elif (env_url := os.environ.get("SCAFFOLD_API_URL")):
        api_url = env_url
        url_source = "env SCAFFOLD_API_URL"
    else:
        api_url = ""
        url_source = ""

    if flag_key:
        api_key: str | None = flag_key
        key_source = "flag"
    elif (env_key := os.environ.get("SCAFFOLD_API_KEY")):
        api_key = env_key
        key_source = "env SCAFFOLD_API_KEY"
    else:
        api_key = None
        key_source = ""

    # 2. User config file
    if not api_url or api_key is None:
        cfg = _read_toml(_user_config_path())
        if not api_url and (cfg_url := cfg.get("api_url")):
            api_url = cfg_url
            url_source = f"user config {_user_config_path()}"
        if api_key is None and (cfg_key := cfg.get("api_key")):
            api_key = cfg_key
            key_source = f"user config {_user_config_path()}"

    # 3. Walked-up .env
    if not api_url or api_key is None:
        dotenv = _walk_for_dotenv(cwd)
        if dotenv is not None:
            env_data = _read_dotenv(dotenv)
            if not api_url and "SCAFFOLD_API_URL" in env_data:
                api_url = env_data["SCAFFOLD_API_URL"]
                url_source = f"walked .env at {dotenv}"
            if api_key is None and "SCAFFOLD_API_KEY" in env_data:
                api_key = env_data["SCAFFOLD_API_KEY"] or None
                if api_key is not None:
                    key_source = f"walked .env at {dotenv}"

    # 4. Default
    if not api_url:
        api_url = DEFAULT_API_URL
        url_source = "default"

    return CLIConfig(
        api_url=api_url, api_key=api_key,
        source=url_source, key_source=key_source,
    )


def provenance_security_note(cfg: CLIConfig) -> str | None:
    """§17.420 — flag the case where a credential from a trusted source would
    be sent to a URL discovered from a walked-up ``.env``.

    The walked-``.env`` discovery (resolution step 4) trusts the first
    ``.env`` found above cwd, with no repo-marker gate — so a ``.env`` in an
    untrusted directory can supply ``api_url``. When ``api_key`` came from a
    HIGHER-precedence source (flag / env / user config), that key would be
    sent to the walked-``.env``'s URL. Both the common-legit shape (key in
    shell env, url from the repo's own ``.env``) and the redirect-attack
    shape match, so this is a surfaced note in ``version`` / ``doctor`` — not
    a hard error or a per-command nag.

    Returns the note string, or None when there's nothing to flag.
    """
    if cfg.api_key is None:
        return None
    if not cfg.source.startswith("walked .env"):
        return None
    if cfg.key_source.startswith("walked .env"):
        # key + url from the SAME walked .env — the intended "run from the
        # repo" case; no provenance mismatch to flag.
        return None
    return (
        f"api_url was discovered from a walked-up .env ({cfg.source}), but "
        f"your api_key came from {cfg.key_source or 'a higher-precedence source'}. "
        f"Your API key WILL be sent to that URL — only proceed if you trust "
        f"the directory you're running from. Pin the URL explicitly "
        f"(--api-url, SCAFFOLD_API_URL, or ~/.scaffold/config.toml) to "
        f"silence this."
    )
