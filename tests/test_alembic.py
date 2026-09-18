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
    # Repo-shape check: needs the Dockerfile, the dev compose and the CI workflow
    # all on disk. The container test lane mounts the Dockerfile but NOT the
    # compose files, so skip there — ci-tier-0's mount-parity test covers this.
    needed = [ROOT / "Dockerfile", ROOT / "docker-compose.dev.yml", ROOT / ".github" / "workflows" / "test.yml"]
    if not all(p.exists() for p in needed):
        pytest.skip("repo-shape check — runs where the repo tree is mounted (ci-tier-0's mount-parity test covers it)")
    assert "COPY --chown=root:root alembic/" in (ROOT / "Dockerfile").read_text()
    assert "./alembic:/code/alembic:ro" in (ROOT / "docker-compose.dev.yml").read_text()
    assert '"$PWD/alembic:/code/alembic:ro"' in (ROOT / ".github" / "workflows" / "test.yml").read_text()


def test_env_never_reconfigures_an_already_configured_logging_stack():
    """§17.1075b — fileConfig(alembic.ini) inside the orchestrator disabled every
    `scaffold.*` logger and replaced the JSON root handler: the live service
    logged nothing after startup. env.py must only apply the ini logging when
    no root handler exists (the CLI case)."""
    src = (ROOT / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "not logging.getLogger().handlers" in src
    assert src.index("not logging.getLogger().handlers") < src.index("fileConfig(config.config_file_name)")
