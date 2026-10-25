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
# 判据——对每张 cron 表命令行里的每个路径 token（重定向目标除外）：
#   /bin /sbin /usr/bin /usr/sbin/*        系统件，放行
#   /etc /var/*                            配置/数据/日志路径（非可执行原件），放行
#   **/tmp/* 不放行**（R3-022）：临时目录里的可执行件没有来源，拒猜。
#   /opt/yiban-auto-sign/<rel>             应用检出件：<rel> 必须存在于仓库
#   /usr/local/{s,}bin/<x>                 必须逐字出现在 manifest 第三列（安装清单）
#   其余绝对路径                            无来源，判红（拒绝猜测）
#
# 值不是路径（B-1，2026-10-25）：`VAR=…`／`--opt=…` 的值不因含 `/` 就当路径。
#   带 scheme 前缀的 URL（`https://…`；`file://` 除外）不是路径。
#   scheme 必须是值开头（首字符字母，后续字母/数字/+.-，再接 `://`）。
#   裸含 `://` 不算 URL——`/tmp/x.sh://` 与 `../../etc/passwd://` 仍按路径判。
#   全段为数字的比值（`30/7`）不是路径；
#   绝对路径、`./`／`../`／`~/` 与其余含 `/` 的值仍按路径判。
#   纯字符串判形，不识别每个旗标的参数语义（例：`--pattern=a/b` 仍按路径判）。
#
# 重定向目标（B-2，2026-10-25）：分离式（`> /var/log/x`）与贴紧式
#   （`2>/dev/null`、`2>>/var/log/x`、`&>/dev/null`）都不判。
#
# 相对路径（R3-022）：只在**同一命令行先 `cd` 到检出内**时判定。
#   门按 shell 语义跟踪 cwd：`cd <检出>` 之后的相对 token（含 `--db data/yiban.db`
#   这类参数）解析成 `$APP_ROOT/<rel>`，再看它是否可回指仓库。
#   没有 cd、或 cd 到检出外 ⇒ 红（无法定位 = 无来源）。
#   两条**显式豁免**（部署期创建、不入库，登记在此而非静默放行）：
#     `.venv/*` —— 虚拟环境（README 第一步建）
#     `data/*`  —— 运行数据（.env 与 SQLite 库，install.sh 建）
#   除此之外的相对路径必须在仓库里真实存在。
# 同时校验 manifest 第二列（仓库源）都存在——"原件入库"的另一半。
# 退出码：0 全部可回指；1 存在无来源路径（stdout 逐条点名 缺少来源: <路径> (文件:行)）。
# 活体反例（测试里跑）：往 cron 表加一行 /usr/local/sbin/ghost-not-in-manifest.sh ⇒ 必红；
#   /tmp/x.sh、未 cd 的相对路径、cd 到检出外的相对路径同样必红
#   （tests/test_cron_provenance_paths.py）。
# ============================================================
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# 生产检出根：唯一环境键 YIBAN_APP_DIR；YIBAN_APP_ROOT 只作旧部署别名（census-P2-repo-root）。
APP_ROOT="${YIBAN_APP_DIR:-${YIBAN_APP_ROOT:-/opt/yiban-auto-sign}}"
# 尾斜杠会被下面的"<前缀>/<rel>"拼接吃掉；先归一，`cd /opt/…/` 与 `cd /opt/…` 同判。
while [ "${APP_ROOT%/}" != "$APP_ROOT" ]; do APP_ROOT="${APP_ROOT%/}"; done
CRON_DIR="$REPO_ROOT/deploy/prod/cron.d"
MANIFEST="$REPO_ROOT/deploy/prod/manifest.tsv"

while [ $# -gt 0 ]; do
    case "$1" in
        --cron-dir|--manifest)
            # 缺值不是"有来源违规"：set -u 下直接引用 "$2" 会以 unbound variable 退出 1，
            # 与头注释的 1=存在无来源路径 混码。用法错误一律 rc=2。
            if [ $# -lt 2 ]; then
                echo "check-cron-provenance: $1 缺少值" >&2
                echo "用法: $0 [--cron-dir DIR] [--manifest FILE]" >&2
                exit 2
            fi
            if [ "$1" = "--cron-dir" ]; then CRON_DIR="$2"; else MANIFEST="$2"; fi
            shift 2 ;;
        *) echo "check-cron-provenance: 未知参数: $1" >&2; exit 2 ;;
    esac
done

[ -d "$CRON_DIR" ] || { echo "缺少来源: cron 目录不存在: $CRON_DIR" >&2; exit 1; }
[ -f "$MANIFEST" ] || { echo "缺少来源: 清单不存在: $MANIFEST" >&2; exit 1; }

viol=0
FDREDIR_RE='^[0-9]*>&[0-9-]*$'   # 形如 2>&1 / &>/dev/null 的 fd 复制：不是重定向目标
# 贴紧式重定向（目标与操作符同一 token）：2>/dev/null、2>>/var/log/x、&>/dev/null。
# `2>&1` 由 FDREDIR_RE 先处理，故这里排除后随 `&`（[^&=]）。
FDREDIR_ATTACHED_RE='^[0-9]*&?>>?[^&=].*$'
declare -a DESTS=()
N_TOKENS=0                        # 真正判定过的路径 token 数（"全跳过"判据，R3-010）
N_TABLES=0

# 纯字符串路径归一（不碰文件系统）：折叠 `.`、`..` 与重复斜杠。
normalize_abs() {
    local IFS=/ part out=()
    for part in $1; do
        case "$part" in
            ''|.) ;;
            ..) [ "${#out[@]}" -gt 0 ] && unset 'out[${#out[@]}-1]' ;;
            *) out+=("$part") ;;
        esac
    done
    printf '/%s\n' "$(IFS=/; printf '%s' "${out[*]:-}")"
}

# 部署期创建、不入库的检出内路径（显式豁免，登记见文件头）。
is_runtime_path() {
    case "$1" in
        .venv/*|data/*) return 0 ;;
        *) return 1 ;;
    esac
}

# 值什么时候是路径候选（B-1）。判形不判旗标语义：
#   URL（`scheme://…`，但 `file://` 的载荷是路径）不是；
#   全段为数字的比值（`30/7`）不是；
#   绝对、`./`／`../`／`~/`、其余含 `/` 的值都是。
is_path_value() {
    local v="$1" seg
    case "$v" in
        # 只认真正的 scheme 前缀（RFC 3986）：首字符字母，后续字母/数字/+.-，再接 `://`。
        # 裸含 `://` 不算——否则 `/tmp/x.sh://` 这类尾巴能绕过绝对路径判定。
        [A-Za-z][A-Za-z0-9+.-]*://*)
            case "$v" in
                [Ff][Ii][Ll][Ee]://*) return 0 ;;
                *) return 1 ;;
            esac ;;
        /*|./*|../*|~/*) return 0 ;;
    esac
    case "$v" in
        */*)
            local IFS=/
            for seg in $v; do
                case "$seg" in
                    ''|*[!0-9]*) return 0 ;;
                esac
            done
            return 1 ;;
        *) return 1 ;;
    esac
}

# 第一半：manifest 自洽（源存在），并收集 dest 白名单
while IFS=$'\t' read -r _mode src dest; do
    case "$_mode" in ''|\#*) continue ;; esac
    if [ ! -f "$REPO_ROOT/$src" ]; then
        echo "缺少来源: manifest 源不在仓库: $src"
        viol=1
    fi
    DESTS+=("$dest")
done < "$MANIFEST"

# 防"扫空当绿"（R3-010）：空清单的"全部可回指"是空真，不是证据。
# 下限 = 1，依据：本门的存在理由是"每个 cron 引用的路径都能追到原件"；清单 0 条时
# 它连一个可追的安装件都没有，任何结论都不成立。
if [ "${#DESTS[@]}" -eq 0 ]; then
    echo "缺少来源: manifest 没有任何安装条目（0 条）：${MANIFEST}"
    viol=1
fi

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
    p="$(normalize_abs "$p")"
    case "$p" in
        /bin/*|/sbin/*|/usr/bin/*|/usr/sbin/*) return 0 ;;
        /etc/*|/var/*) return 0 ;;
        "$APP_ROOT"|"$APP_ROOT"/*)
            rel="${p#"$APP_ROOT"}"
            rel="${rel#/}"
            if [ -e "$REPO_ROOT/$rel" ]; then
                return 0
            fi
            echo "缺少来源: $p（$cf 第 $ln 行：应用检出 $APP_ROOT 应对应仓库检出，但仓库里没有 ${rel:-.}）"
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
    N_TABLES=$((N_TABLES + 1))
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
        want_cd=0
        CWD=""   # shell 语义的当前目录：只有本命令行内的 cd 才会设置
        for tok in $cmdline; do
            if [ "$skip_next" -eq 1 ]; then skip_next=0; continue; fi
            case "$tok" in
                '>'|'>>'|'&>'|'1>'|'2>') skip_next=1; continue ;;
            esac
            if [[ "$tok" =~ $FDREDIR_RE ]]; then continue; fi  # 2>&1 之类（& 走变量，不能裸写）
            if [[ "$tok" =~ $FDREDIR_ATTACHED_RE ]]; then continue; fi  # 2>/dev/null 等（B-2）
            # 环境前缀赋值（VAR=…）与 --opt=value：值按路径形态判（B-1）。
            # R3-022 等价类仍在：`=` 右边的真路径（绝对/显式相对/含 `/`）都要判。
            env_value=""
            if [[ "$tok" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
                env_value="${tok#*=}"
            elif [[ "$tok" == --*=* ]]; then
                env_value="${tok#*=}"
            fi
            if [ -n "$env_value" ]; then
                is_path_value "$env_value" || continue
                path="$env_value"
            else
                path="$tok"
                case "$tok" in
                    '>>'*) path="${tok#>>}" ;;
                    '>'*)  path="${tok#>}" ;;
                esac
            fi
            # `cd` 的目标是目录：它决定后续相对路径的基准（R3-022）。
            if [ "$want_cd" -eq 1 ]; then
                want_cd=0
                N_TOKENS=$((N_TOKENS + 1))
                case "$path" in
                    /*) classify "$path" "$cf" "$lineno"
                        CWD="$(normalize_abs "$path")" ;;
                    *)  if [ -z "$CWD" ]; then
                            echo "缺少来源: $path（$cf 第 $lineno 行：cd 目标无法定位——此前没有 cd 到 $APP_ROOT）"
                            viol=1
                        else
                            CWD="$(normalize_abs "$CWD/$path")"
                            case "$CWD" in
                                "$APP_ROOT"|"$APP_ROOT"/*) ;;
                                *) echo "缺少来源: cd $path（$cf 第 $lineno 行：解析到 $CWD，不在检出 $APP_ROOT 内）"
                                   viol=1 ;;
                            esac
                        fi ;;
                esac
                continue
            fi
            if [ "$tok" = "cd" ]; then want_cd=1; continue; fi
            case "$path" in
                -*) continue ;;   # 旗标本体（值若是独立 token 会按下一轮单独判）
                /*) N_TOKENS=$((N_TOKENS + 1)); classify "$path" "$cf" "$lineno" ;;
                */*)
                    # 相对路径（R3-022）：必须能按 shell 语义定位到检出内。
                    N_TOKENS=$((N_TOKENS + 1))
                    if [ -z "$CWD" ]; then
                        echo "缺少来源: $path（$cf 第 $lineno 行：相对路径无法定位——命令行没有先 cd 到 $APP_ROOT）"
                        viol=1
                        continue
                    fi
                    resolved="$(normalize_abs "$CWD/$path")"
                    case "$resolved" in
                        "$APP_ROOT"|"$APP_ROOT"/*)
                            rel="${resolved#"$APP_ROOT"}"
                            rel="${rel#/}"
                            if [ -e "$REPO_ROOT/$rel" ] || is_runtime_path "$rel"; then
                                :
                            else
                                echo "缺少来源: $path（$cf 第 $lineno 行：按 cwd 解析为 $resolved，但仓库里没有 $rel）"
                                viol=1
                            fi
                            ;;
                        *)
                            echo "缺少来源: $path（$cf 第 $lineno 行：按 cwd 解析为 $resolved，不在检出 $APP_ROOT 内）"
                            viol=1
                            ;;
                    esac
                    ;;
            esac
        done
        set +f
    done < "$cf"
done

# 防"扫空当绿"（R3-010）：0 张表或 0 个 token 时，上面零违规是因为什么都没判。
# 下限 = 1，依据：生产面是 deploy/prod/cron.d 的 6 张表；门至少要看一张表、
# 判一个路径 token 才有资格说"可回指"。跳过 ≠ 绿（AGENTS §30）。
if [ "$N_TABLES" -eq 0 ]; then
    echo "缺少来源: cron 目录 $CRON_DIR 里没有表（0 张）——门没有可判的对象"
    viol=1
fi
if [ "$N_TOKENS" -eq 0 ]; then
    echo "缺少来源: 没有判定到任何路径 token（$N_TABLES 张表）——门空转不算通过"
    viol=1
fi

if [ "$viol" -eq 0 ]; then
    echo "cron provenance ok: $N_TABLES 张表、manifest ${#DESTS[@]} 条、判定 $N_TOKENS 个路径 token 全部可回指仓库来源"
    exit 0
fi
exit 1
