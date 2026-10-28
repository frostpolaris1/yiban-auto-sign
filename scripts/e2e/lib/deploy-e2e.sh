#!/usr/bin/env bash
# ============================================================
# scripts/e2e/lib/deploy-e2e.sh — 部署 e2e 的共用件（被 source，不单独执行）。
#
# 提供三件事：
#   1) 失败步骤点名：e2e_step / e2e_ok / e2e_fail / e2e_die / e2e_summary。
#      e2e_die 打印 "failed step: <ID> <标题>" 并以 1 退出（工单 6jk 验收 3）。
#   2) 环境探测小件：e2e_require、e2e_port_free、e2e_secret_hex、e2e_secret_b64。
#   3) 日志落盘：每一步一个 <log-dir>/<ID>.log；run.log 记步骤与结论。
# 退出码约定：0 全过；1 有失败步骤；2 用法或环境错误。
# ============================================================

# e2e_init <名称> <日志目录>
# 调用一次。之后 e2e_step 才能用。
e2e_init() {
    E2E_NAME="$1"
    E2E_LOG_DIR="$2"
    mkdir -p "$E2E_LOG_DIR"
    : > "$E2E_LOG_DIR/run.log"
    E2E_RESULTS=()
    E2E_STEP_ID="P0"
    E2E_STEP_TITLE="preflight"
    E2E_STEP_LOG="$E2E_LOG_DIR/P0.log"
    : > "$E2E_STEP_LOG"
    E2E_FAILED=0
    E2E_REPORTED=0
    # 未捕获的 shell 错误也要点名失败步骤（验收 3）。
    # 需要调用方 set -E；if/while/&&/|| 里的命令按 bash 规则不进 ERR trap。
    # set -u 的致命错误不进 ERR trap，由调用方 cleanup 里的 e2e_exit_note 兜底。
    trap 'e2e_on_err $?' ERR
}

# e2e_note_failed_step <原因>：打印并标记「已点名」，避免重复。
e2e_note_failed_step() {
    local why="$1"
    [ "$E2E_REPORTED" -eq 1 ] && return 0
    E2E_REPORTED=1
    e2e_say ""
    e2e_say "failed step: $E2E_STEP_ID ($E2E_STEP_TITLE) — $why"
    e2e_tail "$E2E_STEP_LOG" 20
}

# e2e_on_err <rc>：ERR trap 的处理体。
e2e_on_err() {
    local rc="$1"
    local cmd="${BASH_COMMAND:-<unknown>}"
    e2e_note_failed_step "未捕获的 shell 错误 rc=$rc，command: $cmd"
    e2e_summary || true
    exit 1
}

# e2e_exit_note <rc>：EXIT trap 的兜底。set -u 等致命错误不走 ERR trap，
# 由调用方在自己的 cleanup 开头调用本函数，保证失败仍被点名。
e2e_exit_note() {
    local rc="$1"
    if [ "$rc" -ne 0 ]; then
        e2e_note_failed_step "退出码 $rc（未捕获）"
    fi
    return 0
}

# e2e_say <文本>：同时写屏与 run.log。
e2e_say() {
    printf '%s\n' "$*" | tee -a "$E2E_LOG_DIR/run.log"
}

# e2e_step <ID> <标题>：开启一步，切换到该步日志。
e2e_step() {
    E2E_STEP_ID="$1"
    E2E_STEP_TITLE="$2"
    E2E_STEP_LOG="$E2E_LOG_DIR/$1.log"
    : > "$E2E_STEP_LOG"
    e2e_say ""
    e2e_say "==== [$1] $2 ===="
}

# e2e_ok [备注]：记本步为 PASS。
e2e_ok() {
    local note="${1:-}"
    E2E_RESULTS+=("$E2E_STEP_ID|PASS|$note")
    if [ -n "$note" ]; then
        e2e_say "  [PASS] $E2E_STEP_ID $E2E_STEP_TITLE — $note"
    else
        e2e_say "  [PASS] $E2E_STEP_ID $E2E_STEP_TITLE"
    fi
}

# e2e_fail [备注]：记本步为 FAIL，不做退出。
e2e_fail() {
    local note="${1:-}"
    E2E_RESULTS+=("$E2E_STEP_ID|FAIL|$note")
    E2E_FAILED=1
    if [ -n "$note" ]; then
        e2e_say "  [FAIL] $E2E_STEP_ID $E2E_STEP_TITLE — $note"
    else
        e2e_say "  [FAIL] $E2E_STEP_ID $E2E_STEP_TITLE"
    fi
}

# e2e_tail <日志文件> [行数]：把尾部打到屏上。
e2e_tail() {
    local file="$1" lines="${2:-20}"
    [ -f "$file" ] || return 0
    e2e_say "  --- $file (tail $lines) ---"
    tail -n "$lines" "$file" | sed 's/^/  /' | tee -a "$E2E_LOG_DIR/run.log"
}

# e2e_die <原因>：记 FAIL、点名失败步骤、打印汇总、退出 1。
# 清理由调用方的 EXIT trap 承担（步号已打印，排障按步号找日志）。
e2e_die() {
    e2e_fail "$*"
    e2e_note_failed_step "$*"
    e2e_summary || true
    exit 1
}

# e2e_summary：打印 PASS/FAIL 表与 result 行。不改退出码。
e2e_summary() {
    local row state note id title
    e2e_say ""
    e2e_say "==== 结果汇总 ===="
    for row in "${E2E_RESULTS[@]:-}"; do
        [ -n "$row" ] || continue
        id="${row%%|*}"
        row="${row#*|}"
        state="${row%%|*}"
        note="${row#*|}"
        if [ -n "$note" ]; then
            e2e_say "  $state  $id  $note"
        else
            e2e_say "  $state  $id"
        fi
    done
    if [ "$E2E_FAILED" -ne 0 ]; then
        e2e_say "result=FAIL"
        return 1
    fi
    e2e_say "result=PASS"
    return 0
}

# e2e_require <命令>：缺命令即以用法/环境错误退出（退出码 2）。
e2e_require() {
    command -v "$1" >/dev/null 2>&1 || {
        e2e_fail "缺少命令：$1（环境错误，退出码 2）"
        e2e_note_failed_step "缺少命令：$1"
        e2e_summary || true
        exit 2
    }
}

# e2e_port_free <端口>：127.0.0.1 上无监听返回 0，有监听返回 1。
e2e_port_free() {
    local port="$1"
    if timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/$port" 2>/dev/null; then
        return 1
    fi
    return 0
}

# e2e_secret_hex <字节数>：十六进制随机串。
e2e_secret_hex() {
    local n="$1"
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "$n"
        return
    fi
    od -An -N"$n" -tx1 /dev/urandom | tr -d ' \n'
}

# e2e_secret_b64 <字节数>：标准 base64 随机串（单行，解码后恰好 n 字节）。
#
# 只走 /dev/urandom + base64 一条路径：openssl 分支与兜底分支曾产出不同形状，
# 兜底产出的是 hex；配置要求该键 base64 解码后 32 字节，缺 openssl 时服务拒启。
# 产出后立刻自检解码长度；不合格按环境错误返回非 0（调用方的 ERR 兜底会点名步骤）。
e2e_secret_b64() {
    local n="$1" v got
    v="$(head -c "$n" /dev/urandom | base64 | tr -d '\n')" || return 1
    got="$(printf '%s' "$v" | base64 -d 2>/dev/null | wc -c | tr -d ' ')"
    if [ "$got" != "$n" ]; then
        printf 'e2e_secret_b64: 产出解码后 %s 字节，期望 %s\n' "$got" "$n" >&2
        return 1
    fi
    printf '%s' "$v"
}

# e2e_sha256 <文件>：优先 sha256sum，其次 shasum。
e2e_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        return 1
    fi
}

# e2e_expected_schema_version <仓库根>：取 server/migrations/postgres 的最大编号。
# 不从散文抄版本号：新增迁移时断言自动跟上。
e2e_expected_schema_version() {
    local repo="$1" dir="$1/server/migrations/postgres" f base num max=0
    for f in "$dir"/*.sql; do
        [ -f "$f" ] || continue
        base="$(basename "$f")"
        num="${base%%_*}"
        case "$num" in
            ''|*[!0-9]*) continue ;;
        esac
        num=$((10#$num))
        [ "$num" -gt "$max" ] && max="$num"
    done
    printf '%s' "$max"
}
