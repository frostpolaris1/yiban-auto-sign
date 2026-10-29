#!/usr/bin/env bash
# ci-scope.sh —— 按改动面判定该跑哪套测试（AGENTS §37d）。
#
# 用法：
#   scripts/ci-scope.sh --base <ref>            # 打印一行：oldline | newstack | mixed
#   scripts/ci-scope.sh --base <ref> --oldline  # 旧线面有改动时 rc=0，否则 rc=1
#
# 口径：
# - 旧线面＝yiban/ web/ tests/ frontend/ docker/ deploy/ scripts/ config/
#   run.sh run_probe.sh docker-compose.yml requirements*.txt requirements.lock。
# - 其余（server/ ui/ docs/ 等）＝新栈面或文档面。
# - 两类都动＝mixed（两套都跑）。
# - 空改动（如 docs-only）＝newstack：旧线套件与旧线门不需要为文档改动重跑。
# - 判定只依赖 git，不联网、不依赖 CI 专有变量；本地可复算。
#
# 为什么要有它：旧 Python 栈已冻结（AGENTS 头部），但每次 PR 仍跑整套旧线 pytest
# （--ci 约 6 分钟），Go/UI 改动被无关测试拖住。本脚本只决定「跳过哪一套」，
# 不改变任何门的内容。
set -euo pipefail

base=""
mode="scope"
while [ $# -gt 0 ]; do
  case "$1" in
    --base) base=${2:?--base 需要取值}; shift 2 ;;
    --oldline) mode="oldline"; shift ;;
    -h|--help) sed -n '1,20p' "$0"; exit 0 ;;
    *) echo "ci-scope: 未知参数 $1" >&2; exit 2 ;;
  esac
done
[ -n "$base" ] || { echo "ci-scope: 缺少 --base" >&2; exit 2; }

changed=$(git diff --name-only "$base"...HEAD)
oldline=0
if [ -n "$changed" ]; then
  if printf '%s\n' "$changed" | grep -qE '^(yiban|web|tests|frontend|docker|deploy|scripts|config)/|^(run\.sh|run_probe\.sh|docker-compose\.yml|requirements[^/]*)$'; then
    oldline=1
  fi
fi
newstack=0
if [ -n "$changed" ]; then
  if printf '%s\n' "$changed" | grep -qE '^(server|ui)/'; then
    newstack=1
  fi
fi

if [ "$mode" = "oldline" ]; then
  [ "$oldline" = "1" ] && exit 0
  exit 1
fi

if [ "$oldline" = "1" ] && [ "$newstack" = "1" ]; then
  echo mixed
elif [ "$oldline" = "1" ]; then
  echo oldline
else
  echo newstack
fi
