#!/usr/bin/env bash
# ============================================================
# scripts/check-deploy-target.sh —— 部署可达断言（MF-41 仓内半条）
#
# 用法：
#   scripts/check-deploy-target.sh <remote> <branch> <commit-ish>
# 生产调用（现网部署命令是 `git pull gitee server-web`，上线前先跑这道门）：
#   bash scripts/check-deploy-target.sh gitee server-web "$(git rev-parse HEAD)"
# 本地 fixture 远端（测试用，无网络）：<remote> 直接给裸仓路径即可，如
#   bash scripts/check-deploy-target.sh /srv/fixtures/origin.git main <sha>
#
# 存在的原因（现象）：gitee/develop 停在旧提交、本线代码只在别处 ⇒ 按现流程
# `git pull gitee server-web` 部署不到审过的这份代码，且没有任何机制发现这件事。
# 本脚本让"部署不可达"在部署前变红灯：git fetch + merge-base --is-ancestor。
#
# 只读承诺：只 fetch（不改工作区/分支），**不做任何 push**——发布线统一
# （把包含目标提交的分支发布到 <remote>/<branch>）由持有人执行，见任务报告。
# 退出码：0 可达；1 远端分支不含目标提交；2 用法/解析/远端访问失败。
# ============================================================
set -euo pipefail

if [ $# -ne 3 ]; then
    echo "用法: $0 <remote> <branch> <commit-ish>" >&2
    exit 2
fi
remote="$1"; branch="$2"; target="$3"

git rev-parse --git-dir >/dev/null 2>&1 \
    || { echo "不可达: 当前目录不在 git 仓库内，无从断言" >&2; exit 2; }

sha=$(git rev-parse --verify --quiet "${target}^{commit}" || true)
if [ -z "$sha" ]; then
    echo "不可达: 本地仓库解析不出目标提交 $target（先 fetch/checkout 到包含它的提交再跑）" >&2
    exit 2
fi

if ! git ls-remote --exit-code "$remote" "refs/heads/$branch" >/dev/null 2>&1; then
    echo "不可达: 远端 $remote 上找不到分支 $branch（或远端不可访问）" >&2
    exit 2
fi

git fetch --quiet "$remote" "refs/heads/$branch" \
    || { echo "不可达: fetch $remote refs/heads/$branch 失败" >&2; exit 2; }
remote_head=$(git rev-parse --short FETCH_HEAD)

if git merge-base --is-ancestor "$sha" FETCH_HEAD; then
    echo "可达: ${remote}/${branch}（现 ${remote_head}）已包含 ${sha} —— 按现流程 git pull ${remote} ${branch} 可部署到该提交"
    exit 0
fi

echo "不可达: ${remote}/${branch}（现 ${remote_head}）不包含 ${sha}" >&2
echo "  ⇒ 按现流程（git pull ${remote} ${branch}）部署不到这份代码。" >&2
echo "  发布线统一需持有人把包含该提交的分支发布到 ${remote}/${branch}——本脚本不执行任何 push。" >&2
exit 1
