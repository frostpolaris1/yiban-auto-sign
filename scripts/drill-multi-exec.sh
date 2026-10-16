#!/usr/bin/env bash
# ============================================================
# scripts/drill-multi-exec.sh —— 本地多执行体演练靶场入口（零外联）
# ============================================================
# 用途：
#   在**本地 WSL**里验"多个执行体怎么协调"这类性质，不必依赖真平台、不必 docker。
#   它起一个本地假易班上游（yiban/adapters/protocol_fake/__init__.py --no-tls），拉起**真实的监督
#   进程 ＋ 真实执行体子进程**（python -m yiban.cli sign → workers.run_worker_supervisor
#   → executor_v3 → signin），并用传输层改写把子进程的全部 https 请求钉回回环假上游。
#   协调逻辑（领取/收尾/收尸/换代/肘界）走真码，只有"网络去往哪里"被改写。
#
#   详细的判据与边界说明见 scripts/drill_multi_exec.py 的模块头。
#
# 用法（Windows Git Bash 或 WSL 内，同一条命令；脚本自己进 WSL）：
#   bash scripts/drill-multi-exec.sh --executors 2 --accounts 6
#   bash scripts/drill-multi-exec.sh --executors 3 --accounts 9 --kill-executor-at claim
#   bash scripts/drill-multi-exec.sh --executors 2 --accounts 6 --slow-executor 0:20
#   bash scripts/drill-multi-exec.sh --executors 2 --accounts 6 \
#        --kill-executor-at claim --stale-write
#   bash scripts/drill-multi-exec.sh --mutate reap-off --kill-executor-at claim   # 有牙证明（应 FAIL）
#
# 参数（原样透传给 scripts/drill_multi_exec.py；-h 看全）：
#   --executors N              执行体数（注入 YIBAN_EXECUTORS，默认 2）
#   --accounts M               账号数（默认 6）
#   --kill-executor-at claim|SEC   在"首个在飞行被观测到"或 N 秒后 SIGKILL 目标执行体
#   --slow-executor IDENTITY:SEC   让目标执行体每次领到后卡住 SEC 秒（造起飞竞态）。
#                                  IDENTITY = 槽位号 / 完整身份串 / `all`（对所有执行体）。
#   --stale-write              用被杀执行体的旧 owner ＋ 旧 epoch 发收尾写，验 fence
#   --mutate reap-off|claim-steal  对协调层做变异（有牙证明用；正常演练不传）
#   --evidence-dir DIR         证据目录（默认不落盘；报告要求留证据时给它）
#   --timeout-sec S            等监督进程收尾的超时（默认 120）
#
# 前提：
#   1. 只在 WSL 跑（本脚本自己进 WSL）；解释器固定 /root/.venv-yiban-wsl/bin/python。
#   2. 无外联：全程只对回环假上游打。传输层改写是结构性保证，不是靠自觉。
#
# 退出码：
#   0  全部判据通过（两条不变式绿 ＋ 账号全部了结）
#   1  有不变式或断言失败
#   2  前提不满足（解释器缺失等）
# ============================================================
set -euo pipefail

FIXED_VENV=/root/.venv-yiban-wsl
DEFAULT_DISTRO=Ubuntu

die() { echo "drill-multi-exec: 环境错误：$*" >&2; exit 2; }

win_to_unix() {
    local p=$1
    p=${p//\\//}
    p=${p#//?/}
    case "$p" in
        /mnt/[a-z]/*) printf '%s\n' "$p" ;;
        /[a-z]/*) printf '/mnt%s\n' "$p" ;;
        [A-Za-z]:/*) printf '/mnt/%s%s\n' "$(printf '%s' "${p:0:1}" | tr 'A-Z' 'a-z')" "${p:2}" ;;
        *) printf '%s\n' "$p" ;;
    esac
}

# Windows 侧（Git Bash / MSYS）：换算路径后进 WSL 跑同一条命令
case "$(uname -s)" in
    Linux) ;;
    *)
        here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -W)
        args=()
        while [ $# -gt 0 ]; do
            case "$1" in
                --evidence-dir) args+=("$1" "$(win_to_unix "${2:?$1 需要取值}")"); shift ;;
                *) args+=("$1") ;;
            esac
            shift
        done
        MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' exec \
            wsl.exe -d "$DEFAULT_DISTRO" -u root -- \
            bash "$(win_to_unix "$here/drill-multi-exec.sh")" ${args[@]+"${args[@]}"}
        ;;
esac

# Linux/WSL 侧
SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
PY="$FIXED_VENV/bin/python"
[ -x "$PY" ] || die "找不到固定解释器：$PY（本靶场只在 WSL 跑，且只认 $FIXED_VENV）"
exec "$PY" "$SELF_DIR/drill_multi_exec.py" "$@"
