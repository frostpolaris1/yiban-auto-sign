#!/usr/bin/env bash
# Verify the new Go and React stacks with one command.
#
#   bash scripts/verify-stacks.sh
#
# Exit codes: 0 all gates pass; 1 a gate fails; 2 a toolchain is missing.
# A missing toolchain is an environment error, never a green run.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v go >/dev/null 2>&1; then
  echo "SKIP: go not found; server gates were not run" >&2
  exit 2
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "SKIP: npm not found; ui gates were not run" >&2
  exit 2
fi

echo "== server =="
(cd server && ./scripts/verify.sh)

echo "== ui =="
(cd ui && npm run verify)
