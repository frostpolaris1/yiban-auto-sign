#!/usr/bin/env bash
# 主仓 .git/config 卫生检查（工单 c8j）。
#
# 形状：某个代理在 WSL 侧对主仓跑 git 时，把 core.worktree=/mnt/d/... 写进了
# 共享的 .git/config ⇒ Windows 侧主树全部 git 命令报 `Invalid path '/mnt'`，
# 而且无声（另一侧照样能干活）。主工作树的 .git/config 不应出现 core.worktree；
# 链接 worktree 的同名键在各自 gitdir（.git/worktrees/<n>/config）里，语义不同，不查。
#
# **两种仓形态都要查到主仓配置**（P5-D 评审 H1）：
#   · 主工作树：`.git` 是目录 → 查 `.git/config`；
#   · 链接 worktree：`.git` 是文件（`gitdir: <主仓>/.git/worktrees/<名>`）→
#     解析出主仓 gitdir，再查 `<主仓>/.git/config`。日常开发都在 worktree 里，
#     只查前者等于门口没门。
#
# 用法：check-git-config.sh [repo]（默认本脚本所在仓根）。违规退出 1 并给修复命令；
# 配置不可读退 2（响亮失败，不 fail-open）；合规/无主仓配置可查时退出 0。只读。
set -euo pipefail

repo="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
gitpath="$repo/.git"
cfg=""

# 解析要检查的配置文件路径。
if [ -d "$gitpath" ]; then
    cfg="$gitpath/config"
elif [ -f "$gitpath" ]; then
    line=$(sed -n 's/^gitdir:[[:space:]]*//p' "$gitpath" | head -n 1)
    if [ -z "$line" ]; then
        echo "check-git-config: $gitpath 无法解析（缺 gitdir 行）——门无法判定，拒绝放行" >&2
        exit 2
    fi
    gd=${line//\\//}
    case "$gd" in
        /* | [A-Za-z]:*) ;;
        *)
            if ! gd=$(cd "$repo" && cd "$gd" 2>/dev/null && pwd); then
                echo "check-git-config: $gitpath 的相对 gitdir 无法解析——门无法判定，拒绝放行" >&2
                exit 2
            fi
            ;;
    esac
    # 只认 worktree gitdir 形态；不是则该文件指向别处（裸库等），无可查之物。
    case "$gd" in
        */.git/worktrees/*)
            main_gitdir=${gd%%/.git/worktrees/*}/.git
            cfg="$main_gitdir/config"
            ;;
        *) exit 0 ;;
    esac
fi

[ -n "$cfg" ] || exit 0
[ -f "$cfg" ] || exit 0

# 直接解析 config 文本，不调 git：调用方环境的 GIT_* 变量会让 `git config -f`
# 先炸 "not a git repository"（测试与代理 shell 都实际踩到）。字段名/段名按 git
# 语义大小写不敏感（评审 M1：`[Core]`/`Worktree` 同样是该键）。值为单行；
# 主仓本就不该有该键，任何非空值都按违规处理。
rc=0
value=$(awk '
    /^[[:space:]]*\[/ {
        # 段名取第一个 [ ] 之间；其后的行内注释（; 或 #）忽略（评审 M1）。
        line = $0
        sub(/^[[:space:]]*\[/, "", line)
        sub(/\].*$/, "", line)
        sec = tolower(line)
        next
    }
    sec == "core" && tolower($0) ~ /^[[:space:]]*worktree[[:space:]]*=/ {
        sub(/^[^=]*=[[:space:]]*/, "")
        print
        exit
    }
' "$cfg") || rc=$?
if [ "$rc" -ne 0 ]; then
    echo "check-git-config: 无法读取 $cfg（awk rc=$rc）——门无法判定，拒绝放行" >&2
    exit 2
fi
if [ -n "$value" ]; then
    echo "check-git-config: 主仓配置含 core.worktree=$value（$cfg）" >&2
    echo "  主工作树不应设置 core.worktree（c8j：WSL 侧写入会挂掉 Windows 侧 git）。" >&2
    echo "  修复：git -C \"$repo\" config --local --unset core.worktree" >&2
    exit 1
fi
exit 0
