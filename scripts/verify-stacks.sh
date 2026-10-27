#!/usr/bin/env bash
# Verify the new Go and React stacks with one command.
#
#   bash scripts/verify-stacks.sh
#
# Exit codes: 0 all gates pass; 1 a gate fails; 2 a toolchain is missing.
# A missing toolchain is an environment error, never a green run.
#
# Registered gap (AGENTS §29, P7 F12): this script does NOT run the
# config-registry gate (scripts/check-config-registry.py). The stacks CI job
# has no python/portalocker, so the gate cannot run here. It runs in the
# verify job of scripts/dev-verify.sh --ci, which covers server/ and deploy/.
# The difference is registered, not silent: server-side YIBAN_* keys are still
# checked by the Go tests and by go vet, but the registry self-check only runs
# in the dev-verify lane.
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
