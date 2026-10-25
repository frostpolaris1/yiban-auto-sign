#!/usr/bin/env bash
# Regenerate the OpenAPI transport code.
#
#   ./scripts/generate.sh          write internal/entry/http/oapi/oapi.gen.go
#   ./scripts/generate.sh --check  fail if the checked-in file is stale
#
# The generator version is pinned. Set OAPI_CODEGEN to use a local binary.
set -euo pipefail
cd "$(dirname "$0")/.."

SPEC="contracts/openapi.yaml"
CONFIG="oapi-codegen.yaml"
OUT="internal/entry/http/oapi/oapi.gen.go"

if [ -n "${OAPI_CODEGEN:-}" ]; then
  GENERATOR="$OAPI_CODEGEN"
elif command -v oapi-codegen >/dev/null 2>&1; then
  GENERATOR="oapi-codegen"
else
  GENERATOR="go run github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@v2.8.1"
fi

if [ "${1:-}" = "--check" ]; then
  if [ ! -f "$OUT" ]; then
    echo "generate: missing $OUT; run ./scripts/generate.sh" >&2
    exit 1
  fi
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' EXIT
  # shellcheck disable=SC2086
  $GENERATOR -config "$CONFIG" -o "$tmp" "$SPEC"
  if ! diff -u "$OUT" "$tmp"; then
    echo "generate: $OUT is stale; run ./scripts/generate.sh" >&2
    exit 1
  fi
  echo "generate: $OUT is up to date"
  exit 0
fi

# shellcheck disable=SC2086
$GENERATOR -config "$CONFIG" -o "$OUT" "$SPEC"
echo "generate: wrote $OUT"
