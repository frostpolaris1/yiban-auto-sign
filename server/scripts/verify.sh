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

# E-4 contract drift gate: regenerating must not change the checked-in code.
bash ./scripts/generate.sh --check

# Structure gate: top-level and layer directories match structure.tsv.
go run ./tools/structure -root .. -rules structure.tsv
