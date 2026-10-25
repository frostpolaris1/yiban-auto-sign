#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

unformatted="$(gofmt -l .)"
if [ -n "$unformatted" ]; then
  echo "gofmt required:" >&2
  echo "$unformatted" >&2
  exit 1
fi

go vet ./...
go test ./...
go run ./tools/gate -root . -rules canonical.tsv

# PostgreSQL e2e visibility (AGENTS §30). Without a DSN the suite skips, and a
# skipped suite is not a pass: say so on stdout. With a DSN the suite must
# actually run, and the top-level pass count must reach the P1 minimum.
if [ -z "${YIBAN_TEST_DATABASE_URL:-}" ]; then
  echo "SKIP: pg e2e did not run: YIBAN_TEST_DATABASE_URL is not set."
else
  pg_log="$(mktemp)"
  if ! go test -count=1 -v ./internal/adapters/pg/ | tee "$pg_log"; then
    rm -f "$pg_log"
    exit 1
  fi
  pg_pass="$(grep -c '^--- PASS' "$pg_log" || true)"
  rm -f "$pg_log"
  if [ "$pg_pass" -lt 18 ]; then
    echo "FAIL: pg e2e ran $pg_pass top-level tests; want at least 18." >&2
    exit 1
  fi
  echo "pg e2e: $pg_pass top-level tests passed."
fi

# Auth e2e follows the same rule: a skipped suite is not a pass.
# The package holds the DB-backed login, logout and reset flows.
if [ -z "${YIBAN_TEST_DATABASE_URL:-}" ]; then
  echo "SKIP: auth e2e did not run: YIBAN_TEST_DATABASE_URL is not set."
else
  auth_log="$(mktemp)"
  if ! go test -count=1 -v ./cmd/server/ | tee "$auth_log"; then
    rm -f "$auth_log"
    exit 1
  fi
  auth_pass="$(grep -c '^--- PASS' "$auth_log" || true)"
  rm -f "$auth_log"
  if [ "$auth_pass" -lt 9 ]; then
    echo "FAIL: auth e2e ran $auth_pass top-level tests; want at least 9." >&2
    exit 1
  fi
  echo "auth e2e: $auth_pass top-level tests passed."
fi

# E-4 contract drift gate: regenerating must not change the checked-in code.
# The gate fails closed: a generator that produced nothing exits 2.
bash ./scripts/generate.sh --check

# Structure gate: top-level and layer directories match structure.tsv.
go run ./tools/structure -root .. -rules structure.tsv

# License gate: every third-party dependency must carry an allowed license,
# and THIRD_PARTY_LICENSES.md must match the scan. The gate fails closed:
# a missing scanner or an incomplete scan exits 2, never green.
bash ./scripts/license.sh
