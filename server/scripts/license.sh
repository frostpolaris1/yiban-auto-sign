#!/usr/bin/env bash
# 第三方依赖许可证门。
#
#   ./scripts/license.sh          校验允许清单与清单漂移
#   ./scripts/license.sh --write  重新生成 THIRD_PARTY_LICENSES.md
#
# 退出码：
#   0  通过
#   1  许可证不在允许清单内，或清单与扫描结果不一致
#   2  工具不可用、扫描为空、扫描未覆盖直接依赖（门没有真正运行）
#
# 选型：github.com/google/go-licenses v1.6.0。
# 本机（Windows + WSL）没有 trivy，trivy 还需要联网下载漏洞库。
# go-licenses 版本钉住，首次运行装进用户缓存目录，之后离线可用。
#
# 两条 fail-closed 纪律，理由同 generate.sh：
#   1. 工具构建失败即退 2，不许把「没跑」当绿。
#   2. 扫描结果必须覆盖 go.mod 的全部直接依赖，否则退 2。
#
# 离线口径：报告只取许可证标识，URL 列不参与判定。
# 运行报告时把 HTTP(S)_PROXY 指向关闭的端口，强制 URL 发现快速失败。
# 这样本机联网与否都产出同一份「Unknown」URL，清单不漂移。
set -euo pipefail
cd "$(dirname "$0")/.."

MANIFEST="THIRD_PARTY_LICENSES.md"
OWN_MODULE="github.com/frostpolaris1/yiban-auto-sign/server"
TOOL_VERSION="v1.6.0"
TOOL_MODULE="github.com/google/go-licenses"

# 允许清单按主任务书 3.7.5 制定。不在此列的许可证一律红。
# 拆开写，避免逗号分隔的字符串在循环里还要再切一次。
ALLOWED=(
  "Apache-2.0"
  "BSD-2-Clause"
  "BSD-3-Clause"
  "ISC"
  "MIT"
  "0BSD"
  "CC0-1.0"
  "BlueOak-1.0.0"
)

mode="check"
case "${1:-}" in
  "") ;;
  --write) mode="write" ;;
  --check) mode="check" ;;
  *)
    echo "license: unknown argument: $1" >&2
    echo "usage: ./scripts/license.sh [--write|--check]" >&2
    exit 2
    ;;
esac

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# resolve_tool 输出扫描器路径。不可用即退出 2。
resolve_tool() {
  if [ -n "${GO_LICENSES:-}" ]; then
    if [ ! -x "$GO_LICENSES" ]; then
      echo "license: GO_LICENSES=$GO_LICENSES is not executable" >&2
      return 1
    fi
    printf '%s\n' "$GO_LICENSES"
    return 0
  fi

  local cache="${XDG_CACHE_HOME:-$HOME/.cache}/yiban/go-licenses/$TOOL_VERSION"
  local bin="$cache/go-licenses$(go env GOEXE)"
  if [ -x "$bin" ]; then
    printf '%s\n' "$bin"
    return 0
  fi

  mkdir -p "$cache"
  echo "license: installing $TOOL_MODULE@$TOOL_VERSION into $cache" >&2
  if ! GOBIN="$cache" go install "$TOOL_MODULE@$TOOL_VERSION" 1>&2; then
    echo "license: cannot install the scanner; the gate DID NOT RUN" >&2
    return 1
  fi
  if [ ! -x "$bin" ]; then
    echo "license: install produced no $bin; the gate DID NOT RUN" >&2
    return 1
  fi
  printf '%s\n' "$bin"
}

if ! tool="$(resolve_tool)"; then
  exit 2
fi

# scan_report 生成 CSV：module,url,license。
scan_report() {
  HTTPS_PROXY=http://127.0.0.1:1 \
  HTTP_PROXY=http://127.0.0.1:1 \
  "$tool" report ./... >"$tmpdir/report.csv" 2>"$tmpdir/report.err"
}

if ! scan_report; then
  echo "license: the scanner failed; the gate DID NOT RUN" >&2
  tail -20 "$tmpdir/report.err" >&2 || true
  exit 2
fi

# 每行三个字段。URL 不含逗号，许可证可能含逗号并被引号包住。
# 所以跳过前两个字段，把剩余部分整体当作许可证。
# 本模块自身的包没有许可证文件，报告里是 Unknown，这里滤掉。
awk -F, -v own="$OWN_MODULE" '
  {
    mod = $1
    lic = ""
    for (i = 3; i <= NF; i++) {
      lic = lic (i > 3 ? "," : "") $i
    }
    gsub(/"/, "", mod)
    gsub(/"/, "", lic)
    if (index(mod, own) == 1) {
      next
    }
    print mod "\t" lic
  }
' "$tmpdir/report.csv" | LC_ALL=C sort -u >"$tmpdir/deps.tsv"

if [ ! -s "$tmpdir/deps.tsv" ]; then
  echo "license: the scan produced no third-party modules; the gate DID NOT RUN" >&2
  exit 2
fi

# 覆盖检查：go.mod 的每个直接依赖都必须出现在扫描结果里。
if ! direct="$(go list -m -f '{{if not .Indirect}}{{.Path}}{{end}}' all 2>"$tmpdir/q.err")"; then
  echo "license: cannot list the module graph; the gate DID NOT RUN" >&2
  tail -20 "$tmpdir/q.err" >&2 || true
  exit 2
fi
missing=0
while IFS= read -r dep; do
  [ -n "$dep" ] || continue
  [ "$dep" = "$OWN_MODULE" ] && continue
  # go-licenses 的第一列是**包**路径，不是模块路径：
  # 模块根没有 Go 文件时（如 golang.org/x/time），它报 golang.org/x/time/rate。
  # 覆盖判据接受模块路径或其子包路径；只认整段前缀，避免 x/timefoo 误配。
  if ! cut -f1 "$tmpdir/deps.tsv" | awk -v dep="$dep" '
      $0 == dep || index($0, dep "/") == 1 { found = 1 }
      END { exit found ? 0 : 1 }'; then
    echo "license: the scan missed direct dependency $dep; the gate DID NOT RUN" >&2
    missing=1
  fi
done <<<"$direct"
if [ "$missing" -ne 0 ]; then
  exit 2
fi

# 允许清单。未知与无许可证不在清单内，因此同样红。
violations=0
while IFS=$'\t' read -r mod lic; do
  if [ -z "$lic" ] || [ "$lic" = "Unknown" ] || [ "$lic" = "NOASSERTION" ]; then
    echo "license: $mod has no detected license ($lic)" >&2
    violations=1
    continue
  fi
  IFS=',' read -r -a parts <<<"$lic"
  for part in "${parts[@]}"; do
    part="$(printf '%s' "$part" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -n "$part" ] || continue
    ok=0
    for allow in "${ALLOWED[@]}"; do
      if [ "$part" = "$allow" ]; then
        ok=1
        break
      fi
    done
    if [ "$ok" -eq 0 ]; then
      echo "license: $mod uses '$part', which is not allowed" >&2
      case "$part" in
        *GPL*) echo "license: GPL and AGPL are forbidden for this project" >&2 ;;
      esac
      violations=1
    fi
  done
done <"$tmpdir/deps.tsv"

if [ "$violations" -ne 0 ]; then
  echo "license: FAIL: disallowed or unknown licenses found" >&2
  exit 1
fi

render() {
  local count
  count="$(wc -l <"$tmpdir/deps.tsv" | tr -d ' ')"
  cat <<EOF
# 第三方依赖许可证清单（server）

> 本文件由 \`server/scripts/license.sh --write\` 生成，禁止手改。
> 允许清单与禁用清单由该脚本执行，本文件只作留档。
> 工具：${TOOL_MODULE} ${TOOL_VERSION}。
> 口径：\`go-licenses report ./...\` 列出的第三方包，只含参与构建的包；模块根无 Go 文件时记子包路径。
> 本模块 ${OWN_MODULE} 自身不计入。

允许的许可证：Apache-2.0、BSD-2-Clause、BSD-3-Clause、ISC、MIT、0BSD、CC0-1.0、BlueOak-1.0.0。
其余许可证一律使门失败。GPL 系、AGPL 系、未知与无许可证同样失败。

| 模块/包 | 许可证 |
|---|---|
EOF
  while IFS=$'\t' read -r mod lic; do
    printf '| %s | %s |\n' "$mod" "$lic"
  done <"$tmpdir/deps.tsv"
  printf '\n共 %s 个模块。\n' "$count"
}

if [ "$mode" = "write" ]; then
  render >"$MANIFEST"
  echo "license: wrote $MANIFEST"
  exit 0
fi

if [ ! -f "$MANIFEST" ]; then
  echo "license: missing $MANIFEST; run ./scripts/license.sh --write" >&2
  exit 1
fi

render >"$tmpdir/expected.md"

# 行尾统一成 LF，避免 autocrlf 造成假红。
sed -e 's/\r$//' "$MANIFEST" >"$tmpdir/actual.md"
if ! diff -u "$tmpdir/actual.md" "$tmpdir/expected.md"; then
  echo "license: $MANIFEST is stale; run ./scripts/license.sh --write" >&2
  exit 1
fi

count="$(wc -l <"$tmpdir/deps.tsv" | tr -d ' ')"
echo "license: ok ($count third-party modules)"
