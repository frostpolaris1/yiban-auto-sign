#!/usr/bin/env bash
# ============================================================
# scripts/dev-verify.sh —— 仓内统一跑测入口（本地 WSL 与 CI 同源）
#
# 为什么有它（2026-10-05 合流实证）：解释器三轮才找到、副本缺 .git 造成伪红、
#   CRLF 炸门禁、两负责人分组跑法不同致伪红、tail 截断两轮丢名单。
#   这些都不是被测代码的缺陷，是"跑法"没有唯一入口造成的。本脚本把跑法固化为
#   一条命令：建带 .git 的 WSL 副本 → 转 LF → 固定 venv → ruff → 全量并发 pytest
#   → 完整输出 tee 到固定日志路径。
#
# 用法：
#   # 本地（Windows Git Bash 或 WSL 内，同一条命令；脚本自己进 WSL）
#   bash scripts/dev-verify.sh
#   # CI（ubuntu runner 上就地跑关键子集，不建副本）
#   bash scripts/dev-verify.sh --ci
#
# 选项：
#   --ci             跑 CI 关键子集（ruff + 安全子集 -n 4 + e2e smoke + shared-facts
#                    + path-env-reads + config-registry 三道门禁，各带自己的元测试），
#                    就地跑：不建副本、不归一化、不落日志
#   --repo DIR       源仓库目录（默认 = 本脚本所在仓库根）
#   --target PATH    一个或多个 pytest 目标（默认 tests/）；只对默认全量模式有效
#   --log-dir DIR    日志目录（默认 <仓库父目录>/yiban-dev-verify-logs）；只对默认全量模式有效
#   --keep N         日志保留最近 N 份（默认 5）；只对默认全量模式有效
#                    （--ci 跑固定关键子集、就地跑不落日志：以上三者传了会被拒绝）
#   -h, --help       打印本帮助
#
# 全量模式（默认）的固定口径，逐条都是踩过的坑：
#   1. 副本落在 WSL 原生文件系统（/root/.cache/yiban-dev-verify/worktree），
#      不放 /mnt（DrvFs 慢）。每次运行整体重建，保证"副本 = 当前工作树"；
#      只排除 .git 与工具缓存目录（__pycache__ / .pytest_cache / .ruff_cache）。
#      副本路径固定，故并发跑时用 flock 串行化——两个运行不能同时重建同一份副本。
#   2. 副本带**能用的** git 仓库：.git 由本脚本播种（HEAD 指向源提交、
#      对象经 alternates 复用源对象库、索引直接取源索引）。为什么必须带：
#      tests/test_deploy_prod_artifacts.py 与 tests/test_web_vue_sources_tracked.py
#      用 git ls-files 判"哪些文件入库"，没有 .git 就变成伪红。
#      为什么索引取源索引而不是 git add -A：源仓存在"已跟踪但被 .gitignore 命中"的
#      文件（如 scripts/git-hooks/commit-msg），git add -A 会静默漏掉它，
#      跟踪集与源不一致就会让入库类门禁失真。
#   3. 副本内文本先转 LF 再跑。Windows 侧 core.autocrlf=true 会让工作树出现 CRLF
#      （.gitattributes 声明 eol=lf 只约束入库形态）。CRLF 会炸 shell 门禁脚本
#      （`set -euo pipefail` 被 CR 破坏）与入库类断言，是伪红的大头。
#   4. 固定 venv：只认 /root/.venv-yiban-wsl，绝不退化到 PATH 上的任意 python；
#      跑测前打印解释器绝对路径与版本。
#   5. 并发固定 `-n auto --dist loadfile`。--dist loadfile 不得去掉：套内存在
#      文件内先后依赖与进程级 DB 单例，按单条分发即误红。
#   6. 输出整份 tee 到 <日志目录>/dev-verify-<时间戳>-<pid>.log，保留最近 N 份。
#      **不截断**（只留 tail 会丢失败名单）。日志内含：解释器绝对路径与版本、
#      被跑提交 sha、ruff 退出码、pytest 汇总四数、脚本退出码。
#   7. 脚本不改测试本身的行为，也不需要任何人先改环境变量。
#
# 守卫与自证：副本 .git 失活、副本残留 CRLF、副本跟踪集为空，
#   都判为环境错误并响亮失败（退出码 2），不许把伪红当红交出去。
#   这两道守卫由 scripts/e2e/dev-verify-e2e.sh 用"变异体"钉住：
#   摘掉守卫段后 e2e 必须变红，证明守卫承重。
# ============================================================
set -euo pipefail

FIXED_VENV=/root/.venv-yiban-wsl
WORKDIR=/root/.cache/yiban-dev-verify
DEFAULT_KEEP=5
DEFAULT_DISTRO=Ubuntu

usage() {
    awk 'NR>2 && /^# ={10,}$/ {exit} NR>2 {sub(/^# ?/, ""); print}' "${BASH_SOURCE[0]}"
}

die() {
    echo "dev-verify: 环境错误：$*" >&2
    exit 2
}

# 把 Windows 侧路径（D:/x、/d/x、\\?\D:/x 形态）翻成 WSL 侧 /mnt/<盘>/x；已是 POSIX
# 绝对路径则原样返回。（`\\?\UNC\...` 形态不支持：本仓按 /mnt/<盘>/... 工作。）
win_to_unix() {
    local p=$1 drv rest
    p=${p//\\//}
    p=${p#//?/} # 剥掉 \\?\ 长路径前缀，剥完才落得进下面的盘符分支
    case "$p" in
        /mnt/[a-z]/*) printf '%s\n' "$p" ;;
        [A-Za-z]:/*)
            drv=$(printf '%s' "${p:0:1}" | tr 'A-Z' 'a-z')
            rest=${p:2}
            printf '/mnt/%s%s\n' "$drv" "$rest"
            ;;
        /[A-Za-z]/*)
            drv=$(printf '%s' "${p:1:1}" | tr 'A-Z' 'a-z')
            rest=${p:2}
            printf '/mnt/%s%s\n' "$drv" "$rest"
            ;;
        *) printf '%s\n' "$p" ;;
    esac
}

prune_logs() { # $1 = 目录，$2 = 保留份数
    local dir=$1 keep=$2 n=0 f files
    # 按修改时间由新到旧：文件名里的秒级时间戳会同秒并列，那时会退化成按 PID 字符串排，
    # 把新的日志排在旧的前面而被先删。mtime 没有这个歧义。
    files=$(ls -1t "$dir"/dev-verify-*.log 2>/dev/null || true)
    [ -n "$files" ] || return 0
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        n=$((n + 1))
        if [ "$n" -gt "$keep" ]; then
            rm -f "$f"
        fi
    done <<<"$files"
    return 0
}

resolve_gitdir() { # 源仓库的 git 目录（.git 目录，或 .git 文件里 gitdir 指过去的目录）
    local g
    if [ -d "$REPO/.git" ]; then
        printf '%s\n' "$REPO/.git"
        return 0
    fi
    if [ -f "$REPO/.git" ]; then
        g=$(sed -n 's/^gitdir: *//p' "$REPO/.git" | head -1)
        g=${g//\\//}
        case "$g" in
            [A-Za-z]:/*) g=$(win_to_unix "$g") ;;
            /*) ;;
            *) g="$REPO/$g" ;;
        esac
        if [ -d "$g" ]; then
            printf '%s\n' "$g"
            return 0
        fi
    fi
    return 1
}

build_copy_git() { # $1 = 副本目录，$2 = 源 git 目录，$3 = 源 HEAD
    local dest=$1 gitdir=$2 sha=$3 common
    common=$(git --git-dir="$gitdir" rev-parse --git-common-dir)
    git init -q "$dest"
    mkdir -p "$dest/.git/objects/info"
    printf '%s\n' "$common/objects" >"$dest/.git/objects/info/alternates"
    git -C "$dest" update-ref refs/heads/verify "$sha"
    git -C "$dest" symbolic-ref HEAD refs/heads/verify
    cp "$(git --git-dir="$gitdir" rev-parse --git-path index)" "$dest/.git/index" ||
        die "源索引不可复制（git --git-path index 指不到源索引？）：副本跟踪集会与源不一致"
}

guard_copy_git() { # 副本必须是一个"能用的 git 仓库"：可 rev-parse、对象可达、跟踪集非空、HEAD 等于源提交
    local dest=$1 sha=$2 h n
    (cd "$dest" && git rev-parse --git-dir >/dev/null 2>&1) ||
        die "副本 .git 不可用（$dest）：入库类门禁会静默判空，拒绝以此跑测"
    h=$(cd "$dest" && git rev-parse HEAD 2>/dev/null) || die "副本 HEAD 不可解析（$dest）"
    # 引用/索引都读得到不等于**对象库**可达：副本对象走 alternates，
    # alternates 指错时上面两条仍会通过，而读历史的用例会伪红——故这里显式探一次对象。
    (cd "$dest" && git cat-file -e "$h^{commit}" 2>/dev/null) ||
        die "副本对象库不可达（$dest/.git/objects，alternates 指错？）：历史类门禁会伪红"
    n=$(cd "$dest" && git ls-files | wc -l)
    [ "$n" -ge 100 ] || die "副本 git 跟踪集异常（$n 个文件）：门禁会读到空名册"
    [ "$h" = "$sha" ] || die "副本 HEAD($h) 与源 HEAD($sha) 不一致"
    echo "DEV-VERIFY copy-git: HEAD=$h tracked=$n"
}

normalize_lf() { # 副本内文本一律转 LF（只动行尾 CR，行内 CR 不动；二进制跳过）
    local dest=$1 f n=0
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        # \r* 而不是 \r：行尾叠了多个 CR（\r\r\n）也要一次清干净，
        # 否则一次只剥一个，guard_lf 会当场把残余判成"归一化漏网"。
        sed -i 's/\r*$//' "$dest/$f"
        n=$((n + 1))
    done < <(cd "$dest" && grep -rlI --exclude-dir=.git $'\r' . 2>/dev/null || true)
    echo "DEV-VERIFY lf: 归一化 $n 个文本文件为 LF"
}

guard_lf() { # 归一化后副本内不得再有 CRLF 行尾
    local dest=$1 hits
    hits=$(cd "$dest" && grep -rlI --exclude-dir=.git $'\r$' . 2>/dev/null || true)
    [ -z "$hits" ] || die "副本仍有 CRLF 行尾（归一化漏网）：$(printf '%s' "$hits" | tr '\n' ' ')"
    echo "DEV-VERIFY lf-guard: 无 CRLF 行尾"
}

guard_worktree_crlf() { # --ci 就地跑不做归一化：工作树真有 CRLF 时响亮拒绝，不许把伪红当红。
    # 只看 git 跟踪文件——那就是 CI 检出里的文件集（忽略目录里的本地件不进判定）
    local gd hits scan_rc
    gd=$(resolve_gitdir) ||
        die "取不到 git 仓库（$REPO/.git 缺失或不可解析）：--ci 要靠它定哪些文件在 CI 检出里"
    # 先单独确认名册取得出来：后面 grep 的扫描结果无法区分"无命中"与"名册取不到"
    # （两者都让 $hits 为空），名册取不到会让这道唯一的 CRLF 防线空过。
    git --git-dir="$gd" --work-tree="$REPO" ls-files -z >/dev/null ||
        die "git ls-files 取不到跟踪集（$REPO）：--ci 的 CRLF 判定不可信，拒绝放行"
    set +e
    hits=$(cd "$REPO" && git --git-dir="$gd" --work-tree="$REPO" ls-files -z |
        xargs -0 -r grep -lI $'\r$' 2>/dev/null)
    scan_rc=$?
    set -e
    # GNU xargs **不透传**子命令退出码：grep 任一次返回 1~125（无命中即 1）时 xargs 整体
    # 返回 123。故"有命中(0) / 无命中(1 或 123)"都正常，别的一律当扫描出错。
    # 残留局限：grep 自身的读取错误也会被 xargs 归一成 123，与"无命中"不可区分——
    # 上面那条 ls-files 成功性检查只兜住"名册取不到"这一种扫描失败。
    case "$scan_rc" in
        0 | 1 | 123) ;;
        *) die "CRLF 扫描失败（grep/xargs 退出码 $scan_rc）：--ci 的 CRLF 判定不可信，拒绝放行" ;;
    esac
    [ -z "$hits" ] ||
        die "跟踪文件含 CRLF 行尾：$(printf '%s' "$hits" | tr '\n' ' ')——--ci 就地跑不做归一化（CI runner 的新检出按 .gitattributes 是 LF）；本地请用默认全量模式（它会先归一化），或先把工作树修成 LF"
}

run_ci() { # CI 关键子集：就地跑，不建副本；命令逐字冻结在 tests/test_dev_verify_entry.py，加一步必须同批改那份清单
    local py
    trap 'rc=$?; echo "DEV-VERIFY(ci) exit_code=$rc"' EXIT
    py=${DEV_VERIFY_PY:-python}
    command -v "$py" >/dev/null 2>&1 || die "找不到解释器：$py"
    py=$(command -v "$py")
    echo "DEV-VERIFY(ci) interpreter: $py ($("$py" -V 2>&1))"
    echo "DEV-VERIFY(ci) repo: $REPO"
    guard_worktree_crlf
    cd "$REPO"
    echo "DEV-VERIFY(ci) lint"
    "$py" -m ruff check yiban/ tests/ scripts/ web/ --quiet
    echo "DEV-VERIFY(ci) security subset"
    "$py" -m pytest tests/ -q -n 4 --dist loadfile -k "security or mask or audit or login or private or csrf or ratelimit"
    echo "DEV-VERIFY(ci) e2e smoke"
    "$py" -m pytest tests/test_login_e2e_mock.py -q -p no:randomly
    echo "DEV-VERIFY(ci) shared facts gate"
    bash scripts/check-shared-facts.sh
    "$py" -m pytest tests/test_shared_facts_gate.py -q -p no:randomly
    echo "DEV-VERIFY(ci) path env bare-read gate"
    "$py" scripts/check-path-env-reads.py
    "$py" -m pytest tests/test_path_env_read_gate.py -q -p no:randomly
    echo "DEV-VERIFY(ci) config registry gate"
    "$py" scripts/check-config-registry.py
    "$py" -m pytest tests/test_config_registry_gate.py -q -p no:randomly
    echo "DEV-VERIFY(ci) done"
}

# ------------------------------------------------------------
# Windows 侧（Git Bash / MSYS）：换算路径后进 WSL 跑同一条命令
# ------------------------------------------------------------
case "$(uname -s)" in
    Linux) ;;
    *)
        for a in "$@"; do
            if [ "$a" = "--ci" ]; then
                die "--ci 就地跑 Linux 关键子集，Windows 侧不支持（CI 在 ubuntu runner 上跑）"
            fi
        done
        here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -W)
        args=()
        while [ $# -gt 0 ]; do
            case "$1" in
                --repo | --log-dir)
                    args+=("$1" "$(win_to_unix "${2:?$1 需要取值}")")
                    shift
                    ;;
                *) args+=("$1") ;;
            esac
            shift
        done
        MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' exec \
            wsl.exe -d "$DEFAULT_DISTRO" -u root -- \
            bash "$(win_to_unix "$here/dev-verify.sh")" ${args[@]+"${args[@]}"}
        ;;
esac

# ------------------------------------------------------------
# Linux/WSL 侧：解析参数
# ------------------------------------------------------------
ORIG_ARGS=("$@")
REPO=""
TARGET="tests/"
LOG_DIR=""
KEEP=$DEFAULT_KEEP
MODE=full
TARGET_SET=0
LOGDIR_SET=0
KEEP_SET=0

while [ $# -gt 0 ]; do
    case "$1" in
        --ci) MODE=ci ;;
        --repo) REPO=${2:?--repo 需要取值} ;;
        --target)
            TARGET=${2:?--target 需要取值}
            TARGET_SET=1
            ;;
        --log-dir)
            LOG_DIR=${2:?--log-dir 需要取值}
            LOGDIR_SET=1
            ;;
        --keep)
            KEEP=${2:?--keep 需要取值}
            KEEP_SET=1
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *) die "未知参数：$1（用 -h 看用法）" ;;
    esac
    case "$1" in
        --ci) shift ;;
        *) shift 2 ;;
    esac
done

if [ -z "$REPO" ]; then
    REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
fi
[ -d "$REPO" ] || die "仓库目录不存在：$REPO"
REPO=$(cd "$REPO" && pwd)
case "$KEEP" in
    '' | *[!0-9]*) die "--keep 必须是非负整数：$KEEP" ;;
esac
[ "$KEEP" -ge 1 ] || die "--keep 至少为 1：$KEEP"

if [ "$MODE" = ci ]; then
    # --ci 跑的是固定关键子集、就地跑不落日志：不接受只对全量模式生效的参数。
    # 静默忽略会让调用方以为"我点的目标/我设的日志目录生效了"——口径必须响亮一致。
    [ "$TARGET_SET" = "0" ] ||
        die "--target 在 --ci 模式下不生效（--ci 跑固定关键子集）：去掉 --target，或改用默认全量模式"
    [ "$LOGDIR_SET" = "0" ] ||
        die "--log-dir 在 --ci 模式下不生效（--ci 就地跑、不落日志）：去掉 --log-dir"
    [ "$KEEP_SET" = "0" ] ||
        die "--keep 在 --ci 模式下不生效（--ci 就地跑、不落日志）：去掉 --keep"
    run_ci
    exit 0
fi

# ------------------------------------------------------------
# 全量模式：先落日志（tee 整份输出），再在副本里跑
# ------------------------------------------------------------
if [ "${DEV_VERIFY_LOGGED:-0}" = "1" ] && [ -z "${DEV_VERIFY_LOG:-}" ]; then
    # 哨兵只由本脚本自己设。外部继承到 LOGGED=1 却没带 LOG（例如父进程泄漏）时，
    # 不许静默降级成"无日志跑测"——丢名单正是本批要消灭的形状。
    die "DEV_VERIFY_LOGGED=1 但 DEV_VERIFY_LOG 为空：拒绝在没有日志的情况下跑测"
fi
if [ "${DEV_VERIFY_LOGGED:-0}" != "1" ]; then
    [ -n "$LOG_DIR" ] || LOG_DIR="$(dirname "$REPO")/yiban-dev-verify-logs"
    mkdir -p "$LOG_DIR" || die "日志目录建不了：$LOG_DIR"
    LOG="$LOG_DIR/dev-verify-$(date +%Y%m%d-%H%M%S)-$$.log"
    set +e
    DEV_VERIFY_LOGGED=1 DEV_VERIFY_LOG="$LOG" bash "${BASH_SOURCE[0]}" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"} 2>&1 | tee "$LOG"
    rc=${PIPESTATUS[0]}
    set -e
    prune_logs "$LOG_DIR" "$KEEP"
    exit "$rc"
fi
LOG="${DEV_VERIFY_LOG:-}"

PY="$FIXED_VENV/bin/python"
[ -x "$PY" ] || die "固定 venv 不可用：$PY（不许退化成 PATH 上的任意 python）"
TMPOUT=$(mktemp)
trap 'rm -f "$TMPOUT"' EXIT

echo "DEV-VERIFY log: ${LOG:-<未落盘>}"
echo "DEV-VERIFY repo: $REPO"
echo "DEV-VERIFY interpreter: $PY ($("$PY" -V 2>&1)，实到 $(readlink -f "$PY"))"

GITDIR=
SHA=
# dev-verify-mutant-begin: source-git
GITDIR=$(resolve_gitdir) ||
    die "源仓库不是可用的 git 仓库（$REPO/.git 缺失或不可解析）：副本会带一个空名册，拒绝跑测"
SHA=$(git --git-dir="$GITDIR" --work-tree="$REPO" rev-parse HEAD) || die "源 HEAD 取不到"
echo "DEV-VERIFY sha: $SHA"
# dev-verify-mutant-end: source-git

DEST="$WORKDIR/worktree"
# 并发保护：副本路径固定、日志目录按 N 份轮转，两者都是共享资源；两个 dev-verify
# 同时跑会互相 rm -rf 掉对方的副本（正是本脚本要消灭的那类伪红）。
# 后到者等前者结束，不把对方的副本删掉。日志文件名带 PID，故日志本身不互撞。
mkdir -p "$WORKDIR" || die "副本根目录建不了：$WORKDIR"
exec 9>"$WORKDIR/dev-verify-runlock" || die "锁文件不可写：$WORKDIR/dev-verify-runlock"
if command -v flock >/dev/null 2>&1; then
    if ! flock -n 9; then
        echo "DEV-VERIFY: 另一个 dev-verify 正在跑，等它结束（共享副本目录不能被并发清空）"
        flock 9
    fi
else
    # 副本路径是共享的固定路径：没有 flock 就没有串行化，两个运行会互相 rm -rf。
    # "并发跑法互相清副本"正是本脚本要消灭的伪红形状，故这里响亮拒绝，不许降级跑。
    die "没有 flock（util-linux）：共享副本目录 $DEST 无法串行化，拒绝跑测；装上 flock 后再跑"
fi
rm -rf "$DEST"
mkdir -p "$DEST"
# 只排除 .git 与工具缓存目录：副本 = 当前工作树的可跑形态，不带 VCS 元数据与缓存残渣
rsync -a --exclude='.git' --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='.ruff_cache' "$REPO/" "$DEST/" || die "rsync 复制失败：$REPO -> $DEST"
echo "DEV-VERIFY copy: $DEST"

# dev-verify-mutant-begin: copy-git
build_copy_git "$DEST" "$GITDIR" "$SHA"
guard_copy_git "$DEST" "$SHA"
# dev-verify-mutant-end: copy-git
# dev-verify-mutant-begin: lf
normalize_lf "$DEST"
guard_lf "$DEST"
# dev-verify-mutant-end: lf

cd "$DEST"

echo "DEV-VERIFY ruff: ruff check yiban/ tests/ scripts/ web/ --quiet"
set +e
"$PY" -m ruff check yiban/ tests/ scripts/ web/ --quiet
ruff_rc=$?
set -e
echo "DEV-VERIFY ruff_exit=$ruff_rc"

# $TARGET 故意不加引号：允许 `--target "tests/a.py tests/b.py"` 传多个目标
echo "DEV-VERIFY pytest: pytest $TARGET -q -p no:randomly -n auto --dist loadfile"
set +e
"$PY" -m pytest $TARGET -q -p no:randomly -n auto --dist loadfile 2>&1 | tee "$TMPOUT"
py_rc=${PIPESTATUS[0]}
set -e
echo "DEV-VERIFY pytest_exit=$py_rc"

summary=$(grep -E '^[0-9]+ (passed|failed|error)' "$TMPOUT" | tail -1 || true)
if [ -n "$summary" ]; then
    num() {
        local v
        v=$(printf '%s\n' "$summary" | grep -oE "[0-9]+ $1" | head -1 | cut -d' ' -f1 || true)
        printf '%s' "${v:-0}"
    }
    echo "DEV-VERIFY pytest summary line: $summary"
    echo "DEV-VERIFY summary: passed=$(num passed) failed=$(num failed) skipped=$(num skipped) errors=$(num error)"
else
    echo "DEV-VERIFY summary: 无（pytest 未产出汇总行）"
fi

rc=$ruff_rc
[ "$py_rc" = "0" ] || rc=$py_rc
echo "DEV-VERIFY exit_code=$rc"
exit "$rc"
