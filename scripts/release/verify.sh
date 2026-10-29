#!/usr/bin/env bash
# ============================================================
# scripts/release/verify.sh — 验证发布物清单的 Ed25519 签名（openssl 3）。
#
# 用法：
#   bash scripts/release/verify.sh --pubkey PUB.pem --manifest manifest.json --signature manifest.sig
#
# 公钥与清单、签名都用显式路径传入，本脚本不读任何默认位置。
# **公钥不入仓库**：仓库只收指纹示例（deploy/linux/release-pubkey.sha256.example）
# 与文档。信任锚由调用方钉住：
#   - update.sh：读 YIBAN_RELEASE_PUBKEY_SHA256 或 <安装根>/release-pubkey.sha256，
#     先比对指纹再调本脚本；
#   - scripts/release-check.sh：读同一个环境变量，或 --pubkey-sha256-file
#     指定的文件；该文件必须在发布件目录之外（发布门只信外部锚）。
# 本脚本只做一件事：用给定的公钥验签；它不判断这把公钥是否可信任。
# 单独运行本脚本不构成发布校验（见 docs/dev/release-signing.md §2）。
#
# 口径（AGENTS §30 跳过≠绿）：
#   rc=0  签名验证通过（只证明清单字节出自私钥持有者；
#         清单内 sha256 与本地文件的对账由调用方做，update.sh 已接）。
#   rc=1  签名验证不通过（清单/签名/公钥不匹配或文件损坏）。
#   rc=2  用法或环境错误——openssl 缺失、文件缺失、公钥不可读。
#         此时验证 DID NOT run；调用方必须把非 0 一律当失败处理。
# ============================================================
set -euo pipefail

usage() {
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

PUB=""
MANIFEST=""
SIG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --pubkey) PUB="${2:?}"; shift 2 ;;
        --manifest) MANIFEST="${2:?}"; shift 2 ;;
        --signature) SIG="${2:?}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "verify: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "$PUB" ] || { echo "verify: --pubkey is required" >&2; usage >&2; exit 2; }
[ -n "$MANIFEST" ] || { echo "verify: --manifest is required" >&2; usage >&2; exit 2; }
[ -n "$SIG" ] || { echo "verify: --signature is required" >&2; usage >&2; exit 2; }
[ -f "$MANIFEST" ] || { echo "verify: manifest not found: $MANIFEST" >&2; exit 2; }
[ -f "$SIG" ] || { echo "verify: signature not found: $SIG" >&2; exit 2; }
[ -r "$PUB" ] || { echo "verify: public key not readable: $PUB" >&2; exit 2; }

command -v openssl >/dev/null 2>&1 || {
    echo "verify: openssl not found; verification DID NOT run" >&2
    exit 2
}

if openssl pkeyutl -verify -pubin -inkey "$PUB" -rawin -in "$MANIFEST" -sigfile "$SIG" >/dev/null 2>&1; then
    echo "verify: signature ok (manifest: $MANIFEST)"
    exit 0
fi
echo "verify: signature verification FAILED (manifest: $MANIFEST)" >&2
exit 1
