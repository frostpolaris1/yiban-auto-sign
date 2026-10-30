#!/usr/bin/env bash
# ci-scope.sh —— 按改动面判定该跑哪套测试（AGENTS §37d）。
#
# 用法：
#   scripts/ci-scope.sh --base <ref>            # 打印一行：oldline | newstack | mixed | shared
#   scripts/ci-scope.sh --base <ref> --oldline  # 旧线**代码**有改动时 rc=0，否则 rc=1
#
# 口径：
# - 旧线代码面＝yiban/ web/ tests/ frontend/ docker/ deploy/ scripts/
#   run.sh run_probe.sh docker-compose.yml requirements*.txt requirements.lock。
# - 共享配置面＝config/registry.json。它同时被两条线读（旧线：设置页与名册相关测试；
#   新栈：`update_env`、`doctor`、安装脚本都按它校验 YIBAN_* 键）。Go 批次加配置键
#   必须登记它，故它单列一类。
# - 其余（server/ ui/ docs/ 等）＝新栈面或文档面。
# - 旧线代码＋新栈都动＝mixed（两套都跑）。
# - 只动共享配置＝shared：跑读该文件的旧线测试，不跑旧线整套。
# - 空改动（如 docs-only）＝newstack：旧线套件与旧线门不需要为文档改动重跑。
# - 判定只依赖 git，不联网、不依赖 CI 专有变量；本地可复算。
#
# 为什么要有它：旧 Python 栈已冻结（AGENTS 头部），但每次 PR 仍跑整套旧线 pytest
# （--ci 约 6 分钟），Go/UI 改动被无关测试拖住。本脚本只决定「跳过哪一套」，
# 不改变任何门的内容。
#
# 为什么 config/ 要拆出来：2026-10-31 之前 config/ 算旧线代码，于是每个「加一个
# YIBAN_* 键」的 Go 批次都被判 mixed，白付整套旧线 pytest（实测 verify 作业
# 6m28s / 8m31s）。共享配置的读者是有限的十来个测试，点名跑即可（清单住
# scripts/dev-verify.sh 的 SHARED_CONFIG_TESTS，完整性由
# tests/test_dev_verify_entry.py 的 SharedConfigScopeTest 钉住）。
set -euo pipefail

base=""
mode="scope"
while [ $# -gt 0 ]; do
  case "$1" in
    --base) base=${2:?--base 需要取值}; shift 2 ;;
    --oldline) mode="oldline"; shift ;;
    -h|--help) sed -n '1,14p' "$0"; exit 0 ;;
    *) echo "ci-scope: 未知参数 $1" >&2; exit 2 ;;
  esac
done
[ -n "$base" ] || { echo "ci-scope: 缺少 --base" >&2; exit 2; }

# 两点差不用 merge-base：CI 是浅检出，三点差需要共同祖先，浅树下取不到。
# 分支落后时两点差可能多报（把别人的改动算进来）⇒ 只会更保守地跑全套，不会少跑。
changed=$(git diff --name-only "$base" HEAD)
oldline=0
newstack=0
shared=0
if [ -n "$changed" ]; then
  if printf '%s\n' "$changed" | grep -qE '^(yiban|web|tests|frontend|docker|deploy|scripts)/|^(run\.sh|run_probe\.sh|docker-compose\.yml|requirements[^/]*)$'; then
    oldline=1
  fi
  if printf '%s\n' "$changed" | grep -qE '^config/registry\.json$'; then
    shared=1
  fi
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
elif [ "$shared" = "1" ]; then
  echo shared
else
  echo newstack
fi
