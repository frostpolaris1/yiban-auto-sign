#!/usr/bin/env bash
# ============================================================
# scripts/release/make-ui-sums.sh —— UI 发布物 sha256 清单（P1-6 ③）
# ============================================================
# 用途：对 <ui-dir>（ui/dist 或发布 staging 里的 ui）逐文件生成 sha256
#   清单，交给 scripts/release/make-manifest.sh 作为第三件收进签名清单。
#
# 位置约束：清单文件必须写在 ui 目录**之外**（与 dist 平级）。update.sh
#   的激活是整目录换名（cp -R "$UI_DIR/." ...），清单若落在 dist 内会随
#   页面一起发布到站点根。
#
# 用法：
#   bash scripts/release/make-ui-sums.sh <ui-dir> <out-file>
#
# 输出格式：GNU coreutils sha256sum 的文本格式（<64hex><两个空格><相对路径>），
#   路径相对 <ui-dir>，因此可以直接
#     (cd <ui-dir> && sha256sum -c <out-file>)
#   校验。排序固定 LC_ALL=C（字节序），同一棵树在任何机器上产同一份清单。
#
# 口径：
#   - <ui-dir> 本身不能是软链、必须是目录、必须有 index.html（站点入口）；
#   - 只收普通文件；软链、FIFO、设备、套接字一律 exit 1 —— 发布物形状必须
#     确定，否则 update.sh 的「清单外文件」检查失去意义；
#   - 清单内路径必须是 <ui-dir> 内的相对路径：绝对路径、.. 段、换行一律
#     exit 1（会被 sha256sum -c 解释到目录外）；
#   - <out-file> 不得落在 <ui-dir> 内（会把自己算进去）；
#   - 拒绝时不留半成品：先写同目录临时文件，成功后才 mv 到目标。
#
# 退出码：0 成功；1 UI 内容不合规或生成失败；2 用法错误。
# ============================================================
set -euo pipefail

usage() {
    echo "usage: bash scripts/release/make-ui-sums.sh <ui-dir> <out-file>" >&2
}

die() {
    echo "make-ui-sums: $1" >&2
    exit "${2:-1}"
}

[ "$#" -eq 2 ] || { usage; exit 2; }
UI_DIR="$1"
OUT="$2"

[ -n "$UI_DIR" ] || { usage; exit 2; }
[ -n "$OUT" ] || { usage; exit 2; }

if [ -L "$UI_DIR" ]; then
    die "ui dir must not be a symlink: $UI_DIR" 2
fi
[ -d "$UI_DIR" ] || die "ui dir is not a directory: $UI_DIR" 2
if [ -L "$UI_DIR/index.html" ]; then
    die "ui index.html must be a regular file, not a symlink: $UI_DIR/index.html" 2
fi
[ -f "$UI_DIR/index.html" ] || die "ui dir has no index.html: $UI_DIR" 2

case "$(basename -- "$OUT")" in
    ''|*[!A-Za-z0-9._-]*) die "output basename must match [A-Za-z0-9._-]+: $OUT" 2 ;;
esac

UI_ABS="$(cd "$UI_DIR" && pwd -P)" || die "cannot resolve ui dir: $UI_DIR" 2
OUT_PARENT="$(dirname -- "$OUT")"
[ -d "$OUT_PARENT" ] || die "output directory does not exist: $OUT_PARENT" 2
OUT_PARENT_ABS="$(cd "$OUT_PARENT" && pwd -P)" || die "cannot resolve output directory: $OUT_PARENT" 2
OUT_ABS="$OUT_PARENT_ABS/$(basename -- "$OUT")"

case "$OUT_ABS" in
    "$UI_ABS"/*) die "output must live outside the ui dir (it would ship itself): $OUT_ABS" 2 ;;
esac

tmp="$(mktemp "$OUT_PARENT_ABS/.ui-sums.XXXXXX")" || die "cannot create a temporary file in $OUT_PARENT_ABS" 1
trap 'rm -f -- "$tmp"' EXIT

count=0
: > "$tmp"
while IFS= read -r path; do
    rel="${path#"$UI_ABS"/}"
    if [ -L "$path" ]; then
        die "refusing symlink in ui dir: $rel" 1
    fi
    if [ -d "$path" ]; then
        continue
    fi
    if [ ! -f "$path" ]; then
        die "refusing non-regular file in ui dir: $rel" 1
    fi
    case "$rel" in
        ''|/*|..|../*|*/..|*/../*) die "refusing path escape in ui dir: $rel" 1 ;;
        *$'\n'*|*$'\r'*) die "refusing newline in ui path: $rel" 1 ;;
    esac
    sum_line="$(sha256sum -- "$path")" || die "sha256sum failed: $rel" 1
    hash="${sum_line%% *}"
    printf '%s  %s\n' "$hash" "$rel" >> "$tmp"
    count=$((count + 1))
done < <(find "$UI_ABS" -mindepth 1 -print | LC_ALL=C sort)

[ "$count" -gt 0 ] || die "ui dir has no files: $UI_ABS" 1
chmod 0644 "$tmp" || die "cannot chmod $tmp" 1
mv -f -- "$tmp" "$OUT_ABS" || die "cannot write $OUT_ABS" 1
trap - EXIT
echo "make-ui-sums: wrote $OUT_ABS ($count files)"
