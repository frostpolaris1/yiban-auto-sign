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
# -count=1：Go 的测试缓存会让 shell/fixture 改动看不见（派单 F18）。
# 实测：改 deploy/linux/install.sh 后不带 -count=1 返回 cached 绿。
go test -count=1 ./...
go run ./tools/gate -root . -rules canonical.tsv
# 默认口径不含 _test.go。这里再跑一遍 -tests，
# 使测试文件的 allow 登记被实际执行（AGENTS §29）。
go run ./tools/gate -root . -rules canonical.tsv -tests

# PostgreSQL e2e visibility (AGENTS §30). Without a DSN the suite skips, and a
# skipped suite is not a pass: say so on stdout. With a DSN the suite must
# actually run, and the top-level pass count must reach the P1 minimum.
# 阈值口径（派单 F12）：YIBAN_TEST_DATABASE_URL 指向 yiban_test_r4 时，
# 2026-10-27 实测 pg 60 个、auth 57 个、cmd/cli 33 个顶层 PASS。
# 阈值取实测下方留余量（pg ≥50、auth ≥45、cmd/cli ≥20）：
# 少于它说明整段没跑或大面跳过，不是绿。
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
  if [ "$pg_pass" -lt 50 ]; then
    echo "FAIL: pg e2e ran $pg_pass top-level tests; want at least 50." >&2
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
  if [ "$auth_pass" -lt 45 ]; then
    echo "FAIL: auth e2e ran $auth_pass top-level tests; want at least 45." >&2
    exit 1
  fi
  echo "auth e2e: $auth_pass top-level tests passed."
fi

# 运维 CLI 的真库 e2e：backup/restore、create-master、对账表完整性。
# 与 pg/auth 同形：无 DSN 打显性 SKIP 行，有 DSN 必须真的跑够数。
if [ -z "${YIBAN_TEST_DATABASE_URL:-}" ]; then
  echo "SKIP: cli e2e did not run: YIBAN_TEST_DATABASE_URL is not set."
else
  cli_log="$(mktemp)"
  if ! go test -count=1 -v ./cmd/cli/ | tee "$cli_log"; then
    rm -f "$cli_log"
    exit 1
  fi
  cli_pass="$(grep -c '^--- PASS' "$cli_log" || true)"
  rm -f "$cli_log"
  if [ "$cli_pass" -lt 20 ]; then
    echo "FAIL: cli e2e ran $cli_pass top-level tests; want at least 20." >&2
    exit 1
  fi
  echo "cli e2e: $cli_pass top-level tests passed."
fi

# 符合性套件以 -v 跑一遍：未声明能力的判据会打印跳过行（AGENTS §30）。
go test -v -count=1 ./internal/signing/conformance/

# 真实 Provider 的适用判据也带 -v：能力跳过行必须在常规门禁可见。
go test -v -count=1 ./internal/signing/providers/yiban/

# E-4 contract drift gate: regenerating must not change the checked-in code.
# The gate fails closed: a generator that produced nothing exits 2.
bash ./scripts/generate.sh --check

# Structure gate: top-level and layer directories match structure.tsv.
go run ./tools/structure -root .. -rules structure.tsv

# License gate: every third-party dependency must carry an allowed license,
# and THIRD_PARTY_LICENSES.md must match the scan. The gate fails closed:
# a missing scanner or an incomplete scan exits 2, never green.
bash ./scripts/license.sh
