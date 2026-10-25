#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

go vet ./...
go test ./...
go run ./tools/gate -root . -rules canonical.tsv
