"""§17.1191 — a secret the RUNNER holds, that the engine only ever names.

Before this, a runbook needing a password put `<DB_PASSWORD>` in its commands,
the operator typed it at the run pause, and the engine spliced it in. §17.1187
masked it in the node output and the transcript — and then sent the substituted
command to the runner, which writes `SUPERVISED … RUN: <command>` to that
machine's journal and hands it to `create_subprocess_shell`, so it also landed
in the process table. Masking protected the record, never the run.

These cover the runner half: the store it reads, the refusal of a store other
users can read, the reference it resolves as an ENVIRONMENT variable, the log
line that must keep showing `$NAME`, and the redaction of a value that comes
back in a command's own output.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import stat

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _runner():
    spec = importlib.util.spec_from_file_location("local_runner_mcp", ROOT / "scripts" / "local_runner_mcp.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


def _secrets_file(tmp_path, body: str, mode: int = 0o600) -> str:
    p = tmp_path / "secrets.env"
    p.write_text(body, encoding="utf-8")
    os.chmod(p, mode)
    return str(p)


def test_the_store_reads_names_and_values_and_ignores_noise(tmp_path):
    r = _runner()
    path = _secrets_file(tmp_path, "\n".join([
        "# the database", 'DB_PASSWORD="hunter2"', "API_TOKEN = tok-123 ", "EMPTY=",
        "lowercase=nope", "not a line", "WITH_EQUALS=a=b=c",
    ]) + "\n")
    got = r.load_secrets(path)
    assert got == {"DB_PASSWORD": "hunter2", "API_TOKEN": "tok-123", "WITH_EQUALS": "a=b=c"}


def test_a_store_other_users_can_read_is_refused(tmp_path):
    """A store anyone on the box can read is worse than none: the engine would
    believe the secret is held safely."""
    r = _runner()
    path = _secrets_file(tmp_path, "DB_PASSWORD=hunter2\n", mode=0o644)
    assert r.load_secrets(path) == {}
    os.chmod(path, 0o600)
    assert r.load_secrets(path) == {"DB_PASSWORD": "hunter2"}


def test_no_file_is_simply_no_secrets(tmp_path):
    r = _runner()
    assert r.load_secrets(None) == {} and r.load_secrets(str(tmp_path / "nope.env")) == {}


@pytest.mark.parametrize("cmd,want", [
    ("mysql -u root -p$DB_PASSWORD -e 'SELECT 1'", ["DB_PASSWORD"]),
    ("app-cli --key ${API_TOKEN} --host h", ["API_TOKEN"]),
    ("a $ONE then $TWO then $ONE again", ["ONE", "TWO"]),
    ("echo $notupper and $1 and plain", []),
])
def test_secret_refs_finds_the_names(cmd, want):
    assert _runner().secret_refs(cmd) == want


def test_redact_replaces_values_longest_first():
    r = _runner()
    store = {"SHORT": "abc", "LONG": "abcdef"}
    assert r.redact("see abcdef and abc here", store) == "see *** and *** here"
    assert r.redact("nothing to hide", store) == "nothing to hide"
    assert r.redact("", store) == "" and r.redact("x", {}) == "x"


def test_the_policy_reports_names_and_never_values(tmp_path):
    """The engine learns which names exist so it can write `$NAME`; the value
    stays on this machine."""
    r = _runner()
    src = pathlib.Path(ROOT / "scripts" / "local_runner_mcp.py").read_text(encoding="utf-8")
    # the policy payload is built from `sorted(store)` — the keys, never the items
    assert '"secrets": sorted(store)' in src
    assert '"secrets": sorted(store.values())' not in src
    assert r.load_secrets(_secrets_file(tmp_path, "DB_PASSWORD=hunter2\n")) == {"DB_PASSWORD": "hunter2"}


def test_a_value_never_enters_the_command_the_runner_logs_or_runs():
    """The three places the old design leaked: the approval covers `$NAME`, the
    log line prints `$NAME`, and the value is passed as an environment variable
    so argv never carries it."""
    src = pathlib.Path(ROOT / "scripts" / "local_runner_mcp.py").read_text(encoding="utf-8")
    run_sup = src[src.index("async def run_supervised"):src.index("    return mcp")]
    assert "env = {**os.environ, **{n: store[n] for n in refs}}" in run_sup
    assert 'log.warning("SUPERVISED id=%s RUN: %s", aid, run_cmd)' in run_sup
    # the command handed to the shell is the one that was approved, unmodified
    assert "run_cmd = command" in run_sup
    assert "create_subprocess_shell(\n            run_cmd" in run_sup and "env=env)" in run_sup
    # an unresolvable reference is refused, not expanded to the empty string
    assert "unknown = [n for n in refs if n not in store]" in run_sup
    assert "is not in this runner's secrets file" in run_sup
    # sudo drops the environment, so the names ride --preserve-env by NAME
    assert '"--preserve-env=" + ",".join(refs)' in run_sup


def test_both_tools_redact_their_output():
    """A read-only `grep` over a config file can print a secret too."""
    src = pathlib.Path(ROOT / "scripts" / "local_runner_mcp.py").read_text(encoding="utf-8")
    assert src.count("redact(out.decode(") == 2, "a tool returns output without redacting it"


def test_the_unit_and_installer_carry_the_secrets_file():
    r = _runner()
    unit = r.unit_text(python="/p/python", script="/p/s.py", host="0.0.0.0", port=8790, token="t",
                       write_allow=["pct set"], write_sudo=True, secrets_file="/etc/scaffold-runner/secrets.env")
    assert "--secrets-file /etc/scaffold-runner/secrets.env" in unit
    plain = r.unit_text(python="/p/python", script="/p/s.py", host="0.0.0.0", port=8790, token="t")
    assert "--secrets-file" not in plain


# ── §17.1191 — the rewrite must produce a reference the SHELL expands ─────

@pytest.mark.parametrize("cmd,want", [
    ("app-cli --key <TOK>", "app-cli --key $TOK"),
    ('app-cli --key "<TOK>"', 'app-cli --key "$TOK"'),
    # a whole single-quoted token becomes DOUBLE-quoted: single quotes suppress
    # expansion, and the integration test caught exactly this — a runbook wrote
    # `printf '%s' '<TOKEN>' | tee f` and the file ended up holding "$TOKEN"
    ("printf '%s' '<TOK>' | tee /tmp/f", 'printf \'%s\' "$TOK" | tee /tmp/f'),
    ("echo '<TOK>' && app --key <TOK>", 'echo "$TOK" && app --key $TOK'),
    ("nothing to do here", "nothing to do here"),
])
def test_the_reference_is_written_where_the_shell_will_expand_it(cmd, want):
    from app.modules.supervised_runs import secret_ref_rewrite
    out, problem = secret_ref_rewrite(cmd, "TOK")
    assert (out, problem) == (want, "")


def test_a_placeholder_buried_in_a_single_quoted_string_is_refused():
    """Rewriting that span's quotes could change what the rest of it means, and
    leaving it would run the command with the literal text `$TOK`."""
    from app.modules.supervised_runs import secret_ref_rewrite
    out, problem = secret_ref_rewrite("psql 'host=h password=<TOK>'", "TOK")
    assert out == "psql 'host=h password=<TOK>'"          # unchanged
    assert "single-quoted" in problem and "$TOK" in problem


def test_an_unexpandable_secret_moves_from_resolved_to_missing():
    """The frame must not offer `run` for a command that would silently write
    the literal reference instead of the value."""
    from app.modules.supervised_runs import apply_runner_secrets
    inputs = [{"name": "TOK", "hint": "", "secret": True}]
    cmds, verify, kept, resolved, missing = apply_runner_secrets(
        ["psql 'host=h password=<TOK>'"], [], inputs, {"secrets": ["TOK"]})
    assert resolved == [] and missing == ["TOK"]
    assert cmds == ["psql 'host=h password=<TOK>'"]
    assert kept == []                                     # still never asked of the operator
