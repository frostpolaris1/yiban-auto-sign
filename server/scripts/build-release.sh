#!/usr/bin/env bash
# ============================================================
# build-release.sh —— Go 线发布构建（2026-11-05 版本批）
# ============================================================
# 用途：从仓库根 VERSION（Go 线产品版本的单一来源）读版本号，
#   构建 yiban-server（./cmd/server）与 yiban-cli（./cmd/cli），
#   并把版本身份三元组用 -ldflags -X 注入 server/internal/buildinfo。
#   产物自报版本：yiban-server --version / yiban-cli version。
#
# 用法（在任意目录执行）：
#   bash server/scripts/build-release.sh <outdir> [--ui-dir <ui-dist>]
#   YIBAN_UI_DIST=<ui-dist> bash server/scripts/build-release.sh <outdir>
#
# 行为口径：
#   - VERSION 缺失 / 非 X.Y.Z / 多行 ⇒ exit 2 并点名；
#   - outdir 已存在则复用（幂等），缺失则创建；相对路径先归一为绝对
#     路径（后文会 cd 进 server/ 编译，不归一会让相对 outdir 落进
#     server/ 下）；产物只写 outdir，不写仓库内任何文件；
#   - 先在 outdir 内的临时 staging 构建与生成清单，全部成功后再原子
#     搬入 outdir；失败（含坏 --ui-dir）不留半个二进制或半份清单；
#   - commit 取 git rev-parse --short HEAD；取不到（非 git 检出）⇒
#     exit 2：发布件必须带真实提交，不许回落 unknown；
#   - 一律不注入分支名：发布件不带分支身份；
#   - 产出版本/提交/时间各打印一行（build-release: version=… …）；
#   - 给了 --ui-dir（或环境 YIBAN_UI_DIST）⇒ 校验目录与 index.html 后，
#     用 scripts/release/make-ui-sums.sh 在 <outdir>/ui-SHA256SUMS 生成 UI
#     清单（与二进制同居发布 staging，绝不写进 ui-dist 本身），并打印
#     make-manifest.sh 三件交接命令；不给 ⇒ 行为与旧版完全一致。
#
# 产物名：Linux 为 yiban-server / yiban-cli（与 deploy/baremetal/install.sh、
#   scripts/release/make-manifest.sh 的命名一致）；Windows 宿主（开发与
#   门环境，Git Bash）为 yiban-server.exe / yiban-cli.exe——Go 的
#   os/exec 在 Windows 上不认无 .exe 后缀的 PE。
#
# 口径对齐：-X 的注入名是 Go 标识符（version/commit/buildTime），
#   见 server/internal/buildinfo/buildinfo.go 头注。Dockerfile 与本脚本
#   注入同一组符号；deploy/container/build.sh 给容器构建传同一组值。
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUILDINFO_PKG="github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo"

usage() {
    echo "usage: bash server/scripts/build-release.sh <outdir> [--ui-dir <ui-dist>]" >&2
    echo "       YIBAN_UI_DIST=<ui-dist> bash server/scripts/build-release.sh <outdir>" >&2
}

die() {
    echo "build-release: $1" >&2
    exit 2
}

# 产物后缀：Windows 宿主必须带 .exe（os/exec 不认无后缀 PE）。
exe_suffix() {
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) printf '.exe' ;;
    esac
}

OUT=""
UI_DIST="${YIBAN_UI_DIST:-}"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --ui-dir)
            [ "$#" -ge 2 ] || { usage; die "--ui-dir requires a value"; }
            [ -n "$2" ] || { usage; die "--ui-dir requires a non-empty value"; }
            UI_DIST="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --*)
            usage
            die "unknown argument: $1"
            ;;
        *)
            [ -z "$OUT" ] || { usage; die "exactly one outdir argument is required (extra: $1)"; }
            OUT="$1"
            shift
            ;;
    esac
done
[ -n "$OUT" ] || { usage; die "exactly one argument (outdir) is required"; }

# 版本单一来源：单行、换行结尾、严格 X.Y.Z。
VFILE="$ROOT/VERSION"
[ -f "$VFILE" ] || die "VERSION file missing: $VFILE (Go-line product version single source)"
vlines="$(wc -l < "$VFILE" | tr -d ' ')"
[ "$vlines" = "1" ] || die "$VFILE must be exactly one newline-terminated line, found $vlines"
ver="$(head -n 1 "$VFILE" | tr -d '\r')"
printf '%s' "$ver" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
    || die "$VFILE content is not X.Y.Z semver: '$ver'"

# 提交身份：取不到就拒绝，不编造。
commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)" \
    || die "$ROOT is not a usable git checkout; release builds must carry a real commit"
build_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# 隔离构建环境：外部 GOFLAGS/CGO_ENABLED 不得改写发布件形状；
# GOOS/GOARCH 必须按宿主（继承 GOOS=linux 会产出 linux 二进制却按
# 宿主 uname 命名，产物名与内容错位）。
export CGO_ENABLED=0
export GOFLAGS=
unset GOOS GOARCH
ldflags="-s -w \
-X ${BUILDINFO_PKG}.version=${ver} \
-X ${BUILDINFO_PKG}.commit=${commit} \
-X ${BUILDINFO_PKG}.buildTime=${build_time}"

mkdir -p "$OUT"
# 归一为绝对路径：后面要 cd 进 server/ 编译，相对 outdir 不归一会写进 server/ 下。
OUT="$(cd "$OUT" && pwd)"
exe="$(exe_suffix)"

# UI 产物先校验：坏 --ui-dir 必须在任何编译之前失败。
if [ -n "$UI_DIST" ]; then
    [ -d "$UI_DIST" ] || die "--ui-dir is not a directory: $UI_DIST"
    [ -f "$UI_DIST/index.html" ] || die "--ui-dir has no index.html: $UI_DIST"
fi

# 全部产物先落临时 staging；任何一步失败都由 trap 清掉，
# outdir 里不会出现「编译成功但清单失败」的半成品。
stage="$(mktemp -d "$OUT/.build-release.XXXXXX")"
cleanup() { rm -rf "$stage"; }
trap cleanup EXIT

(
    cd "$ROOT/server"
    go build -trimpath -ldflags="$ldflags" -o "$stage/yiban-server$exe" ./cmd/server
    go build -trimpath -ldflags="$ldflags" -o "$stage/yiban-cli$exe" ./cmd/cli
)

# UI 产物入签名清单（P1-6 ③）：清单落在发布 staging（与二进制同居），
# 由 make-manifest.sh 以 basename 收录；绝不写进 ui-dist 本身。
if [ -n "$UI_DIST" ]; then
    bash "$ROOT/scripts/release/make-ui-sums.sh" "$UI_DIST" "$stage/ui-SHA256SUMS" \
        || die "cannot generate ui checksums in $stage"
fi

# 全部成功后再搬入 outdir；无 UI 的构建清掉旧清单，避免复用目录时
# 让上一次的 ui-SHA256SUMS 混进新 manifest。
mv -f "$stage/yiban-server$exe" "$OUT/yiban-server$exe"
mv -f "$stage/yiban-cli$exe" "$OUT/yiban-cli$exe"
if [ -n "$UI_DIST" ]; then
    mv -f "$stage/ui-SHA256SUMS" "$OUT/ui-SHA256SUMS"
else
    rm -f "$OUT/ui-SHA256SUMS"
fi

echo "build-release: version=$ver commit=$commit build_time=$build_time"
echo "build-release: wrote $OUT/yiban-server$exe"
echo "build-release: wrote $OUT/yiban-cli$exe"
if [ -n "$UI_DIST" ]; then
    echo "build-release: wrote $OUT/ui-SHA256SUMS"
    echo "build-release: manifest: bash $ROOT/scripts/release/make-manifest.sh $ver $OUT/yiban-server$exe $OUT/yiban-cli$exe $OUT/ui-SHA256SUMS"
fi
