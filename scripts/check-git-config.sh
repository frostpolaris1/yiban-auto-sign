#!/usr/bin/env bash
# 主仓 .git/config 卫生检查（工单 c8j）。
#
# 形状：某个代理在 WSL 侧对主仓跑 git 时，把 core.worktree=/mnt/d/... 写进了
# 共享的 .git/config ⇒ Windows 侧主树全部 git 命令报 `Invalid path '/mnt'`，
# 而且无声（另一侧照样能干活）。主工作树的 .git/config 不应出现 core.worktree；
# 链接 worktree 的同名键在各自 gitdir（.git/worktrees/<n>/config）里，不受本检。
#
# 用法：check-git-config.sh [repo]（默认本脚本所在仓根）。违规退出 1 并给修复命令；
# 合规退出 0。只读，不修改任何文件。
set -euo pipefail

repo="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
gitpath="$repo/.git"

# 只查主仓（.git 是目录）；worktree 的 .git 是文件，键在各自 config 里，语义不同。
if [ -d "$gitpath" ] && [ -f "$gitpath/config" ]; then
    # 直接解析 config 文本，不调 git：调用方环境的 GIT_* 变量会让 `git config -f`
    # 先炸 "not a git repository"（测试与代理 shell 都实际踩到），门自身不能被
    # 外部环境状态左右。只认 [core] 段的 worktree 键（值为单行路径；引号值不展开，
    # 主仓本就不该有该键，任何非空值都按违规处理）。
    rc=0
    value=$(awk '
        /^[[:space:]]*\[/ {
            sec = $0
            gsub(/^[[:space:]]*\[|\][[:space:]]*$/, "", sec)
            next
        }
        sec == "core" && /^[[:space:]]*worktree[[:space:]]*=/ {
            sub(/^[^=]*=[[:space:]]*/, "")
            print
            exit
        }
    ' "$gitpath/config") || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "check-git-config: 无法读取 $gitpath/config（awk rc=$rc）——门无法判定，拒绝放行" >&2
        exit 2
    fi
    if [ -n "$value" ]; then
        echo "check-git-config: 主仓 .git/config 含 core.worktree=$value" >&2
        echo "  主工作树不应设置 core.worktree（c8j：WSL 侧写入会挂掉 Windows 侧 git）。" >&2
        echo "  修复：git -C \"$repo\" config --local --unset core.worktree" >&2
        exit 1
    fi
fi
exit 0
