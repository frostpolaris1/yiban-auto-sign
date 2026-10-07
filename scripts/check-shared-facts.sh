#!/usr/bin/env bash
# ============================================================
# scripts/check-shared-facts.sh —— B0.5 §14 共享事实名册门禁
#
# 用法：
#   scripts/check-shared-facts.sh [--root DIR] [--roster FILE] [--min-files N]
#   默认：--root = 仓库根；--roster = scripts/gate/shared-facts.tsv；--min-files = 50
#
# 存在的原因（现象）：census 把全仓 84 枚"共享事实"（同一个事实在多个地方各自
# 定义一遍）编成名册，其中三族最脏——路径配置 / 状态件 / 语义常量——没有任何
# 机器守卫。B1–B3 在删旧定义点的同时很容易顺手长出新定义点，而评审看不见。
# 本脚本把"每枚事实今天允许有几个定义点"冻结成仓内数据（名册），逐键数**非注释**
# 命中，超出即红。治理成本 O(1)：新增一枚定义点 = 改一行名册并说明理由。
#
# 判据——对名册里每行 active 记录：
#   在 扫描范围（相对 --root 的路径列表，写 `default` 表示全树）内，用 判定模式
#   （ERE）数**非注释命中**。注释一律不算：Python/bash/shell 的 `#`（含行尾，但
#   `${#x}` / `${x#y}` 这类参数展开里的 `#` 不是注释）、JS 的 `//` 与 `/* */`、
#   CSS 的 `/* */`（CSS 里 `//` 不是注释）、HTML 的 `<!-- -->` 与 Jinja 的
#   `{# #}`（HTML 里 `<script>…</script>` 段按 JS 规则剥）。
#   命中数 > 允许上限 ⇒ 判红，stdout 逐条点名（键 + 实际数/允许数 + 全部命中
#   的文件:行）。命中数 ≤ 上限 ⇒ 绿（修到零也绿：门只管"不许长出新定义点"）。
#   pending 行不参与判定——今天不可机械单模式计数（见名册"口径备注"），不许硬编
#   上限凑数。
#
# 引擎路由（2026-10-06 起）：判定模式以 `ast:` 开头的 active 行由 AST 引擎计数，
#   本脚本见该前缀**让开**，两者共用同一行的扫描范围与允许上限。为什么必须让开：
#   这类判据分得开"读"与"写"（`environ[K] = v` 不算绕过），awk 的单条 ERE 分不开——
#   两个引擎各数一遍会把同一枚键的上限重复扣。让开以后由
#   scripts/check-path-env-reads.py 数，执行器在 run_ci 里紧跟本脚本；那一行的
#   扫描范围校验（0 个可扫文件即退 2）也随路由交给它，本脚本不再校验它。
#   分工的守卫：tests/test_path_env_read_gate.py 断言"每枚 active 键恰好被一个引擎
#   计数"——少一个=静默废键，多一个=重复扣档，都要红。
#
# 退出码：0 全部在册内；1 有键超标；2 门禁自身参数/环境错误（名册或根目录不存在、
#   --min-files 非整数、选项后缺值、名册落在扫描白名单里、扫到的文件数不足
#   --min-files、**某行声明的扫描范围在 --root 下贡献 0 个文件**）。
#   最后两条都是**防废门**：前者不许因为"一个文件都没扫到"而零命中即通过；后者
#   不许因为某键的 scope 路径打错/文件已改名而让它静默变成恒零命中的废键。
#
# 活体反例（tests/test_shared_facts_gate.py 里真跑子进程）：合成树里加一处定义点
#   ⇒ 必红并点名 文件:行；把该处改成注释 ⇒ 又绿；删到不足上限 ⇒ 仍绿；scope 指向
#   不存在的路径 ⇒ 退出码 2 并点名键与 scope；真名册 + 真文件加一处真定义点 ⇒ 必红。
#   其余已知盲点（跨行的 `${`、here-doc 内的 `#`）登记在名册文件头。
#
# 名册事实源：D:/code/_census/out/{ROSTER,POINTS}.csv（仓外、只读，一次性裁进
#   scripts/gate/shared-facts.tsv）。本脚本运行期**不依赖仓外任何文件**。
# ============================================================
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ROOT="$REPO_ROOT"
ROSTER="$REPO_ROOT/scripts/gate/shared-facts.tsv"
MIN_FILES=50
# 引擎路由标记：名册"判定模式"列以此开头的行交给 AST 门计数（约定见文件头与名册文件头）
ENGINE_PREFIX="ast:"

# 生产树默认扫描面（相对于 --root）。文档、锁定清单、名册自身不在内：
# 它们不是"定义点"，把它们算进来只会让上限虚高。
DEFAULT_SCOPE="yiban scripts web docker deploy .github run.sh run_probe.sh docker-compose.yml pyproject.toml .env.example .env.docker.example"

# 只扫代码/配置类文件（名册是 .tsv，天然落在这张白名单外——否则名册自己会把
# 每条判定模式都命中一遍，门禁当场自杀）。
EXT_RE='\.(py|sh|bash|js|html|htm|j2|css|conf|cfg|ini|service|yml|yaml|toml|example)$|(^|/)(Dockerfile|cron\.d/[^/]+)$'
EXCL_RE='(^|/)(\.git|\.venv|__pycache__|node_modules|out|\.tmp|vendor|\.pytest_cache|\.ruff_cache|web/static/vue/assets)(/|$)'

while [ $# -gt 0 ]; do
    case "$1" in
        --root|--roster|--min-files)
            # 缺值时是"用错参数"（2），不是"有键超标"（1）——两者混码会被误读
            [ $# -ge 2 ] || { echo "check-shared-facts: 参数 $1 缺取值" >&2; exit 2; }
            case "$1" in
                --root)      ROOT="$2" ;;
                --roster)    ROSTER="$2" ;;
                --min-files) MIN_FILES="$2" ;;
            esac
            shift 2 ;;
        *) echo "check-shared-facts: 未知参数: $1" >&2; exit 2 ;;
    esac
done

[ -d "$ROOT" ] || { echo "check-shared-facts: 根目录不存在: $ROOT" >&2; exit 2; }
[ -f "$ROSTER" ] || { echo "check-shared-facts: 名册不存在: $ROSTER（门禁必须自包含，CI runner 上没有仓外 census）" >&2; exit 2; }
case "$MIN_FILES" in ''|*[!0-9]*) echo "check-shared-facts: --min-files 必须是非负整数: $MIN_FILES" >&2; exit 2 ;; esac

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ---- 注释剥离 + 多模式计数的 awk 内核 -----------------------------------------
# 一次 awk 调用吃下整组文件、整组模式（性能：真树 ~700 文件 × ~35 模式 = 一次遍历）。
cat > "$TMP/strip.awk" <<'AWK'
function langclass(f,   e) {
    e = tolower(f)
    if (e ~ /\.(js|mjs|cjs)$/) return "slash"
    if (e ~ /\.css$/) return "css"
    if (e ~ /\.(html|htm|j2|jinja|xml|vue|svg)$/) return "html"
    return "hash"
}

# `#` / `//` / `/* */`：逐字符扫描，字符串字面量内的符号不算注释起始。
# shell 的 `${...}` 参数展开内 `#` 也不是注释起始（`${#arr[@]}` / `${x#y}`）——
# 这两处是"行尾截断 ⇒ 同行真定义点漏检"的经典来源（残余盲点见名册文件头）。
function strip_code(line, cls,   i, n, c, q, out, blk, pexp) {
    blk = cstate[FILENAME]
    if (!blk) {
        if (cls == "hash" && index(line, "#") == 0) return line
        if (cls != "hash" && index(line, "/") == 0) return line
    }
    out = ""; q = ""; n = length(line); i = 1; pexp = 0
    while (i <= n) {
        c = substr(line, i, 1)
        if (blk) {
            if (c == "*" && substr(line, i + 1, 1) == "/") { blk = 0; i += 2; continue }
            i++; continue
        }
        if (q != "") {
            if (c == "\\") { out = out c substr(line, i + 1, 1); i += 2; continue }
            if (c == q) q = ""
            out = out c; i++; continue
        }
        if (c == "'" || c == "\"") { q = c; out = out c; i++; continue }
        if (cls == "hash" && c == "$" && substr(line, i + 1, 1) == "{") {
            pexp++; out = out c; i++; continue
        }
        if (cls == "hash" && pexp > 0) {
            if (c == "}") pexp--
            out = out c; i++; continue
        }
        if (cls == "hash" && c == "#") break
        if (cls == "slash" && c == "/" && substr(line, i + 1, 1) == "/") break
        if ((cls == "slash" || cls == "css") && c == "/" && substr(line, i + 1, 1) == "*") {
            blk = 1; i += 2; continue
        }
        out = out c; i++
    }
    cstate[FILENAME] = blk
    return out
}

# `<!-- -->`（跨行）与 Jinja `{# #}`（跨行）
function strip_html(line,   pos, out) {
    out = line
    while (1) {
        if (hstate[FILENAME]) {
            pos = index(out, "-->")
            if (pos == 0) return ""
            out = substr(out, pos + 3); hstate[FILENAME] = 0; continue
        }
        pos = index(out, "<!--")
        if (pos == 0) break
        out = substr(out, 1, pos - 1) substr(out, pos + 4); hstate[FILENAME] = 1
    }
    while (1) {
        if (jstate[FILENAME]) {
            pos = index(out, "#}")
            if (pos == 0) return ""
            out = substr(out, pos + 2); jstate[FILENAME] = 0; continue
        }
        pos = index(out, "{#")
        if (pos == 0) break
        out = substr(out, 1, pos - 1) substr(out, pos + 2); jstate[FILENAME] = 1
    }
    return out
}

# HTML：`<script>…</script>` 段内是 JS，按 slash 规则剥（`//` 与 `/* */` 都算注释）；
# 段外按 HTML 规则（`<!-- -->` / `{# #}`）。`<script>`/`</script>` 可跨行。
# 顺序要紧：**先剥注释、再在"已剥注释"的残余里定位 `<script`**。注释里写的
# `<script`（含 `<!-- <script> … -->` 这种被整段注释掉的脚本）只是注释文本，不是
# 脚本开标签；反过来若先认 `<script`，被注释掉的脚本段既会走 JS 规则、又把
# 注释态/脚本态留置成未闭合，污染后续行（常见⇒把注释行计成命中，门噪；
# 特定条件下⇒泄漏的 `//` 吞掉紧跟的真定义点，门变松）。
function strip_mixed(line,   out, low, pos, gt, sp, hp, jp, best, kind) {
    out = ""
    while (line != "") {
        low = tolower(line)
        if (sstate[FILENAME]) {
            pos = index(low, "</script")
            if (pos == 0) return out strip_code(line, "slash")
            out = out strip_code(substr(line, 1, pos - 1), "slash")
            sstate[FILENAME] = 0
            line = substr(line, pos)
            continue
        }
        if (hstate[FILENAME]) {          # 上一行留下的 `<!--`：整段注释，直到 `-->`
            pos = index(line, "-->")
            if (pos == 0) return out
            hstate[FILENAME] = 0
            line = substr(line, pos + 3)
            continue
        }
        if (jstate[FILENAME]) {          # 上一行留下的 `{#`：整段注释，直到 `#}`
            pos = index(line, "#}")
            if (pos == 0) return out
            jstate[FILENAME] = 0
            line = substr(line, pos + 2)
            continue
        }
        sp = index(low, "<script")
        if (sp == 0) return out strip_html(line)
        # 本行同时可能有注释开标记：取最早者——注释开在 `<script` 之前时，
        # 该 `<script` 在注释里（注释可能本行就闭合），必须先把注释剥掉再看。
        hp = index(line, "<!--")
        jp = index(line, "{#")
        best = sp; kind = "s"
        if (hp > 0 && hp < best) { best = hp; kind = "h" }
        if (jp > 0 && jp < best) { best = jp; kind = "j" }
        out = out substr(line, 1, best - 1)
        line = substr(line, best)
        if (kind == "h") { hstate[FILENAME] = 1; line = substr(line, 5); continue }
        if (kind == "j") { jstate[FILENAME] = 1; line = substr(line, 3); continue }
        gt = index(line, ">")
        if (gt == 0) { sstate[FILENAME] = 1; return out }
        line = substr(line, gt + 1)
        sstate[FILENAME] = 1
    }
    return out
}

BEGIN {
    nid = 0
    while ((getline pline < patfile) > 0) {
        if (pline == "" || substr(pline, 1, 1) == "#") continue
        t = index(pline, "\t")
        if (t == 0) continue
        nid++
        ids[nid] = substr(pline, 1, t - 1)
        pats[ids[nid]] = substr(pline, t + 1)
    }
    close(patfile)
}

{
    cls = langclass(FILENAME)
    s = (cls == "html") ? strip_mixed($0) : strip_code($0, cls)
    if (s == "") next
    for (k = 1; k <= nid; k++)
        if (s ~ pats[ids[k]]) printf "HIT\t%s\t%s:%d\n", ids[k], FILENAME, FNR
}
AWK

# ---- 读名册，按扫描范围分组 ------------------------------------------------
declare -A GROUP_N=()
declare -a GROUP_SCOPE=()
declare -a GROUP_RAW=()
declare -A GROUP_KEYS=()
n_groups=0
pending=0
ast_routed=0

while IFS=$'\t' read -r fam key status pat scope max note; do
    case "$fam" in ''|'#'*) continue ;; esac
    if [ -z "${key:-}" ]; then continue; fi
    if [ "$status" != "active" ]; then pending=$((pending + 1)); continue; fi
    # 引擎路由：`ast:` 行交给 AST 门计数，本脚本既不误数也不校验它（详见文件头）
    case "$pat" in
        "$ENGINE_PREFIX"*) ast_routed=$((ast_routed + 1)); continue ;;
    esac
    raw_scope="$scope"
    if [ "$scope" = "default" ]; then scope="$DEFAULT_SCOPE"; fi
    g="${GROUP_N[$scope]:-}"
    if [ -z "$g" ]; then
        n_groups=$((n_groups + 1))
        g=$n_groups
        GROUP_N[$scope]=$g
        GROUP_SCOPE[$g]="$scope"
        GROUP_RAW[$g]="$raw_scope"
        GROUP_KEYS[$g]=""
        : > "$TMP/pat_$g"
    fi
    printf '%s\t%s\n' "$key" "$pat" >> "$TMP/pat_$g"
    GROUP_KEYS[$g]="${GROUP_KEYS[$g]:-}$key"$'\n'
done < "$ROSTER"

# 名册自己不能落在扫描白名单里（否则每条模式都会命中名册，门禁自杀）
case "$ROSTER" in
    *.py|*.sh|*.bash|*.js|*.html|*.htm|*.j2|*.css|*.conf|*.cfg|*.ini|*.service|*.yml|*.yaml|*.toml|*.example)
        echo "check-shared-facts: 名册扩展名落在扫描白名单内，会自命中: $ROSTER" >&2; exit 2 ;;
esac

# ---- 收集文件、分组跑 awk 内核 --------------------------------------------
collect_files() {  # $@ = 相对路径列表
    local p
    for p in "$@"; do
        if [ -f "$ROOT/$p" ]; then
            printf '%s\n' "$p"
        elif [ -d "$ROOT/$p" ]; then
            ( cd "$ROOT" && find "$p" -type f )
        fi
    done | grep -E "$EXT_RE" | grep -v -E "$EXCL_RE" || true
}

: > "$TMP/allfiles"
: > "$TMP/hits"
for i in $(seq 1 "$n_groups"); do
    scope="${GROUP_SCOPE[$i]}"
    mapfile -t files < <(collect_files $scope)
    if [ "${#files[@]}" -eq 0 ]; then
        # 某行声明了非空 scope，但它在这棵树下贡献 0 个文件 ⇒ 该键必然恒零命中。
        # 静默跳过 = "看着有门其实是废门"，正是本门最该防的失效模式 ⇒ 硬失败。
        while IFS= read -r k; do
            [ -n "$k" ] || continue
            echo "check-shared-facts: 键 '$k' 的扫描范围 '$scope'（名册写的是 '${GROUP_RAW[$i]}'）" >&2
        done <<< "${GROUP_KEYS[$i]:-}"
        echo "check-shared-facts: 上面的扫描范围在 --root=$ROOT 下贡献 0 个文件——键会静默变成废键（路径打错 / 文件已改名 / 无可扫文件）" >&2
        exit 2
    fi
    printf '%s\n' "${files[@]}" >> "$TMP/allfiles"
    ( cd "$ROOT" && awk -v patfile="$TMP/pat_$i" -f "$TMP/strip.awk" "${files[@]}" ) >> "$TMP/hits"
done

NFILES="$(sort -u "$TMP/allfiles" | wc -l | tr -d ' ')"
if [ "$NFILES" -lt "$MIN_FILES" ]; then
    echo "check-shared-facts: 只扫到 $NFILES 个文件（< --min-files $MIN_FILES）——拒绝零命中即通过" >&2
    exit 2
fi

# ---- 汇总命中 --------------------------------------------------------------
# 位置用**换行**分隔（不是空格）：路径含空格时点名列表才不会被拆成伪位置。
declare -A HITS=() LOCS=()
while IFS=$'\t' read -r tag key loc; do
    [ "${tag:-}" = "HIT" ] || continue
    HITS["$key"]=$(( ${HITS[$key]:-0} + 1 ))
    LOCS["$key"]="${LOCS[$key]:-}$loc"$'\n'
done < "$TMP/hits"

# ---- 逐键判定（按名册顺序） ------------------------------------------------
viol=0
active=0
zero=0
while IFS=$'\t' read -r fam key status pat scope max note; do
    case "$fam" in ''|'#'*) continue ;; esac
    if [ -z "${key:-}" ] || [ "$status" != "active" ]; then continue; fi
    active=$((active + 1))
    case "$pat" in
        "$ENGINE_PREFIX"*) continue ;;
    esac
    h="${HITS[$key]:-0}"
    if [ "$h" -gt "$max" ]; then
        viol=1
        echo "超标: $fam / $key —— 命中 $h/$max（允许上限 $max，超出 $((h - max)) 处；以下逐条点名非注释命中）"
        i=0
        while IFS= read -r l; do
            [ -n "$l" ] || continue
            i=$((i + 1))
            if [ "$i" -le 20 ]; then echo "    $l"; fi
        done <<< "${LOCS[$key]:-}"
        if [ "$i" -gt 20 ]; then echo "    …（其余 $((i - 20)) 处省略）"; fi
    elif [ "$h" -eq 0 ]; then
        zero=$((zero + 1))
        echo "提示: $fam / $key —— 命中 0/$max（事实可能已被修到零，也可能判定模式已失效，请复核）"
    else
        echo "ok: $fam / $key —— 命中 $h/$max"
    fi
done < "$ROSTER"

echo "扫描 $NFILES 个文件；登记 $active 枚 active 键 + $pending 枚 pending（pending 不判定）"
if [ "$ast_routed" -gt 0 ]; then
    echo "其中 $ast_routed 枚 active 键走 $ENGINE_PREFIX 引擎路由，由 scripts/check-path-env-reads.py 计数"
fi
if [ "$viol" -eq 0 ]; then
    echo "shared-facts ok: 全部登记键的非注释定义点数都在允许上限内"
    exit 0
fi
exit 1
