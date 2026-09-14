# OpenAPI breaking-change severities (§17.1068)

`docs/openapi-severity.txt` feeds `oasdiff --severity-levels` in `make check-openapi-breaking`
(ci-tier-0). The file format allows no comments: one `<rule-id> <ERR|WARN|INFO>` per line.

| rule | level | why |
|---|---|---|
| `request-parameter-max-set` | WARN | Setting a bound on a request parameter is how the engine keeps a hostile or absurd `limit` out of SQL (schemathesis drove `limit=-469` and a 24-digit limit into two routes → 500). Every client in this repo (SPA, SDK, CLI, pipelines) stays inside the bounds. Reported, not fatal. |
| `request-parameter-min-set` | WARN | Same. |

Add a rule only with a §-entry that says why the change is safe for every client.
