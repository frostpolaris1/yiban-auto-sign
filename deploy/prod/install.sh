#!/usr/bin/env bash
# ============================================================
# deploy/prod/install.sh —— 生产执行件安装器（"审仓库 = 审生产"的落位半边，MF-42）
#
# 用法：
#   sudo bash deploy/prod/install.sh                  # 真机安装（root，装到真实路径）
#   DESTDIR=/tmp/prefix bash deploy/prod/install.sh   # 暂存安装（测试/打包，标准
#                                                      Makefile DESTDIR 约定：目标绝对
#                                                      路径整体加前缀，生产默认不受影响）
#                                                      配合**非 root** 才是"无特权安装"；
#                                                      root + DESTDIR 仍要过下面的 M01 门
#   bash deploy/prod/install.sh --adopt-production    # 校验和不符时的裁决开关（见下）
#
# 行为契约（tests/test_deploy_prod_artifacts.py 逐条钉死，活体反例可红）：
#   1) 逐行读 manifest.tsv（mode<TAB>仓库源<TAB>生产绝对路径）；安装前跑
#      scripts/check-cron-provenance.sh，cron 来源断言不过 ⇒ 拒装。
#   2) sha256 对账：目标已存在且与仓库源校验和不符 ⇒ 拒装并打印双侧校验和——
#      现网手工漂移（yiban-backup.sh 差 3 行那类）不得被静默覆盖；漂移内容先人工
#      diff 回填仓库。确要"以仓库为准"时加 --adopt-production：现网件先归档为
#      <目标>.reconcile-<时间戳> 再覆写（归档件是回填 scripts/backup.sh 的原料）。
#   3) 旧件残留清理：安装时删除 <目标>.bak-*（现象：yiban-backup.sh.bak-20260923）
#      并留痕。reconcile 归档用不同模式命名，不在清理范围。
#   4) 幂等：内容一致的目标跳过覆写；第二轮不产生任何归档。
#   5) 每个目标安装后输出 sha256 与 mode（"安装后校验和输出"，供人核对/巡检比对）。
#   6) 属主设置门：非 root 或带 DESTDIR 时跳过 root:root 属主设置（会明确提示，不假装成功）。
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$REPO_ROOT/deploy/prod/manifest.tsv"
DESTDIR="${DESTDIR:-}"
ADOPT=0
for arg in "$@"; do
    case "$arg" in
        --adopt-production) ADOPT=1 ;;
        *) echo "yiban-install: 未知参数: $arg" >&2; exit 2 ;;
    esac
done

sha() { sha256sum "$1" | cut -d' ' -f1; }

[ -f "$MANIFEST" ] || { echo "yiban-install: 清单缺失: $MANIFEST" >&2; exit 1; }
mapfile -t ROWS < <(grep -vE '^[[:space:]]*(#|$)' "$MANIFEST")
[ "${#ROWS[@]}" -gt 0 ] || { echo "yiban-install: 清单为空" >&2; exit 1; }

# 预检 1：manifest 的仓库源必须齐备（原件入库的门）
missing=0
for row in "${ROWS[@]}"; do
    IFS=$'\t' read -r _mode src _dest <<< "$row"
    if [ ! -f "$REPO_ROOT/$src" ]; then
        echo "yiban-install: manifest 源缺失: $src" >&2
        missing=1
    fi
done
[ "$missing" -eq 0 ] || exit 1

# 前置门（M01，fail-closed）：以 root 安装时，本脚本会 root 执行检出内的
# scripts/check-cron-provenance.sh 并把检出件 root:root 落到生产路径。若检出本身对
# 服务账号可写（典型：/opt/yiban-auto-sign 被 ReadWritePaths 放开给 yiban 写 .env/
# yiban.db，见 web/deploy/yiban-web.service），服务账号被拿下后等一次 sudo 安装即提权
# root。故以 root 安装时检出必须属 root 且组/其他不可写。非 root 不受本门约束。
#
# M01 复审补全（2026-10-01）：原实现只 stat 检出**顶层**一个目录，挡不住
# 「顶层 root:755、子目录/文件对服务账号可写」——那正是提权路径本身（scripts/ 可写
# ⇒ 改掉将被 root 执行的 check-cron-provenance.sh）。现改为**逐级**校验 root 真正
# 会读取或执行的那几条路径（清单本身、cron 断言脚本、manifest 里的每个源件），
# 沿途每一个目录分量都要 root 属主且非组/其他可写。
# 为什么不是全树 find -perm：生产检出下面还有 .env / yiban.db / 日志这些**本来就归
# 服务账号**的运行数据，把它们纳入门会把一次正常升级变成拒装。门只管"root 会拿它做
# 决定"的那几条路径。
#
# ba-p12-02（2026-10-06）修正门的作用域：判据只看 uid，去掉原判据多带的 `-z "$DESTDIR"`。
# 本门防的是"root 执行检出内的文件"。这与把文件装到哪里无关。
# DESTDIR 只改写入目标，不改装前与装后那两次
# `bash "$REPO_ROOT/scripts/check-cron-provenance.sh"` 读取和执行的来源。
# 旧判据下 root + DESTDIR（本文件头注释列出的暂存用法）整段跳过本门。
# 于是门管不住它自称要防的那一步：执行点在门外，无条件跑。
#
# 下面的属主设置门是**另一件事**：它的不变量在写入侧，故它继续看 DESTDIR。
# 两条判据不同，不是实现不一致。别把它们"统一"回去。
if [ "$(id -u)" -eq 0 ]; then
    # 逐级 stat 一个检出内路径；不在检出内 / 读不到 stat ⇒ 同样拒（fail-closed）
    gate_path() {
        local target="$1" cur rel owner mode
        case "$target" in
            "$REPO_ROOT"/*) rel="${target#"$REPO_ROOT"/}" ;;
            *) echo "yiban-install: 待校验路径不在检出内（请查清单）：$target" >&2; return 1 ;;
        esac
        cur="$REPO_ROOT"
        local -a comps=()
        IFS='/' read -r -a comps <<< "$rel"
        for comp in "${comps[@]}"; do
            [ -n "$comp" ] || continue
            cur="$cur/$comp"
            owner="$(stat -c %u "$cur" 2>/dev/null)" || {
                echo "yiban-install: 无法 stat 检出内路径：$cur" >&2; return 1; }
            mode="$(stat -c %a "$cur" 2>/dev/null)" || {
                echo "yiban-install: 无法读取权限位：$cur" >&2; return 1; }
            if [ "$owner" -ne 0 ] || [ "$(( 8#$mode & 8#022 ))" -ne 0 ]; then
                echo "yiban-install: 以 root 安装时检出内每个被 root 读取/执行的路径都必须属 root 且非组/其他可写" >&2
                echo "                违规路径：$cur（owner=$owner mode=$mode）" >&2
                echo "                处置：把该路径连同各级父目录 chown -R root:root，并去掉组/其他写位。" >&2
                echo "                注意：DESTDIR 不豁免本门——带 DESTDIR 时 root 照旧执行检出内的那枚脚本。" >&2
                return 1
            fi
        done
        return 0
    }

    repo_owner="$(stat -c %u "$REPO_ROOT")"
    repo_mode="$(stat -c %a "$REPO_ROOT")"
    if [ "$repo_owner" -ne 0 ] || [ "$(( 8#$repo_mode & 8#022 ))" -ne 0 ]; then
        echo "yiban-install: 以 root 安装时检出必须属 root 且非组/其他可写；请把检出移到 root 只读路径，再 chown -R root:root 并去掉组/其他写位（DESTDIR 不豁免本门；当前 owner=$repo_owner mode=$repo_mode: $REPO_ROOT）" >&2
        exit 1
    fi
    for row in "${ROWS[@]}"; do
        IFS=$'\t' read -r _mode src _dest <<< "$row"
        gate_path "$REPO_ROOT/$src" || exit 1
    done
    gate_path "$MANIFEST" || exit 1
    gate_path "$REPO_ROOT/scripts/check-cron-provenance.sh" || exit 1
fi

# 预检 2：cron 路径来源断言（装前必过；装后同门复查一道，双保险）
bash "$REPO_ROOT/scripts/check-cron-provenance.sh"

# 对账（只读阶段）：收集全部漂移；非 --adopt-production 一律拒装、零改动
drift=0
for row in "${ROWS[@]}"; do
    IFS=$'\t' read -r _mode src dest <<< "$row"
    destpath="$DESTDIR$dest"
    if [ -f "$destpath" ] && [ "$(sha "$destpath")" != "$(sha "$REPO_ROOT/$src")" ]; then
        drift=1
        echo "checksum mismatch: $dest" >&2
        echo "  expected(repo):    $(sha "$REPO_ROOT/$src")" >&2
        echo "  actual(present):   $(sha "$destpath")" >&2
    fi
done
if [ "$drift" -eq 1 ] && [ "$ADOPT" -ne 1 ]; then
    echo "yiban-install: refusing install —— 现网与仓库校验和不符（漂移不得静默覆盖）。" >&2
    echo "  先 diff 两侧把差异回填仓库；确认以仓库为准时加 --adopt-production" >&2
    echo "  （会把现网件归档为 <目标>.reconcile-<ts> 后覆写）。未做任何改动。" >&2
    exit 1
fi

OWNERSHIP=()
# 第二道门：不变量在**写入侧**——是否把产物设成 root:root 属主。
# DESTDIR 只把件装进暂存前缀，暂存件不该改属主，故这里保留 `-z "$DESTDIR"`。
# M01 前置门不看 DESTDIR。它管的是另一条不变量：root 会不会执行检出内文件。
# 两个条件不同 = 两条不变量不同；改成同一个判据即回到 ba-p12-02 的缺陷。
# 钉住这道差异的用例：tests/test_deploy_prod_artifacts.py
# ::InstallRootCheckoutSubPathGateTest::test_root_staging_install_does_not_force_root_ownership
if [ "$(id -u)" -eq 0 ] && [ -z "$DESTDIR" ]; then
    OWNERSHIP=(-o root -g root)
else
    echo "yiban-install: note: 非 root 或 DESTDIR 暂存安装——跳过 root:root 属主设置" >&2
fi

TS=$(date +%Y%m%d%H%M%S)

for row in "${ROWS[@]}"; do
    IFS=$'\t' read -r mode src dest <<< "$row"
    srcpath="$REPO_ROOT/$src"
    destpath="$DESTDIR$dest"
    dd=$(dirname "$destpath")
    mkdir -p "$dd"
    # 旧件残留清理（现象点名 .bak-20260923 类）；只动 <目标>.bak-*，reconcile 归档不碰
    shopt -s nullglob
    for junk in "$destpath".bak-*; do
        rm -f -- "$junk"
        echo "cleaned stale residue: $junk"
    done
    shopt -u nullglob
    if [ -f "$destpath" ] && [ "$(sha "$destpath")" = "$(sha "$srcpath")" ]; then
        echo "unchanged: $dest mode=$mode sha256=$(sha "$destpath")"
        continue
    fi
    if [ -f "$destpath" ] && [ "$ADOPT" -eq 1 ]; then
        mv -- "$destpath" "$destpath.reconcile-$TS"
        echo "archived drifted production file: ${dest}.reconcile-$TS —— 请 diff 它并回填 $src"
    fi
    install -m "$mode" ${OWNERSHIP[@]+"${OWNERSHIP[@]}"} "$srcpath" "$destpath"
    echo "installed: $dest mode=$mode sha256=$(sha "$destpath")"
done

bash "$REPO_ROOT/scripts/check-cron-provenance.sh" >/dev/null
echo "yiban-install: complete —— ${#ROWS[@]} 个生产执行件与仓库对账完毕"
