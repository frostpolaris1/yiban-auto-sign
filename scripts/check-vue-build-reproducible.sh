#!/usr/bin/env bash
# ============================================================
# scripts/check-vue-build-reproducible.sh —— Vue 构建产物可复现门禁
#
# 本脚本钉住两条不变量。
#
# 守卫一（换行符口径）：构建的输入与输出全为 LF。
#   扫描范围 = `frontend/`（剪掉 node_modules 与工具缓存）+ `web/static/vue/`。
#   判据 = 任一文本文件含 CR 字节即红，并点名文件与 CR 字节数。
#   二进制文件（含 NUL 字节）不参与判定。
#
# 守卫二（可复现）：干净重建的产物与已提交产物逐字节相同。
#   做法 = 在临时目录重建 `frontend/`，再与 `web/static/vue/` 递归比对。
#   判据 = 任一条目内容不同、多余或缺失即红，并打印 diff。
#
# 用法：
#   bash scripts/check-vue-build-reproducible.sh              # 两守卫全跑（需要 node 与 npm）
#   bash scripts/check-vue-build-reproducible.sh --lf-only    # 只跑守卫一（不需要 node）
#   bash scripts/check-vue-build-reproducible.sh --compare D  # 只跑守卫二的比对段（不需要 node）
#   bash scripts/check-vue-build-reproducible.sh --repo D     # 换仓库根（默认 = 脚本上一级）
#
# 本守卫的承重段突变证明（e2e）在 scripts/e2e/vue-build-reproducible-e2e.sh：
#   bash scripts/e2e/vue-build-reproducible-e2e.sh            # WSL 内手跑，需要 node
#
# 退出码：0 = 全绿；1 = 门禁红（点名到条目）；2 = 环境错误（缺 node、缺目录、参数错）。
#
# 判定边界：本脚本从工作树读构建输入，并与工作树里的产物比对。
#   CI（干净检出）上两边都等于已提交内容，判的就是「已提交产物可由已提交源码复现」。
#   工作树带未提交改动时，它判的是「工作树内部一致」。
#   执行点在 CI 的 frontend job（见 .github/workflows/ci.yml）。
#
# 为什么需要守卫二（2026-10-06 工单 spn6 的实测）：
#   Vite 底层的 rolldown 按源文件字节算 Vue scopeId 与产物文件名。
#   源文件若为 CRLF，scopeId 与文件名都变。
#   实测：`src/logs/Logs.vue` 由 LF 改成 CRLF 后，产物由 `logs-C3BqO6Xu.js`
#   变成 `logs-BxEIruLL.js`，样式由 `logs-D0xFHfEE.css` 变成 `logs-D4NVhZ3O.css`，
#   同时 `logs.html` 与 `.vite/manifest.json` 也变。
#   单次构建因此无法同时复现两种来源的产物。
#   2026-10-06 的 PR #66 在 LF 口径下重建了 logs 包，混合来源随之消失。
#   本守卫钉住“LF 口径 + 单次构建可完整复现”，防止混合来源复发。
#
# 变异标记：`# vue-eol-mutant-begin: <名>` 与 `# vue-eol-mutant-end: <名>`
#   夹住承重段落。`scripts/e2e/vue-build-reproducible-e2e.sh` 用 sed 删段造变异体，
#   再断言“伪红出现”，以此证明该段承重（AGENTS §14）。
# ============================================================
set -uo pipefail

SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$SELF_DIR/.." && pwd)

die() { printf '环境错误：%s\n' "$*" >&2; exit 2; }
red() { printf '门禁红：%s\n' "$*" >&2; }

MODE=full
COMPARE_DIR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --lf-only) MODE=lf ;;
        --compare)
            MODE=compare; shift
            [ $# -gt 0 ] || die "--compare 缺目录参数"
            COMPARE_DIR=$1
            ;;
        --repo)
            shift
            [ $# -gt 0 ] || die "--repo 缺目录参数"
            REPO=$1
            ;;
        -h|--help)
            # 打印文件头（第 2 行到 `set -uo pipefail` 之前）。按行号写死会长短漂移。
            awk 'NR > 1 && /^set -uo pipefail/ { exit } NR > 1 { print }' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *) die "未知参数：$1" ;;
    esac
    shift
done

REPO=$(cd "$REPO" 2>/dev/null && pwd) || die "仓库根不可解析：$REPO"
FRONTEND="$REPO/frontend"
COMMITTED="$REPO/web/static/vue"

# 工具缓存目录：不是构建输入，也不入库（`frontend/.vite/` 在 .gitignore 里）。
# 源码侧剪掉 `.vite`；产物侧的 `.vite/manifest.json` 是**跟踪产物**，必须扫（见 D2）。
PRUNE_SRC=(-path '*/node_modules' -o -path '*/.vite' -o -path '*/test-results'
           -o -path '*/playwright-report' -o -path '*/.git')
PRUNE_OUT=(-path '*/node_modules' -o -path '*/test-results'
           -o -path '*/playwright-report' -o -path '*/.git')

# ------------------------------------------------------------
# 守卫一的扫描器：数 CR 字节，非零即红。
# 目录缺失或零命中即退 2：否则删掉 frontend/ 后 --lf-only 会打印全绿（假绿路径）。
# 用法：census_scan <目录> <find 剪枝表达式…>
# 不用 grep：Git Bash 的 grep 把 CR 模式当空模式，会匹配每一行（实测误报）。
# ------------------------------------------------------------
census_scan() {
    local dir=$1 f n rc=0 count=0
    shift
    local -a prune=("$@")
    [ -d "$dir" ] || die "扫描目录不存在：$dir（缺目录不许判绿）"
    while IFS= read -r f; do
        count=$((count + 1))
        # 含 NUL 字节的文件是二进制，不参与行尾判定
        if [ "$(wc -c < "$f")" -ne "$(tr -d '\000' < "$f" | wc -c)" ]; then
            continue
        fi
        n=$(tr -cd '\r' < "$f" | wc -c)
        if [ "$n" -gt 0 ]; then
            red "CRLF 行尾：${f#"$REPO"/}（CR 字节数 $n）"
            rc=1
        fi
    done < <(find "$dir" \( "${prune[@]}" \) -prune -o -type f -print)
    [ "$count" -gt 0 ] || die "扫描目录下没有可扫文件：$dir（零命中即通过是假绿路径）"
    return $rc
}

guard_lf() {
    local rc=0
    # vue-eol-mutant-begin: lf-census
    census_scan "$FRONTEND" "${PRUNE_SRC[@]}" || rc=1
    census_scan "$COMMITTED" "${PRUNE_OUT[@]}" || rc=1
    # vue-eol-mutant-end: lf-census
    return $rc
}

# ------------------------------------------------------------
# 守卫二的比对段：逐字节比对重建目录与已提交产物目录。不依赖 node。
# ------------------------------------------------------------
compare_artifacts() {
    local built=$1 out
    [ -d "$built" ] || die "待比对目录不存在：$built"
    [ -d "$COMMITTED" ] || die "已提交产物目录不存在：$COMMITTED（先构建并提交 web/static/vue/）"
    out=$(diff -r "$built" "$COMMITTED" 2>&1) && {
        printf '重建产物与已提交产物逐字节相同：%s\n' "$COMMITTED"
        return 0
    }
    printf '%s\n' "$out" >&2
    red "重建产物与已提交产物不一致（差异见上：Only in / differ）"
    return 1
}

# ------------------------------------------------------------
# 守卫二主体：临时目录里重建，再比对。工作树只读，构建产物不落回工作树。
# ------------------------------------------------------------
guard_reproducible() {
    command -v node >/dev/null 2>&1 || die "缺 node：守卫二需要 node 与 npm（只跑守卫一请加 --lf-only）"
    command -v npm >/dev/null 2>&1 || die "缺 npm：守卫二需要 node 与 npm（只跑守卫一请加 --lf-only）"
    [ -d "$FRONTEND" ] || die "构建源码目录不存在：$FRONTEND"
    command -v tar >/dev/null 2>&1 || die "缺 tar：守卫二用 tar 复制构建输入"

    local tmp rc=0 build_out
    # 环境错误路径（die → exit 2）也要清临时目录：否则每次退 2 都残留一份 /tmp/vue-repro-*。
    # `tmp` 未建时为空串，故先判非空；`rm -rf` 对已删路径幂等。
    trap 'if [ -n "${tmp:-}" ]; then rm -rf "$tmp"; fi' EXIT
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/vue-repro-XXXXXX") || die "建临时目录失败"

    mkdir -p "$tmp/proj"
    ( cd "$FRONTEND" && tar -cf - \
        --exclude=./node_modules --exclude=./.vite \
        --exclude=./test-results --exclude=./playwright-report . ) \
        | ( cd "$tmp/proj" && tar -xf - ) || { rm -rf "$tmp"; die "复制构建输入失败"; }

    if [ -d "$FRONTEND/node_modules" ]; then
        ln -s "$FRONTEND/node_modules" "$tmp/proj/node_modules"
    else
        printf '未发现 %s/node_modules，执行 npm ci …\n' "$FRONTEND"
        ( cd "$tmp/proj" && npm ci --no-audit --no-fund ) \
            || { rm -rf "$tmp"; die "npm ci 失败（无 node_modules 且装不上依赖）"; }
    fi

    if ! build_out=$( cd "$tmp/proj" && npx vite build --outDir "$tmp/out" --emptyOutDir 2>&1 ); then
        printf '%s\n' "$build_out" >&2
        rm -rf "$tmp"
        die "重建失败（见上方构建输出）"
    fi

    # vue-eol-mutant-begin: compare-artifacts
    compare_artifacts "$tmp/out" || rc=1
    # vue-eol-mutant-end: compare-artifacts

    rm -rf "$tmp"
    return $rc
}

rc=0
case "$MODE" in
    lf)
        guard_lf || rc=1
        ;;
    compare)
        compare_artifacts "$COMPARE_DIR" || rc=1
        ;;
    full)
        guard_lf || rc=1
        guard_reproducible || rc=1
        ;;
esac

if [ "$rc" -eq 0 ]; then
    printf 'Vue 构建产物可复现门禁：全绿（模式 %s）\n' "$MODE"
else
    printf 'Vue 构建产物可复现门禁：红（模式 %s）\n' "$MODE" >&2
fi
exit "$rc"
