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
├── internal/core/capacity     the only capacity formula (P4)
├── internal/core/window       the only window predicates and edge shrinking (P4)
├── internal/core/rate         the only per-egress GCRA limiter (P4)
├── internal/core/account      account status, record and credential types
├── internal/core/audit        audit chain encoding and verification
├── internal/core/scope        master / admin / user authorization scope
├── internal/core/school       school and binding types
├── internal/core/user         user record types
├── internal/app               use-case ports (repository interfaces, TxManager)
├── internal/adapters/pg       PostgreSQL repositories, pool, transactions, migrations
├── internal/core/image        owner stub (P5)
├── internal/adapters/config   the only environment reader
├── internal/adapters/session  in-memory SessionIssuer (development only)
├── migrations/postgres        goose migrations, embedded in the binary
├── internal/signing/contract  provider v1
├── internal/entry/http/oapi     generated contract types and Gin handlers
├── internal/entry/http/router   Gin assembly, middleware, auth skeleton
├── internal/entry/http/respond  the only error envelope
├── tools/gate                 single-source scanner
├── tools/structure            repository layout and naming gate
├── structure.tsv              registered directories for the structure gate
├── THIRD_PARTY_LICENSES.md    generated dependency license manifest
└── scripts/verify.sh          gofmt + vet + test + gate + drift + structure + license
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
./scripts/license.sh --write
```

## License gate

`scripts/license.sh` scans every third-party module with `go-licenses` v1.6.0
and compares the result with `THIRD_PARTY_LICENSES.md`.

- Allowed: Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, MIT, 0BSD, CC0-1.0,
  BlueOak-1.0.0.
- GPL, AGPL, unknown and license-less modules exit 1.
- A missing scanner, an empty scan or a missing direct dependency exits 2.

The scanner is installed into the user cache on first use. Run
`./scripts/license.sh --write` after a dependency change. Set `GO_LICENSES` to
use a local scanner binary.

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
- `kind=forbid`: forbidden `pkg.Func` calls **inside** `owner`; the inverse of
  `call`. `owner` is the checked scope and must be non-empty. `owner *` is only
  valid for `import`.

`import` and `forbid` owners are checked scopes: each must match at least one Go
file. A rule that matches no files is an error (exit 2). A test-file owner is
counted here but only scanned in the `-tests` pass.

The gate is empty-safe: an empty rule file is an error, never a green run.

## Release checks

The application rejects email addresses that contain Unicode format characters
(Cf). The database CHECK does not. Before a release, confirm the target database
has no such rows:

```sql
SELECT count(*) FROM users
WHERE email ~ '[\u00AD\u0600-\u0605\u061C\u06DD\u070F\u0890-\u0891\u08E2\u180E\u200B-\u200F\u202A-\u202E\u2060-\u2064\u2066-\u206F\uFEFF\uFFF9-\uFFFB]';
```

The query covers all BMP Cf ranges. Astral Cf code points (U+110BD, U+E0001,
U+E0020–U+E007F) are not covered. The application check uses the full Unicode
category and remains the authoritative gate.

## P0 scope

Implemented in P0: clock, status, errors, mask, config, retry, egress, plan,
provider contract, error envelope, and the gate with mutation tests.

P1 so far: the OpenAPI 3.1 contract, generated transport types, the Gin
assembly with request id, panic recovery, access log and auth middleware, an
in-memory `SessionIssuer`, the PostgreSQL layer (eleven goose migrations, a
pgxpool with `statement_timeout`, a READ COMMITTED transaction manager, and
repositories for users, sessions, schools, administrator bindings, accounts,
credentials, audit and password resets), and the live use cases behind every
business endpoint: auth and password reset, users, schools, administrators and
accounts. A domain handler that is not wired answers 501; production wires all
four.

Not implemented yet: scheduling, image pipeline, upstream client, frontend.
Those land in later P1 tasks and P2+. `scripts/generate.sh --check` fails when
`contracts/openapi.yaml` and the generated code drift apart.

Database tests need a real PostgreSQL 18. Set `YIBAN_TEST_DATABASE_URL`; when
it is unset they skip and print a warning. CI starts a `postgres:18` service
for the `stacks` job.

## Boundary

- `core` is pure: no I/O, no clock, no random.
- `app` owns use cases and transaction boundaries; no SQL or HTTP.
- `adapters` are the only I/O surface.
- `entry` only translates HTTP; it does not import adapters.
- `signing/providers` must not import the host.

## Environment

`cmd/server` reads the process environment through `internal/adapters/config`
only. `YIBAN_ENV` is required: an unset or empty value stops startup, so a
deployment cannot silently fall back to development defaults.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `YIBAN_ENV` | yes | — | `development`, `test`, `staging` or `production` |
| `YIBAN_HTTP_ADDR` | no | `127.0.0.1:47183` | listen address |
| `YIBAN_DATABASE_URL` | no | empty | PostgreSQL DSN; unused until W1 |
| `YIBAN_TZ` | no | `Asia/Shanghai` | business time zone |
| `YIBAN_INSECURE_COOKIES` | no | `true` for development and test | use the unprefixed cookie name; production refuses a true value |
| `YIBAN_HASH_PEPPER` | **yes** | — | at least 16 characters; keys the HMAC digests of IP addresses and user agents, and derives the audit chain key. Rotating it breaks verification of the existing audit chain. |
| `YIBAN_CREDENTIAL_KEY` | **yes** | — | base64 of exactly 32 bytes; encrypts stored credentials with AES-256-GCM. Rotating it goes through kid re-encryption (P4/P8), so old rows stay decryptable. |

Production always uses `__Host-yiban_session` with `Secure`. A true
`YIBAN_INSECURE_COOKIES` under `YIBAN_ENV=production` is a startup error.
