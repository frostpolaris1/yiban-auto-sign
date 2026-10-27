#!/usr/bin/env bash
# ============================================================
# release-check.sh —— 发布门清单（P8-D 切片）
# ============================================================
# 用途：把发布前要过的检查收成一条命令。八项清单与考古结论见
#   deploy/release.md（权威版）；本脚本只负责可执行的部分。
#
# 用法（在仓库根目录或任意位置执行）：
#   bash scripts/release-check.sh                # 跑全部八项
#   bash scripts/release-check.sh --only version # 只跑一项
#   bash scripts/release-check.sh --list         # 列出项名
#   bash scripts/release-check.sh --selftest     # 跑自带突变自测
#
# 状态口径（AGENTS §30：跳过 ≠ 绿）：
#   PASS    检查运行且通过
#   FAIL    检查运行且不通过（汇总退出码 1）
#   SKIP    门没有运行；打印原因（如 license.sh 首跑需联网）
#   POINTER 本项是对既有门/演练脚本的调用引用，本脚本只验指向存在，
#           不代跑（全量与演练的固定开销归发布操作者付）
#   MANUAL  无既有门，登记为人工步骤；不打 PASS
# 退出码：0 = 无 FAIL；1 = 有 FAIL；2 = 用法错误（--only 不存在的项名）。
#
# 可调环境变量：
#   RELEASE_CHECK_ROOT  被检仓库根。默认脚本位置的上两级。
#                       自测用它指向临时夹具，不碰真文件。
#
# 突变自测（--selftest）钉两条不变式：
#   1. 某项被破坏时 --only 该项退出非零（临时坏夹具触发，不改真文件）；
#   2. --only 不存在的项名报错退出非零。
# ============================================================
set -u

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
ROOT="${RELEASE_CHECK_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

ITEMS="version license generate tests container backup rollback signing"

usage() {
    sed -n '2,30p' "$SELF" | sed 's/^# \{0,1\}//'
}

# ---------- 各项检查 ----------
# 约定：每个 check_* 把结果写进全局 STATUS_<item>（PASS/FAIL/SKIP/POINTER/MANUAL）
# 与 REASON_<item>，明细直接打到 stdout。

# 项 1 版本一致性。考古（2026-10-27，ref ad5b3639）：
#   版本唯一来源 = yiban/__init__.py 的 __version__（tests/test_release_version_source.py 守卫）。
#   二进制 --version 不存在（server/cmd/server/main.go 不读参数，server/cmd/cli 无 version 子命令）。
#   openapi info.version = "1.0.0"，是 API 契约版本，不随发布走。两条缺口在 deploy/release.md 登记。
#   本项钉存在的两处：发布 tag v$V 存在且指向 HEAD；__version__ 与 CHANGELOG 顶部一致。
check_version() {
    local init="$ROOT/yiban/__init__.py" changelog="$ROOT/CHANGELOG.md"
    STATUS_version=FAIL; REASON_version=""
    [ -f "$init" ] || { REASON_version="missing $init"; return; }
    [ -f "$changelog" ] || { REASON_version="missing $changelog"; return; }
    local v
    v="$(grep -E '^__version__ = "[0-9]+\.[0-9]+\.[0-9]+"$' "$init" \
        | head -1 | sed -E 's/^__version__ = "([0-9.]+)"$/\1/')"
    if [ -z "$v" ]; then
        REASON_version="no __version__ literal in yiban/__init__.py"; return
    fi
    local top
    top="$(grep -m1 -E '^## \[v[0-9]+\.[0-9]+\.[0-9]+\]' "$changelog" \
        | sed -E 's/^## \[(v[0-9.]+)\].*/\1/')"
    if [ "$top" != "v$v" ]; then
        REASON_version="__version__=$v but CHANGELOG top is '${top:-none}'"; return
    fi
    if ! git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
        REASON_version="$ROOT is not a git checkout"; return
    fi
    local tag="v$v" tag_sha head_sha
    tag_sha="$(git -C "$ROOT" rev-parse -q --verify "refs/tags/$tag^{commit}" 2>/dev/null || true)"
    head_sha="$(git -C "$ROOT" rev-parse HEAD)"
    if [ -z "$tag_sha" ]; then
        REASON_version="tag $tag does not exist; cut the release tag first"; return
    fi
    if [ "$tag_sha" != "$head_sha" ]; then
        REASON_version="tag $tag points at $tag_sha, HEAD is $head_sha"; return
    fi
    STATUS_version=PASS
    echo "version: __version__=$v, CHANGELOG top=$top, tag $tag -> HEAD ok"
}

# 项 2 许可证清单重生成。server/scripts/license.sh 退出码语义：
#   0 通过；1 清单漂移或违禁许可证（真红）；2 门没跑（首跑需联网装
#   go-licenses，或扫描不可用）。2 → SKIP 显性，不当绿。
check_license() {
    local gate="$ROOT/server/scripts/license.sh"
    STATUS_license=FAIL; REASON_license=""
    [ -f "$gate" ] || { REASON_license="missing $gate"; return; }
    local out rc
    out="$(bash "$gate" 2>&1)"; rc=$?
    if [ "$rc" -eq 0 ]; then
        STATUS_license=PASS; echo "license: $out" | tail -1
    elif [ "$rc" -eq 2 ]; then
        STATUS_license=SKIP
        REASON_license="gate did not run (exit 2; first run needs network to install go-licenses): $(printf '%s' "$out" | tail -1)"
    else
        REASON_license="exit $rc: $(printf '%s' "$out" | tail -1)"
    fi
}

# 项 3 生成物不过期。generate.sh --check 自带 fail-closed：
#   生成器产不出字节即退 2，不把「没跑」当绿；这里透传退出码。
check_generate() {
    local gate="$ROOT/server/scripts/generate.sh"
    STATUS_generate=FAIL; REASON_generate=""
    [ -f "$gate" ] || { REASON_generate="missing $gate"; return; }
    local out rc
    out="$(bash "$gate" --check 2>&1)"; rc=$?
    if [ "$rc" -eq 0 ]; then
        STATUS_generate=PASS; echo "generate: $out" | tail -1
    else
        REASON_generate="exit $rc: $(printf '%s' "$out" | tail -1)"
    fi
}

# 项 4 全量测试 + 真库。本切片只保证指向正确，不代跑全量（AGENTS §8e/§8g：
#   全量由主会话/发布操作者收口跑）。两条既有门：
#   server/scripts/verify.sh（YIBAN_TEST_DATABASE_URL 指真库时 pg/auth e2e 实跑，
#   未设时显性 SKIP）与 scripts/dev-verify.sh（统一跑测入口，提交前 --ci，
#   合并前全量）。
check_tests() {
    STATUS_tests=FAIL; REASON_tests=""
    local missing=0 f
    for f in "$ROOT/server/scripts/verify.sh" "$ROOT/scripts/dev-verify.sh"; do
        if [ ! -f "$f" ]; then echo "tests: missing $f" >&2; missing=1; fi
    done
    [ "$missing" -eq 0 ] || { REASON_tests="referenced gate missing"; return; }
    STATUS_tests=POINTER; REASON_tests="not executed here"
    echo "tests: POINTER — release operator runs:"
    echo "  YIBAN_TEST_DATABASE_URL=<test dsn> bash server/scripts/verify.sh   # 退出码 0，且 pg/auth e2e 非 SKIP"
    echo "  bash scripts/dev-verify.sh --ci                                    # 提交前；合并前全量"
}

# 项 5 容器烟测。引用既有演练：scripts/drill-container.sh（容器形态真签到链路）
#   与 scripts/deploy_gate.sh --phase A（全新 docker 环境按 README 一次装成 + 健康
#   检查，AGENTS §17 判据）。
check_container() {
    STATUS_container=FAIL; REASON_container=""
    local missing=0 f
    for f in "$ROOT/scripts/drill-container.sh" "$ROOT/scripts/deploy_gate.sh"; do
        if [ ! -f "$f" ]; then echo "container: missing $f" >&2; missing=1; fi
    done
    [ "$missing" -eq 0 ] || { REASON_container="referenced drill missing"; return; }
    STATUS_container=POINTER; REASON_container="not executed here"
    echo "container: POINTER — release operator runs:"
    echo "  bash scripts/deploy_gate.sh --phase A      # 干净容器按 README 装成 + /healthz"
    echo "  bash scripts/drill-container.sh            # 容器形态整轮真签到（演练机）"
}

# 项 6 备份演练。引用既有件：scripts/backup.sh（--restore 解包后自动跑
#   integrity_check + audit_verify）与 server/cmd/cli 的 backup/restore 子命令
#   （server/cmd/cli/main.go 注册）；deploy_gate.sh --phase C 是容器形态备份恢复
#   的一键判据。
check_backup() {
    STATUS_backup=FAIL; REASON_backup=""
    [ -f "$ROOT/scripts/backup.sh" ] || { REASON_backup="missing scripts/backup.sh"; return; }
    local cli="$ROOT/server/cmd/cli/main.go"
    [ -f "$cli" ] || { REASON_backup="missing $cli"; return; }
    if ! grep -q 'case "backup"' "$cli" || ! grep -q 'case "restore"' "$cli"; then
        REASON_backup="cli has no backup/restore subcommand"; return
    fi
    [ -f "$ROOT/scripts/deploy_gate.sh" ] || { REASON_backup="missing scripts/deploy_gate.sh"; return; }
    STATUS_backup=POINTER; REASON_backup="not executed here"
    echo "backup: POINTER — release operator runs:"
    echo "  bash scripts/backup.sh --restore <最新备份包> <临时目录>   # 退出码 0（6/7/8 = 自检失败）"
    echo "  yiban-cli backup && yiban-cli restore                      # Go 服务端形态（pg + GPG）"
    echo "  bash scripts/deploy_gate.sh --phase C                      # 容器形态备份恢复判据"
}

# 项 7 回滚演练。考古（2026-10-27，ref ad5b3639）：机制存在——
#   deploy/linux/update.sh 迁移失败回滚为旧件继续跑（exit 1）、健康检查失败换回
#   .bak 旧二进制再起（exit 1）、回滚后仍不健康 exit 2。已知边界：schema 不随
#   二进制回滚（无 down 迁移），update.sh 头注登记。
#   既有守卫：server/cmd/cli/install_update_e2e_test.go 的
#   TestUpdateShRollsBackOnFailedMigration；scripts/deploy_gate.sh --phase B
#   （升到本树顶端再回滚到 v0.6.2，不降 schema）。
check_rollback() {
    STATUS_rollback=FAIL; REASON_rollback=""
    local upd="$ROOT/deploy/linux/update.sh"
    [ -f "$upd" ] || { REASON_rollback="missing $upd"; return; }
    if ! grep -q 'BAK' "$upd" || ! grep -q 'rolling back' "$upd"; then
        REASON_rollback="update.sh has no .bak rollback path"; return
    fi
    if ! grep -q 'TestUpdateShRollsBackOnFailedMigration' \
        "$ROOT/server/cmd/cli/install_update_e2e_test.go"; then
        REASON_rollback="rollback e2e guard missing in install_update_e2e_test.go"; return
    fi
    [ -f "$ROOT/scripts/deploy_gate.sh" ] || { REASON_rollback="missing scripts/deploy_gate.sh"; return; }
    STATUS_rollback=POINTER; REASON_rollback="not executed here"
    echo "rollback: POINTER — mechanism exists (update.sh .bak swap-back; no down migrations)."
    echo "  release operator runs:"
    echo "  bash scripts/deploy_gate.sh --phase B      # 升级→回滚 v0.6.2→数据完整性"
    echo "  go test ./cmd/cli/ -run TestUpdateShRollsBackOnFailedMigration -count=1"
}

# 项 8 ed25519/SPA 发布件检查。考古（2026-10-27，ref ad5b3639）：未发现既有门
#   （scripts/、server/scripts/、deploy/ 无 ed25519/SPA 发布件检查；SPA 仅作为
#   协议注释里的 H5 页面名出现）。无门即不装门，登记人工步骤。
check_signing() {
    STATUS_signing=MANUAL; REASON_signing="no automated gate exists"
    echo "signing: MANUAL — 发布件 ed25519/SPA 相关检查无既有自动化门（考古见 deploy/release.md）。"
    echo "  人工步骤：核对发布件与构建物料清单、校验产物 sha256、确认无未登记的二进制面变更。"
}

# ---------- 汇总与入口 ----------

run_items() {
    local only="$1" item failed=0
    for item in $ITEMS; do
        if [ -n "$only" ] && [ "$item" != "$only" ]; then continue; fi
        "check_$item"
    done
    echo "---- release-check summary (root=$ROOT) ----"
    for item in $ITEMS; do
        if [ -n "$only" ] && [ "$item" != "$only" ]; then continue; fi
        printf '%-9s %-8s %s\n' "$item" "$(eval "printf '%s' \"\$STATUS_$item\"")" \
            "$(eval "printf '%s' \"\$REASON_$item\"")"
        if [ "$(eval "printf '%s' \"\$STATUS_$item\"")" = "FAIL" ]; then failed=1; fi
    done
    return $failed
}

# ---------- 突变自测 ----------
# 不碰真文件：全部夹具在 mktemp 临时树里，经 RELEASE_CHECK_ROOT 注入。
selftest() {
    local tmp rc fails=0
    say() { printf 'selftest: %s\n' "$1"; }
    expect() { # expect <描述> <期望rc> <实际rc>
        if [ "$2" -eq "$3" ]; then say "ok: $1 (rc=$3)"
        else say "MISMATCH: $1 want rc=$2 got rc=$3"; fails=$((fails+1)); fi
    }

    # 不变式 2：--only 不存在的项名报错非零。
    bash "$SELF" --only __no_such_item__ >/dev/null 2>&1; rc=$?
    expect "--only with unknown item is non-zero" 2 "$rc"

    # 不变式 1a：version 项在一致夹具上绿、破坏后红。
    tmp="$(mktemp -d)"
    git -C "$tmp" init -q
    git -C "$tmp" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
    mkdir -p "$tmp/yiban"
    printf '__version__ = "9.9.9"\n' > "$tmp/yiban/__init__.py"
    printf '# 更新日志\n\n## [v9.9.9] - 2026-01-01\n' > "$tmp/CHANGELOG.md"
    git -C "$tmp" -c user.email=t@t -c user.name=t commit -q --allow-empty -m v
    git -C "$tmp" tag v9.9.9
    RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only version >/dev/null 2>&1; rc=$?
    expect "version green on consistent fixture" 0 "$rc"
    # 突变：改 __init__ 版本，tag 不再匹配 → 红。
    printf '__version__ = "9.9.10"\n' > "$tmp/yiban/__init__.py"
    RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only version >/dev/null 2>&1; rc=$?
    expect "version red after breaking __version__" 1 "$rc"
    rm -rf "$tmp"

    # 不变式 1b：generate 项透传被引命令的非零退出码。
    tmp="$(mktemp -d)"
    mkdir -p "$tmp/server/scripts"
    printf '#!/usr/bin/env bash\nexit 0\n' > "$tmp/server/scripts/generate.sh"
    RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only generate >/dev/null 2>&1; rc=$?
    expect "generate green when gate exits 0" 0 "$rc"
    printf '#!/usr/bin/env bash\nexit 1\n' > "$tmp/server/scripts/generate.sh"
    RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only generate >/dev/null 2>&1; rc=$?
    expect "generate red when gate exits 1" 1 "$rc"

    # 不变式 1c：license 项 exit 1（清单漂移）红；exit 2（门没跑）SKIP 不算 FAIL。
    printf '#!/usr/bin/env bash\necho stale >&2\nexit 1\n' > "$tmp/server/scripts/license.sh"
    RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only license >/dev/null 2>&1; rc=$?
    expect "license red when gate exits 1" 1 "$rc"
    printf '#!/usr/bin/env bash\necho cannot install scanner >&2\nexit 2\n' > "$tmp/server/scripts/license.sh"
    RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only license >/dev/null 2>&1; rc=$?
    expect "license SKIP (exit 2) is not FAIL" 0 "$rc"
    RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only license 2>/dev/null \
        | grep -q '^license *SKIP' || { say "MISMATCH: SKIP not visible in summary"; fails=$((fails+1)); }
    rm -rf "$tmp"

    if [ "$fails" -eq 0 ]; then say "all assertions passed"; return 0; fi
    say "$fails assertion(s) failed"; return 1
}

main() {
    local only=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --only)
                [ $# -ge 2 ] || { echo "release-check: --only needs an item" >&2; exit 2; }
                only="$2"; shift 2
                case " $ITEMS " in
                    *" $only "*) ;;
                    *) echo "release-check: unknown item: $only" >&2
                       echo "known items: $ITEMS" >&2; exit 2 ;;
                esac ;;
            --list) echo "$ITEMS"; exit 0 ;;
            --selftest) selftest; exit $? ;;
            -h|--help) usage; exit 0 ;;
            *) echo "release-check: unknown argument: $1" >&2; exit 2 ;;
        esac
    done
    run_items "$only"
}
main "$@"
