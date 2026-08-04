# Contributing to scaffold-engine

Thanks for your interest. This is an actively developed solo project by LocketKey LLC, so contributions are welcome but the bar for merging is "fits the architecture and passes the suite."

## Before you start

- Read the [README](./README.md) for setup and [OVERVIEW.md](./OVERVIEW.md) for architecture. Most "why is it built this way" questions are answered in OVERVIEW.
- For anything larger than a small fix, **open an issue first** describing what you want to change and why. This avoids wasted work on changes that won't merge.

## Development setup

Follow the README's first-time install, then bring up the dev stack:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

The dev override mounts `tests/`, the `Makefile`, and docs into the `scaffold-orchestrator` container.

## Running tests

```bash
make test
```

Or target a specific file inside the container:

```bash
docker exec scaffold-orchestrator pytest tests/test_<name>.py -v
```

All tests must pass before a PR is reviewed. If your change touches retrieval, run the retrieval-quality workflow locally where practical (see `.github/workflows/retrieval-quality.yml`).

## Ground rules

- **Migrations are forward-only.** New schema changes go in `db/migrations/` as a new numbered file; never edit an applied migration. Run `python3 scripts/lint_migrations.py` before committing.
- **Python is `python3`**, never `python`.
- **API contract:** `docs/openapi.json` is the pinned contract. If your change alters an endpoint, regenerate the snapshot (`python3 scripts/openapi_snapshot.py`) and include it in the PR.
- **Commit style:** follow the existing convention — `fix(§X.Y): short description` / `feat(§X.Y): short description`. If your change has no sprint reference, use `fix:` / `feat:` / `docs:` / `chore:` prefixes.
- **No cloud-service assumptions.** The stack is self-hosted by design; changes must work with local Ollama/Milvus/Postgres/SearXNG defaults.

## Pull requests

1. Fork, branch from `main`.
2. Keep PRs focused — one logical change per PR.
3. Include tests for new behavior.
4. Note in the PR description whether OVERVIEW.md or USER_GUIDE.md needs a corresponding doc update, and include it if so.

## License of contributions

By submitting a contribution you agree that it is licensed under the repository's [LICENSE](./LICENSE) (Business Source License 1.1) and that LocketKey LLC may relicense it under the Change License or a commercial license as described there.
