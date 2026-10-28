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
    # git config 的三态：0=有键（红）、1=无键（绿）、≥2=读不了配置（响亮红——
    # 不允许把"读不动"静默当成"没设置"，这是本门自身的 fail-open 洞）。
    # 两点必须显式处理：①`|| rc=$?` 接码——`set -e` 会让裸赋值语句在非零时直接退出；
    # ②清空继承的 GIT_DIR/GIT_WORK_TREE——调用方 shell 若带这些变量，git 会先炸
    #   "not a git repository" 而不是读我们指定的 -f 文件。
    rc=0
    value=$(env -u GIT_DIR -u GIT_WORK_TREE git config -f "$gitpath/config" --get core.worktree 2>&1) || rc=$?
    case "$rc" in
        0)
            echo "check-git-config: 主仓 .git/config 含 core.worktree=$value" >&2
            echo "  主工作树不应设置 core.worktree（c8j：WSL 侧写入会挂掉 Windows 侧 git）。" >&2
            echo "  修复：git -C \"$repo\" config --local --unset core.worktree" >&2
            exit 1
            ;;
        1) : ;;
        *)
            echo "check-git-config: 无法读取 $gitpath/config（git config rc=$rc）——门无法判定，拒绝放行" >&2
            echo "  git 输出：$value" >&2
            exit 2
            ;;
    esac
fi
exit 0
