#!/usr/bin/env bash
# ============================================================
# scripts/e2e/ui-signing-e2e.sh — UI 入签名清单的本地 e2e（P1-6 ③/④）
#
# 为什么存在：ui-SHA256SUMS 必须放在发布 staging 里、与 ui/dist 平级，
# 并由 update.sh 在停服/换件之前逐文件校验。本脚本用假 root + 假 systemctl
# + 假 pg 客户端干跑真 update.sh，不需要 docker、root 或数据库，
# 因此可在本地与容器里单跑。
#
# 用例（每条都断言「失败先于停服/换件」：旧页面未变、无 ui.bak、无 stop）：
#   success      正常 UI（manifest 三件）→ rc=0，页面换新，旧页留 ui.bak
#   tampered     index.html 被篡改 → rc=1
#   extra        UI 里塞入清单外文件 → rc=1
#   symlink      UI 树里塞软链 → rc=1
#   no-ui-entry  manifest 无 ui 条目却给 --ui-dir → rc=1（互锁）
#
# 用法（仓库根目录）：
#   bash scripts/e2e/ui-signing-e2e.sh                  # 全部用例
#   bash scripts/e2e/ui-signing-e2e.sh tampered         # 单跑一个
#   bash scripts/e2e/ui-signing-e2e.sh --keep --log-dir /tmp/ui-e2e success
# 退出码：0 全过；1 有用例失败；2 用法/环境错误。
# ============================================================
set -Eeuo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SELF_DIR/../.." && pwd)"
UPDATE="$REPO/deploy/baremetal/update.sh"
RELEASE_DIR="$REPO/scripts/release"
MAKE_MANIFEST="$RELEASE_DIR/make-manifest.sh"
MAKE_UI_SUMS="$RELEASE_DIR/make-ui-sums.sh"
SIGN_SH="$RELEASE_DIR/sign.sh"
VERIFY_SH="$RELEASE_DIR/verify.sh"

DSN="postgres://yiban:pw@127.0.0.1:5432/yiban?sslmode=disable"
KEEP=0
LOG_ROOT=""
CASES=()

say() { printf '%s\n' "$*"; }
die() { local code="$2"; say "ui-signing-e2e: $1" >&2; exit "$code"; }
usage() {
    sed -n '2,30p' "$0"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1; shift ;;
        --log-dir)
            [ "$#" -ge 2 ] || die "--log-dir 需要参数" 2
            LOG_ROOT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        success|tampered|extra|symlink|no-ui-entry) CASES+=("$1"); shift ;;
        *) die "未知参数：$1（--help 看用法）" 2 ;;
    esac
done
[ "${#CASES[@]}" -gt 0 ] || CASES=(success tampered extra symlink no-ui-entry)

for cmd in bash openssl sha256sum mktemp; do
    command -v "$cmd" >/dev/null 2>&1 || die "缺少命令：$cmd" 2
done
for f in "$UPDATE" "$MAKE_MANIFEST" "$MAKE_UI_SUMS" "$SIGN_SH" "$VERIFY_SH"; do
    [ -f "$f" ] || die "缺少仓库文件：$f" 2
done

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ui-signing-e2e.XXXXXX")" || die "创建临时目录失败" 2
say "工作目录：$WORK"
[ -z "$LOG_ROOT" ] || mkdir -p "$LOG_ROOT" || die "创建日志目录失败：$LOG_ROOT" 2

cleanup() {
    if [ "$KEEP" -eq 1 ]; then
        say "（--keep）保留工作目录：$WORK"
    else
        rm -rf -- "$WORK"
    fi
}
trap cleanup EXIT

# ---- 桩件 --------------------------------------------------------------
write_fake_server() {
    cat > "$1" <<SH
#!/bin/sh
echo "$2"
SH
    chmod 755 "$1"
}

# write_fake_cli 写 yiban-cli 桩：selfcheck/version/backup/healthcheck 安全缺省。
# selfcheck --json 的形状必须与 update.sh 的逐行解析一致（name/status 各占一行）。
write_fake_cli() {
    cat > "$1" <<'CLI'
#!/bin/sh
case "$1" in
  selfcheck)
    case "$2" in
      --json)
        cat <<'JSON'
{
  "schema_version": 1,
  "generated_at": "2026-11-04T00:00:00Z",
  "build": {
    "version": "0.0.0-test",
    "commit": "test",
    "build_time": "test"
  },
  "checks": [
    {
      "name": "config.dsn",
      "status": "PASS",
      "detail": "已设置"
    }
  ],
  "summary": {
    "pass": 1,
    "fail": 0,
    "skip": 0,
    "exit_code": 0
  }
}
JSON
        exit 0 ;;
    esac ;;
  version) printf 'version: 0.0.0-test\ncommit: test\nbuild_time: test\n' ;;
  backup) exit 0 ;;
  healthcheck) exit 0 ;;
  migrate) echo "schema version 16" ;;
esac
exit 0
CLI
    chmod 755 "$1"
}

write_fake_pg() {
    local dir="$1"
    cat > "$dir/pg_dump" <<'SH'
#!/bin/sh
out=""
for a in "$@"; do case "$a" in --file=*) out="${a#--file=}" ;; esac; done
if [ -n "$out" ]; then : > "$out"; fi
exit 0
SH
    cat > "$dir/pg_restore" <<'SH'
#!/bin/sh
for a in "$@"; do
  case "$a" in --list) echo "; TOC"; echo "1; 1259 16385 TABLE public users yiban"; exit 0 ;; esac
done
exit 0
SH
    cat > "$dir/psql" <<'SH'
#!/bin/sh
case "$*" in
  *rolcreatedb*) echo t ;;
  *goose_db_version*) echo 16 ;;
  *information_schema.tables*) echo 20 ;;
  *current_database*) printf 'yiban' ;;
  *) echo 0 ;;
esac
exit 0
SH
    chmod 755 "$dir/pg_dump" "$dir/pg_restore" "$dir/psql"
}

# setup_case <name>：全新的假安装位，互不污染。
setup_case() {
    CASE_DIR="$WORK/$1"
    ROOT="$CASE_DIR/root"
    BIN="$CASE_DIR/bin"
    RELEASE="$CASE_DIR/release"
    UI="$CASE_DIR/ui-new"
    SYSCTL_LOG="$CASE_DIR/systemctl.log"
    mkdir -p "$ROOT/usr/local/bin" "$ROOT/var/lib/yiban/ui" "$ROOT/etc/yiban" \
        "$BIN" "$RELEASE" "$UI/assets"
    printf 'old-ui' > "$ROOT/var/lib/yiban/ui/index.html"
    printf 'YIBAN_UI_DIR=/var/lib/yiban/ui\nYIBAN_HTTP_ADDR=127.0.0.1:47183\n' \
        > "$ROOT/etc/yiban/server.env"
    chmod 600 "$ROOT/etc/yiban/server.env"
    write_fake_server "$ROOT/usr/local/bin/yiban-server" OLD-SERVER
    write_fake_cli "$ROOT/usr/local/bin/yiban-cli"
    write_fake_server "$BIN/yiban-server" NEW-SERVER
    write_fake_cli "$BIN/yiban-cli"
    write_fake_pg "$BIN"
    cat > "$BIN/systemctl" <<SH
#!/bin/sh
printf 'systemctl %s\n' "\$*" >> "$SYSCTL_LOG"
exit 0
SH
    chmod 755 "$BIN/systemctl"
    cp "$BIN/yiban-server" "$RELEASE/yiban-server"
    cp "$BIN/yiban-cli" "$RELEASE/yiban-cli"
    chmod 755 "$RELEASE/yiban-server" "$RELEASE/yiban-cli"
    printf '<div id="root">new-ui</div>' > "$UI/index.html"
    printf '// new-ui' > "$UI/assets/app-1.js"
}

# build_signed_release <with_ui 1|0>：生成 ui-SHA256SUMS（可选进清单）并签发三件。
build_signed_release() {
    local with_ui="$1"
    bash "$MAKE_UI_SUMS" "$UI" "$RELEASE/ui-SHA256SUMS" >/dev/null
    local files=("$RELEASE/yiban-server" "$RELEASE/yiban-cli")
    if [ "$with_ui" = "1" ]; then
        files+=("$RELEASE/ui-SHA256SUMS")
    fi
    bash "$MAKE_MANIFEST" "e2e-ui" "${files[@]}" > "$RELEASE/manifest.json"
    openssl genpkey -algorithm ED25519 -out "$RELEASE/key.pem" 2>>"$CASE_LOG" \
        || die "openssl 生成私钥失败" 1
    openssl pkey -in "$RELEASE/key.pem" -pubout -out "$RELEASE/pub.pem" 2>>"$CASE_LOG" \
        || die "openssl 导出公钥失败" 1
    bash "$SIGN_SH" --key "$RELEASE/key.pem" --in "$RELEASE/manifest.json" \
        --out "$RELEASE/manifest.sig" >>"$CASE_LOG" 2>&1 || die "sign.sh 失败" 1
    PIN="$(sha256sum "$RELEASE/pub.pem" | cut -d' ' -f1)"
    bash "$VERIFY_SH" --pubkey "$RELEASE/pub.pem" --manifest "$RELEASE/manifest.json" \
        --signature "$RELEASE/manifest.sig" >>"$CASE_LOG" 2>&1 \
        || die "正路径验签失败（夹具坏了）" 1
}

run_update() {
    set +e
    OUT="$(env YIBAN_INSTALL_ROOT="$ROOT" SYSTEMCTL="$BIN/systemctl" \
        YIBAN_DATABASE_URL="$DSN" YIBAN_HEALTH_RETRIES=1 \
        YIBAN_HEALTH_RETRY_INTERVAL=0 YIBAN_RELEASE_PUBKEY_SHA256="$PIN" \
        PATH="$BIN:$PATH" \
        bash "$UPDATE" --server-bin "$RELEASE/yiban-server" \
        --cli-bin "$RELEASE/yiban-cli" \
        --manifest "$RELEASE/manifest.json" --signature "$RELEASE/manifest.sig" \
        --pubkey "$RELEASE/pub.pem" "$@" 2>&1)"
    RC=$?
    set -e
    printf '\n---- update.sh rc=%s ----\n%s\n' "$RC" "$OUT" >> "$CASE_LOG"
}

# ---- 断言 --------------------------------------------------------------
fail() {
    say "  [FAIL] $1" >&2
    say "---- update.sh 输出 ----" >&2
    printf '%s\n' "$OUT" >&2
    return 1
}
expect_rc() { [ "$RC" = "$1" ] || fail "rc=$RC（期望 $1）"; }
expect_out() {
    case "$OUT" in *"$1"*) return 0 ;; esac
    fail "输出里没有：$1"
}
assert_page_untouched() {
    local active
    active="$(cat "$ROOT/var/lib/yiban/ui/index.html" 2>/dev/null || true)"
    if [ "$active" != "old-ui" ]; then
        fail "旧页面被改写（现在：$active）"
        return 1
    fi
    if [ -e "$ROOT/var/lib/yiban/ui.bak" ]; then
        fail "失败路径留下了 ui.bak"
        return 1
    fi
    if [ -f "$SYSCTL_LOG" ] && grep -q 'stop' "$SYSCTL_LOG"; then
        fail "校验失败发生在停服之后（systemctl 日志出现 stop）"
        return 1
    fi
    return 0
}
assert_page_activated() {
    if ! grep -q 'new-ui' "$ROOT/var/lib/yiban/ui/index.html"; then
        fail "激活后的页面不是新页面"
        return 1
    fi
    if [ ! -f "$ROOT/var/lib/yiban/ui.bak/index.html" ]; then
        fail "旧页面没有留在 ui.bak"
        return 1
    fi
    if [ "$(cat "$ROOT/var/lib/yiban/ui.bak/index.html")" != "old-ui" ]; then
        fail "ui.bak 里不是旧页面"
        return 1
    fi
    if ! grep -q 'stop' "$SYSCTL_LOG"; then
        fail "成功路径没有停服"
        return 1
    fi
    if ! grep -q 'start' "$SYSCTL_LOG"; then
        fail "成功路径没有起服"
        return 1
    fi
    return 0
}

# ---- 用例 --------------------------------------------------------------
case_success() {
    setup_case success
    build_signed_release 1
    run_update --ui-dir "$UI"
    expect_rc 0 || return 1
    expect_out "ui verification ok" || return 1
    expect_out "signing verification ok" || return 1
    assert_page_activated || return 1
    return 0
}

case_tampered() {
    setup_case tampered
    build_signed_release 1
    printf '<div id="root">tampered</div>' > "$UI/index.html"
    run_update --ui-dir "$UI"
    expect_rc 1 || return 1
    expect_out "signing verification FAILED (step: ui)" || return 1
    expect_out "ui file contents do not match" || return 1
    assert_page_untouched || return 1
    return 0
}

case_extra() {
    setup_case extra
    build_signed_release 1
    printf 'not signed' > "$UI/evil.txt"
    run_update --ui-dir "$UI"
    expect_rc 1 || return 1
    expect_out "FAILED (step: ui)" || return 1
    expect_out "outside the signed list" || return 1
    assert_page_untouched || return 1
    return 0
}

case_symlink() {
    setup_case symlink
    build_signed_release 1
    ln -s index.html "$UI/assets/alias.html"
    run_update --ui-dir "$UI"
    expect_rc 1 || return 1
    expect_out "refusing symlink in ui dir" || return 1
    assert_page_untouched || return 1
    return 0
}

case_no-ui-entry() {
    setup_case no-ui-entry
    build_signed_release 0
    run_update --ui-dir "$UI"
    expect_rc 1 || return 1
    expect_out "manifest has no ui-SHA256SUMS entry but --ui-dir was given" || return 1
    assert_page_untouched || return 1
    return 0
}

# ---- 驱动 --------------------------------------------------------------
FAILED=0
PASSED=0
for name in "${CASES[@]}"; do
    say "== 用例 $name"
    if [ -n "$LOG_ROOT" ]; then
        CASE_LOG="$LOG_ROOT/$name.log"
    else
        CASE_LOG="$WORK/$name.log"
    fi
    : > "$CASE_LOG"
    if "case_$name"; then
        say "  [PASS] $name"
        PASSED=$((PASSED + 1))
    else
        say "  [FAIL] $name（日志：$CASE_LOG）" >&2
        FAILED=$((FAILED + 1))
    fi
done

say "ui-signing-e2e: passed=$PASSED failed=$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
say "OK：UI 签名链在失败时先于停服/换件拒绝，正常 UI 可通过"
