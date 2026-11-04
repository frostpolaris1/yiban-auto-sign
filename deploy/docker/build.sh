#!/usr/bin/env bash
# ============================================================
# build.sh —— 容器镜像发布构建入口（2026-11-05 版本批）
# ============================================================
# 用途：从仓库根 VERSION 读产品版本，给 deploy/docker/Dockerfile
#   传齐三个 build-arg（BUILD_VERSION/BUILD_COMMIT/BUILD_TIME），
#   构建生产镜像。这是 docker compose build 之外、带版本身份的
#   正式构建入口；裸 `docker build` 不传参时二进制显性自报 dev。
#
# 用法（在任意目录执行）：
#   bash deploy/docker/build.sh <image-tag>
#   例：bash deploy/docker/build.sh "yiban-server:v$(cat VERSION)"
#
# 行为口径：
#   - VERSION 缺失 / 非 X.Y.Z / 多行 ⇒ exit 2 并点名（与
#     server/scripts/build-release.sh 同一口径）；
#   - commit 取 git rev-parse --short HEAD；取不到 ⇒ exit 2；
#   - 一律不注入分支名；
#   - 三个值以 --build-arg 传给 Dockerfile，Docker 里是构建期参数，
#     不进运行时配置名册（compose.env 不含它们）；
#   - docker CLI 缺失 ⇒ exit 2 并点名。
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

usage() {
    echo "usage: bash deploy/docker/build.sh <image-tag>" >&2
}

die() {
    echo "deploy/docker/build.sh: $1" >&2
    exit 2
}

[ "$#" -eq 1 ] || { usage; die "exactly one argument (image-tag) is required"; }
TAG="$1"

# 版本单一来源：与 server/scripts/build-release.sh 同口径（单行、换行结尾、X.Y.Z）。
VFILE="$ROOT/VERSION"
[ -f "$VFILE" ] || die "VERSION file missing: $VFILE (Go-line product version single source)"
vlines="$(wc -l < "$VFILE" | tr -d ' ')"
[ "$vlines" = "1" ] || die "$VFILE must be exactly one newline-terminated line, found $vlines"
ver="$(head -n 1 "$VFILE" | tr -d '\r')"
printf '%s' "$ver" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
    || die "$VFILE content is not X.Y.Z semver: '$ver'"

commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)" \
    || die "$ROOT is not a usable git checkout; release builds must carry a real commit"
build_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

command -v docker >/dev/null 2>&1 || die "docker CLI not in PATH; image build DID NOT run"

export BUILD_VERSION="$ver"
export BUILD_COMMIT="$commit"
export BUILD_TIME="$build_time"

echo "build.sh: version=$BUILD_VERSION commit=$BUILD_COMMIT build_time=$BUILD_TIME tag=$TAG"

docker build \
    -f "$ROOT/deploy/docker/Dockerfile" \
    --build-arg BUILD_VERSION="$BUILD_VERSION" \
    --build-arg BUILD_COMMIT="$BUILD_COMMIT" \
    --build-arg BUILD_TIME="$BUILD_TIME" \
    -t "$TAG" \
    "$ROOT"
