#!/usr/bin/env bash
# ============================================================
# release-check.sh —— 发布门清单（P8-D 切片）
# ============================================================
# 用途：把发布前要过的检查收成一条命令。九项清单与考古结论见
#   deploy/release.md（权威版）；本脚本只负责可执行的部分。
#
# 用法（在仓库根目录或任意位置执行）：
#   bash scripts/release-check.sh                # 跑全部九项
#   bash scripts/release-check.sh --only version # 只跑一项（旧 Python 线遗留）
#   bash scripts/release-check.sh --only go-version # Go 线：按根 VERSION 构建并验自报版本 + 发布 tag
#   bash scripts/release-check.sh --list         # 列出项名
#   bash scripts/release-check.sh --require-signing  # 发布操作档：signing 的 SKIP 升为 FAIL
#   bash scripts/release-check.sh --only signing --require-signing \
#       [--pubkey-sha256-file /path/release-pubkey.sha256]  # 外部信任锚（不得在发布件目录内）
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
#   YIBAN_RELEASE_DIR   发布件目录。默认 $ROOT/release。
#   YIBAN_RELEASE_PUBKEY_SHA256
#                       信任锚：发布公钥的 SHA-256 指纹（64 位十六进制）。
#                       也可用 --pubkey-sha256-file <路径> 给文件。
#                       两条来源都必须在发布件目录之外；目录内的指纹一律不认。
#
# 突变自测（--selftest）钉三类不变式：
#   1. 某项被破坏时 --only 该项退出非零（临时坏夹具触发，不改真文件）；
#   2. --only 不存在的项名报错退出非零；
#   3. 帮助文本只取文件头注释块；sign.sh 不把私钥写成输出；
#      realpath 缺失时锚外判定 FAIL（缺工具即拒绝）。
#   go-version 项（2026-11-05 版本批）钉四类突变：缺 VERSION、坏 semver、
#   构建不注入（自报 dev）、tag 不指向 HEAD，各必须非零；一致夹具必须绿。
# ============================================================
set -u

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
ROOT="${RELEASE_CHECK_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
REQUIRE_SIGNING=0
PUBKEY_SHA256_FILE=""

ITEMS="version go-version license generate tests container backup rollback signing"

# 帮助文本只取文件头注释块（到第一个非注释行为止）：行号会随改动漂移。
usage() {
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$SELF"
}

# ---------- 各项检查 ----------
# 约定：每个 check_* 把结果写进全局 STATUS_<item>（PASS/FAIL/SKIP/POINTER/MANUAL）
# 与 REASON_<item>，明细直接打到 stdout。

# 项 1 版本一致性（旧 Python 线遗留；Go 线用 go-version 项）。考古（2026-10-27，ref ad5b3639）：
#   版本唯一来源 = yiban/__init__.py 的 __version__（tests/test_release_version_source.py 守卫）。
#   二进制自报版本自 2026-11-04 起存在：yiban-server --version / yiban-cli version，
#   三值由构建期 -ldflags -X 注入 server/internal/buildinfo（未注入显性 dev/unknown）。
#   openapi info.version = "1.0.0"，是 API 契约版本，不随发布走；2026-11-05 口径落定：
#   契约版本只在破坏性契约变更时升，发布 tag 用 v$VERSION（根 VERSION），两者独立，
#   见 server/contracts/README.md 与 deploy/release.md 项 1b。
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
    # 双行共库（2026-11-10 定稿）：旧 Python 线的 __version__ 冻结在 0.6.4，
    # Go 线的产品版本在仓库根 VERSION（1.0.0）。两条线共用一份 CHANGELOG，
    # 顶部只能写一个条目，故「等于 __version__」或「等于根 VERSION」任一成立即放行；
    # 两个都不是才红——那时顶部是第三个值，说明有人手改了版本或 CHANGELOG。
    local go_v=""
    if [ -f "$ROOT/VERSION" ]; then
        go_v="$(head -n 1 "$ROOT/VERSION" | tr -d '\r')"
        printf '%s' "$go_v" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || go_v=""
    fi
    if [ "$top" != "v$v" ] && { [ -z "$go_v" ] || [ "$top" != "v$go_v" ]; }; then
        REASON_version="__version__=$v, root VERSION=${go_v:-none}, but CHANGELOG top is '${top:-none}'"; return
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
    echo "version: __version__=$v, root VERSION=${go_v:-none}, CHANGELOG top=$top, tag $tag -> HEAD ok"
}

# 项 1b Go 线产品版本与发布 tag（2026-11-05 版本批）。
#   单一来源 = 仓库根 VERSION（单行 X.Y.Z，换行结尾）；旧 Python 线的
#   yiban/__init__.py 与 CHANGELOG 保持冻结，不在本项范围。
#   三个动作：
#   a) tag 对账：v$VERSION 存在且指向 HEAD；不存在 ⇒ FAIL 并提示先打
#      tag（先于构建：缺 tag 时不再白付一次编译）；
#   b) 真构建：调 server/scripts/build-release.sh 到 mktemp 临时目录，
#      跑 yiban-server --version 与 yiban-cli version，断言自报 version
#      == VERSION（输出是稳定的 "version: X" 三行文本，机器可读）；
#   c) 契约版本不参与：openapi info.version 只在破坏性契约变更时升，
#      与本项无关（server/contracts/README.md）。
#   真仓库在打发布 tag 之前本项 FAIL 是预期（发布时才绿）；selftest 用
#   桩 builder（不编译 Go）覆盖四类突变。构建开销：模块缓存热时约数秒。
check_go_version() {
    STATUS_go_version=FAIL; REASON_go_version=""
    local vfile="$ROOT/VERSION"
    if [ ! -f "$vfile" ]; then
        REASON_go_version="missing $vfile (Go-line product version single source)"; return
    fi
    local vlines ver
    vlines="$(wc -l < "$vfile" | tr -d ' ')"
    if [ "$vlines" != "1" ]; then
        REASON_go_version="$vfile must be exactly one newline-terminated line, found $vlines"; return
    fi
    ver="$(head -n 1 "$vfile" | tr -d '\r')"
    if ! printf '%s' "$ver" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
        REASON_go_version="$vfile content is not X.Y.Z semver: '$ver'"; return
    fi
    local builder="$ROOT/server/scripts/build-release.sh"
    if [ ! -f "$builder" ]; then
        REASON_go_version="missing $builder"; return
    fi
    if ! git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
        REASON_go_version="$ROOT is not a git checkout"; return
    fi
    # a) 发布 tag 对账在前：缺 tag／tag 不指向 HEAD 时直接红，
    #    不白付一次两二进制的编译（审查 P2）。
    local tag="v$ver" tag_sha head_sha
    tag_sha="$(git -C "$ROOT" rev-parse -q --verify "refs/tags/$tag^{commit}" 2>/dev/null || true)"
    head_sha="$(git -C "$ROOT" rev-parse HEAD)"
    if [ -z "$tag_sha" ]; then
        REASON_go_version="tag $tag does not exist; cut the release tag first (Go line: root VERSION)"; return
    fi
    if [ "$tag_sha" != "$head_sha" ]; then
        REASON_go_version="tag $tag points at $tag_sha, HEAD is $head_sha"; return
    fi
    # b) 真构建 + 自报版本断言。产物名后缀规则见 build-release.sh 头注。
    local out build_out brc exe sv cv sver cver
    out="$(mktemp -d)" || { REASON_go_version="mktemp failed"; return; }
    build_out="$(bash "$builder" "$out" 2>&1)"; brc=$?
    if [ "$brc" -ne 0 ]; then
        rm -rf "$out"
        REASON_go_version="release build failed: build-release.sh exit $brc: $(printf '%s' "$build_out" | tail -1)"
        return
    fi
    printf '%s\n' "$build_out" | grep '^build-release: version=' | head -1
    exe=""
    case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) exe=".exe" ;; esac
    sv="$("$out/yiban-server$exe" --version 2>&1)"
    cv="$("$out/yiban-cli$exe" version 2>&1)"
    sver="$(printf '%s\n' "$sv" | sed -n 's/^version: //p')"
    cver="$(printf '%s\n' "$cv" | sed -n 's/^version: //p')"
    rm -rf "$out"
    if [ "$sver" != "$ver" ] || [ "$cver" != "$ver" ]; then
        REASON_go_version="self-report mismatch: server='${sver:-none}' cli='${cver:-none}', want $ver"
        return
    fi
    STATUS_go_version=PASS
    echo "go-version: VERSION=$ver, yiban-server/yiban-cli self-report $ver, tag $tag -> HEAD ok"
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
#   deploy/baremetal/update.sh 迁移失败回滚为旧件继续跑（exit 1）、健康检查失败换回
#   .bak 旧二进制再起（exit 1）、回滚后仍不健康 exit 2。
#   2026-11-04（U2）起 update.sh 是一键更新：停服前自检门（selfcheck --json，
#   FAIL 即 exit 2）、统一备份（pg_dump -Fc + pg_restore --list + 照片 tar +
#   sha256）、迁移后失败默认把 pre-migrate 备份恢复到新库并切 DSN
#   （--no-restore-db 关闭）、恢复失败 exit 2 + manual action 且不启动旧件；
#   二进制/UI/CLI 一律自动回滚。
#   既有守卫：server/cmd/cli/update_u2_e2e_test.go（自检门、备份布局/保留、
#   数据库回滚成功/失败）与 install_update_e2e_test.go（.bak 回滚、签名）。
#   演练正本＝scripts/e2e/update-rollback-e2e.sh（R1..R10 全矩阵，一次性
#   systemd 容器真跑 update.sh；容器外跑 run 被守卫拒绝，exit 2 零写入）。
#   2026-11-04（U2 审查）：失败矩阵已进 CI——deploy-gate.yml 的
#   update-rollback-e2e job 先真跑 update.sh 干跑契约，再跑容器演练；
#   环境起不来时显性 SKIP（写进 step summary，不算绿）。本项仍记 POINTER：
#   CI 的 SKIP 不是绿，发布前要按下面那条命令手跑一遍。
#   旧线 deploy_gate.sh --phase B：本项不再指向；脚本与旧线 CI 仍在
#   （phase B 未删）。
check_rollback() {
    STATUS_rollback=FAIL; REASON_rollback=""
    local upd="$ROOT/deploy/baremetal/update.sh"
    local drill="$ROOT/scripts/e2e/update-rollback-e2e.sh"
    local guard="$ROOT/server/cmd/cli/update_u2_e2e_test.go"
    local marker
    [ -f "$upd" ] || { REASON_rollback="missing $upd"; return; }
    if ! grep -q 'BAK' "$upd" || ! grep -q 'rolling back' "$upd"; then
        REASON_rollback="update.sh has no .bak rollback path"; return
    fi
    # U2 的三个机件必须在 update.sh 里真接线，不能只写在文档里。
    for marker in 'selfcheck FAILED' 'backup FAILED' 'db restore' 'manual action required'; do
        if ! grep -q "$marker" "$upd"; then
            REASON_rollback="update.sh is missing the U2 mechanism: $marker"; return
        fi
    done
    [ -f "$drill" ] || { REASON_rollback="missing $drill"; return; }
    if ! grep -q 'REFUSING to run the drill outside the e2e container' "$drill"; then
        REASON_rollback="drill script has no container guard"; return
    fi
    # 演练必须覆盖失败矩阵的每一类，不能只剩三步。
    for marker in '备份失败' '自检 FAIL' 'CREATEDB' '数据库恢复失败'; do
        if ! grep -q "$marker" "$drill"; then
            REASON_rollback="drill does not cover: $marker"; return
        fi
    done
    if ! grep -q 'TestUpdateShRollsBackOnFailedMigration' \
        "$ROOT/server/cmd/cli/install_update_e2e_test.go"; then
        REASON_rollback="rollback e2e guard missing in install_update_e2e_test.go"; return
    fi
    [ -f "$guard" ] || { REASON_rollback="missing $guard"; return; }
    # CI 接线：job 必须在，且真的调用演练（不是只在注释里）。
    local wf="$ROOT/.github/workflows/deploy-gate.yml"
    [ -f "$wf" ] || { REASON_rollback="missing $wf"; return; }
    if ! grep -q '^  update-rollback-e2e:' "$wf"; then
        REASON_rollback="deploy-gate.yml has no update-rollback-e2e job"; return
    fi
    if ! grep -q 'bash scripts/e2e/update-rollback-e2e.sh host' "$wf"; then
        REASON_rollback="update-rollback-e2e job does not invoke the drill"; return
    fi
    STATUS_rollback=POINTER; REASON_rollback="not executed here"
    echo "rollback: POINTER — mechanism exists (update.sh .bak swap-back; DB restore into a new database; no down migrations)."
    echo "  release operator runs:"
    echo "  bash scripts/e2e/update-rollback-e2e.sh host --log-dir <日志目录>   # R1..R10 全矩阵真跑（docker --privileged；守卫只许容器内跑 run）"
    echo "  go test ./cmd/cli/ -run 'TestUpdateSh' -count=1"
}

# 项 8 ed25519/SPA 发布件检查。考古（2026-10-27，ref ad5b3639）：无既有门，
#   曾登记 MANUAL。2026-10-29 起：发布物签名链落地（scripts/release/
#   make-manifest.sh / sign.sh / verify.sh，docs/dev/release-signing.md），
#   本项升级为可执行门——发布件目录（$ROOT/release/，env
#   YIBAN_RELEASE_DIR 可覆盖）里三件齐全（manifest.json + manifest.json.sig
#   + release-pubkey.pem）且信任锚匹配时跑 verify.sh 验签。
#   信任锚（P0，2026-10-29 裁决 4）：只认发布件目录**之外**的来源——
#   YIBAN_RELEASE_PUBKEY_SHA256（环境）或 --pubkey-sha256-file <路径>。
#   发布件目录内的 release-pubkey.sha256 一律不认：整包不可信时锚也不可信。
#   缺锚 → SKIP（默认档）/ FAIL（--require-signing），并报
#   "anchored fingerprint must come from outside the release directory"；
#   指纹不符 → FAIL。不得接受「随便给的公钥」。
#   --require-signing：缺件与缺 openssl 从 SKIP 升为 FAIL；发布操作必须用该档。
#   缺 realpath → FAIL（缺工具即拒绝，2026-10-29 复审 P3）。
#   verify.sh rc=1（验签不过）→ FAIL；rc=2（openssl 缺等，验证没跑）→ SKIP 显性。
#   sha256 与二进制的对账由 update.sh --manifest/--signature/--pubkey 在升级时承担。
check_signing() {
    STATUS_signing=FAIL; REASON_signing=""
    local dir="${YIBAN_RELEASE_DIR:-$ROOT/release}"
    local mf="$dir/manifest.json" sig="$dir/manifest.json.sig" pub="$dir/release-pubkey.pem"
    local verifier="$ROOT/scripts/release/verify.sh"
    local missing=()
    [ -f "$mf" ] || missing+=(manifest.json)
    [ -f "$sig" ] || missing+=(manifest.json.sig)
    [ -f "$pub" ] || missing+=(release-pubkey.pem)
    if [ "${#missing[@]}" -gt 0 ]; then
        if [ "$REQUIRE_SIGNING" = "1" ]; then
            REASON_signing="required release artifacts missing in $dir: ${missing[*]}"
            return
        fi
        STATUS_signing=SKIP
        REASON_signing="release artifacts incomplete in $dir: missing ${missing[*]}"
        echo "signing: SKIP — 缺件：${missing[*]}（$dir），显性跳过不记绿；签发见 docs/dev/release-signing.md（发布操作须加 --require-signing）"
        return
    fi
    # 信任锚：先钉公钥指纹，再验签。锚不得来自发布件目录本身。
    local pin="" pin_source="" anchor_problem=""
    if [ -n "${YIBAN_RELEASE_PUBKEY_SHA256:-}" ]; then
        pin="$YIBAN_RELEASE_PUBKEY_SHA256"; pin_source="env YIBAN_RELEASE_PUBKEY_SHA256"
    elif [ -n "$PUBKEY_SHA256_FILE" ]; then
        # 缺 realpath 即拒绝（2026-10-29 复审 P3）：路径归一化做不了，
        # 「锚在发布件目录之外」就证明不了。此处直接 FAIL，不走 SKIP。
        if ! command -v realpath >/dev/null 2>&1; then
            REASON_signing="realpath not found; cannot verify the anchored fingerprint is outside the release directory"
            return
        fi
        local dir_real pin_real
        dir_real="$(realpath -m -- "$dir" 2>/dev/null)" || {
            REASON_signing="realpath failed on release directory: $dir"; return; }
        pin_real="$(realpath -m -- "$PUBKEY_SHA256_FILE" 2>/dev/null)" || {
            REASON_signing="realpath failed on anchored fingerprint file: $PUBKEY_SHA256_FILE"; return; }
        case "$pin_real" in
            "$dir_real"|"$dir_real"/*)
                anchor_problem="anchored fingerprint must come from outside the release directory ($PUBKEY_SHA256_FILE is inside $dir)" ;;
            *)
                if [ ! -f "$PUBKEY_SHA256_FILE" ]; then
                    anchor_problem="pinned fingerprint file not found: $PUBKEY_SHA256_FILE"
                else
                    pin="$(head -n1 "$PUBKEY_SHA256_FILE" 2>/dev/null || true)"
                    pin_source="$PUBKEY_SHA256_FILE"
                fi ;;
        esac
    else
        anchor_problem="anchored fingerprint must come from outside the release directory (set YIBAN_RELEASE_PUBKEY_SHA256 or --pubkey-sha256-file)"
    fi
    if [ -z "$pin" ]; then
        [ -n "$anchor_problem" ] || anchor_problem="pinned fingerprint file is empty: $PUBKEY_SHA256_FILE"
        if [ "$REQUIRE_SIGNING" = "1" ]; then
            REASON_signing="$anchor_problem"
            return
        fi
        STATUS_signing=SKIP
        REASON_signing="$anchor_problem"
        echo "signing: SKIP — $anchor_problem（验证没跑，不算绿；发布操作须加 --require-signing）"
        return
    fi
    pin="$(printf '%s' "$pin" | tr -d '\r' | tr 'A-F' 'a-f' | awk '{print $1}')"
    case "$pin" in
        ''|*[!0-9a-f]*) REASON_signing="pinned fingerprint is not hex (source: $pin_source)"; return ;;
    esac
    if [ "${#pin}" -ne 64 ]; then
        REASON_signing="pinned fingerprint is not 64 hex chars (source: $pin_source)"; return
    fi
    local got
    got="$(sha256sum -- "$pub" 2>/dev/null)" || { REASON_signing="sha256sum failed on $pub"; return; }
    got="${got%% *}"
    if [ "$got" != "$pin" ]; then
        REASON_signing="public key fingerprint $got does not match pinned $pin (source: $pin_source)"
        return
    fi
    if [ ! -f "$verifier" ]; then
        REASON_signing="verifier missing: $verifier"
        return
    fi
    if ! command -v openssl >/dev/null 2>&1; then
        if [ "$REQUIRE_SIGNING" = "1" ]; then
            REASON_signing="openssl not in PATH; verification DID NOT run"
            return
        fi
        STATUS_signing=SKIP
        REASON_signing="openssl not in PATH; verification DID NOT run"
        echo "signing: SKIP — openssl 不可用，验证没跑（不算绿；发布操作须加 --require-signing）"
        return
    fi
    local out rc
    out="$(bash "$verifier" --pubkey "$pub" --manifest "$mf" --signature "$sig" 2>&1)"; rc=$?
    if [ "$rc" -eq 0 ]; then
        STATUS_signing=PASS; echo "signing: $out"
    elif [ "$rc" -eq 2 ]; then
        if [ "$REQUIRE_SIGNING" = "1" ]; then
            REASON_signing="verifier exit 2 (environment): $out"
            return
        fi
        STATUS_signing=SKIP
        REASON_signing="verifier exit 2 (environment): $out"
        echo "signing: SKIP — verify.sh 退出 2（环境错误，验证没跑；发布操作须加 --require-signing）"
    else
        REASON_signing="exit $rc: $out"
    fi
}

# ---------- 汇总与入口 ----------

# 项名 → 状态变量/检查函数后缀：item 名可带连字符（go-version），
# bash 变量与函数名不可，故统一把 - 映射为 _。
# 必须是纯 bash 展开（不借 tr 等外部命令）：selftest 会在受限 PATH 下
# 跑本脚本，外部命令可能缺失或不可用（MSYS 复制的 exe 缺共享库）。
item_var() { printf '%s' "${1//-/_}"; }

run_items() {
    local only="$1" item vs failed=0 skipped=""
    for item in $ITEMS; do
        if [ -n "$only" ] && [ "$item" != "$only" ]; then continue; fi
        "check_$(item_var "$item")"
    done
    echo "---- release-check summary (root=$ROOT) ----"
    for item in $ITEMS; do
        if [ -n "$only" ] && [ "$item" != "$only" ]; then continue; fi
        vs="$(item_var "$item")"
        printf '%-10s %-8s %s\n' "$item" "$(eval "printf '%s' \"\$STATUS_$vs\"")" \
            "$(eval "printf '%s' \"\$REASON_$vs\"")"
        if [ "$(eval "printf '%s' \"\$STATUS_$vs\"")" = "FAIL" ]; then failed=1; fi
        if [ "$(eval "printf '%s' \"\$STATUS_$vs\"")" = "SKIP" ]; then
            skipped="$skipped $item"
        fi
    done
    if [ -n "$skipped" ]; then
        echo "本次为跳过：$skipped（跳过不等于绿；发布操作必须用 --require-signing）"
    fi
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
    # 双行共库（2026-11-10 定稿）：顶部等于根 VERSION 也放行。
    printf '__version__ = "9.9.9"\n' > "$tmp/yiban/__init__.py"
    printf '9.9.10\n' > "$tmp/VERSION"
    printf '# 更新日志\n\n## [v9.9.10] - 2026-01-02\n' > "$tmp/CHANGELOG.md"
    RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only version >/dev/null 2>&1; rc=$?
    expect "version green when CHANGELOG top matches root VERSION" 0 "$rc"
    # 突变：顶部写成两线之外的第三个值 → 红。
    printf '# 更新日志\n\n## [v9.9.11] - 2026-01-03\n' > "$tmp/CHANGELOG.md"
    RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only version >/dev/null 2>&1; rc=$?
    expect "version red when CHANGELOG top is a third version" 1 "$rc"
    rm -rf "$tmp"

    # 不变式 1g：go-version 项有牙（2026-11-05 版本批）。夹具用桩 builder
    # （不编译 Go）模拟产物自报，覆盖一致夹具绿 + 四类突变红：
    #   a) 缺 VERSION；b) 坏 semver；c) 构建不注入（自报 dev）；
    #   d) tag 不指向 HEAD。
    tmp="$(mktemp -d)"
    git -C "$tmp" init -q
    git -C "$tmp" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
    mkdir -p "$tmp/server/scripts"
    # 桩 builder：产两个自报 STUB_VERSION（缺省 dev）的假二进制，命名规则与
    # 真 build-release.sh 一致（Windows 宿主带 .exe）。
    cat > "$tmp/server/scripts/build-release.sh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
out="$1"
exe=""
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) exe=".exe" ;; esac
ver="${STUB_VERSION:-dev}"
for name in yiban-server yiban-cli; do
    {
        printf '#!/usr/bin/env bash\n'
        printf "printf 'version: %s\\ncommit: deadbee\\nbuild_time: 2026-01-01T00:00:00Z\\n'\n" "$ver"
    } > "$out/$name$exe"
    chmod +x "$out/$name$exe"
done
echo "build-release: version=$ver commit=deadbee build_time=2026-01-01T00:00:00Z"
STUB
    chmod +x "$tmp/server/scripts/build-release.sh"
    printf '9.9.9\n' > "$tmp/VERSION"
    git -C "$tmp" add -A
    git -C "$tmp" -c user.email=t@t -c user.name=t commit -q -m v
    git -C "$tmp" tag v9.9.9
    STUB_VERSION=9.9.9 RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only go-version >/dev/null 2>&1; rc=$?
    expect "go-version green on consistent fixture" 0 "$rc"
    # 突变 c：构建不注入（桩自报 dev）→ 自报 != VERSION → 红。
    RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only go-version >/dev/null 2>&1; rc=$?
    expect "go-version red when build self-reports dev (not injected)" 1 "$rc"
    RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only go-version 2>/dev/null \
        | grep -q "self-report mismatch: server='dev'" \
        || { say "MISMATCH: non-injected build not named"; fails=$((fails+1)); }
    # 突变 b：坏 semver → 红。
    printf '9.9\n' > "$tmp/VERSION"
    STUB_VERSION=9.9.9 RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only go-version >/dev/null 2>&1; rc=$?
    expect "go-version red on non-semver VERSION" 1 "$rc"
    # 突变 d：tag 不指向 HEAD（HEAD 前进一个提交）→ 红。
    printf '9.9.9\n' > "$tmp/VERSION"
    git -C "$tmp" -c user.email=t@t -c user.name=t commit -q --allow-empty -m next
    STUB_VERSION=9.9.9 RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only go-version >/dev/null 2>&1; rc=$?
    expect "go-version red when tag does not point at HEAD" 1 "$rc"
    # 突变 a：缺 VERSION → 红。
    rm -f "$tmp/VERSION"
    STUB_VERSION=9.9.9 RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only go-version >/dev/null 2>&1; rc=$?
    expect "go-version red when VERSION is missing" 1 "$rc"
    RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only go-version 2>/dev/null \
        | grep -q 'missing .*VERSION' \
        || { say "MISMATCH: missing VERSION not named"; fails=$((fails+1)); }
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

    # 不变式 1e：帮助文本只取文件头注释块，不得把实现行（set -euo pipefail）
    # 打进帮助；三段脚本都要有「用法」段（2026-10-29 审查 P3）。
    for s in make-manifest.sh sign.sh verify.sh; do
        out="$(bash "$(dirname "$SELF")/release/$s" --help 2>&1 || true)"
        printf '%s' "$out" | grep -q 'set -euo pipefail' \
            && { say "MISMATCH: $s usage includes implementation line"; fails=$((fails+1)); }
        printf '%s' "$out" | grep -q '用法' \
            || { say "MISMATCH: $s usage missing header text"; fails=$((fails+1)); }
    done

    # 不变式 1d：signing 项有牙（AGENTS §30）。
    # 夹具里放真 verify.sh + openssl 现场生成的三件套。
    # 信任锚只认发布件目录**之外**的来源（2026-10-29 裁决 4）：
    #   a) 缺锚 → 默认 SKIP 显性；--require-signing 下 FAIL；
    #   b) 发布件目录内自带指纹不得被信 → 默认 SKIP；发布档 FAIL；
    #   c) 环境变量给正确指纹 → 默认与发布档都 PASS；
    #   d) --pubkey-sha256-file 给目录外文件 → PASS；给目录内文件 → FAIL；
    #   e) 篡改清单一字节 → FAIL（门必须挡得住坏发布物）；
    #   f) 错指纹 → FAIL（不得接受随便给的公钥）；
    #   g) 缺件 → SKIP 显性；--require-signing 下 FAIL。
    # openssl 不在的机器上本组断言整组显性跳过（门自身无法构造，如实说）。
    if command -v openssl >/dev/null 2>&1; then
        tmp="$(mktemp -d)"
        mkdir -p "$tmp/scripts/release" "$tmp/release"
        cp "$(dirname "$SELF")/release/verify.sh" "$tmp/scripts/release/verify.sh"
        openssl genpkey -algorithm ED25519 -out "$tmp/_k.pem" 2>/dev/null
        openssl pkey -in "$tmp/_k.pem" -pubout -out "$tmp/release/release-pubkey.pem" 2>/dev/null
        printf '{"version":"9.9.9","files":[{"name":"yiban-server","sha256":"%s"}]}\n' \
            "$(printf body | sha256sum | cut -d' ' -f1)" > "$tmp/release/manifest.json"
        bash "$(dirname "$SELF")/release/sign.sh" --key "$tmp/_k.pem" \
            --in "$tmp/release/manifest.json" --out "$tmp/release/manifest.json.sig" >/dev/null 2>&1
        pin="$(sha256sum "$tmp/release/release-pubkey.pem" | cut -d' ' -f1)"
        # a) 缺锚：默认 SKIP（可见），发布档红。
        YIBAN_RELEASE_PUBKEY_SHA256= RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing >/dev/null 2>&1; rc=$?
        expect "signing SKIP without trust anchor is not FAIL" 0 "$rc"
        YIBAN_RELEASE_PUBKEY_SHA256= RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing 2>/dev/null \
            | grep -q '^signing *SKIP' || { say "MISMATCH: anchor-missing SKIP not visible"; fails=$((fails+1)); }
        YIBAN_RELEASE_PUBKEY_SHA256= RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing --require-signing >/dev/null 2>&1; rc=$?
        expect "signing red without trust anchor under --require-signing" 1 "$rc"
        # b) 发布件目录内自带指纹不得被信：整包不可信时锚也不可信。
        printf '%s  release-pubkey.pem\n' "$pin" > "$tmp/release/release-pubkey.sha256"
        YIBAN_RELEASE_PUBKEY_SHA256= RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing --require-signing >/dev/null 2>&1; rc=$?
        expect "signing red on in-dir fingerprint under --require-signing" 1 "$rc"
        YIBAN_RELEASE_PUBKEY_SHA256= RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing --require-signing 2>/dev/null \
            | grep -q 'anchored fingerprint must come from outside the release directory' \
            || { say "MISMATCH: in-dir anchor rejection not named"; fails=$((fails+1)); }
        YIBAN_RELEASE_PUBKEY_SHA256= RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing >/dev/null 2>&1; rc=$?
        expect "signing SKIP on in-dir fingerprint is not FAIL" 0 "$rc"
        # c) 环境变量给正确指纹 → 默认与发布档都 PASS。
        YIBAN_RELEASE_PUBKEY_SHA256="$pin" RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing >/dev/null 2>&1; rc=$?
        expect "signing green on intact fixture" 0 "$rc"
        YIBAN_RELEASE_PUBKEY_SHA256="$pin" RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing --require-signing >/dev/null 2>&1; rc=$?
        expect "signing green under --require-signing with external anchor" 0 "$rc"
        # d) 显式外部文件锚：目录外通过，目录内拒绝。
        printf '%s  release-pubkey.pem\n' "$pin" > "$tmp/trusted.sha256"
        YIBAN_RELEASE_PUBKEY_SHA256= RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing \
            --pubkey-sha256-file "$tmp/trusted.sha256" --require-signing >/dev/null 2>&1; rc=$?
        expect "signing green with external --pubkey-sha256-file" 0 "$rc"
        YIBAN_RELEASE_PUBKEY_SHA256= RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing \
            --pubkey-sha256-file "$tmp/release/release-pubkey.sha256" --require-signing >/dev/null 2>&1; rc=$?
        expect "signing red with in-dir --pubkey-sha256-file" 1 "$rc"
        # e) 篡改清单一字节 → 红。
        sed 's/"version":"9.9.9"/"version":"9.9.8"/' "$tmp/release/manifest.json" \
            > "$tmp/release/manifest.json.t" && mv "$tmp/release/manifest.json.t" "$tmp/release/manifest.json"
        YIBAN_RELEASE_PUBKEY_SHA256="$pin" RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing >/dev/null 2>&1; rc=$?
        expect "signing red after manifest tamper" 1 "$rc"
        # f) 错指纹（另一把钥的 sha256）→ 红，且点名指纹不符。
        openssl genpkey -algorithm ED25519 -out "$tmp/_k2.pem" 2>/dev/null
        other="$(sha256sum "$tmp/_k2.pem" | cut -d' ' -f1)"
        YIBAN_RELEASE_PUBKEY_SHA256="$other" RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing >/dev/null 2>&1; rc=$?
        expect "signing red on wrong pinned fingerprint" 1 "$rc"
        YIBAN_RELEASE_PUBKEY_SHA256="$other" RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing 2>/dev/null \
            | grep -q 'does not match pinned' \
            || { say "MISMATCH: fingerprint mismatch not named in summary"; fails=$((fails+1)); }
        # g) 缺件：默认 SKIP 显性；发布档红。
        rm -f "$tmp/release/manifest.json.sig" "$tmp/release/release-pubkey.pem"
        YIBAN_RELEASE_PUBKEY_SHA256="$pin" RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing 2>/dev/null \
            | grep -q '^signing *SKIP' || { say "MISMATCH: missing artifacts not SKIP-visible"; fails=$((fails+1)); }
        YIBAN_RELEASE_PUBKEY_SHA256="$pin" RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing >/dev/null 2>&1; rc=$?
        expect "signing SKIP (missing artifacts) is not FAIL" 0 "$rc"
        YIBAN_RELEASE_PUBKEY_SHA256="$pin" RELEASE_CHECK_ROOT="$tmp" bash "$SELF" --only signing --require-signing >/dev/null 2>&1; rc=$?
        expect "signing red on missing artifacts under --require-signing" 1 "$rc"
        # h) sign.sh 不得把私钥写成输出（--out 指向 --key 会覆盖私钥）。
        key_before="$(sha256sum "$tmp/_k.pem" | cut -d' ' -f1)"
        bash "$(dirname "$SELF")/release/sign.sh" --key "$tmp/_k.pem" \
            --in "$tmp/release/manifest.json" --out "$tmp/_k.pem" >/dev/null 2>&1; rc=$?
        expect "sign.sh rejects --out equal to --key" 2 "$rc"
        [ "$(sha256sum "$tmp/_k.pem" | cut -d' ' -f1)" = "$key_before" ] \
            || { say "MISMATCH: private key was overwritten by sign.sh"; fails=$((fails+1)); }
        rm -rf "$tmp"
    else
        say "SKIP: signing selftest group (openssl not in PATH; group DID NOT run)"
    fi

    # 不变式 1f：realpath 缺失 ⇒ 锚外判定做不了 ⇒ FAIL（缺工具即拒绝）。
    # 沙箱 PATH 里只放脚本启动与判定所需的命令，不放 realpath。
    tmp="$(mktemp -d)"
    mkdir -p "$tmp/scripts/release" "$tmp/release" "$tmp/bin"
    : > "$tmp/release/manifest.json"
    : > "$tmp/release/manifest.json.sig"
    : > "$tmp/release/release-pubkey.pem"
    printf '%s  x\n' \
        "0000000000000000000000000000000000000000000000000000000000000000" > "$tmp/anchor.sha256"
    bash_abs="$(command -v bash)"
    for c in dirname basename awk sed grep head tr cut sha256sum mktemp mkdir rm cp cat; do
        src="$(command -v "$c" 2>/dev/null || true)"
        if [ -n "$src" ]; then ln -s "$src" "$tmp/bin/$c"; fi
    done
    PATH="$tmp/bin" YIBAN_RELEASE_PUBKEY_SHA256= RELEASE_CHECK_ROOT="$tmp" \
        "$bash_abs" "$SELF" --only signing --pubkey-sha256-file "$tmp/anchor.sha256" >/dev/null 2>&1; rc=$?
    expect "signing FAIL when realpath is unavailable (fail-closed)" 1 "$rc"
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
            --require-signing) REQUIRE_SIGNING=1; shift ;;
            --pubkey-sha256-file)
                [ $# -ge 2 ] || { echo "release-check: --pubkey-sha256-file needs a path" >&2; exit 2; }
                PUBKEY_SHA256_FILE="$2"; shift 2 ;;
            --selftest) selftest; exit $? ;;
            -h|--help) usage; exit 0 ;;
            *) echo "release-check: unknown argument: $1" >&2; exit 2 ;;
        esac
    done
    run_items "$only"
}
main "$@"
