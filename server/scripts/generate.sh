#!/usr/bin/env bash
# Regenerate the OpenAPI transport code.
#
#   ./scripts/generate.sh          write internal/entry/http/oapi/oapi.gen.go
#   ./scripts/generate.sh --check  fail if the checked-in file is stale
#
# The generator version is pinned. Set OAPI_CODEGEN to use a local binary.
#
# Fail closed: a generator that fails or produces no bytes must not read as
# green. `go run pkg@version` has been observed to exit 0 without output when it
# cannot reach the module proxy (Go 1.27, network-less host). The script checks
# both the exit status and the produced bytes. Write mode generates into a
# temporary file, so a failed run cannot leave the old checked-in file behind
# and read as a fresh write.
set -euo pipefail
cd "$(dirname "$0")/.."

SPEC="contracts/openapi.yaml"
CONFIG="oapi-codegen.yaml"
OUT="internal/entry/http/oapi/oapi.gen.go"

# The pinned module version wins. A bare oapi-codegen on PATH is used only
# when the caller asks for it, because an older binary would produce different
# output and make --check meaningless.
PINNED="go run github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@v2.8.1"
if [ -n "${OAPI_CODEGEN:-}" ]; then
  GENERATOR="$OAPI_CODEGEN"
else
  GENERATOR="$PINNED"
fi

# run_generator FILE runs the generator. A non-zero exit is an environment
# error: the gate did not run, and neither mode may continue.
run_generator() {
  # shellcheck disable=SC2086
  $GENERATOR -config "$CONFIG" -o "$1" "$SPEC" || {
    echo "generate: the generator failed (exit $?); the gate DID NOT RUN" >&2
    exit 2
  }
}

require_output() {
  if [ ! -s "$1" ]; then
    echo "generate: the generator produced no output; the gate DID NOT RUN" >&2
    exit 2
  fi
}

if [ "${1:-}" = "--check" ]; then
  if [ ! -f "$OUT" ]; then
    echo "generate: missing $OUT; run ./scripts/generate.sh" >&2
    exit 1
  fi
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' EXIT
  run_generator "$tmp"
  require_output "$tmp"
  if ! diff -u "$OUT" "$tmp"; then
    echo "generate: $OUT is stale; run ./scripts/generate.sh" >&2
    exit 1
  fi
  echo "generate: $OUT is up to date"
  exit 0
fi

# Write mode goes through a temporary file: a failed generator leaves the
# checked-in output untouched, and an empty result never reaches $OUT.
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
run_generator "$tmp"
require_output "$tmp"
chmod 0644 "$tmp"
mv "$tmp" "$OUT"
echo "generate: wrote $OUT"
