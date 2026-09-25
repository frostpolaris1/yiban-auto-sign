#!/usr/bin/env bash
# ============================================================
# deploy/prod/install.sh —— 生产执行件安装器（"审仓库 = 审生产"的落位半边，MF-42）
#
# 用法：
#   sudo bash deploy/prod/install.sh                  # 真机安装（root，装到真实路径）
#   DESTDIR=/tmp/prefix bash deploy/prod/install.sh   # 无特权安装（测试/暂存，标准
#                                                      Makefile DESTDIR 约定：目标绝对
#                                                      路径整体加前缀，生产默认不受影响）
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
#   6) 非 root 或 DESTDIR 安装跳过 root:root 属主设置（会明确提示，不假装成功）。
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
