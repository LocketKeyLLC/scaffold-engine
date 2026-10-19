"""§17.1665 — `docker compose <read>` is a read (job 3cc24343 T5: the check `docker compose version` was dropped)."""
from __future__ import annotations

from app.modules.assist_state_check import read_only_command
from app.modules.supervised_runs import verify_commands


def test_compose_reads():
    for c in ("docker compose version", "docker compose ps", "docker compose -f /opt/bitmagnet/docker-compose.yml ps",
              "docker compose --project-directory /opt/b logs --tail 20", "docker compose ls",
              'pct exec 109 -- bash -c "docker compose version"'):
        assert read_only_command(c), c


def test_compose_writes_and_a_following_log_are_not():
    for c in ("docker compose up -d", "docker compose down", "docker compose pull", "docker compose restart",
              "docker compose logs -f", "docker compose -f x.yml up", "docker compose rm -f"):
        assert not read_only_command(c), c


def test_a_check_is_judged_as_the_runner_judges_it():
    """§17.1666 — live (T5): the runner refused `docker compose version` and the step was recorded failed, install done.
    A frame never offers a check the runner will refuse; inside a script the engine-side read still counts."""
    assert verify_commands('## Verify\n\n- `pct exec 109 -- bash -c "docker compose version"`\n') == []
    assert not read_only_command("docker compose version", runner_side=True)
    assert read_only_command("docker compose version")
    assert verify_commands('## Verify\n\n- `pct exec 109 -- systemctl is-active docker`\n') == [
        "pct exec 109 -- systemctl is-active docker"]
