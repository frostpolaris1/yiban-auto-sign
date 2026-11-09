#!/usr/bin/env bash
# ============================================================
# scripts/contract-gate.sh — OpenAPI 契约门。
#
# 两道检查：
#   1. spectral lint：server/contracts/openapi.yaml 对 spectral:oas 规则集；
#      warning 及以上即失败。当前 0 发现，所以不需要基线豁免文件。
#   2. oasdiff breaking：与 --base 指向的 git ref 里的同一份 spec 相比；
#      ERR 级破坏性变更新增即失败（删除端点、改响应字段类型等）。
#      豁免走 `server/contracts/.breaking-allowlist.tsv`：只按
#      `id<TAB>method<TAB>path<TAB>批次` 三元组精确放行**单条 finding**，
#      同一检查的其余端点、以及全部其它检查不受影响。新增行必须写批次链接，
#      语义是「该端点的该变更已被当批评审接受」，不是关闭规则——
#      本项目的响应枚举闭集按批次演进（渠道词表 2d 扩 webhook），
#      契约唯一消费者是仓内生成物（oapi.gen.go / schema.d.ts 同批 --check 钉住，
#      ui tsc 同批咬合），不存在第三方穷举客户端，enum-additive 不构成现实破坏面。
#
# 口径：
#   - 缺工具 = 环境错误，exit 2（跳过 ≠ 绿）。
#   - 不给 --base 时 oasdiff 这一半显式打印 SKIP 行：推送新分支等没有可比底本，
#     不能默认绿，也不能默认红。
#   - 给了 --base 但取不到 base spec = 环境错误 exit 2：调用方说要比，却比不了。
#   - base spec 从 git 对象直接读出到临时目录，不切换工作树。
#   - 工具版本：spectral 由 server/contracts/tools/package-lock.json 钉住；
#     oasdiff 由调用方安装（CI 钉 v1.35.0，见 .github/workflows/ci.yml）。
#   - spectral 的 postinstall 脚本不发网络请求：安装用 --ignore-scripts。
# ============================================================
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SELF_DIR/.." && pwd)"

SPEC="server/contracts/openapi.yaml"
RULESET="server/contracts/.spectral.yaml"
SPECTRAL="${SPECTRAL:-$ROOT/server/contracts/tools/node_modules/.bin/spectral}"
OASDIFF="${OASDIFF:-oasdiff}"

BASE=""

die_env() {
    echo "contract-gate: $*" >&2
    exit 2
}

usage() {
    echo "usage: bash scripts/contract-gate.sh [--base <git-ref>]" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --base)
            [ "$#" -ge 2 ] || usage
            BASE="$2"
            shift 2
            ;;
        --base=*)
            BASE="${1#--base=}"
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            usage
            ;;
    esac
done

cd "$ROOT"

command -v node >/dev/null 2>&1 || die_env "node not found; spectral needs it"

if [ ! -x "$SPECTRAL" ]; then
    die_env "spectral not found at $SPECTRAL; install it with: (cd server/contracts/tools && npm ci --ignore-scripts)"
fi

echo "== spectral lint（warning 及以上）=="
"$SPECTRAL" lint "$SPEC" -r "$RULESET" --fail-severity warn

if [ -z "$BASE" ]; then
    line="contract-gate SKIP (oasdiff: no --base given; breaking-diff was not run)"
    echo "$line"
    if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
        echo "$line" >> "$GITHUB_STEP_SUMMARY"
    fi
    echo "contract-gate: ok"
    exit 0
fi

command -v "$OASDIFF" >/dev/null 2>&1 || die_env "oasdiff not found; install it with: go install github.com/oasdiff/oasdiff@v1.35.0"

git rev-parse --verify "$BASE^{commit}" >/dev/null 2>&1 ||
    die_env "base ref '$BASE' is not resolvable; the gate cannot compare"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

git show "$BASE:$SPEC" > "$TMP_DIR/base.yaml" 2>/dev/null ||
    die_env "$SPEC does not exist at base ref '$BASE'; nothing to compare"

echo "== oasdiff breaking（$BASE -> HEAD，ERR 级，allowlist 过滤后）=="
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || die_env "python3 not found; the breaking-allowlist filter needs it"
FINDINGS="$TMP_DIR/findings.json"
"$OASDIFF" breaking --format json "$TMP_DIR/base.yaml" "$SPEC" > "$FINDINGS"
ALLOWLIST="${BREAKS_ALLOWLIST:-$ROOT/server/contracts/.breaking-allowlist.tsv}"
"$PY" - "$ALLOWLIST" "$FINDINGS" <<'PYEOF'
import json, sys
allow_path, findings_path = sys.argv[1], sys.argv[2]
allow = set()
try:
    with open(allow_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                allow.add((parts[0], parts[1].lower(), parts[2]))
except FileNotFoundError:
    pass
remaining = []
for x in json.load(open(findings_path, encoding="utf-8")):
    key = (x.get("id", ""), str(x.get("operation", "")).lower(), x.get("path", ""))
    if key in allow:
        print("ALLOWED\t[%s] %s %s" % key, file=sys.stderr)
        continue
    remaining.append(x)
if remaining:
    print("%d 个未获准的 ERR 级破坏性变更：" % len(remaining), file=sys.stderr)
    for x in remaining:
        print("ERR\t[%s] %s %s\n\t%s" % (
            x.get("id", ""), x.get("operation", ""), x.get("path", ""),
            x.get("text", "")), file=sys.stderr)
    sys.exit(1)
print("oasdiff breaking: ok（0 个非获准 ERR 变更）")
PYEOF

echo "contract-gate: ok"
