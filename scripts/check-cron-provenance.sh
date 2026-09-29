#!/usr/bin/env bash
# ============================================================
# scripts/check-cron-provenance.sh —— cron 路径来源断言（MF-42 验收不变量①）
#
# 用法：
#   scripts/check-cron-provenance.sh [--cron-dir DIR] [--manifest FILE]
#   默认：--cron-dir deploy/prod/cron.d --manifest deploy/prod/manifest.tsv
#
# 存在的原因（现象）：生产 /etc/cron.d/yiban-* 与 /usr/local/sbin/yiban-backup*.sh
# 全仓无原件 ⇒ 生产实际执行的东西不在审查范围内。本脚本把"cron 引用的每个路径都
# 能从仓库追到原件"变成机器可执行的门：审仓库 = 审生产。
#
# 判据——对每张 cron 表命令行里的每个绝对路径 token（重定向目标除外）：
#   /bin /sbin /usr/bin /usr/sbin/*        系统件，放行
#   /etc /var /tmp/*                       配置/数据/日志路径（非可执行原件），放行
#   /opt/yiban-auto-sign/<rel>             应用检出件：<rel> 必须存在于仓库
#   /usr/local/{s,}bin/<x>                 必须逐字出现在 manifest 第三列（安装清单）
#   其余绝对路径                            无来源，判红（拒绝猜测）
# 同时校验 manifest 第二列（仓库源）都存在——"原件入库"的另一半。
# 退出码：0 全部可回指；1 存在无来源路径（stdout 逐条点名 缺少来源: <路径> (文件:行)）。
# 活体反例（测试里跑）：往 cron 表加一行 /usr/local/sbin/ghost-not-in-manifest.sh ⇒ 必红。
# ============================================================
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
APP_ROOT="${YIBAN_APP_ROOT:-/opt/yiban-auto-sign}"
CRON_DIR="$REPO_ROOT/deploy/prod/cron.d"
MANIFEST="$REPO_ROOT/deploy/prod/manifest.tsv"

while [ $# -gt 0 ]; do
    case "$1" in
        --cron-dir) CRON_DIR="$2"; shift 2 ;;
        --manifest) MANIFEST="$2"; shift 2 ;;
        *) echo "check-cron-provenance: 未知参数: $1" >&2; exit 2 ;;
    esac
done

[ -d "$CRON_DIR" ] || { echo "缺少来源: cron 目录不存在: $CRON_DIR" >&2; exit 1; }
[ -f "$MANIFEST" ] || { echo "缺少来源: 清单不存在: $MANIFEST" >&2; exit 1; }

viol=0
FDREDIR_RE='^[0-9]*>&[0-9-]*$'   # 形如 2>&1 / &>/dev/null 的 fd 复制：不是重定向目标
declare -a DESTS=()

# 第一半：manifest 自洽（源存在），并收集 dest 白名单
while IFS=$'\t' read -r _mode src dest; do
    case "$_mode" in ''|\#*) continue ;; esac
    if [ ! -f "$REPO_ROOT/$src" ]; then
        echo "缺少来源: manifest 源不在仓库: $src"
        viol=1
    fi
    DESTS+=("$dest")
done < "$MANIFEST"

is_dest() {
    local t="$1" d
    for d in ${DESTS[@]+"${DESTS[@]}"}; do
        [ "$d" = "$t" ] && return 0
    done
    return 1
}

classify() {
    # $1=绝对路径 $2=cron 文件 $3=行号
    local p="$1" cf="$2" ln="$3" rel
    case "$p" in
        /bin/*|/sbin/*|/usr/bin/*|/usr/sbin/*) return 0 ;;
        /etc/*|/var/*|/tmp/*) return 0 ;;
        "$APP_ROOT"/*)
            rel="${p#"$APP_ROOT"/}"
            if [ -e "$REPO_ROOT/$rel" ]; then
                return 0
            fi
            echo "缺少来源: $p（$cf 第 $ln 行：应用检出 $APP_ROOT 应对应仓库检出，但仓库里没有 $rel）"
            viol=1
            ;;
        /usr/local/sbin/*|/usr/local/bin/*)
            if is_dest "$p"; then
                return 0
            fi
            echo "缺少来源: $p（$cf 第 $ln 行：不在 manifest 安装清单——生产件未入库）"
            viol=1
            ;;
        *)
            echo "缺少来源: $p（$cf 第 $ln 行：无法归类，拒绝猜测）"
            viol=1
            ;;
    esac
}

# 第二半：逐表逐行解析 cron 命令行
for cf in "$CRON_DIR"/*; do
    [ -f "$cf" ] || continue
    lineno=0
    while IFS= read -r line || [ -n "$line" ]; do
        lineno=$((lineno + 1))
        t="${line#"${line%%[![:space:]]*}"}"
        case "$t" in ''|'#'*) continue ;; esac
        # /etc/cron.d 允许表级 env 行（SHELL=…/MAILTO=…）——不是命令行
        if [[ "$t" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then continue; fi
        read -r _f1 _f2 _f3 _f4 _f5 _user cmdline <<< "$t"
        if [ -z "${_user:-}" ] || [ -z "${cmdline:-}" ]; then
            echo "缺少来源: $cf 第 $lineno 行无法拆解（缺用户列或命令列）"
            viol=1
            continue
        fi
        set -f  # 命令行 token 不做 glob 展开
        skip_next=0
        for tok in $cmdline; do
            if [ "$skip_next" -eq 1 ]; then skip_next=0; continue; fi
            case "$tok" in
                '>'|'>>'|'&>'|'1>'|'2>') skip_next=1; continue ;;
            esac
            if [[ "$tok" =~ $FDREDIR_RE ]]; then continue; fi  # 2>&1 之类（& 走变量，不能裸写）
            path="$tok"
            case "$tok" in
                '>>'*) path="${tok#>>}" ;;
                '>'*)  path="${tok#>}" ;;
            esac
            case "$path" in
                /*) classify "$path" "$cf" "$lineno" ;;
            esac
        done
        set +f
    done < "$cf"
done

if [ "$viol" -eq 0 ]; then
    echo "cron provenance ok: $(ls "$CRON_DIR" | wc -l) 张表、manifest ${#DESTS[@]} 条全部可回指仓库来源"
    exit 0
fi
exit 1
