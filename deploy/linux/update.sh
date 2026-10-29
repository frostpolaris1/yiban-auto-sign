#!/usr/bin/env bash
# ============================================================
# deploy/linux/update.sh — Go 服务端升级器。
#
# 顺序（派单 P7）：放置新二进制 → 停服务 → 跑迁移 → 换二进制 →
# 起服务 → /healthz；迁移失败回滚为「旧件继续运行」；
# 健康检查失败回滚旧二进制并报错。回滚后仍不健康退出 2。
#
# 用法：
#   sudo bash deploy/linux/update.sh --server-bin NEW [--cli-bin NEW]
#   （--cli-bin 与服务二进制同版本发布；只给 --server-bin 时
#     迁移用已安装的 yiban-cli。）
#
# 签名发布物（可选，docs/dev/release-signing.md）：
#   sudo bash deploy/linux/update.sh --server-bin NEW \
#       --manifest manifest.json --signature manifest.sig --pubkey release-pubkey.pem \
#       [--expect-version 0.7.0]
#   三件必须同时给或同时不给。给了 → 在任何文件替换之前先钉公钥指纹、
#   再跑 scripts/release/verify.sh 验签，并把清单里的 sha256 逐一与本地新件
#   对账（server 必须，cli 给了也算）；任一失败即非 0 退出，不碰旧件。
#   没给 → 行为不变，但打印一行显性 WARN（跳过 ≠ 绿）。
#
# 信任锚（P0）：签名只证明清单出自某把私钥，不证明那把钥是发布方的。
# update.sh 先钉公钥指纹，再验签：
#   YIBAN_RELEASE_PUBKEY_SHA256（环境），或
#   <安装根>/release-pubkey.sha256（管理员安装时写入，一行 64 位十六进制）
# 两者都缺 → exit 2（fail-closed）；指纹不匹配 → exit 2。
#
# 并发：起手拿 flock（<安装位>.lock）。两次并发共用 .new/.bak，交错写会
# 覆盖回滚件；拿不到锁即 exit 2。
#
# 干跑钩子：YIBAN_INSTALL_ROOT=前缀 SYSTEMCTL=命令覆盖（与 install.sh 同义）。
#
# 退出码：0 成功；1 迁移失败或回滚成功（旧件在跑）或签名/对账/版本校验不通过；
#         2 用法错误（含三件只给一件、信任锚缺失或指纹不符）或回滚后仍不健康。
# 已知残余风险：迁移一旦应用不会随二进制回滚（没有 down 迁移）；
# 旧二进制须能向前兼容新 schema。迁移都按向后兼容发布。
# ============================================================
set -euo pipefail

ROOT="${YIBAN_INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
UNIT="yiban.service"

# 仓库根由脚本自身位置推出：名册缺省取 <repo>/config/registry.json。
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

SERVER_BIN=""
CLI_BIN=""
REGISTRY=""
MANIFEST=""
SIGNATURE=""
PUBKEY=""
EXPECT_VERSION=""
while [ $# -gt 0 ]; do
    case "$1" in
        --server-bin) SERVER_BIN="${2:?}"; shift 2 ;;
        --cli-bin) CLI_BIN="${2:?}"; shift 2 ;;
        --registry) REGISTRY="${2:?}"; shift 2 ;;
        --manifest) MANIFEST="${2:?}"; shift 2 ;;
        --signature) SIGNATURE="${2:?}"; shift 2 ;;
        --pubkey) PUBKEY="${2:?}"; shift 2 ;;
        --expect-version) EXPECT_VERSION="${2:?}"; shift 2 ;;
        *) echo "update: unknown argument: $1" >&2; exit 2 ;;
    esac
done

# 三件同给同不给：半给是操作员笔误，静默跳过校验等于没校验。
if [ -n "$MANIFEST" ] || [ -n "$SIGNATURE" ] || [ -n "$PUBKEY" ]; then
    if [ -z "$MANIFEST" ] || [ -z "$SIGNATURE" ] || [ -z "$PUBKEY" ]; then
        echo "update: --manifest/--signature/--pubkey must be given together (got manifest=${MANIFEST:+yes} signature=${SIGNATURE:+yes} pubkey=${PUBKEY:+yes})" >&2
        exit 2
    fi
fi
# 版本期望只有在验签路径里才有意义；单独给是用法错误。
if [ -n "$EXPECT_VERSION" ] && [ -z "$MANIFEST" ]; then
    echo "update: --expect-version requires --manifest/--signature/--pubkey" >&2
    exit 2
fi

[ -n "$SERVER_BIN" ] || { echo "update: --server-bin is required" >&2; exit 2; }
[ -x "$SERVER_BIN" ] || { echo "update: new server binary missing: $SERVER_BIN" >&2; exit 2; }
# 存在性检查前移（P3）：否则签名模式下缺 --cli-bin 会被归类成验签失败（1）。
if [ -n "$CLI_BIN" ] && [ ! -x "$CLI_BIN" ]; then
    echo "update: new cli binary missing: $CLI_BIN" >&2
    exit 2
fi
REGISTRY="${REGISTRY:-$REPO_ROOT/config/registry.json}"
[ -f "$REGISTRY" ] || { echo "update: config registry missing: $REGISTRY" >&2; exit 2; }

dest() { printf '%s%s' "$ROOT" "$1"; }
SERVER_DEST="$(dest /usr/local/bin/yiban-server)"
CLI_DEST="$(dest /usr/local/bin/yiban-cli)"
SERVER_NEW="$SERVER_DEST.new"
BAK="$SERVER_DEST.bak"

[ -x "$SERVER_DEST" ] || { echo "update: no installed server at $SERVER_DEST; run install.sh first" >&2; exit 2; }

# 并发互斥（P3）：两次并发共用 .new/.bak，交错写会覆盖回滚件。
LOCK_FILE="$SERVER_DEST.lock"
if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        echo "update: another update is running (lock: $LOCK_FILE); run one update at a time" >&2
        exit 2
    fi
else
    echo "update: WARNING: flock not available; concurrent updates are not prevented" >&2
fi

# 签名发布物校验（可选）。放在任何文件替换之前：失败即退出，旧件原样。
VERIFY_SH="$REPO_ROOT/scripts/release/verify.sh"
if [ -n "$MANIFEST" ]; then
    # 1) 信任锚：公钥指纹必须与钉住值一致。缺锚或不符一律 exit 2，
    #    绝不能把「操作员随手给的一把钥」当成发布方的钥。
    anchor_fail() {
        echo "update: signing verification FAILED (step: trust-anchor) — $1" >&2
        exit 2
    }
    PUBKEY_PIN=""
    PIN_SOURCE=""
    if [ -n "${YIBAN_RELEASE_PUBKEY_SHA256:-}" ]; then
        PUBKEY_PIN="$YIBAN_RELEASE_PUBKEY_SHA256"
        PIN_SOURCE="env YIBAN_RELEASE_PUBKEY_SHA256"
    else
        PIN_FILE="$(dest /release-pubkey.sha256)"
        if [ -f "$PIN_FILE" ]; then
            PUBKEY_PIN="$(head -n1 "$PIN_FILE" 2>/dev/null || true)"
            PIN_SOURCE="$PIN_FILE"
        fi
    fi
    [ -n "$PUBKEY_PIN" ] || anchor_fail \
        "no pinned public key fingerprint; set YIBAN_RELEASE_PUBKEY_SHA256 or write $(dest /release-pubkey.sha256)"
    # sha256sum 输出形如 `<hex>  <file>`；只取第一个字段，容忍 CRLF 与大写。
    PUBKEY_PIN="$(printf '%s' "$PUBKEY_PIN" | tr -d '\r' | tr 'A-F' 'a-f' | awk '{print $1}')"
    case "$PUBKEY_PIN" in
        ''|*[!0-9a-f]*) anchor_fail "pinned fingerprint is not lowercase hex (source: $PIN_SOURCE)" ;;
    esac
    [ "${#PUBKEY_PIN}" -eq 64 ] || anchor_fail \
        "pinned fingerprint is not 64 hex chars (source: $PIN_SOURCE)"
    command -v sha256sum >/dev/null 2>&1 || anchor_fail \
        "sha256sum not found; fingerprint DID NOT verify"
    PUBKEY_GOT="$(sha256sum -- "$PUBKEY" 2>/dev/null)" || anchor_fail \
        "cannot hash public key: $PUBKEY"
    PUBKEY_GOT="${PUBKEY_GOT%% *}"
    if [ "$PUBKEY_GOT" != "$PUBKEY_PIN" ]; then
        anchor_fail "public key fingerprint $PUBKEY_GOT does not match pinned $PUBKEY_PIN (source: $PIN_SOURCE)"
    fi

    echo "update: verifying signed release manifest"
    if [ ! -f "$VERIFY_SH" ]; then
        echo "update: signing verification FAILED (step: signing) — verifier missing: $VERIFY_SH" >&2
        exit 1
    fi
    if ! bash "$VERIFY_SH" --pubkey "$PUBKEY" --manifest "$MANIFEST" --signature "$SIGNATURE"; then
        echo "update: signing verification FAILED (step: signing) — refusing to touch installed files" >&2
        exit 1
    fi
    # 2) 防降级：清单版本必须等于操作员期望版本（已签名旧包的重放由此被挡）。
    if [ -n "$EXPECT_VERSION" ]; then
        GOT_VERSION="$(grep -o '"version":"[^"]*"' "$MANIFEST" 2>/dev/null \
            | head -n1 | sed 's/.*"version":"//; s/"$//' || true)"
        if [ -z "$GOT_VERSION" ]; then
            echo "update: signing verification FAILED (step: signing) — manifest has no version field" >&2
            exit 1
        fi
        if [ "$GOT_VERSION" != "$EXPECT_VERSION" ]; then
            echo "update: signing verification FAILED (step: signing) — manifest version $GOT_VERSION does not match --expect-version $EXPECT_VERSION" >&2
            exit 1
        fi
    fi
    # 清单 sha256 与本地新件逐一对账。只对账本次要替换的文件：
    # server 必须在清单里；cli 给了 --cli-bin 才对账。清单可以带其他
    # 条目（全量发布清单），不在本机替换范围内的不拦。
    manifest_hash() {
        # 单行 JSON、键序固定（make-manifest.sh 保证）；grep -o 逐匹配提取，
        # 不用跨条目的贪婪 sed。name 只含 [A-Za-z0-9._-]。
        # 末尾 || true 兜住两种非零：无匹配（rc=1）与 head 提前关管的 SIGPIPE。
        # 缺条目的失败由调用方判空后显性点名，不能让 set -e 静默退出。
        grep -o "\"name\":\"$1\",\"sha256\":\"[0-9a-f]\{64\}\"" "$MANIFEST" 2>/dev/null \
            | head -n1 | sed "s/.*\"sha256\":\"//; s/\"\$//" || true
    }
    local_check() {
        # local_check <name> <file>
        want="$(manifest_hash "$1")"
        if [ -z "$want" ]; then
            echo "update: signing verification FAILED (step: signing) — manifest has no entry for $1" >&2
            return 1
        fi
        got="$(sha256sum -- "$2")" || {
            echo "update: signing verification FAILED (step: signing) — sha256sum failed on $2" >&2
            return 1
        }
        got="${got%% *}"
        if [ "$got" != "$want" ]; then
            echo "update: signing verification FAILED (step: signing) — $1 sha256 mismatch: manifest=$want local=$got" >&2
            return 1
        fi
    }
    if ! local_check "yiban-server" "$SERVER_BIN"; then
        exit 1
    fi
    if [ -n "$CLI_BIN" ] && ! local_check "yiban-cli" "$CLI_BIN"; then
        exit 1
    fi
    echo "update: signing verification ok"
else
    echo "update: WARNING: no --manifest/--signature/--pubkey given; skipping release verification (unsigned update)" >&2
fi

# 迁移要用新版的嵌入迁移集：给了 --cli-bin 就暂存新 cli，否则用已装的。
STAGED_CLI=""
if [ -n "$CLI_BIN" ]; then
    install -m 0755 "$CLI_BIN" "$CLI_DEST.new"
    STAGED_CLI="$CLI_DEST.new"
fi
install -m 0755 "$SERVER_BIN" "$SERVER_NEW"

# 名册随二进制一起升级：新版本可能新增配置键，
# 只换二进制不换名册会让 doctor 的未登记键体检过时。
# 落在停服务之前：失败时服务仍在跑旧件。
mkdir -p "$(dest /etc/yiban)"
install -m 0644 "$REGISTRY" "$(dest /etc/yiban/registry.json)"
if [ -z "$ROOT" ]; then
    chown root:root "$(dest /etc/yiban/registry.json)"
fi

# 载入 /etc/yiban/.env 的 KEY=VALUE（systemd 同源），DSN 由此而来。
# **不** source：DSN 含 `&` 或 `#` 时 shell 会切开赋值，变量落进子 shell
# 不外泄，迁移收到空 DSN。本函数只做四件事：跳过空行与 `#` 注释、
# 取第一个 `=` 分割键值、去掉值两端的成对单/双引号、只导出合法键名。
load_env_file() {
    local file="$1" line key value
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        case "$line" in
            ''|'#'*) continue ;;
        esac
        case "$line" in
            *=*) ;;
            *) continue ;;
        esac
        key="${line%%=*}"
        value="${line#*=}"
        # 键名必须匹配 ^[A-Za-z_][A-Za-z0-9_]*$；其余行不是配置项。
        case "$key" in
            ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;;
        esac
        case "$value" in
            \"*\") value="${value:1:${#value}-2}" ;;
            \'*\') value="${value:1:${#value}-2}" ;;
        esac
        export "$key=$value"
    done < "$file"
}

ENV_FILE="$(dest /etc/yiban/.env)"
load_env_file "$ENV_FILE"

echo "update: stopping $UNIT"
"$SYSTEMCTL" stop "$UNIT"

MIGRATE_BIN="${STAGED_CLI:-$CLI_DEST}"
if [ ! -x "$MIGRATE_BIN" ]; then
    echo "update: no cli to run migrate at $MIGRATE_BIN; restore service" >&2
    "$SYSTEMCTL" start "$UNIT"
    exit 1
fi
if ! "$MIGRATE_BIN" migrate; then
    echo "update: migrate FAILED; keeping the old binary and starting the service back" >&2
    "$SYSTEMCTL" start "$UNIT"
    exit 1
fi

# 换二进制：旧件留 .bak 供回滚。
cp -p "$SERVER_DEST" "$BAK"
mv "$SERVER_NEW" "$SERVER_DEST"
if [ -n "$STAGED_CLI" ]; then
    mv "$CLI_DEST.new" "$CLI_DEST"
fi

echo "update: starting $UNIT"
"$SYSTEMCTL" start "$UNIT"

# 健康检查；失败回滚旧二进制再试一次。
ADDR="${YIBAN_HTTP_ADDR:-127.0.0.1:47183}"
RETRIES="${YIBAN_HEALTH_RETRIES:-10}"
INTERVAL="${YIBAN_HEALTH_RETRY_INTERVAL:-1}"

probe() {
    local i=1
    while [ "$i" -le "$RETRIES" ]; do
        if "$CLI_DEST" healthcheck --addr "$ADDR" --timeout 2s; then
            return 0
        fi
        sleep "$INTERVAL"
        i=$((i + 1))
    done
    return 1
}

if probe; then
    echo "update complete: healthz ok at $ADDR (old binary kept at $BAK)"
    exit 0
fi

echo "update: healthz failed after swap; rolling back to $BAK" >&2
# 必须先 stop：旧进程仍持有新二进制，对 active 单元 start 是 no-op，
# 只换文件不重启则回滚后的「旧件在跑」是假的。
"$SYSTEMCTL" stop "$UNIT"
mv "$BAK" "$SERVER_DEST"
"$SYSTEMCTL" start "$UNIT"
if probe; then
    echo "update: rolled back; old binary is serving again" >&2
    exit 1
fi
echo "update: rollback did not restore health; manual action required" >&2
exit 2
