#!/usr/bin/env bash
# scripts/dev-verify.sh —— 仓内统一跑测入口（本地与 CI 同源）。
#
# 2026-11-27 旧 Python 栈退役后，本脚本只承载新栈的 Python 面：
#   ruff（tests/ 与 scripts/）+ pytest tests/。
# server/ui 的门在 server/scripts/verify.sh 与 ui 的 npm scripts，由 CI 的
# stacks 作业经 scripts/verify-stacks.sh 调用；两条栈不在这里混跑。
#
# 用法：
#   bash scripts/dev-verify.sh            # 全量（ruff + pytest tests/）
#   bash scripts/dev-verify.sh --ci       # CI 入口（同上，不建副本、不落日志）
#   bash scripts/dev-verify.sh --fast     # 提交前自查（同集合，留给调用方习惯）
#   bash scripts/dev-verify.sh --slow     # 兼容旧调用；本仓已无独立慢集
#   --log-dir DIR                         # 兼容旧调用；保留参数，不再落日志
#
# 退出码：0 通过；1 有红；2 环境错误。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${DEV_VERIFY_PY:-python3}"
MODE="full"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --ci|--fast|--slow|--fast-all|--fast-scoped) MODE="${1#--}" ;;
        --log-dir) shift ;;
        --log-dir=*) ;;
        --repo) shift ;;
        --repo=*) ;;
        -h|--help)
            sed -n '1,26p' "${BASH_SOURCE[0]}" | sed 's/^# {0,1}//'
            exit 0
            ;;
        *) echo "dev-verify: 未知参数：$1" >&2; exit 2 ;;
    esac
    shift
done

die() { echo "dev-verify: 环境错误：$*" >&2; exit 2; }

command -v "$PY" >/dev/null 2>&1 || die "找不到解释器：$PY"
PY="$(command -v "$PY")"
"$PY" -c "import pytest, ruff" >/dev/null 2>&1 || die "解释器缺 pytest/ruff：$PY"
cd "$REPO"

run_ci() {
    # CI 与本地共用同一条命令面：ruff 先跑，pytest 后跑。
    # 公开数据红线门在 run_ci 内显式点名，且与接线守卫同一步执行；
    # 不放在任何条件分支里（旧线退役时保留这条约束）。
    local py="$PY"
    local cores workers
    cores="$(nproc 2>/dev/null || echo 1)"
    workers="${PYTEST_WORKERS:-$(( cores > 1 ? cores - 1 : 1 ))}"
    echo "DEV-VERIFY($MODE) interpreter: $py ($("$py" -V 2>&1))"
    echo "DEV-VERIFY($MODE) repo: $REPO"
    echo "DEV-VERIFY($MODE) ruff"
    "$py" -m ruff check tests scripts --quiet || return 1
    echo "DEV-VERIFY($MODE) public exposure gate"
    "$py" -m pytest tests/test_public_exposure_gate.py \
        tests/test_public_exposure_gate_wiring.py -q || return 1
    echo "DEV-VERIFY($MODE) pytest tests/ (workers=$workers)"
    "$py" -m pytest tests/ -q -n "$workers" --dist loadfile || return 1
    echo "DEV-VERIFY($MODE) OK"
}

rc=0
run_ci || rc=$?
echo "DEV-VERIFY($MODE) exit_code=$rc"
exit "$rc"
