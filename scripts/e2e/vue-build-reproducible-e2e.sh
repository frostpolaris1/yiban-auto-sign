#!/usr/bin/env bash
# ============================================================
# scripts/e2e/vue-build-reproducible-e2e.sh —— 可复现门禁的端到端校验
#
# 被测对象：`scripts/check-vue-build-reproducible.sh`。
# 用法（在 WSL 内、仓库根目录跑；需要 node 与 npm）：
#   bash scripts/e2e/vue-build-reproducible-e2e.sh
#
# 覆盖：
#   T1 基线绿   真仓两守卫全跑 ⇒ 退出 0
#   T2 CRLF 源红 样本树的 `frontend/src/logs/Logs.vue` 改成 CRLF ⇒ 红
#   T3 陈旧产物红 样本树的已提交产物改一个字节（保持 LF）⇒ 红
#   T4 compare 承重 摘掉 compare-artifacts 段后，T3 的红消失 ⇒ 该段承重
#   T5 census 承重  摘掉 lf-census 段后，非构建文件带 CRLF 的红消失 ⇒ 该段承重
#   T6 census 单独抓产物 CRLF  --lf-only 下产物带 CRLF ⇒ 红
#
# 每例用一棵独立样本树，互不干扰。样本树把 `node_modules` 软链到真仓的那份，
# 门禁于是不必联网装依赖。临时树成功即删；失败保留并打印路径。
#
# 变异机制：门禁脚本内以 `# vue-eol-mutant-begin: <名>` … `# vue-eol-mutant-end: <名>`
# 夹住承重段落；本脚本用 sed 删段造变异体。这是“摘掉该段后伪红出现”的可执行证明
# （AGENTS §14：守卫必须真的承重）。
# ============================================================
set -uo pipefail

REQUIRED_CMDS="tar mktemp node npm diff find"
MISSING_CMDS=""
for _cmd in $REQUIRED_CMDS; do
    command -v "$_cmd" >/dev/null 2>&1 || MISSING_CMDS="${MISSING_CMDS:+$MISSING_CMDS }$_cmd"
done
if [ -n "$MISSING_CMDS" ]; then
    printf '错误：本 e2e 需要在 WSL 内跑，当前环境缺少命令：%s\n' "$MISSING_CMDS" >&2
    printf '      正确跑法（WSL 内、仓库根目录）：bash scripts/e2e/vue-build-reproducible-e2e.sh\n' >&2
    exit 2
fi

SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$SELF_DIR/../.." && pwd)
GUARD="$REPO/scripts/check-vue-build-reproducible.sh"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/vue-repro-e2e.XXXXXX")
[ -f "$GUARD" ] || { printf '错误：门禁脚本不存在：%s\n' "$GUARD" >&2; exit 2; }

BAD=0
cleanup_tmp() {
    if [ "${BAD:-0}" = "0" ]; then
        rm -rf "$TMP"
    else
        printf '工作目录（失败保留，供排查）：%s\n' "$TMP"
    fi
}
trap cleanup_tmp EXIT

CASE_NO=0
FAILS=0

pass() { CASE_NO=$((CASE_NO + 1)); printf 'ok %2d - %s\n' "$CASE_NO" "$1"; }
fail() {
    CASE_NO=$((CASE_NO + 1)); BAD=1; FAILS=$((FAILS + 1))
    printf 'FAIL %2d - %s\n' "$CASE_NO" "$1"
}

# node_modules 来源：优先复用真仓装好的那份；缺了就装一次给各样本树复用。
# 样本树不各自安装：一次安装约 25 秒，六例各装一次会把本 e2e 拖到分钟级。
NM="$REPO/frontend/node_modules"
if [ ! -d "$NM" ]; then
    printf '未发现 %s，先在临时目录装一次依赖…\n' "$NM"
    mkdir -p "$TMP/nmproj"
    ( cd "$REPO/frontend" && tar -cf - --exclude=./node_modules --exclude=./.vite . ) \
        | ( cd "$TMP/nmproj" && tar -xf - )
    ( cd "$TMP/nmproj" && npm ci --no-audit --no-fund >/dev/null 2>&1 ) \
        || { printf '错误：npm ci 失败，本 e2e 需要可用的前端依赖\n' >&2; exit 2; }
    NM="$TMP/nmproj/node_modules"
fi

# 样本树：`<TMP>/tree-<名>/`，含 frontend/（软链共享的 node_modules）与 web/static/vue/。
make_tree() {
    local name=$1 dir="$TMP/tree-$1"
    rm -rf "$dir"
    mkdir -p "$dir/frontend" "$dir/web/static"
    ( cd "$REPO/frontend" && tar -cf - \
        --exclude=./node_modules --exclude=./.vite \
        --exclude=./test-results --exclude=./playwright-report . ) \
        | ( cd "$dir/frontend" && tar -xf - )
    ln -s "$NM" "$dir/frontend/node_modules"
    cp -r "$REPO/web/static/vue" "$dir/web/static/vue"
    printf '%s' "$dir"
}

# 变异体：删掉门禁脚本里名 name 的变异段，写到 `<TMP>/guard-mutant-<name>.sh`。
mutant_guard() {
    local name=$1 out="$TMP/guard-mutant-$1.sh"
    sed "/vue-eol-mutant-begin: $name/,/vue-eol-mutant-end: $name/d" "$GUARD" > "$out"
    printf '%s' "$out"
}

# 取样本树里 logs 入口的 JS 产物路径。产物名含内容哈希，故反查，不写死。
logs_js_of() {
    local hits=("$1/web/static/vue/assets"/logs-*.js)
    [ -f "${hits[0]}" ] || { printf '错误：%s 下找不到 logs JS 产物\n' "$1" >&2; exit 2; }
    printf '%s' "${hits[0]}"
}

run_guard() {  # run_guard <脚本路径> <参数…>；打印退出码
    local script=$1; shift
    bash "$script" "$@" >"$TMP/last.out" 2>&1
    printf '%s' "$?"
}

expect_rc() {  # expect_rc <名> <期望> <实得> [证据文件]
    local name=$1 want=$2 got=$3 ev=${4:-$TMP/last.out}
    if [ "$want" = "$got" ]; then
        pass "$name（退出码 $got）"
    else
        fail "$name（期望退出码 $want，实得 $got）"
        printf '    证据（末 15 行）：\n' >&2
        tail -n 15 "$ev" | sed 's/^/    /' >&2
    fi
}

# ------------------------------------------------------------
# T1 基线绿
# ------------------------------------------------------------
expect_rc "T1 基线绿：真仓两守卫全跑" 0 "$(run_guard "$GUARD" --repo "$REPO")"

# ------------------------------------------------------------
# T2 CRLF 源红：Logs.vue 改 CRLF
# ------------------------------------------------------------
T2=$(make_tree crlfsrc)
sed -i 's/$/\r/' "$T2/frontend/src/logs/Logs.vue"
expect_rc "T2 CRLF 源应判红" 1 "$(run_guard "$GUARD" --repo "$T2")"

# ------------------------------------------------------------
# T3 陈旧产物红：改已提交产物的一个字节（保持 LF）
# ------------------------------------------------------------
T3=$(make_tree stale)
printf '\n/* stale */\n' >> "$(logs_js_of "$T3")"
expect_rc "T3 陈旧产物应判红" 1 "$(run_guard "$GUARD" --repo "$T3")"

# ------------------------------------------------------------
# T4 compare 段承重：摘掉后 T3 的红必须消失
# ------------------------------------------------------------
M4=$(mutant_guard compare-artifacts)
expect_rc "T4 摘掉 compare 段后陈旧产物不再判红（证明该段承重）" 0 \
    "$(run_guard "$M4" --repo "$T3")"

# ------------------------------------------------------------
# T5 lf-census 段承重：非构建文件带 CRLF，门禁靠 census 才抓到
# ------------------------------------------------------------
T5=$(make_tree censussrc)
printf '// probe\r\n' > "$T5/frontend/e2e/probe-crlf.spec.ts"
expect_rc "T5 非构建文件带 CRLF 应判红" 1 "$(run_guard "$GUARD" --repo "$T5")"
M5=$(mutant_guard lf-census)
expect_rc "T5b 摘掉 lf-census 段后该红消失（证明该段承重）" 0 \
    "$(run_guard "$M5" --repo "$T5")"

# ------------------------------------------------------------
# T6 census 单独抓产物 CRLF（--lf-only，不需要 node）
# ------------------------------------------------------------
T6=$(make_tree crlfart)
sed -i 's/$/\r/' "$(logs_js_of "$T6")"
expect_rc "T6 --lf-only 下产物带 CRLF 应判红" 1 "$(run_guard "$GUARD" --repo "$T6" --lf-only)"

# ------------------------------------------------------------
printf -- '----\nvue-build-reproducible-e2e：%d 例，%d 失败\n' "$CASE_NO" "$FAILS"
[ "$FAILS" -eq 0 ] || exit 1
