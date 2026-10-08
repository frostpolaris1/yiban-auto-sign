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
#   # 提交前自查（最快；两档）
#   bash scripts/dev-verify.sh --fast              # 安全档：全量减已知长尾（实测 57s）
#   bash scripts/dev-verify.sh --fast-scoped       # 范围档：只跑改动相邻面（实测 9~11s，覆盖面不全）
#                                                  # 空覆盖（无改动/无命中）退出码 3，不是 0
#
# 选项：
#   --ci             跑 CI 关键子集（ruff + 安全子集 -n 4 + e2e smoke + shared-facts
#                    + path-env-reads + config-registry 三道门禁，各带自己的元测试），
#                    就地跑：不建副本、不归一化、不落日志
#   --fast           **提交前自查·安全档**：跑全量，但剔除已实测的 5 条长尾
#                    （等待型/全仓扫描型，见 FAST_KNOWN_SLOW）。实测 146s → 57s，
#                    覆盖面无选择损失。**仍非门禁**：推送前跑全量或确认 CI 绿。
#   --fast-scoped    **提交前自查·范围档**：只跑"改动相邻面"（目标为入口自检时实测 9~11s，
#                    目标集越大越久）。覆盖面明确不全，输出会声明"未被选中的用例没有跑"；
#                    空覆盖时退出码 3（见下）。
#   --base REF       --fast / --fast-scoped 的比较基准（默认 HEAD = 只算工作树与暂存区改动；
#                    传 origin/develop 则算整条分支的改动）
#   --fast-all       --fast 但不剔除长尾（只有改动确实落在那些文件上时才用）
#   （--ci / --fast / --fast-scoped / --fast-all 四者互斥：给两个**不同**模式即报错退出 2；
#     同一模式标志重复给（如 --fast --fast）按幂等接受）
#   --repo DIR       源仓库目录（默认 = 本脚本所在仓库根）
#   --target PATH    一个或多个 pytest 目标（默认 tests/）；只对默认全量模式有效
#   --log-dir DIR    日志目录（默认按模式分开：全量 <仓库父目录>/yiban-dev-verify-logs，
#                    fast 两档 <仓库父目录>/yiban-dev-verify-logs-fast——两档走不同的锁、
#                    可以并发，共用目录会互相轮转删掉对方的日志）
#   --keep N         日志保留最近 N 份（默认 5）；fast 两档与全量各自计数
#                    （--ci 跑固定关键子集、就地跑不落日志：--log-dir / --keep 传了会被拒绝；
#                     --target 只对默认全量模式有效）
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
# fast 模式口径（2026-10-07 立，依据实测）：
#   8. **两档，都是为了提交前自查，都不替代门禁**：
#      · `--fast`（安全档，默认推荐）：覆盖＝**全量 − 已知长尾**，无选择面损失。实测 146s → 57s。
#      · `--fast-scoped`（范围档）：只跑改动相邻面（实测 9~11s），但**未选中的用例没跑**。
#      推送前必须跑全量（默认模式）或确认 CI 绿；fast 的绿不得当成门禁的绿。
#      为什么要有它：全量实测 4331 用例（passed 4325 + skipped 6）约 146s，
#      单次改动后反复跑全量 + 报错重修极费时间。
#      墙钟随机器负载变：并发跑测时本档从约 57s 涨到约 104s（见 docs/dev/dev-verify.md）。
#   9. 长尾剔除依据（实测 `--durations`）：单个用例 135.92s（占全量墙钟 88%）＋"同文件四条被
#      --dist loadfile 串行化约 116s"两处。它们只与全仓扫描/等待超时有关，与提交前自查无关。
#      剔除清单硬编码在 FAST_KNOWN_SLOW，改它要连着重测墙钟。
#   10. 范围档的选择法（无依赖、无状态）：改动文件 → 测试目标。
#      · 改到 tests/ 下的用例 → 直接跑它；
#      · 改到源码 → **按导入路径**反查（裸词会大面积误命中：实测 `window` 裸词命中 70 个文件＝29%，
#        导入路径只命中 11 个）；导入路径 0 命中时回退裸词并如实打印回退与命中量；
#      · 改到本脚本自身 → 补 tests/test_dev_verify_entry.py（它冻结了 CI 命令表）。
#   11. 两档都必须打印"选了什么、依据是什么、没覆盖什么"——防止把 fast 的绿读成门禁的绿。
#      另：本脚本只跑 pytest。**前端改动**（frontend/ 下的 vitest / playwright）不在本脚本面内，
#      范围档遇到 frontend/ 改动会如实说"未反查到用例"，不要读成"有人覆盖"。
#   12. 退出码只有四种：0 = 通过；1 = 有红（ruff 或 pytest 非 0）；2 = 环境错误；3 = **空覆盖**。
#      3 的语义是"没测到东西"，不是"测了但失败"：范围档没测到任何真实用例、只跑了入口自检。
#      文档类改动的正常结果就是 3，推送前仍要跑全量。两种 fast 档都打印 covered=<用例数>
#      （pytest 汇总四数之和）。**3 由空覆盖独占**：ruff 或 pytest 的原始码一律归一为 1，
#      不被透出（原始码逐行打印在日志里：ruff_exit= / pytest_exit=），否则 pytest 自己的
#      3（INTERNALERROR）会与空覆盖撞码、语义两用。
#
# 守卫与自证：副本 .git 失活、副本残留 CRLF、副本跟踪集为空，
#   都判为环境错误并响亮失败（退出码 2），不许把伪红当红交出去。
#   这两道守卫由 scripts/e2e/dev-verify-e2e.sh 用"变异体"钉住：
#   摘掉守卫段后 e2e 必须变红，证明守卫承重。
#   第三道守卫是 FAST_KNOWN_SLOW 名单本身：guard_fast_known_slow 要求每条 nodeid 的
#   文件、类、用例三层都存在（pytest 对不存在的 --deselect 静默忽略），且无前缀嵌套条目。
#   它由 tests/test_dev_verify_entry.py 的 DevVerifyFastModeTest 同批钉住（元测试 + 在场性）。
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

default_log_dir() { # $1 = 仓库目录；$2 = 模式（full / fast）；返回默认日志目录
    # fast 两档与全量走**不同的锁**，两者可以同时跑。日志目录按 mtime 轮转（--keep），
    # 共用一个目录就会互相删掉对方的日志——实测：fast 档用默认目录跑几次，把同目录里
    # 他方的 5 份日志轮转删掉了。不同锁的两个流程不许共享可被轮转的资源，故按模式分目录。
    case "$2" in
        fast) printf '%s\n' "$(dirname "$1")/yiban-dev-verify-logs-fast" ;;
        *) printf '%s\n' "$(dirname "$1")/yiban-dev-verify-logs" ;;
    esac
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
# fast 模式：改动 → 测试目标（无依赖、无状态；只做文件级反查）
# ------------------------------------------------------------
#: --fast 默认剔除的长尾用例（2026-10-07 实测：第一条单点 135.92s，占全量墙钟 88%；
#: 余下同处一个文件、被 --dist loadfile 串行化合计约 116s）。均为"等待超时/全仓扫描"型。
FAST_KNOWN_SLOW=(
    "tests/test_audit_transaction.py::CliExitCodeTest::test_locked_db_exit_two_not_tampered"
    "tests/test_shared_facts_gate.py::SharedFactsGateLiveTest::test_current_repo_tree_is_green"
    "tests/test_shared_facts_gate.py::SharedFactsNewFamilyCoverageTest::test_gate_is_silent_when_a_registered_key_is_dropped"
    "tests/test_shared_facts_gate.py::SharedFactsNewFamilyCoverageTest::test_cap_equals_measured_hit_count_for_both_new_families"
    "tests/test_path_env_read_gate.py::GateNoDoubleCountTest::test_routed_key_is_counted_by_exactly_one_engine"
)

guard_fast_known_slow() { # 名单防漂移：nodeid 的文件、类、用例三层都必须存在，且不许有前缀嵌套条目
    # 名单是硬编码。文件、类或用例改名后 --deselect 指向空气：pytest 对不存在的
    # --deselect **静默忽略**（实测 rc=0、无告警），剔除静默失效——长尾溜回 --fast，
    # 提交前自查又变回两分半。故在建副本之前验名单三层：文件在、类在、用例在。
    # 用文本匹配而不是 pytest --collect-only：后者要拉起 pytest 收集，会毁掉 --fast 的墙钟。
    # 这里要挡的是"改名后静默变 no-op"，不是断言源码内容，文本匹配够用。
    # 前缀嵌套指同时有 `x.py::Cls` 与 `x.py::Cls::test_a`：前者已覆盖后者，后者是假条目。
    local e path rest cls meth bad="" nested=""
    for e in "${FAST_KNOWN_SLOW[@]}"; do
        path=${e%%::*}
        if [ ! -f "$REPO/$path" ]; then
            bad="$bad $e（文件 $path 不存在）"
            continue
        fi
        case "$e" in
            *::*::*)
                rest=${e#*::}
                cls=${rest%%::*}
                meth=${rest##*::}
                grep -qE "^[[:space:]]*class[[:space:]]+${cls}([^A-Za-z0-9_]|$)" "$REPO/$path" ||
                    bad="$bad $e（类 $cls 不在 $path 里）"
                grep -qE "^[[:space:]]*(async[[:space:]]+)?def[[:space:]]+${meth}([^A-Za-z0-9_]|$)" "$REPO/$path" ||
                    bad="$bad $e（用例 $meth 不在 $path 里）"
                ;;
            *::*)
                # 单段 `::` 有两种合法形状：模块级用例 `x.py::test_a`，与**类级** nodeid
                # `x.py::Cls`（pytest --deselect 接受 file::Class 整类形状）。名字可能是
                # 用例名或类名，故对两者取或——只认 def 会把合法类级 nodeid 误判（实测 rc=2 假红）。
                meth=${e##*::}
                if grep -qE "^[[:space:]]*(async[[:space:]]+)?def[[:space:]]+${meth}([^A-Za-z0-9_]|$)" "$REPO/$path" ||
                    grep -qE "^[[:space:]]*class[[:space:]]+${meth}([^A-Za-z0-9_]|$)" "$REPO/$path"; then
                    :
                else
                    bad="$bad $e（$meth 在 $path 里既不是用例名也不是类名）"
                fi
                ;;
        esac
    done
    [ -z "$bad" ] ||
        die "FAST_KNOWN_SLOW 有条目指向不存在的文件/类/用例（改名后 --deselect 被 pytest 静默忽略，剔除失效）：$bad"
    # 前缀嵌套检查不按 nodeid 形状分支，只做字符串前缀比较：类级形状 `x.py::Cls`
    # 与 `x.py::Cls::test_a` 同样可达（长条目被判为多余）。故上面的形状分支不影响本检查。
    for e in "${FAST_KNOWN_SLOW[@]}"; do
        for pre in "${FAST_KNOWN_SLOW[@]}"; do
            case "$e" in
                "$pre"::*) nested="$nested $e（已被 $pre 覆盖）" ;;
            esac
        done
    done
    [ -z "$nested" ] ||
        die "FAST_KNOWN_SLOW 有前缀嵌套条目（短条目已覆盖长条目，长条目多余）：$nested"
    echo "DEV-VERIFY fast-guard: FAST_KNOWN_SLOW ${#FAST_KNOWN_SLOW[@]} 条的文件/类/用例全部存在，无前缀嵌套"
}

fast_collect_changed() { # $1 = 基准 ref；$2 = 输出文件（临时）；git 失败即 die
    local base=$1 outfile=$2
    : >"$outfile"
    # **必须在主壳里收集**：`die` 在 `$( )` 子壳里只杀子壳，会让"git 失败"退化成
    # "改动集为空 ⇒ 只跑入口自检 ⇒ 报绿"——伪绿的另一种形状。
    # 必须走 `--git-dir` / `--work-tree`（与脚本其余部分同一套解析）：Windows 侧建的 worktree
    # 其 `.git` 文件里是 Windows 绝对路径，WSL 的 git 用 `-C` 解析不了（fatal: not a git repository）。
    if ! git --git-dir="$GITDIR" --work-tree="$REPO" diff --name-only "$base" >>"$outfile" 2>/dev/null; then
        die "改动集取不到（git diff 失败，base=$base）：常见原因是 worktree 的 .git 指向 Windows 路径而 WSL 解析不了——fast 拒绝在'改动未知'时给绿"
    fi
    # 暂存区与未跟踪文件：任一取不到只影响"多选/少选"，不改变"改动集本身可信"这一前提，
    # 故此处不 die；但下面会把空集当"确实无改动"如实打印，不冒充已覆盖。
    git --git-dir="$GITDIR" --work-tree="$REPO" diff --cached --name-only >>"$outfile" 2>/dev/null || true
    git --git-dir="$GITDIR" --work-tree="$REPO" ls-files --others --exclude-standard >>"$outfile" 2>/dev/null || true
}

select_fast_targets() { # $1 = 改动清单文件；$2 = 覆盖标记文件（写 1 表示空覆盖）；
    # stdout 输出 pytest 目标（空格分隔），依据打到 stderr
    local f stem hits h
    local found=""
    # 只留"当前存在的文件"：删除类改动没有可跑的相邻面，留着只会让目标集混入空路径
    local changed
    changed=$(sort -u "$1" | while IFS= read -r f; do
        if [ -n "$f" ] && [ -f "$REPO/$f" ]; then printf '%s\n' "$f"; fi
    done)

    if [ -z "$changed" ]; then
        # 空覆盖之一：改动集为空。写标记给调用方，让"只跑了入口自检"与"真覆盖"可区分。
        echo "DEV-VERIFY(fast): 改动集为空 ⇒ 只跑 ruff + 入口自检（脚本不会把它当作'已覆盖全量'）" >&2
        printf '1' >"$2"
        printf '%s' "tests/test_dev_verify_entry.py"
        return 0
    fi

    while IFS= read -r f; do
        [ -n "$f" ] || continue
        case "$f" in
            tests/test_*.py)
                echo "DEV-VERIFY(fast): 改到用例 ⇒ 直接跑 $f" >&2
                found="$found $f"
                continue
                ;;
            scripts/dev-verify.sh)
                echo "DEV-VERIFY(fast): 改入口脚本 ⇒ 补 tests/test_dev_verify_entry.py（它冻结了 CI 命令表）" >&2
                found="$found tests/test_dev_verify_entry.py"
                continue
                ;;
            */*)
                stem=$(basename "$f")
                stem=${stem%.*}
                ;;
            *)
                continue
                ;;
        esac
        # 主干短于 4 字符不做反查：'a1'、'ui' 这类会命中一大片，等于退回全量
        if [ "${#stem}" -lt 4 ]; then
            echo "DEV-VERIFY(fast): 跳过 $f（主干 '$stem' 过短，反查不可信）" >&2
            continue
        fi
        # 精确优先：按**导入路径**反查（裸词会大面积误命中——实测 `window` 裸词命中 70 个
        # 用例文件、占全体的 29%，而导入路径只命中 11 个）。导入路径为 0 命中时回退裸词，
        # 并如实打印回退与命中量：宁可慢，不许漏。
        local mod=${f%.py}
        mod=${mod//\//.}
        local parent=${mod%.*}
        hits=$(grep -rlE --include='test_*.py' \
            "from ${mod} import|import ${mod}|${mod}\.|from ${parent} import ${stem}" \
            "$REPO/tests" 2>/dev/null || true)
        if [ -n "$hits" ]; then
            while IFS= read -r h; do
                [ -n "$h" ] || continue
                h=${h#"$REPO"/}
                echo "DEV-VERIFY(fast-scoped): $f ⇒ $h（导入路径 $mod）" >&2
                found="$found $h"
            done <<<"$hits"
            continue
        fi
        hits=$(grep -rl --include='test_*.py' -F -- "$stem" "$REPO/tests" 2>/dev/null || true)
        if [ -n "$hits" ]; then
            local n
            n=$(printf '%s\n' "$hits" | grep -c . || true)
            echo "DEV-VERIFY(fast-scoped): ⚠ $f 导入路径无命中 ⇒ 回退裸词 '$stem'，命中 $n 个用例文件（选择面偏宽）" >&2
            while IFS= read -r h; do
                [ -n "$h" ] || continue
                h=${h#"$REPO"/}
                found="$found $h"
            done <<<"$hits"
        else
            echo "DEV-VERIFY(fast-scoped): ⚠ $f 未反查到任何用例（主干 '$stem'）——该改动可能无人覆盖" >&2
        fi
    done <<<"$changed"

    found=$(printf '%s\n' $found | sort -u | tr '\n' ' ')
    if [ -z "${found// /}" ]; then
        # 空覆盖之二：改动有，但反查不到任何相邻用例。写标记给调用方（退出码 3）。
        echo "DEV-VERIFY(fast): ⚠ 改动没有任何相邻用例命中 ⇒ 只跑入口自检。" >&2
        echo "DEV-VERIFY(fast): ⚠ 这不代表改动能过门禁——推送前请跑全量。" >&2
        found="tests/test_dev_verify_entry.py"
        printf '1' >"$2"
    else
        printf '0' >"$2"
    fi
    printf '%s' "$found"
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
BASE=""
BASESET=0
FAST_KEEP_SLOW=0
FAST_SCOPED=0
FAST_EMPTY=0
MODE_FLAG=""

mark_mode() { # $1 = 本次给的模式标志名；同名重复幂等接受，异名互斥响亮拒绝
    # 同名重复（`--fast --fast`）按幂等接受：重复同一标志不改变语义，拒绝它只会误伤
    # 脚本化调用。异名（`--fast --fast-scoped`）必须拒绝——静默让后者胜是危险方向：
    # 会退成范围档，调用方以为跑了全量减长尾，实际只跑了相邻面。
    if [ -z "$MODE_FLAG" ]; then
        MODE_FLAG=$1
        return 0
    fi
    [ "$MODE_FLAG" = "$1" ] ||
        die "模式标志互斥（--ci / --fast / --fast-scoped / --fast-all）：已给 $MODE_FLAG，又给 $1"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --ci)
            mark_mode --ci
            MODE=ci
            ;;
        --fast)
            mark_mode --fast
            MODE=fast
            ;;
        --fast-scoped)
            mark_mode --fast-scoped
            MODE=fast
            FAST_SCOPED=1
            ;;
        --fast-all)
            mark_mode --fast-all
            MODE=fast
            FAST_KEEP_SLOW=1
            ;;
        --base)
            BASE=${2:?--base 需要取值}
            BASESET=1
            ;;
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
        --ci | --fast | --fast-scoped | --fast-all) shift ;;
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
    [ "$BASESET" = "0" ] ||
        die "--base 在 --ci 模式下不生效（--ci 跑固定关键子集）：去掉 --base"
    run_ci
    exit 0
fi

# fast 模式：与全量共用同一套副本 / 固定 venv / LF 归一化管线（跑法一致，只有目标集不同）。
# 目标由"改动 → 相邻面"推导，在副本建好并 cd 进去之后算（那之前没有源仓的位置信息）。
if [ "$MODE" = fast ]; then
    [ "$TARGET_SET" = "0" ] ||
        die "--target 在 --fast 模式下不生效（fast 自己从改动推导目标）：去掉 --target，或改用默认全量模式"
    [ -n "$BASE" ] || BASE=HEAD
elif [ "$BASESET" = "1" ]; then
    die "--base 只对 --fast 有效（全量模式没有比较基准）：去掉 --base，或改用 --fast"
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
    [ -n "$LOG_DIR" ] || LOG_DIR=$(default_log_dir "$REPO" "$MODE")
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

# 名单守卫放在落盘之后、建副本之前。落盘之后是因为本脚本被 tee 包装跑两遍：
# 放这里只跑一次，且失败信息进日志。建副本之前是为了 fail fast（名单坏了就别复制 750 个文件）。
if [ "$MODE" = fast ] && [ "$FAST_KEEP_SLOW" != "1" ]; then
    guard_fast_known_slow
fi

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

if [ "$MODE" = fast ]; then
    # fast 用**独立副本 + 独立锁**：共用同一份副本会互相 rm -rf（既有的 flock 就是防这个），
    # 而串行化会让"提交前自查"去等一个可能跑满两分半的全量——那正是 fast 要消灭的等待。
    DEST="$WORKDIR/fast-worktree"
    LOCKFILE="$WORKDIR/dev-verify-fast-runlock"
else
    DEST="$WORKDIR/worktree"
    LOCKFILE="$WORKDIR/dev-verify-runlock"
fi
# 并发保护：副本路径固定，是共享资源；两个 dev-verify 同时跑会互相 rm -rf 掉对方的副本
# （正是本脚本要消灭的那类伪红）。后到者等前者结束，不把对方的副本删掉。
# 日志目录也是共享的轮转资源，但**按模式分目录**（default_log_dir）：fast 两档与全量
# 走不同的锁、可以并发，共用一个目录就会互相轮转删除。日志文件名带 PID，同目录也不互撞。
mkdir -p "$WORKDIR" || die "副本根目录建不了：$WORKDIR"
exec 9>"$LOCKFILE" || die "锁文件不可写：$LOCKFILE"
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
if [ "$MODE" = fast ] && [ -d "$DEST" ]; then
    # fast 复用上一份副本、走增量同步：不变的文件不再重传，把"提交前自查"的固定开销
    # 从"整树重建"降到"只同步改动"。不变式仍是"副本 = 当前工作树"——靠 rsync --delete
    # 删掉源里已不存在的文件（**不加** --delete-excluded，故副本自带的 .git 存活）。
    echo "DEV-VERIFY(fast): 增量同步已有副本（$DEST）"
else
    rm -rf "$DEST"
    mkdir -p "$DEST"
fi
# 只排除 .git 与工具缓存目录：副本 = 当前工作树的可跑形态，不带 VCS 元数据与缓存残渣。
# -c 是"副本 = 当前工作树"这条不变式的守卫本体：默认快检只看尺寸 + 整秒 mtime，
# 同一秒内改完且尺寸不变的改动会被判成"没变"而跳过，副本陈旧后跑出来的是旧代码，
# 可能报绿。加了 -c 就按内容校验，rsync 非 0 退出即 die，故不变式由 rsync 承重。
# 代价实测（本树 rsync 面内约 750 个文件，2026-10-07）：副本已同步时 rsync -a 约 0.65s，
# 加 -c 约 1.4s，多付约 0.7s；--fast-scoped 整跑墙钟 9~11s（含选中用例自身的执行）。
rsync -a -c --delete --exclude='.git' --exclude='__pycache__' --exclude='.pytest_cache' \
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

DESELECT=()
if [ "$MODE" = fast ]; then
    if [ "$FAST_SCOPED" = "1" ]; then
        # 范围档：只跑"改动相邻面"。**覆盖面明确不全**，故声明必须响亮。
        # 墙钟随目标集大小变：目标为入口自检时实测 9~11s，目标集越大越久。
        FAST_CHANGED="$WORKDIR/fast-changed-$$.txt"
        FAST_COVERAGE="$WORKDIR/fast-coverage-$$.flag"
        fast_collect_changed "$BASE" "$FAST_CHANGED"
        TARGET=$(select_fast_targets "$FAST_CHANGED" "$FAST_COVERAGE")
        FAST_EMPTY=$(cat "$FAST_COVERAGE" 2>/dev/null || printf '0')
        rm -f "$FAST_CHANGED" "$FAST_COVERAGE"
        echo "DEV-VERIFY(fast-scoped): 基准 $BASE ｜ 目标集：$TARGET"
        echo "DEV-VERIFY(fast-scoped): ⚠ 只覆盖改动相邻面 —— 未被选中的用例**没有跑**，它们照样可能红"
    else
        # 安全档（默认）：**全量 − 已知长尾**。零启发式、无漏选风险，只是剔掉 5 条与
        # "提交前自查"无关的长尾（实测 146s → 57s）。目标保持默认 tests/。
        echo "DEV-VERIFY(fast): 覆盖＝全量（tests/）减去已知长尾，无选择面丢失"
    fi
    if [ "$FAST_KEEP_SLOW" = "1" ]; then
        echo "DEV-VERIFY(fast): --fast-all：不剔除长尾（本跑可能长达数分钟）"
    else
        for _n in "${FAST_KNOWN_SLOW[@]}"; do DESELECT+=(--deselect "$_n"); done
        echo "DEV-VERIFY(fast): 已剔除 ${#FAST_KNOWN_SLOW[@]} 条已知长尾（等待型/全仓扫描型；全量与 CI 仍覆盖）"
    fi
    echo "DEV-VERIFY(fast): ⚠ 本模式仍不是门禁——推送前跑全量，或至少确认 CI 绿"
fi

echo "DEV-VERIFY ruff: ruff check yiban/ tests/ scripts/ web/ --quiet"
set +e
"$PY" -m ruff check yiban/ tests/ scripts/ web/ --quiet
ruff_rc=$?
set -e
echo "DEV-VERIFY ruff_exit=$ruff_rc"

# $TARGET 故意不加引号：允许 `--target "tests/a.py tests/b.py"` 传多个目标
echo "DEV-VERIFY pytest: pytest $TARGET -q -p no:randomly -n auto --dist loadfile ${DESELECT[*]:-}"
set +e
"$PY" -m pytest $TARGET -q -p no:randomly -n auto --dist loadfile \
    ${DESELECT[@]+"${DESELECT[@]}"} 2>&1 | tee "$TMPOUT"
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
    if [ "$MODE" = fast ]; then
        covered=$(($(num passed) + $(num failed) + $(num error) + $(num skipped)))
        echo "DEV-VERIFY(fast): covered=$covered（pytest 汇总四数之和：passed+failed+errors+skipped）"
    fi
else
    echo "DEV-VERIFY summary: 无（pytest 未产出汇总行）"
fi

# 退出码裁决只有这一处：0 = 通过；1 = 有红（ruff 或 pytest 非 0）；
# 2 = 环境错误（die）；3 = 空覆盖（没测到真实用例）。
# 原始码不丢：ruff_exit= 与 pytest_exit= 已逐行打印在上面。
resolve_exit_code() { # $1 = ruff 退出码；$2 = pytest 退出码；$3 = 空覆盖(1/0)
    if [ "$1" != "0" ] || [ "$2" != "0" ]; then
        printf '1'
        return 0
    fi
    if [ "$3" = "1" ]; then printf '3'; else printf '0'; fi
}

# 空覆盖提示按**最终 rc** 打印：只按 FAST_EMPTY 打，会在"空覆盖同时有红"时报出
# 与下一行 exit_code 矛盾的码（实测：提示写 3，下一行写 1）。
note_fast_empty_coverage() { # $1 = 空覆盖标记（1/0）；$2 = 最终退出码
    [ "${1:-0}" = "1" ] || return 0
    if [ "$2" = "3" ]; then
        echo "DEV-VERIFY(fast): ⚠ 空覆盖 ⇒ 退出码 3。3 的意思是「没测到东西」，不是「测了但失败」："
        echo "DEV-VERIFY(fast): ⚠ 上面的 covered 只是入口自检的数。推送前必须跑全量。"
    else
        echo "DEV-VERIFY(fast): ⚠ 本轮是空覆盖（只跑了入口自检），但有红 ⇒ 实际退出码 $2（红优先于空覆盖）"
    fi
}

rc=$(resolve_exit_code "$ruff_rc" "$py_rc" "${FAST_EMPTY:-0}")
note_fast_empty_coverage "${FAST_EMPTY:-0}" "$rc"
echo "DEV-VERIFY exit_code=$rc"
exit "$rc"
