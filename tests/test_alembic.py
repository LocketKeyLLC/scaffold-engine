"""§17.1075 — Alembic wiring: one head, the baseline is empty, the runner runs after the SQL runner."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_script_directory_loads_with_one_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config(str(ROOT / "alembic.ini")); cfg.set_main_option("script_location", str(ROOT / "alembic"))
    sd = ScriptDirectory.from_config(cfg)
    heads = sd.get_heads()
    assert heads == ["0001_baseline"]
    base = sd.get_revision("0001_baseline")
    assert base.down_revision is None


def test_startup_runs_alembic_after_the_sql_runner_and_only_if_it_succeeded():
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    i_sql = src.index("mig_result = await run_migrations()")
    i_al = src.index("run_alembic_upgrade()")
    assert i_sql < i_al
    assert "if _mig_failure is None:" in src[i_sql:i_al + 200]


def test_three_mount_lists_carry_alembic():
    import pytest
    if not (ROOT / "Dockerfile").exists():
        pytest.skip("repo-shape check — runs where the Dockerfile is mounted (ci-tier-0's mount-parity test covers it)")
    assert "COPY --chown=root:root alembic/" in (ROOT / "Dockerfile").read_text()
    assert "./alembic:/code/alembic:ro" in (ROOT / "docker-compose.dev.yml").read_text()
    assert '"$PWD/alembic:/code/alembic:ro"' in (ROOT / ".github" / "workflows" / "test.yml").read_text()
