# server

P0 skeleton for the Go service. This directory is independent of the Python
implementation; no Python file imports it and no Python behavior changes.

## Layout

```text
server/
├── canonical.tsv              single-source rules (concept, owner, pattern)
├── contracts/                 host API contract v1 (P0 freezes the shape)
├── internal/core/clock        the only wall-clock reader
├── internal/core/status       the only task-state definition
├── internal/core/errs         stable error codes
├── internal/core/mask         the only masking implementation
├── internal/core/retry        the only retry policy
├── internal/core/egress       the only egress identity
├── internal/core/plan         the only slot geometry
├── internal/core/capacity     owner stub (P4)
├── internal/core/image        owner stub (P5)
├── internal/adapters/config   the only environment reader
├── internal/signing/contract  provider v1
├── internal/entry/http/respond  the only error envelope
├── tools/gate                 single-source scanner
└── scripts/verify.sh          vet + test + gate
```

## Commands

```bash
cd server
./scripts/verify.sh
go test ./...
go run ./tools/gate -root . -rules canonical.tsv
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

Implemented: clock, status, errors, mask, config, retry, egress, plan,
provider contract, error envelope, and the gate with mutation tests.

Not implemented yet: Gin/pgx, database, auth, scheduling, image pipeline,
frontend, OpenAPI YAML. Those land in P1 and later.

## Boundary

- `core` is pure: no I/O, no clock, no random.
- `app` owns use cases and transaction boundaries; no SQL or HTTP.
- `adapters` are the only I/O surface.
- `entry` only translates HTTP; it does not import adapters.
- `signing/providers` must not import the host.
