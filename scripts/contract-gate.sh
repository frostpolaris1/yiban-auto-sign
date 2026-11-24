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
#      `id<TAB>method<TAB>path<TAB>批次<TAB>过期日` 精确放行**单条 finding**，
#      同一检查的其余端点、以及全部其它检查不受影响。新增行必须写批次链接，
#      语义是「该端点的该变更已被当批评审接受」，不是关闭规则——
#      本项目的响应枚举闭集按批次演进（渠道词表 2d 扩 webhook），
#      契约唯一消费者是仓内生成物（oapi.gen.go / schema.d.ts 同批 --check 钉住，
#      ui tsc 同批咬合），不存在第三方穷举客户端，enum-additive 不构成现实破坏面。
#      过期日（YYYY-MM-DD）是强制列：缺列、格式错或早于当天，该行失效，
#      finding 回到红；到期必须重新评审（续期写新批次，或修契约/删行）。
#      级别判据：只 ERR（level>=3）判红，warn/info findings 展示不失败——与
#      旧 `--fail-on ERR` 语义逐字一致（kqa 收口 2026-11-10，随 c542e68c 复审引入）。
#      共享错误码枚举例外：components/schemas/ErrorCode 是单一共享枚举，契约在
#      它的 description 里承诺「新值只增不删」；加一个值会同时命中全部响应的
#      error.code（含批量结果里的 items/items/code），按端点逐行豁免没有信息量，
#      且每加一个码就要写 ~98 行。判据要求**两件事同时成立**：
#        ① 新增值属于 ErrorCode 的 enum 序列（散文 bullet 不算，见 §解析口径）；
#        ② finding 的 property 路径是 error.code 链（error/code 或 items…/code），
#           所以局部枚举（SettingSource 等）加同名词仍然判红。
#      满足两条才降为 SHARED 行（按值聚合、不判红）；端点局部枚举的其它值仍走
#      allowlist 逐条评审。解析不出来（块缺失、无 enum、非 UTF-8）一律 exit 2，
#      不静默放行。判据由 tests/test_contract_gate_wiring.py 双向钉住
#      （共享码放行、嵌套 items 放行、局部枚举加同名词仍红、散文 bullet 不算、
#      坏 spec 走 CONTRACT_ERRORCODE_SPEC 注入后 exit 2）。
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
"$PY" - "$ALLOWLIST" "$FINDINGS" "$SPEC" <<'PYEOF'
import datetime, json, os, re, sys
allow_path, findings_path, spec_path = sys.argv[1], sys.argv[2], sys.argv[3]

# ErrorCode 是共享枚举：新增值会同时出现在所有响应的 error.code 上（含批量结果
# 里的 items/items/code）。判据要求两件事同时成立，缺一即回到 ERR：
#   ① 新增值在 ErrorCode 的 enum 序列里——只认 `enum:` 子块，description 散文
#      里的 bullet（缩进相同）不算，否则给散文加一行就能放行任意词；
#   ② finding 的 property 路径是 error.code 那条链，否则局部枚举加个同名词
#      （如 SettingSource 加 conflict）会被一起放过。
ERROR_CODE_PROPERTY = re.compile(r"^(?:error|items)(?:/items)*/code$")
ADDED_VALUE = re.compile(r"added the new `([^`]+)`")
ADDED_PROPERTY = re.compile(r"to the `([^`]+)` response property")
# CONTRACT_ERRORCODE_SPEC 只给测试注入坏 spec、验证 fail-closed；默认读真 spec。
# 它是测试钩子：被设上时打一行 WARN，避免本地 shell 残留 env 时无人察觉。
parse_path = os.environ.get("CONTRACT_ERRORCODE_SPEC") or spec_path
if os.environ.get("CONTRACT_ERRORCODE_SPEC"):
    print("WARN\tCONTRACT_ERRORCODE_SPEC=%s（测试钩子，不是生产路径）" % parse_path,
          file=sys.stderr)
shared_codes = set()
in_block = False
in_enum = False
try:
    with open(parse_path, encoding="utf-8") as fh:
        for line in fh:
            if not in_block:
                if line.rstrip("\r\n") == "    ErrorCode:":
                    in_block = True
                continue
            if re.match(r"^    \S", line):
                break  # 离开 ErrorCode 块
            if not in_enum:
                if line.rstrip("\r\n") == "      enum:":
                    in_enum = True
                continue
            # 合法 YAML 变体：行内注释与引号值都要照收。只剥行内注释
            # （" #" 起），再剥首尾成对引号；否则编辑器一键格式化会让项匹配
            # 不上、值集残缺 → exit 2 或某个码不再放行。
            item = line.split(" #", 1)[0].rstrip("\r\n").rstrip()
            m = re.match(r"^        - (\S+)$", item)
            if m:
                value = m.group(1)
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                shared_codes.add(value)
                continue
            if line.strip():
                break  # enum 序列结束
except (OSError, UnicodeDecodeError) as exc:
    print("ErrorCode schema unreadable: %s" % exc, file=sys.stderr)
    sys.exit(2)
if not shared_codes:
    # 解析失败必须 exit 2：否则共享枚举判据会静默失效，变成「全都不判红」。
    print("cannot parse ErrorCode enum from %s" % parse_path, file=sys.stderr)
    sys.exit(2)
today = datetime.date.today()
allow = set()
try:
    with open(allow_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 5:
                print("INVALID\t%s\tmissing expiry column (id/method/path/batch/expiry)"
                      % "\t".join(parts[:3]), file=sys.stderr)
                continue
            try:
                expiry = datetime.date.fromisoformat(parts[4].strip())
            except ValueError:
                print("INVALID\t%s\tbad expiry %r (want YYYY-MM-DD)"
                      % ("\t".join(parts[:3]), parts[4]), file=sys.stderr)
                continue
            if expiry < today:
                print("EXPIRED\t[%s] %s %s\t%s" % (
                    parts[0], parts[1].lower(), parts[2], expiry.isoformat()),
                    file=sys.stderr)
                continue
            allow.add((parts[0], parts[1].lower(), parts[2]))
except FileNotFoundError:
    pass
remaining = []
shared_hits = []
for x in json.load(open(findings_path, encoding="utf-8")):
    key = (x.get("id", ""), str(x.get("operation", "")).lower(), x.get("path", ""))
    if key in allow:
        print("ALLOWED\t[%s] %s %s" % key, file=sys.stderr)
        continue
    text = str(x.get("text", ""))
    if key[0] == "response-property-enum-value-added":
        added = ADDED_VALUE.search(text)
        prop = ADDED_PROPERTY.search(text)
        # 两条同时成立才算共享枚举（见文件头）：值在 ErrorCode 的 enum 序列里，
        # 且 property 路径是 error.code 链。少任何一条都落到下面的 ERR。
        if (added and prop and added.group(1) in shared_codes
                and ERROR_CODE_PROPERTY.match(prop.group(1))):
            shared_hits.append((added.group(1), prop.group(1)))
            continue
    if int(x.get("level", 3)) < 3:
        # 非 ERR 级 findings 只展示不判红——与旧 `--fail-on ERR` 语义逐字一致。
        print("NOTE\t[%s] %s %s\t%s" % (key[0], key[1].upper(), key[2], text),
              file=sys.stderr)
        continue
    remaining.append(x)
if shared_hits:
    # 按「值 × property」聚合：492 条逐行打印会把门禁日志淹掉，且它们本来就是
    # 同一个枚举行在全部响应上的投影。聚合后仍能看出新增的是哪个码、加在哪条链。
    counts = {}
    for value, prop in shared_hits:
        counts.setdefault(value, {})
        counts[value][prop] = counts[value].get(prop, 0) + 1
    for value in sorted(counts):
        props = ", ".join("%s ×%d" % (p, n) for p, n in sorted(counts[value].items()))
        print("SHARED\tErrorCode 新增值 %s（%d 条 finding）：%s——契约声明新值只增不删，"
              "共享枚举按加成放行" % (value, sum(counts[value].values()), props),
              file=sys.stderr)
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
