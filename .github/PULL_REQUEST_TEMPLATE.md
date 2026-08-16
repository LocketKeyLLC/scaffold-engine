<!-- Thanks for contributing to scaffold-engine! Please read CONTRIBUTING.md first. -->

## What & why

<!-- What does this change do, and what problem does it solve? Link any related issue. -->

Closes #

## Type of change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that changes existing behavior)
- [ ] Docs / tooling only

## Checklist

- [ ] I read [CONTRIBUTING.md](../CONTRIBUTING.md).
- [ ] The change follows the repo conventions (migrations under `db/migrations/`, deps pinned, async-first I/O).
- [ ] `make test` passes locally (dev image). If this touches pipelines, `make test-pipelines` too.
- [ ] Schema/contract changes ran `make sync-schemas` and `make openapi-snapshot` where applicable.
- [ ] Docs updated (README / USER_GUIDE / OVERVIEW) if behavior, config, or architecture changed.
- [ ] No secrets, API keys, or personal paths introduced.
