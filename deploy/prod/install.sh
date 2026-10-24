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
#   7) 依赖同步：执行件落位后，把生产 venv（<检出>/.venv）同步到
#      requirements.lock，再跑 scripts/check-deps-lock.sh（一致性门，工单 j06f）。
#      同步前先留 pip freeze 快照；失败即非 0 退出并打印回滚命令。
#      DESTDIR 暂存或 venv 缺失时跳过本步，并各打一条说明。
#      本步的三个检出内触点（那枚门脚本、venv 的 pip 或解释器、requirements.lock）
#      与 M01 判据同批收全——见下方 M01 段。
#   8) 运行数据目录：root 且无 DESTDIR 时建立/修复 <检出>/data（属服务账号、0700）。
#      它是 `.env` 与 SQLite 库的落点，也是检出根能被 M01 门收成 root 只读的前提。
#      本步是**唯一建立点**（服务不得自建：检出根变只读后 mkdir 会失败）。
#      修复是**递归**的（`chown -Rh`）：README 的部署顺序先 `chown -R root:root .`，
#      会把 data/ 连同其下的 `.env` 与库一起带走；只修目录会让服务写库报
#      `attempt to write a readonly database` 而本安装器照旧报 complete（静默假成功）。
#      同一个门内把旧布局的运行数据（`.env` 与库本体连同 `-wal`/`-shm`）从检出根
#      **迁入** data/（升级既有安装；迁入件改属服务账号）。同名件两侧并存 ⇒ 拒装。
#   9) 服务在跑门（F2）：无 DESTDIR 时，装前探测服务是否在跑，在跑即拒装。
#      本门只管**裸机形态**：探测用 `systemctl is-active <单元>` 与 /proc 里 argv
#      指向本检出的活进程。容器形态**不走本安装器**（容器升级 = `git pull` ＋
#      `docker compose up -d --build`），其进程 argv 也不含宿主检出路径，故本门不宣称
#      覆盖它。拒装输出给出可复制的停服命令。带 DESTDIR 的暂存安装豁免本门。
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

# 对账·**只读收集与打印**（2026-10-09 修正，工单 yiban-auto-sign-ybg8）。
#
# **为什么必须在 M01 前置门之前**：M01 在检出属主不合时 `exit 1`。本段原先排在它之后，
# 于是"M01 拒装"的现场只看到归属报错，看不到"有几个执行件已与仓库不符"——检测器被门
# 自己挡在门外。生产实测即如此**静默漂移**：一份哨兵脚本缺失、一份旧版 wrapper 共存，
# 两周无人发现（哨兵缺 = 备份静默失败无防护 + 审计链离机留痕断；旧 wrapper =
# 口令经 export 进整棵子进程树）。
#
# 本段只读、零改动，提前**只改"先报事实"的顺序，不改任何拒装语义**：裁决留在 M01 之后
# 的原位置（`drift` 在这里只赋值、不退出）。两条判据各自完整，互不遮蔽。
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

# 依赖同步步（工单 j06f）要用的 venv 与解释器，口径只此一处。
# 必须定义在 M01 之前：M01 要 gate 本步将以 root 执行/读取的每个检出内对象，
# 而那些对象由下面这两个变量决定。
VENV="$REPO_ROOT/.venv"
VENV_PY=""
for cand in "$VENV/bin/python3" "$VENV/bin/python"; do
    if [ -x "$cand" ]; then VENV_PY="$cand"; break; fi
done

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
# 为什么不是全树 find -perm：生产检出下面还有 data/（.env / yiban.db / 服务写下的
# 运行数据）这些**本来就归服务账号**的东西，把它们纳入门会把一次正常升级变成拒装。
# 门只管"root 会拿它做决定"的那几条路径；运行数据收在检出内一个子目录（`data/`），
# 由下面那段单独建立并属服务账号（见「运行数据目录」段）。
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
            # 符号链接：**解引用后套同一判据**（目标属主 + 目标组/其他写位）。
            # 两条都不许走：
            #   · 只看链接自身的权限位 —— 链接的 mode 在 Linux 上恒为 777，判它只会恒红
            #     （venv 的 bin/python3 就是链接，那样每次干净部署都拒装）；
            #   · 只看链接自身的属主 —— 那是把改前"目标属主/权限也判"的检查丢掉（判据缩水）。
            # 断链或目标读不到 ⇒ fail-closed 拒装：读不到目标就不放行。
            if [ -L "$cur" ]; then
                owner="$(stat -Lc %u "$cur" 2>/dev/null)" || {
                    echo "yiban-install: 符号链接解引用失败（断链或目标不可读）：$cur" >&2
                    echo "                处置：修好或删掉该链接后重跑；读不到目标就不放行。" >&2
                    return 1; }
                mode="$(stat -Lc %a "$cur" 2>/dev/null)" || {
                    echo "yiban-install: 无法读取链接目标的权限位：$cur" >&2; return 1; }
            else
                owner="$(stat -c %u "$cur" 2>/dev/null)" || {
                    echo "yiban-install: 无法 stat 检出内路径：$cur" >&2; return 1; }
                mode="$(stat -c %a "$cur" 2>/dev/null)" || {
                    echo "yiban-install: 无法读取权限位：$cur" >&2; return 1; }
            fi
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
    # 依赖同步步（工单 j06f）另开三个 root 触点，判据必须同批收全：
    #   ① 它执行检出内的一枚脚本 scripts/check-deps-lock.sh；
    #   ② 它执行 venv 的 pip（或 venv 解释器，见下 venv_pip 的回退）；
    #   ③ 它把检出内的 requirements.lock 读给 pip 当输入。
    # 少收任一条 = 服务账号可写检出根（web/deploy/yiban-web.service 的
    # ReadWritePaths=/opt/yiban-auto-sign）⇒ 一次 sudo 安装即 root 执行可写文件。
    # 无 venv 时本步整段不跑（方式 B），故 ② 只在解释器存在时收。
    gate_path "$REPO_ROOT/scripts/check-deps-lock.sh" || exit 1
    gate_path "$REPO_ROOT/requirements.lock" || exit 1
    # venv 的入口按**存在**收件（不看 +x、也看断链）：可写就等于可被替掉，
    # 而"可执行位/可解析"只是这枚文件当下的属性。断链也收——解引用失败即拒装
    # （否则一枚坏链接会让本步静默降级成"跳过同步"，那正是本单要修的缺口）。
    # 存在即收与执行谓词（venv_pip 的 `-x`）**故意不同**：收件要宽，执行要真。
    for _venv_entry in "$VENV/bin/python3" "$VENV/bin/python" "$VENV/bin/pip"; do
        if [ -e "$_venv_entry" ] || [ -L "$_venv_entry" ]; then
            gate_path "$_venv_entry" || exit 1
        fi
    done
fi

# 预检 2：cron 路径来源断言（装前必过；装后同门复查一道，双保险）
bash "$REPO_ROOT/scripts/check-cron-provenance.sh"

# 对账·**拒装裁决**（漂移的收集与打印已提前到 M01 前置门之前；此处只裁决，见该段注释）
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

# ------------------------------------------------------------
# 预检 3：服务在跑就拒装（工单 F2）。
#
# 为什么必须拒：本安装器要搬动/改属 data/ 下的库与 `.env`。服务若仍在跑：
#   ① 它按旧路径另建空库（旧代码的默认值在检出根）⇒ 那份新数据随后被「两侧并存」
#      拒装挡在门外，而拒装提示让人删掉旧布局那一份 ⇒ 按提示做就丢掉刚写的新数据；
#   ② 它持着旧库的 fd ⇒ 搬迁后仍写旧 inode。
# README 只写"务必先停服"（散文约束），本段把它变成门。
#
# 本门只管**裸机形态**（systemd 单元）。容器形态不走本安装器——容器升级是
# `git pull && docker compose up -d --build`（见 README「Docker 部署」）。
# 容器内的服务进程 argv 不含宿主检出路径（`docker/supervisord.conf` 的 command 是
# `/usr/local/bin/gunicorn ... "web.app:create_app()"`，`directory=/app`），故进程扫描
# 在容器形态下判不出。本门不宣称覆盖它，也不为兑现声明去加更弱的探测。
#
# 探测两层（都针对裸机）：
#   ① systemd 权威层：`systemctl` 可用时问 `is-active`（生产单元 = SERVICE_UNIT）。
#   ② /proc 兜底层：argv 指向本检出的活进程（排除本安装器自身及其 sudo 父进程）。
#      systemd 缺席时这一层仍有效；cron 触发的 run.sh 也在这里被抓住。
# 没有"两层都探测不了"的静默路径：/proc 不可读时本脚本在更早那一步读 manifest
# （`mapfile < <(...)`，依赖 /dev/fd）就已非 0 退出，到不了这里。故不设空告警分支
# ——"看着有防御、其实永远跑不到"是 AGENTS §十三.30 同族。
#
# 豁免：带 DESTDIR 的暂存安装不碰现网运行数据（data/ 段与依赖同步段整段跳过），
# 故不受本门约束。豁免由用例钉住：tests/test_runtime_data_layout.py
# ::ServiceRunningGuardTest::test_staged_install_is_exempt
# ------------------------------------------------------------
SERVICE_UNIT="yiban-web.service"
if [ -z "$DESTDIR" ]; then
    _svc_why=""
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet "$SERVICE_UNIT" 2>/dev/null; then
            _svc_why="systemctl is-active $SERVICE_UNIT 报告 active"
        fi
    fi
    if [ -z "$_svc_why" ]; then
        # 探测对象天生短命：`/proc/<pid>` 可能在两步之间消失。**只开一次、拿 fd**，
        # 让"检查 → open"这一步的竞态在结构上不存在：open 失败即跳过，不再有**第二次
        # 触达** ⇒ 不会再有重定向失败把 `set -e` 触发成安装中断（伪失败 rc=1）。
        # ⚠ **不许把这条读成"fd 拿到之后读就不受影响"**：进程退出后读该 fd 仍会报
        # `read error: No such process`（实测，回收态与僵尸态皆然）。那一族错误由下面
        # read 上的 `2>/dev/null` 单独抑制——**删掉它噪声立刻回来**。本节的收益是
        # "少了第二次触达"，不是"读不再出错"；两件事不要混。
        # 反面（本批修掉的形状）：先 `[ -r "$_pid/cmdline" ]` 再 `done < "$_pid/cmdline"`，
        # 检查与 open 之间进程退出 ⇒ bash 打一行
        # `/proc/<pid>/cmdline: No such file or directory`；本脚本带 `set -e`
        # （见第 46 行），该重定向失败还会**中断安装**（伪失败，rc=1）。
        # 打开失败只说明该进程已消失，属正常情形 ⇒ 静默跳过。
        # 抑制写法的两个坑（都实测过）：
        #   ① `done < file 2>/dev/null` 无效——`<` 先开、报错先于 `2>/dev/null` 生效；
        #   ② `exec {_fd}< file 2>/dev/null` **不能用**——`exec` 无命令时重定向作用于
        #      当前 shell 且**持久**，`2>/dev/null` 会把本脚本此后所有 stderr 永久丢进
        #      /dev/null（实测：后面的告警/拒装理由全消失）。故用**受限的组重定向**
        #      `{ exec {_fd}< file; } 2>/dev/null`：报错被吞，组外的 stderr 照旧。
        for _pid in /proc/[0-9]*; do
            if ! { exec {_fd}< "$_pid/cmdline"; } 2>/dev/null; then
                continue
            fi
            _self=0
            _hit=0
            # 读期间进程仍可能退出 ⇒ `read` 报 `read error: No such process`。
            # 同样是"该进程已消失"，不是可处置的错误，故同样静默；read 是普通命令，
            # 其重定向只作用于本条命令、不持久（不会碰组外的 stderr）。read 失败只是
            # while 条件为假，不触发 `set -e`（不会中断安装）。
            while IFS= read -r -d '' _arg <&"$_fd" 2>/dev/null; do
                case "$_arg" in
                    *install.sh*) _self=1; break ;;
                esac
                # 匹配必须带**路径边界**：整串等于检出根，或检出根后紧跟 `/`。
                # 纯子串匹配会把兄弟目录（`<检出>-backup`）判进来，那时提示语断言的
                # "argv 指向本检出"就是假的（方向虽 fail-closed，事实仍不成立）。
                case "$_arg" in
                    "$REPO_ROOT"|*"$REPO_ROOT"/*) _hit=1 ;;
                esac
            done
            exec {_fd}<&-
            if [ "$_self" -eq 0 ] && [ "$_hit" -eq 1 ]; then
                _svc_why="进程 ${_pid#/proc/} 的 argv 指向本检出"
                break
            fi
        done
    fi
    if [ -n "$_svc_why" ]; then
        echo "yiban-install: 检测到服务仍在运行（$_svc_why）——拒绝安装。" >&2
        echo "  原因：本安装器要搬动/改属 data/ 下的库与 .env；服务在跑会按旧路径另建空库，" >&2
        echo "        随后重跑会因「两侧并存」拒装，按提示删旧件就丢掉服务刚写的新数据。" >&2
        echo "  处置（先停服，再重跑本安装器）：" >&2
        echo "    sudo systemctl stop yiban-web" >&2
        echo "  本门只管裸机（systemd 单元）。容器形态不走本安装器——" >&2
        echo "    容器升级 = git pull && docker compose up -d --build（见 README「Docker 部署」）。" >&2
        exit 1
    fi
fi

# ------------------------------------------------------------
# 运行数据目录（检出内 `data/`）：**唯一建立点**（工单 ybg8 / ddok）。
#
# 为什么有本步：M01 前置门要求检出根属 root 且组/其他不可写，而服务要往检出里写
# `.env` 与 SQLite 库。`.env` 的原子写把临时文件建在 `.env` **同目录**
# （yiban/adapters/config/env_io.py 的 _atomic_replace_env），目录不给服务账号写就写不进。
# 两条要求在旧布局下互斥，生产因此只能手工拷贝执行件（哨兵脚本漂移、两条 cron
# 从未装上）。修法：把运行数据收进检出内的 `data/`，属服务账号、模式 0700。
#
# 口径六条：
#   1) 唯一建立点 = 本步。服务**不得**自建：检出根变 root 只读后 mkdir 会失败。
#   2) 本步同时是**幂等修复点**：`chown -R root:root` 会把 data/ 一起带走，
#      重跑本安装器即改回服务账号（README 的部署顺序因此是"先 chown、再 install"）。
#   3) 只在本机会真落位时做（root 且无 DESTDIR）：暂存安装不该在源检出里造目录。
#   4) 服务账号＝yiban:yiban（与 `web/deploy/yiban-web.service` 的 User=/Group= 同值）。
#      两条不许分叉：由 tests/test_runtime_data_layout.py::RuntimeDataCarriersTest
#      ::test_installer_service_account_matches_unit 钉住（先例同
#      deploy/prod/tmpfiles.d/yiban.conf：字面量 + 用例钉住与单元文件相等）。
#      换登录名时这几处一起改（README「换目录部署」同口径）。该账号在本机不存在时
#      install 会非 0 退出——那是配置错，不该静默跳过；不改名则不需要任何配置键。
#   5) 本步还**接管旧布局的运行数据**（`LEGACY_RUNTIME_FILES`，升级既有安装用）。
#      默认值改到 data/ 之后，既有安装的 `.env` 与库仍留在检出根；服务按新默认值
#      去 data/ 找，找不到就自建空库、读不到密钥与管理员口令（页面显示零账号）。
#      故本步把旧件**迁入** data/（不是复制：旧位置不得留下第二份权威）。
#      迁入件随后 chown 给服务账号——服务要接着写这两件（含 SQLite 的 -wal/-shm）。
#   6) 迁移只做"目标不存在"的情形。同名件同时在检出根与 data/ 时**权威未知**，
#      本步 fail-closed 拒装并打印两侧路径，绝不猜、绝不覆盖（先例同 manifest 的
#      校验和漂移拒装）。拒装前**先全量判冲突、再搬**：一趟内边走边搬会在中途拒装时
#      留下"半搬"状态（`.env` 已进 data/、库还在检出根），那正是本仓主导缺陷形状
#      "同批只改了一半"。幂等：迁完之后重跑没有任何可迁的件。
# ------------------------------------------------------------
SERVICE_USER="yiban"
SERVICE_GROUP="yiban"
DATA_DIR="$REPO_ROOT/data"
# 旧布局的运行数据。集合与搬迁提交（a5bc16b7）改动的默认值同源：只有 YIBAN_ENV_FILE
# 与 YIBAN_DB_FILE 两枚默认值从检出根改到 data/，故旧件就是 `.env` 与库本体连同
# SQLite 的同目录 `-wal`/`-shm`（本项目统一 WAL 模式，见 yiban/adapters/persistence/db.py）。
# `accounts.json` 的默认值未动（仍是检出根），不属本集合。
LEGACY_RUNTIME_FILES=(.env yiban.db yiban.db-wal yiban.db-shm)
if [ "$(id -u)" -eq 0 ] && [ -z "$DESTDIR" ]; then
    if ! install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$DATA_DIR"; then
        echo "yiban-install: 无法建立运行数据目录 $DATA_DIR（属主 $SERVICE_USER:$SERVICE_GROUP）。" >&2
        echo "  处置：确认该服务账号在本机存在（与 web/deploy/yiban-web.service 的 User=/Group= 同值）。" >&2
        exit 1
    fi
    # 模式可能被上一轮的 chmod 放宽（例如部署者手工 chmod -R 755），显式收紧。
    chmod 0700 "$DATA_DIR"
    echo "runtime data dir ready: $DATA_DIR owner=$SERVICE_USER:$SERVICE_GROUP mode=0700"
    # 迁移分两趟：**先全量判冲突，再搬**。一趟内边走边判会在中途拒装时留下"半搬"
    # 状态（.env 已进 data/、库还在检出根）——那正是本仓主导缺陷形状"同批只改了一半"。
    for _name in "${LEGACY_RUNTIME_FILES[@]}"; do
        _legacy="$REPO_ROOT/$_name"
        _target="$DATA_DIR/$_name"
        # 存在即收（断链也收）：一枚坏链接同样是"旧位置留着东西"，要报不要跳。
        if [ ! -e "$_legacy" ] && [ ! -L "$_legacy" ]; then continue; fi
        if [ -e "$_target" ] || [ -L "$_target" ]; then
            echo "yiban-install: 旧布局运行数据与 data/ 下同名件并存，权威未知，拒绝继续：" >&2
            echo "  旧布局: $_legacy" >&2
            echo "  新布局: $_target" >&2
            echo "  处置：人工 diff 两份，删掉旧布局那一份（保留 data/ 下的），再重跑本安装器。" >&2
            exit 1
        fi
    done
    # 幂等修复必须覆盖 data/ 的**内容**，不只是目录本身（工单 F1）。
    # README 的部署/升级顺序先 `chown -R root:root .`，会把 data/ 连同其下的 `.env` 与库
    # 一起带走（文件仍 root:root 644）。只修目录 ⇒ 服务写库报
    # `attempt to write a readonly database`，而本安装器照旧 rc=0 报 complete
    # （静默假成功）；README 那句"重跑一次安装器即修复"对**文件内容**为假。
    # 容器形态的同一件事由 docker/entrypoint.sh 的 `chown -R yiban:yiban /data` 承担
    # ——两个形态同形，裸机侧也必须递归到子树（data/ 下还会有 data/state 等子目录）。
    # `-R -h`：递归但**不解引用**——data/ 下的符号链接（把库指到别的盘是合法部署形态）
    # 只改链接自身，不跟随到外部目标（与下方迁移段的 `chown -h` 同口径）。
    # 判据排在冲突检查**之后**：冲突拒装时本步不改任何**内容件**。
    # 注意措辞边界：`data/` **目录本身**在冲突判定之前就可能已被建/修正
    # （`install -d -o` 与 `chmod 0700` 排在那之前，属既有行为）；内容件那条
    # "先全量判冲突、再搬"不变，故拒装不会留下半搬状态。
    chown -Rh "$SERVICE_USER:$SERVICE_GROUP" "$DATA_DIR"
    echo "runtime data ownership repaired: $DATA_DIR (recursive)"
    for _name in "${LEGACY_RUNTIME_FILES[@]}"; do
        _legacy="$REPO_ROOT/$_name"
        _target="$DATA_DIR/$_name"
        if [ ! -e "$_legacy" ] && [ ! -L "$_legacy" ]; then continue; fi
        # 同文件系统内 mv = rename：不复制、不留旧件（旧位置的第二份权威必须消失）。
        mv -- "$_legacy" "$_target"
        # `chown -h`：迁入件若是符号链接（把数据指到别的盘是合法部署形态），只改链接
        # 自身，不跟随到目标——跟随会把链接指向的那个外部文件改属主。
        chown -h "$SERVICE_USER:$SERVICE_GROUP" "$_target"
        echo "adopted legacy runtime data: $_name -> data/$_name"
    done
else
    echo "yiban-install: note: 非 root 或 DESTDIR 暂存安装——跳过运行数据目录 $DATA_DIR 的建立" >&2
fi

# ------------------------------------------------------------
# cron 日志目录（/var/log/yiban）：**唯一建立点**（R3-009）
#
# 为什么必须有本步：六张 cron 表都 `>> /var/log/yiban/<x>.log`。目录不存在时 `sh`
# 在**执行命令之前**就因重定向失败退出——签到/备份/审计一行都不跑，cron 只往 mail
# 里丢一行。旧流程靠 README 让人手工 `mkdir -p`，与「干净环境一次装成、零手动调整」
# 的发布判据冲突。
# 属主/模式：与 `<检出>/data` 同一条判据——属服务账号、0700。`run.sh` / `run_probe.sh`
# / 兜底执行体对状态目录做 M07 硬检查（属本用户且收紧成功），属主不对即拒绝运行。
# 备份 cron 以 root 运行；root 有 CAP_DAC_OVERRIDE，仍能写 0700 的本目录。
# 幂等修复：已存在的目录同样重设属主与模式（README 的 `chown -R root:root .` 不影响
# 检出外的本目录，但旧部署可能被手工 chmod 放宽）。
# DESTDIR 暂存只在前缀内建目录、不改属主（与 data/ 段同一分工）。
# ------------------------------------------------------------
LOG_DIR_REAL="/var/log/yiban"
if [ "$(id -u)" -eq 0 ] && [ -z "$DESTDIR" ]; then
    if ! install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$LOG_DIR_REAL"; then
        echo "yiban-install: 无法建立 cron 日志目录 $LOG_DIR_REAL（属主 $SERVICE_USER:$SERVICE_GROUP）。" >&2
        echo "  处置：确认服务账号在本机存在。该目录是六张 cron 表的重定向目标：" >&2
        echo "        缺它时 sh 在执行命令前就退出，签到/备份/审计全部静默不跑。" >&2
        exit 1
    fi
    chmod 0700 "$LOG_DIR_REAL"
    echo "cron log dir ready: $LOG_DIR_REAL owner=$SERVICE_USER:$SERVICE_GROUP mode=0700"
elif [ -n "$DESTDIR" ]; then
    if ! install -d -m 0700 "$DESTDIR$LOG_DIR_REAL"; then
        echo "yiban-install: 无法在暂存前缀内建立 cron 日志目录 $DESTDIR$LOG_DIR_REAL。" >&2
        exit 1
    fi
    echo "cron log dir staged: $DESTDIR$LOG_DIR_REAL（DESTDIR 暂存不改属主）"
else
    echo "yiban-install: note: 非 root 且无 DESTDIR——跳过 $LOG_DIR_REAL 的建立" >&2
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

# ------------------------------------------------------------
# 依赖同步（工单 j06f）：把生产 venv 同步到 requirements.lock。
#
# 为什么有本步：本安装器此前只落位执行件与 cron，**不同步依赖**。
# venv 建好之后就不再更新，lock 后续的安全修复上不了生产。
# 2026-10-12 实测：生产 venv 落后 lock 两个含 CVE 修复的包
# （urllib3 2.7.0 对 lock 2.8.0；Werkzeug 3.1.8 对 lock 3.1.9）。
#
# 四条口径：
#   1) 装进**同一个** venv：<检出>/.venv。它与 run.sh、systemd 单元同源。
#      本步不新建环境，也不引新配置键。
#   2) 先留回滚点：pip freeze 写 <检出>/deps-snapshot-<时间戳>.txt。
#      回滚 = 本脚本**当场打印的那条命令**（下面 $ROLLBACK）：venv 有 bin/pip 时是
#      `<venv>/bin/pip install -r <快照>`；缺 bin/pip 时是
#      `<venv>/bin/python3 -m pip install -r <快照>`。不要照抄某一支的写法。
#      回滚命令沿用宿主 pip 配置取包（README 的部署步骤先设了清华镜像）。
#   3) 同步后立刻跑 scripts/check-deps-lock.sh（venv 与 lock 一致性门）。
#      门红即非 0 退出："装过"不等于"装对了"。
#   4) DESTDIR 暂存安装不碰现网 venv，故跳过本步。
#      找不到 venv 时只告警：方式 B（apt 装 gunicorn、无 .venv）是受支持形态，
#      它的依赖不由 lock 管辖。
# ------------------------------------------------------------
if [ -n "$DESTDIR" ]; then
    echo "yiban-install: note: DESTDIR 暂存安装——跳过依赖同步（不碰现网 venv）" >&2
else
    if [ -z "$VENV_PY" ]; then
        echo "yiban-install: 警告: 找不到 venv 解释器（$VENV/bin/python3）——跳过依赖同步。" >&2
        echo "  若本项目该用 venv：先按 README「服务器部署」建 .venv，再重跑本安装器。" >&2
        echo "  若生产用系统 gunicorn（方式 B）：依赖不由 requirements.lock 管辖，可忽略本条。" >&2
    else
        # pip 入口只判一次：执行与"打印给运维的回滚命令"共用同一条判据。
        # 旧写法把回滚命令硬编码成 <venv>/bin/pip，与下面这条回退自相矛盾：
        # venv 缺 bin/pip 时同步能跑成，打印出来的回滚命令却跑不了（实测 rc=127）。
        if [ -x "$VENV/bin/pip" ]; then
            PIP_CMD=("$VENV/bin/pip")
        else
            PIP_CMD=("$VENV_PY" -m pip)
        fi
        venv_pip() { "${PIP_CMD[@]}" "$@"; }
        SNAPSHOT="$REPO_ROOT/deps-snapshot-$TS.txt"
        # 回滚命令由同一个数组派生：它打印什么，运维就能照样跑什么。
        ROLLBACK="${PIP_CMD[*]} install -r $SNAPSHOT"
        if ! venv_pip freeze > "$SNAPSHOT"; then
            echo "yiban-install: 依赖同步中止——留不下回滚快照（$SNAPSHOT）。venv 未改。" >&2
            exit 1
        fi
        echo "deps snapshot: $SNAPSHOT（回滚：$ROLLBACK）"
        if ! venv_pip install --no-input --disable-pip-version-check \
                -r "$REPO_ROOT/requirements.lock" ${PIP_INDEX_URL:+-i "$PIP_INDEX_URL"}; then
            echo "yiban-install: 依赖同步失败（pip 非 0）。venv 可能停在中间状态。" >&2
            echo "  回滚：$ROLLBACK" >&2
            exit 1
        fi
        echo "deps synced: $REPO_ROOT/requirements.lock"
        if ! bash "$REPO_ROOT/scripts/check-deps-lock.sh" --venv "$VENV"; then
            echo "yiban-install: venv 与 requirements.lock 仍不一致（见上）。" >&2
            echo "  回滚：$ROLLBACK" >&2
            exit 1
        fi
    fi
fi

bash "$REPO_ROOT/scripts/check-cron-provenance.sh" >/dev/null
echo "yiban-install: complete —— ${#ROWS[@]} 个生产执行件与仓库对账完毕"
