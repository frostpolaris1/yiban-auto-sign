# server

P0 skeleton for the Go service. This directory is independent of the Python
implementation; no Python file imports it and no Python behavior changes.

## Layout

```text
server/
├── canonical.tsv              single-source rules (concept, owner, pattern)
├── contracts/                 host API contract v1 (OpenAPI 3.1 YAML)
├── oapi-codegen.yaml          generator config (pinned v2.8.1)
├── internal/core/clock        the only wall-clock reader
├── internal/core/status       the only task-state definition
├── internal/core/errs         stable error codes
├── internal/core/auth        session contract (roles, users, SessionIssuer)
├── internal/core/mask         the only masking implementation
├── internal/core/retry        the only retry policy
├── internal/core/egress       the only egress identity
├── internal/core/plan         the only slot geometry
├── internal/core/capacity     owner stub (P4)
├── internal/core/image        owner stub (P5)
├── internal/adapters/config   the only environment reader
├── internal/adapters/session  in-memory SessionIssuer (development only)
├── internal/signing/contract  provider v1
├── internal/entry/http/oapi     generated contract types and Gin handlers
├── internal/entry/http/router   Gin assembly, middleware, auth skeleton
├── internal/entry/http/respond  the only error envelope
├── tools/gate                 single-source scanner
├── tools/structure            repository layout and naming gate
├── structure.tsv              registered directories for the structure gate
└── scripts/verify.sh          gofmt + vet + test + gate + drift + structure
```

Layer and domain names are fixed by the repository map in `../STRUCTURE.md`.

## Commands

```bash
cd server
./scripts/verify.sh
go test ./...
go run ./tools/gate -root . -rules canonical.tsv
go run ./tools/structure -root .. -rules structure.tsv
./scripts/generate.sh --check
```

## Canonical rules

`canonical.tsv` is the only list of owners. A rule has six tab-separated
columns:

```text
id  kind  owner  pattern  allow  message
```

- `kind=call`: forbidden `pkg.Func` calls outside `owner` (and `allow`).
- `kind=string`: forbidden exact string literals outside `owner`.
- `kind=func` / `kind=type`: forbidden definitions outside `owner`.
- `kind=import`: forbidden imports inside `owner`.

The gate is empty-safe: an empty rule file is an error, never a green run.

## P0 scope

Implemented in P0: clock, status, errors, mask, config, retry, egress, plan,
provider contract, error envelope, and the gate with mutation tests.

P1 so far: the OpenAPI 3.1 contract, generated transport types, the Gin
assembly with request id, panic recovery, access log and auth middleware, and
an in-memory `SessionIssuer`. Every business endpoint answers 501 through the
generated handlers until its own task lands.

Not implemented yet: database, login, RBAC, scheduling, image pipeline,
frontend. Those land in later P1 tasks and P2+. `scripts/generate.sh --check`
fails when `contracts/openapi.yaml` and the generated code drift apart.

## Boundary

- `core` is pure: no I/O, no clock, no random.
- `app` owns use cases and transaction boundaries; no SQL or HTTP.
- `adapters` are the only I/O surface.
- `entry` only translates HTTP; it does not import adapters.
- `signing/providers` must not import the host.
