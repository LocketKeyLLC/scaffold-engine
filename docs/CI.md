# Scaffold Engine — CI/CD Guide

## CI Tiers

| Tier | Target | Trigger | Where | Tests | Time |
|------|--------|---------|-------|-------|------|
| 1 | `make ci-smoke` | Every push & PR | GitHub cloud runners | 24 unit (extraction pipeline) | <30s |
| 2 | `make ci-local` | Manual / main merge | T480 (local) | 24 unit + 4 integration + 7 golden | ~5 min |
| 3 | `make ci-eval` | Manual | T480 (local) | 40-query ground truth | ~8 min (cached: <1s) |

## What Runs Where

**Cloud-safe (Tier 1 — `ci-smoke`):**
- `tests/test_verify_extraction.py` — 24 pure Python unit tests
- No Docker, no Milvus, no PostgreSQL, no Ollama
- Installs from `requirements-ci.txt` (minimal deps)
- Runs on GitHub's free `ubuntu-latest` runners (7 GB RAM, 2 vCPU)

**Local-only (Tier 2+ — `ci-local`, `ci-eval`):**
- `tests/test_integration.py` — 4 integration tests (needs PostgreSQL + Milvus + orchestrator)
- `tests/test_retrieval_golden.py` — 7 golden retrieval tests (needs Milvus + Ollama, ~12s/query on CPU)
- `tests/eval_retrieval.py` — 40-query ground truth evaluation
- **Why local?** Milvus standalone requires 8 GB+ RAM (GitHub free runners cap at 7 GB). Ollama CPU inference on `qwen3-embedding:8b` is too slow for cloud CI timeouts.

## Running CI Locally

### Quick (no act, no workflow)

```bash
# Tier 1 only (no services needed)
cd ~/scaffold-engine
make ci-smoke

# Tier 2 (services must be running)
docker start milvus-standalone
docker compose up -d
make ci-local

# All tiers
make ci-full
```

### Via `act` (tests the GitHub Actions workflow itself)

Install `act`:
```bash
# Ubuntu/Debian
curl -s https://raw.githubusercontent.com/nektos/act/master/install.sh | sudo bash

# Or via Go
go install github.com/nektos/act@latest
```

Run the smoke job:
```bash
cd ~/scaffold-engine
act -j smoke
```

The `.actrc` file provides default flags. First run downloads the runner image (~600 MB).

**`act` limitations:**
- The medium runner image lacks Docker CLI — Tier 2 cannot run via `act`
- `act` sets `ACT=true` in env (useful for conditional logic)
- `services:` block support is partial — use `make ci-local` for integration tests
- On Apple Silicon: add `--container-architecture linux/amd64` to `.actrc`

## Adding the Workflow to Your Repo

```bash
cd ~/scaffold-engine

# 1. Create the workflow directory
mkdir -p .github/workflows

# 2. Copy the workflow file
cp <path-to>/ci.yml .github/workflows/ci.yml

# 3. Append CI targets to existing Makefile
cat Makefile.ci >> Makefile

# 4. Add requirements-ci.txt
cp <path-to>/requirements-ci.txt .

# 5. Add conftest_ci.py fixtures (merge into existing conftest.py or import)
cp <path-to>/tests/conftest_ci.py tests/

# 6. Add .actrc for local workflow testing
cp <path-to>/.actrc .

# 7. Commit
git add .github/ Makefile requirements-ci.txt tests/conftest_ci.py .actrc
git commit -m "ci: add GitHub Actions workflow with cloud/local tier split"
```

## Enabling Tier 2 on a Self-Hosted Runner

If you later set up the T480 as a self-hosted GitHub Actions runner:

1. Install the runner: `Settings → Actions → Runners → New self-hosted runner`
2. Ensure the boot sequence has run: `docker start milvus-standalone && docker compose up -d`
3. Uncomment the `integration` job in `ci.yml`
4. Add `SCAFFOLD_API_KEY` to GitHub Secrets (`Settings → Secrets → Actions`)

The integration job will then run automatically on `main` merges after the smoke job passes.

## Secrets Required

| Secret | Used By | Required For |
|--------|---------|-------------|
| `SCAFFOLD_API_KEY` | Tier 2 (integration) | Authenticated API calls in integration tests |
| `OLLAMA_API_KEY` | Already in GitHub Secrets | Not used by CI yet (Ollama is local) |

## Future Improvements

- **Milvus Lite for cloud integration tests**: `pymilvus` bundles Milvus Lite — swap `localhost:19530` for `./test.db` in a `MILVUS_URI` env var to run a subset of integration tests in cloud CI without Docker
- **`pytest-docker` plugin**: Auto-start/stop Docker Compose from pytest fixtures
- **Caching**: `actions/cache` for pip dependencies (~10s savings)
- **Matrix builds**: Test against Python 3.11 + 3.12 (not needed yet)
