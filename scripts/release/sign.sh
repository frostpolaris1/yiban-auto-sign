#!/usr/bin/env bash
# ============================================================
# scripts/release/sign.sh — 用 Ed25519 私钥对清单签名（openssl 3）。
#
# 用法：
#   bash scripts/release/sign.sh --key PRIVATE.pem --in manifest.json --out manifest.sig
#   私钥路径也可经环境变量 YIBAN_SIGNING_KEY 给出（--key 优先）。
#
# 签名对象是清单文件的字节本身（Ed25519 纯签名，-rawin）。
# 验签对应 scripts/release/verify.sh（--pubkey + --manifest + --signature）。
#
# 私钥红线（docs/dev/release-signing.md）：
#   - 私钥永不入库、不与服务器同机明文存放。
#   - 本脚本只读私钥文件，不生成、不打印、不复制私钥内容。
#
# 退出码：0 签名成功；1 openssl 签名失败；2 用法或环境错误（含 openssl 缺失）。
# ============================================================
set -euo pipefail

usage() {
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

KEY="${YIBAN_SIGNING_KEY:-}"
IN=""
OUT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --key) KEY="${2:?}"; shift 2 ;;
        --in) IN="${2:?}"; shift 2 ;;
        --out) OUT="${2:?}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "sign: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "$KEY" ] || { echo "sign: private key required (--key or YIBAN_SIGNING_KEY)" >&2; usage >&2; exit 2; }
[ -n "$IN" ] || { echo "sign: --in is required" >&2; usage >&2; exit 2; }
[ -n "$OUT" ] || { echo "sign: --out is required" >&2; usage >&2; exit 2; }
[ -f "$IN" ] || { echo "sign: manifest not found: $IN" >&2; exit 2; }
[ -r "$KEY" ] || { echo "sign: private key not readable: $KEY" >&2; exit 2; }

# --in 与 --out 同文件会先截断再签名，等于毁掉输入；直接拒绝。
in_real="$(realpath -m -- "$IN" 2>/dev/null || printf '%s' "$IN")"
out_real="$(realpath -m -- "$OUT" 2>/dev/null || printf '%s' "$OUT")"
if [ "$in_real" = "$out_real" ]; then
    echo "sign: --in and --out must differ (would truncate the manifest)" >&2
    exit 2
fi
# --out 指向私钥会覆盖私钥（2026-10-29 审查 P3）：直接拒绝。
key_real="$(realpath -m -- "$KEY" 2>/dev/null || printf '%s' "$KEY")"
if [ "$out_real" = "$key_real" ]; then
    echo "sign: --out must not be the private key (would overwrite it)" >&2
    exit 2
fi

command -v openssl >/dev/null 2>&1 || {
    echo "sign: openssl not found; signing DID NOT run" >&2
    exit 2
}

# -rawin：Ed25519 是纯签名算法，openssl 3 要求显式传原始消息。
# openssl 自己的报错保持 stderr 直通。先写临时件、成功再原子替换：
# 失败时既有输出（上一次的签名）原样保留，不得毁掉。
TMP_OUT="$OUT.tmp.$$"
if ! openssl pkeyutl -sign -inkey "$KEY" -rawin -in "$IN" -out "$TMP_OUT"; then
    echo "sign: openssl signing FAILED; existing output left untouched: $OUT" >&2
    rm -f -- "$TMP_OUT"
    exit 1
fi
mv -f -- "$TMP_OUT" "$OUT"
echo "sign: signed $IN -> $OUT"
