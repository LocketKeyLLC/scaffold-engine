# scaffold-engine-cli

Terminal client for [Scaffold Engine](../). Talks to the orchestrator's
HTTP API on `:8000` so you can drive jobs from a shell, CI, or script —
no Open WebUI required.

## Install

From a checkout of the scaffold-engine repo:

```bash
pip install ./cli
```

Or in editable mode while iterating:

```bash
pip install -e ./cli
```

That installs a `scaffold` command on your PATH. To upgrade later, re-run
the same command after pulling.

## Configure

The CLI resolves connection settings in this order (first non-empty wins):

1. **CLI flag** — `--api-url`, `--api-key`
2. **Environment** — `SCAFFOLD_API_URL`, `SCAFFOLD_API_KEY`
3. **User config** — `~/.scaffold/config.toml` (or `$XDG_CONFIG_HOME/scaffold/config.toml`)
4. **Walked `.env`** — first `.env` found by walking up from `cwd` (so running from inside the repo just works)
5. **Default** — `http://localhost:8000`, no key

Minimal `~/.scaffold/config.toml`:

```toml
api_url = "http://localhost:8000"
api_key = "sk-scaffold-..."
```

`scaffold version` prints the resolved URL and where it came from — handy
for debugging precedence surprises.

## Commands (Sprint H)

| Command | Purpose |
|---|---|
| `scaffold version` | Print the CLI version + resolved config source |
| `scaffold doctor` | Probe `/health` and render per-subsystem status |
| `scaffold ideate <text>` | POST `/ideate`. Halts at `awaiting_confirmation` |
| `scaffold confirm <job_id> [feedback]` | POST `/ideate/confirm`. Triggers research + planning |
| `scaffold jobs list` | GET `/jobs`. Compact table of recent jobs |
| `scaffold jobs status <job_id>` | GET `/jobs/<id>`. Show full status |

Add `--json` to `ideate`, `confirm`, `jobs list`, and `jobs status` to get
the raw JSON response (useful for piping into `jq`).

SSE-streamed endpoints (`/research`, `/execute/all`) are not in this
release — they ship in Sprint I as part of the streaming-uniformity work.
For now use OWUI for those flows.

## Examples

```bash
# Confirm the orchestrator is reachable + healthy
scaffold doctor

# Submit an idea
scaffold ideate "build a RAG-backed knowledge base"

# Confirm with optional feedback
scaffold confirm 11111111-1111-1111-1111-111111111111 "use Postgres"

# Filter jobs by status
scaffold jobs list --status awaiting_confirmation

# Pipe a job into jq
scaffold jobs status <id> --json | jq '.error_summary'
```

## Tests

The CLI ships with its own pytest suite. From the dev container:

```bash
make test-cli
```

That runs `pytest cli/tests/` — config discovery, HTTP error translation,
and command-output formatting — with no live orchestrator required.
