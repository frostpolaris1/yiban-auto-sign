#!/usr/bin/env bash
# ============================================================
# scripts/e2e/dev-verify-e2e.sh —— `scripts/dev-verify.sh` 的端到端校验
#
# 用法（必须在 WSL 内跑；脚本用真解释器真跑 dev-verify.sh，不用替身）：
#   bash scripts/e2e/dev-verify-e2e.sh           # 快速用例
#   bash scripts/e2e/dev-verify-e2e.sh --full    # 追加 T5 全量绿（约 2~3 分钟）
#
# 临时树：成功走完即删；失败则保留并在结尾打印路径（只看有东西要看的那次）。
#
# 覆盖：
#   T1 绿     脚本退出 0；日志含解释器绝对路径/版本、被跑提交 sha、汇总四数；
#             passed+skipped+errors 与副本内 --collect-only 的条数一致；
#             日志首行与末行都在（证明日志完整，不是 tail 截断）
#   T2 红     注入一个必失败的用例 ⇒ 脚本非 0，日志能读到该失败的名字与 failed=1
#   T3 无 .git 守卫  源仓库没有 .git 时脚本要么自己修好、要么响亮失败，
#             不许静默把伪红当红。变异（摘掉该守卫）必须让"伪红真的出现"
#   T4 CRLF 归一化   源树含 CRLF 时先归一化再跑 ⇒ 0 伪红；
#             变异（摘掉归一化）必须让 CRLF 造成的伪红重现
#   T5 全量绿（--full）真仓全量 0 失败
#   T6 日志保留       --keep N 只留最近 N 份
#
# 变异机制：dev-verify.sh 内以
#   `# dev-verify-mutant-begin: <名>` … `# dev-verify-mutant-end: <名>`
# 夹住被钉住的段落；本脚本用 sed 删掉该段造出变异体，再断言"守卫承重"。
# 这是"守卫摘掉后 e2e 必须变红"的可执行证明（AGENTS §14）。
# ============================================================
set -uo pipefail

SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$SELF_DIR/../.." && pwd)
SCRIPT="$REPO/scripts/dev-verify.sh"
FIXED_PY=/root/.venv-yiban-wsl/bin/python
TMP=$(mktemp -d "${TMPDIR:-/tmp}/dev-verify-e2e.XXXXXX")
LOGS="$TMP/logs"

# 收尾：成功即删掉临时树（每次运行建数个仓副本，不删会在 /tmp 里累积）；失败则保留并
# 在结尾打印路径供排查——"留证据"只在真有东西要看的时候才有意义。
BAD=0
cleanup_tmp() {
    if [ "${BAD:-0}" = "0" ]; then
        rm -rf "$TMP"
    else
        printf '工作目录（失败保留，供排查）：%s\n' "$TMP"
    fi
}
trap cleanup_tmp EXIT

# 快速目标：一个纯单元测试文件，够快且不依赖 git
TARGET_FAST=tests/test_masking_tokens.py
GIT_TESTS="tests/test_deploy_prod_artifacts.py tests/test_web_vue_sources_tracked.py"
GATE_TEST=tests/test_shared_facts_gate.py

FULL=0
for a in "$@"; do
    case "$a" in
        --full) FULL=1 ;;
        *)
            echo "用法：bash scripts/e2e/dev-verify-e2e.sh [--full]（未知参数：$a）" >&2
            exit 2
            ;;
    esac
done

# --ci 模式取 PATH 上的 python（CI runner 由 setup-python 保证）。WSL 内没有 `python`
# 这个名字，故 e2e 显式给解释器；全量模式不读这个变量（它钉死固定 venv）。
export DEV_VERIFY_PY=/root/.venv-yiban-wsl/bin/python

CASE_NO=0
OUT=""
RC=0
LOG=""

say() { printf '\n--- %s\n' "$1"; }
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; BAD=$((BAD + 1)); }

# 跑一次 dev-verify.sh（真脚本真调用）；回写 OUT / RC / LOG
run_dv() {
    local script=$1
    shift
    CASE_NO=$((CASE_NO + 1))
    OUT="$TMP/out-$CASE_NO.txt"
    bash "$script" "$@" >"$OUT" 2>&1
    RC=$?
    LOG=$(sed -n 's/^DEV-VERIFY log: //p' "$OUT" | tail -1)
    printf '  (case %s) script=%s rc=%s\n' "$CASE_NO" "$(basename "$script")" "$RC"
}

# 断言读"日志文件"（不是终端捕获），因为判据要求落在日志里
logtext() { cat "${LOG:-$OUT}"; }

expect_rc() {
    if [ "$RC" = "$1" ]; then pass "退出码=$RC"; else fail "退出码=$RC，期望 $1"; fi
}
expect_rc_nonzero() {
    if [ "$RC" != "0" ]; then pass "退出码=$RC（非 0）"; else fail "退出码=0，期望非 0"; fi
}
expect_log_has() {
    if grep -qF -- "$1" <<<"$(logtext)"; then pass "日志含「$1」"; else fail "日志缺「$1」"; fi
}
expect_log_lacks() {
    if grep -qF -- "$1" <<<"$(logtext)"; then fail "日志不该含「$1」"; else pass "日志不含「$1」"; fi
}
expect_log_re() {
    if grep -qE -- "$1" <<<"$(logtext)"; then pass "日志匹配 /$1/"; else fail "日志不匹配 /$1/"; fi
}
# 从日志取 `DEV-VERIFY summary:` 行里的某个数（形如 passed=3800 failed=0）
sum_num() {
    local line
    line=$(sed -n 's/^DEV-VERIFY summary: //p' <<<"$(logtext)" | tail -1)
    printf '%s\n' "$line" | grep -oE "(^| )$1=[0-9]+" | head -1 | sed 's/.*=//'
}

mred() { # 变异体的伪红总数：failed 与 errors 都算（收集期出错也是伪红的一种）
    local f e
    f=$(sum_num failed)
    e=$(sum_num errors)
    printf '%s\n' "$(( ${f:-0} + ${e:-0} ))"
}

make_mutant() {
    # $1 = 产物路径，其余 = 变异点名（可多个）
    local out=$1
    shift
    local exprs=()
    for n in "$@"; do
        # 标记必须成对且各一枚：begin 没有配对的 end 时 sed 会删到文件尾，
        # 变异体虽然"与真脚本不同"却已不再是"只摘掉那一段"，断言就钉错了对象。
        local nb ne
        nb=$(grep -c "^# dev-verify-mutant-begin: $n$" "$SCRIPT")
        ne=$(grep -c "^# dev-verify-mutant-end: $n$" "$SCRIPT")
        if [ "$nb" != "1" ] || [ "$ne" != "1" ]; then
            echo "  变异点标记不成对（$n：begin=$nb end=$ne）" >&2
            return 1
        fi
        exprs+=(-e "/^# dev-verify-mutant-begin: $n$/,/^# dev-verify-mutant-end: $n$/d")
    done
    sed "${exprs[@]}" "$SCRIPT" >"$out"
    chmod +x "$out"
}

gitify() { # $1 = 目录：建成一个真 git 仓库
    (cd "$1" && git init -q . &&
        git add -A >/dev/null 2>&1 &&
        git -c user.email=e2e@local -c user.name=e2e commit -qm e2e-scratch >/dev/null)
}

scratch() { # $1 = 名字：复制真仓工作树（不含 .git）到 WSL 原生盘
    local d="$TMP/src-$1"
    mkdir -p "$d"
    rsync -a --exclude='.git' "$REPO/" "$d/"
    printf '%s\n' "$d"
}

lfclean() { # $1 = 目录：把副本内文本一律转 LF（等价于 dev-verify.sh 的归一化那一步）
    (cd "$1" && grep -rlI --exclude-dir=.git $'\r' . 2>/dev/null |
        while IFS= read -r f; do sed -i 's/\r*$//' "$f"; done) || true
}

# ============================================================
say "T1 绿：真仓 + 小目标"
# ============================================================
if [ ! -f "$SCRIPT" ]; then
    fail "入口脚本不存在：$SCRIPT"
else
    run_dv "$SCRIPT" --repo "$REPO" --target "$TARGET_FAST" --log-dir "$LOGS"
    expect_rc 0
    expect_log_has "DEV-VERIFY repo: $REPO"
    expect_log_has "DEV-VERIFY interpreter: $FIXED_PY ("
    expect_log_re '^DEV-VERIFY sha: [0-9a-f]{40}$'
    expect_log_has "DEV-VERIFY summary:"
    expect_log_has "DEV-VERIFY exit_code=0"
    if [ "$(sum_num failed)" = "0" ] && [ "$(sum_num errors)" = "0" ]; then
        pass "日志汇总 failed=0 errors=0"
    else
        fail "日志汇总 failed=$(sum_num failed) errors=$(sum_num errors)"
    fi
    # 副本必须是能用的 git 仓库（有真历史、跟踪集与源同级）
    COPY=$(sed -n 's/^DEV-VERIFY copy: //p' "$OUT" | tail -1)
    SHA=$(sed -n 's/^DEV-VERIFY sha: //p' <<<"$(logtext)" | tail -1)
    if [ -n "$COPY" ] && git -C "$COPY" rev-parse HEAD >/dev/null 2>&1; then
        pass "副本 .git 可用（$COPY）"
        if [ "$(git -C "$COPY" rev-parse HEAD)" = "$SHA" ]; then
            pass "副本 HEAD == 被跑提交 sha"
        else
            fail "副本 HEAD != 被跑提交 sha"
        fi
        ntr=$(git -C "$COPY" ls-files | wc -l)
        if [ "$ntr" -ge 600 ]; then pass "副本跟踪集 $ntr 个（≥600）"; else fail "副本跟踪集过小：$ntr"; fi
        nhist=$(git -C "$COPY" log --oneline 2>/dev/null | wc -l)
        if [ "$nhist" -ge 10 ]; then pass "副本保留真历史（$nhist 个提交）"; else fail "副本历史过短：$nhist"; fi
    else
        fail "副本 .git 不可用（COPY='$COPY'）"
    fi
    # 汇总四数与"副本内 --collect-only 的条数"一致（不是自证）
    collected=$(cd "$COPY" && "$FIXED_PY" -m pytest $TARGET_FAST -q -p no:randomly --collect-only 2>/dev/null |
        grep -oE '^[0-9]+ tests? collected' | grep -oE '^[0-9]+')
    tot=0
    for k in passed failed skipped errors; do
        v=$(sum_num "$k")
        tot=$((tot + ${v:-0}))
    done
    if [ -n "$collected" ] && [ "$tot" = "$collected" ]; then
        pass "汇总四数之和=$tot == collect-only=$collected"
    else
        fail "汇总四数之和=$tot != collect-only=${collected:-无}"
    fi
fi

# ============================================================
say "T2 红：注入必失败用例"
# ============================================================
d=$(scratch red)
cat >"$d/tests/test_zz_dev_verify_e2e_fail.py" <<'PY'
# -*- coding: utf-8 -*-
"""e2e 注入：本用例必须失败，用于证明 dev-verify.sh 把失败如实传出。"""
import unittest


class DevVerifyE2EInjectedFailure(unittest.TestCase):
    def test_injected_failure(self):
        self.assertEqual(1, 2, "dev-verify e2e 注入的失败")


if __name__ == "__main__":
    unittest.main()
PY
gitify "$d"
run_dv "$SCRIPT" --repo "$d" --target tests/test_zz_dev_verify_e2e_fail.py --log-dir "$LOGS"
expect_rc_nonzero
expect_log_has "test_injected_failure"
expect_log_has "failed=1"
expect_log_re '^DEV-VERIFY exit_code=[1-9]'

# ============================================================
say "T3 无 .git：守卫必须响亮失败（不许静默伪红）"
# ============================================================
d=$(scratch nogit)
run_dv "$SCRIPT" --repo "$d" --target "$TARGET_FAST" --log-dir "$LOGS"
expect_rc_nonzero
expect_log_has "源仓库不是可用的 git 仓库"
expect_log_lacks "DEV-VERIFY summary:"
# 变异：摘掉副本 git 守卫 ⇒ 伪红必须真的出现（证明守卫承重）
m="$TMP/dev-verify.mutant-copy-git.sh"
if make_mutant "$m" source-git copy-git && [ -s "$m" ] && ! cmp -s "$SCRIPT" "$m"; then
    pass "变异体已摘掉 source-git / copy-git 段"
    run_dv "$m" --repo "$d" --target "$GIT_TESTS" --log-dir "$LOGS"
    expect_rc_nonzero
    expect_log_has "DEV-VERIFY summary:"
    mr=$(mred)
    if [ "${mr:-0}" -gt 0 ]; then
        pass "变异体伪红 $mr 条（无 .git 的伪红确实存在）"
    else
        fail "变异体伪红=${mr:-0}（failed+errors）：无 .git 的伪红未被复现，守卫的承重性无法证明"
    fi
else
    fail "变异体不可信（标记不成对 / 为空 / 与真脚本相同）：变异点不再被 e2e 钉住"
fi

# ============================================================
say "T4 CRLF：先归一化再跑（0 伪红）；摘掉归一化必须伪红"
# ============================================================
d=$(scratch crlf)
sed -i 's/\r*$/\r/' "$d/scripts/check-shared-facts.sh"
if grep -qU $'\r' "$d/scripts/check-shared-facts.sh"; then pass "CRLF 已注入"; else fail "CRLF 注入失败"; fi
gitify "$d"
run_dv "$SCRIPT" --repo "$d" --target "$GATE_TEST" --log-dir "$LOGS"
expect_rc 0
if [ "$(sum_num failed)" = "0" ]; then pass "归一化后 failed=0"; else fail "归一化后 failed=$(sum_num failed)"; fi
m="$TMP/dev-verify.mutant-lf.sh"
if make_mutant "$m" lf && [ -s "$m" ] && ! cmp -s "$SCRIPT" "$m"; then
    pass "变异体已摘掉 lf 段"
    run_dv "$m" --repo "$d" --target "$GATE_TEST" --log-dir "$LOGS"
    expect_rc_nonzero
    mr=$(mred)
    if [ "${mr:-0}" -gt 0 ]; then
        pass "变异体伪红 $mr 条（CRLF 的伪红确实存在）"
    else
        fail "变异体伪红=${mr:-0}（failed+errors）：CRLF 伪红未被复现，归一化的承重性无法证明"
    fi
else
    fail "变异体不可信（标记不成对 / 为空 / 与真脚本相同）：变异点不再被 e2e 钉住"
fi

# ============================================================
say "T7 --ci 就地跑：跟踪文件含 CRLF 时必须响亮拒绝（不许伪红）"
# ============================================================
# T7 复用 T4 建的 CRLF 样本仓（$d）。先自检样本真是 CRLF 仓：否则一旦 T4 被改动或重排，
# T7 会静默对着别的仓断言（"测错了对象"也是伪绿的一种）。
if grep -qU $'\r' "$d/scripts/check-shared-facts.sh"; then
    pass "T7 样本仓自检：CRLF 在"
else
    fail "T7 样本仓不是 CRLF 仓（T4 的样本被动过？）"
fi
run_dv "$SCRIPT" --ci --repo "$d"
expect_rc 2
expect_log_has "跟踪文件含 CRLF 行尾"
expect_log_has "scripts/check-shared-facts.sh"

# ============================================================
say "T8 --ci 在干净树（LF + 有 .git）：CRLF 守卫必须放行（不许把正常检出判成环境错误）"
# ============================================================
# 这是 CI 主路径：runner 的检出是 LF，守卫必须放行。解释器故意给 /bin/false——它在第一条
# 真命令（ruff）处返回 1，脚本于是停在守卫之后、跑不到子集，用例是秒级，而守卫那段走的
# 是真脚本真判定。缺这条时踩过：xargs 在"无命中"时返回 123，被当成"扫描失败"，干净树上
# 的 --ci 会在跑任何测试前退出 2（CI 误红）。
dc=$(scratch ci-clean)
lfclean "$dc"
gitify "$dc"
if grep -rlI --exclude-dir=.git $'\r$' "$dc" 2>/dev/null | grep -q .; then
    fail "T8 样本仓不是 LF 仓（fixture 没归一化干净）"
else
    pass "T8 样本仓自检：无 CRLF 行尾"
fi
CASE_NO=$((CASE_NO + 1))
OUT="$TMP/out-$CASE_NO.txt"
DEV_VERIFY_PY=/bin/false bash "$SCRIPT" --ci --repo "$dc" >"$OUT" 2>&1
RC=$?
LOG="$OUT"
printf '  (case %s) script=%s rc=%s [DEV_VERIFY_PY=/bin/false]\n' \
    "$CASE_NO" "$(basename "$SCRIPT")" "$RC"
expect_rc 1
expect_log_has "DEV-VERIFY(ci) lint"
expect_log_has "DEV-VERIFY(ci) exit_code=1"
expect_log_lacks "CRLF 扫描失败"
expect_log_lacks "跟踪文件含 CRLF 行尾"

# ============================================================
say "T6 日志保留：--keep 2 只留最近 2 份"
# ============================================================
KEEP_DIR="$TMP/logs-keep"
for _ in 1 2 3; do
    run_dv "$SCRIPT" --repo "$REPO" --target "$TARGET_FAST" --log-dir "$KEEP_DIR" --keep 2 >/dev/null
    sleep 1
done
n=$(ls -1 "$KEEP_DIR"/dev-verify-*.log 2>/dev/null | wc -l)
if [ "$n" = "2" ]; then pass "保留 2 份（实际 $n）"; else fail "保留份数=$n，期望 2"; fi

# ============================================================
if [ "$FULL" = "1" ]; then
    say "T5 全量绿：真仓全量（不注目标）"
    run_dv "$SCRIPT" --repo "$REPO" --log-dir "$LOGS"
    expect_rc 0
    if [ "$(sum_num failed)" = "0" ] && [ "$(sum_num errors)" = "0" ]; then
        pass "全量 failed=0 errors=0 passed=$(sum_num passed)"
    else
        fail "全量 failed=$(sum_num failed) errors=$(sum_num errors)"
    fi
    p=$(sum_num passed)
    if [ "${p:-0}" -ge 3000 ]; then pass "全量 passed=$p（≥3000）"; else fail "全量 passed=${p:-0} 偏低"; fi
fi

# ============================================================
printf '\n============================================\n'
if [ "$BAD" = "0" ]; then
    printf 'e2e 结果：全部通过（%s 次脚本调用）\n' "$CASE_NO"
else
    printf 'e2e 结果：%s 项失败（%s 次脚本调用）\n' "$BAD" "$CASE_NO"
fi
# 工作目录由 cleanup_tmp 在退出时处理：成功删掉，失败才打印路径留证。
printf '============================================\n'
[ "$BAD" = "0" ]
