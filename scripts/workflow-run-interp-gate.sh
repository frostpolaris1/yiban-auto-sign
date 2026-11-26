#!/usr/bin/env bash
# 静态门：workflow 的 run 段内禁止直接插值 ${{ github.base_ref }} /
# ${{ github.event.before }}。
#
# 为什么：这两个表达式在 YAML 里先被 GitHub 展开成裸文本，再交给 shell。值来自
# base 分支名 / commit SHA，属外部输入；直插进双引号里时，含引号、$、; 的取值
# 会改变命令结构（表达式不经过 shell 转义），git 参数注入就是从这里进来的。
# 统一做法：在 job/step 的 env: 里把它们变成普通环境变量（BASE_REF /
# BEFORE_SHA），shell 内以带引号的 "$BASE_REF" 使用，由 shell 自己决定转义。
#
# 扫描口径：只判 run 段（块标量 run: | / run: > 的正文与 run: 行内联值）。
# env:、with:、if: 等键不进 shell，不在此门范围内。
#
# 用法：bash scripts/workflow-run-interp-gate.sh [workflow 目录]
# 退出码：0 = 干净；1 = 发现直插；2 = 用法/目录错误。
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SELF_DIR/.." && pwd)"
WORKFLOW_DIR="${1:-$ROOT/.github/workflows}"

if [ ! -d "$WORKFLOW_DIR" ]; then
    echo "workflow-run-interp-gate: not a directory: $WORKFLOW_DIR" >&2
    exit 2
fi

# 只判 run 段。块标量的正文按缩进归属：缩进大于 run: 键的缩进即正文，
# 缩进回落到同级/更浅即结束。YAML 缩进只用空格（制表符在缩进位非法）。
SCAN_AWK='
function indent_of(s,   i) {
    i = 0
    while (i < length(s) && substr(s, i + 1, 1) == " ") i++
    return i
}
{
    if (in_run) {
        if ($0 ~ /^[ \t]*$/) { next }
        if (indent_of($0) > run_indent) {
            if ($0 ~ /\$\{\{[^}]*github\.(base_ref|event\.before)[^}]*\}\}/) {
                printf "%d:%s\n", FNR, $0
            }
            next
        }
        in_run = 0
    }
    if ($0 ~ /^[ \t]*(- )?run:[ \t]*[|>][-+]?[0-9]?[ \t]*$/) {
        in_run = 1
        run_indent = indent_of($0)
        next
    }
    if ($0 ~ /^[ \t]*(- )?run:[ \t]*[^|>]/ &&
        $0 ~ /\$\{\{[^}]*github\.(base_ref|event\.before)[^}]*\}\}/) {
        printf "%d:%s\n", FNR, $0
    }
}
'

status=0
count=0
for wf in "$WORKFLOW_DIR"/*.yml "$WORKFLOW_DIR"/*.yaml; do
    [ -e "$wf" ] || continue
    count=$((count + 1))
    hits="$(awk "$SCAN_AWK" "$wf")"
    if [ -n "$hits" ]; then
        status=1
        while IFS= read -r hit; do
            printf '%s:%s\n' "$wf" "$hit" >&2
        done <<< "$hits"
    fi
done

if [ "$status" -ne 0 ]; then
    echo 'workflow-run-interp-gate: run 段内直接插值 ${{ github.base_ref }} /' >&2
    echo '  ${{ github.event.before }}：改为在 job/step 的 env: 里映射成' >&2
    echo "  BASE_REF / BEFORE_SHA，shell 内用带引号的变量（表达式不经过 shell 转义）。" >&2
    exit 1
fi

echo "workflow-run-interp-gate: OK ($count 个 workflow 的 run 段无直插)"
