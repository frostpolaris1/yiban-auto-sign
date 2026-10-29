#!/usr/bin/env bash
# ============================================================
# scripts/release/make-manifest.sh — 生成发布物清单 manifest.json。
#
# 用法：
#   bash scripts/release/make-manifest.sh <version> <file> [file...]
#   bash scripts/release/make-manifest.sh 0.7.0 dist/yiban-server dist/yiban-cli \
#       > release/manifest.json
#
# 输出（单行 JSON + 结尾换行；键序固定，可复现字节）：
#   {"version":"0.7.0","files":[{"name":"yiban-server","sha256":"<64hex>"},...]}
#
# 口径：
#   - name 取文件基名。清单消费者（update.sh、verify.sh）按基名对账。
#   - sha256 用 sha256sum（GNU coreutils）。
#   - version 与基名只允许 [A-Za-z0-9._-]；其余字符拒绝。
#     手写 JSON 转义是缺陷来源，本脚本不做转义，直接拒绝。
#
# 退出码：0 成功；1 文件不可读或 sha256 失败；2 用法错误。
# ============================================================
set -euo pipefail

usage() {
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

[ $# -ge 2 ] || { echo "make-manifest: need <version> and at least one file" >&2; usage >&2; exit 2; }
VERSION="$1"
shift

case "$VERSION" in
    *[!A-Za-z0-9._-]*) echo "make-manifest: version has characters outside [A-Za-z0-9._-]: $VERSION" >&2; exit 2 ;;
esac
[ -n "$VERSION" ] || { echo "make-manifest: version is empty" >&2; exit 2; }

command -v sha256sum >/dev/null 2>&1 || {
    echo "make-manifest: sha256sum not found; verification DID NOT run" >&2
    exit 1
}

entries=""
seen=""
for f in "$@"; do
    base="$(basename -- "$f")"
    case "$base" in
        *[!A-Za-z0-9._-]*) echo "make-manifest: basename has characters outside [A-Za-z0-9._-]: $base" >&2; exit 2 ;;
    esac
    # 同名条目会让消费者拿到不确定的哈希，直接拒绝。
    case ",$seen," in
        *,"$base",*) echo "make-manifest: duplicate basename: $base" >&2; exit 2 ;;
    esac
    seen="$seen,$base"
    if [ ! -f "$f" ]; then
        echo "make-manifest: not a regular file: $f" >&2
        exit 1
    fi
    sum="$(sha256sum -- "$f")" || { echo "make-manifest: sha256sum failed on $f" >&2; exit 1; }
    sum="${sum%% *}"
    case "$sum" in
        [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
        *) echo "make-manifest: sha256sum output not 64hex for $f: $sum" >&2; exit 1 ;;
    esac
    if [ -n "$entries" ]; then entries="$entries,"; fi
    entries="$entries{\"name\":\"$base\",\"sha256\":\"$sum\"}"
done

printf '{"version":"%s","files":[%s]}\n' "$VERSION" "$entries"
