#!/usr/bin/env bash
# ============================================================
# scripts/tooling-gate.sh — 每 PR 的工具链快门。
#
# 五道检查，全部只看静态事实：
#   1. actionlint：GitHub Actions 工作流的语法/表达式/内置 shellcheck。
#   2. shellcheck：新栈部署与 e2e 脚本（旧线脚本冻结，不在扫描面）。
#   3. golangci-lint：标准五件套（errcheck/govet/ineffassign/staticcheck/unused）。
#   4. govulncheck：Go 依赖与调用面可达的已知漏洞。
#   5. npm audit：ui 与契约工具（server/contracts/tools）锁文件的高危/严重漏洞
#      （moderate 不拦）。
#
# 口径：
#   - 缺工具 = 环境错误，exit 2（跳过 ≠ 绿）。
#   - npm audit 走当前 npm registry；镜像不支持 audit 时显式换官方源。
#   - shellcheck 只收 warning 及以上；info（如 trap 间接调用的函数）不拦。
#     同一条口径也经 SHELLCHECK_OPTS 传给 actionlint 内嵌的 shellcheck。
#   - 新增部署/e2e 脚本要登记到 SHELL_FILES，否则不在扫描面。
# ============================================================
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SELF_DIR/.." && pwd)"

# 新栈脚本清单。旧线（yiban/ web/ scripts/ 的旧入口）已冻结，不做同批整改。
SHELL_FILES=(
    deploy/linux/install.sh
    deploy/linux/update.sh
    scripts/e2e/deploy-container-e2e.sh
    scripts/e2e/deploy-baremetal-e2e.sh
    scripts/e2e/update-rollback-e2e.sh
    scripts/e2e/lib/deploy-e2e.sh
    scripts/verify-stacks.sh
    scripts/tooling-gate.sh
    scripts/contract-gate.sh
    server/scripts/generate.sh
    server/scripts/license.sh
    server/scripts/verify.sh
)

require() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "tooling-gate: $1 not found; install it or set PATH" >&2
        exit 2
    }
}

require actionlint
require shellcheck
require golangci-lint
require govulncheck
require go
require npm

# actionlint 会对 workflow 的 run: 段调用 shellcheck，且不带 severity 过滤。
# 用 ShellCheck 自己的环境变量把 severity 统一到 warning：runner 镜像里的
# ShellCheck 的版本会漂移（0.9 对 SC2015 报 info，0.11 不报），口径不能跟着漂。
export SHELLCHECK_OPTS="-S warning"

cd "$ROOT"

echo "== actionlint =="
actionlint

echo "== shellcheck（warning 及以上）=="
shellcheck -S warning "${SHELL_FILES[@]}"

echo "== golangci-lint =="
(cd server && golangci-lint run ./...)

echo "== govulncheck =="
# stdlib 漏洞按工具链版本判定，固定到 go.mod 的 toolchain 行同值。
(cd server && GOTOOLCHAIN=go1.27.3 govulncheck ./...)

echo "== npm audit（high 及以上）=="
(cd ui && npm audit --audit-level=high)
# 契约工具的锁单独审计：它不在 ui 依赖图里（server/contracts/tools）。
(cd server/contracts/tools && npm audit --audit-level=high)

echo "tooling-gate: ok"
