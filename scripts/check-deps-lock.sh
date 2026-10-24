#!/usr/bin/env bash
# ============================================================
# scripts/check-deps-lock.sh —— venv 与 requirements.lock 一致性门（只读，工单 j06f）
#
# 用法：
#   bash scripts/check-deps-lock.sh                          # 查 <检出>/.venv
#   bash scripts/check-deps-lock.sh --venv /opt/yiban-auto-sign/.venv
#   bash scripts/check-deps-lock.sh --lock /tmp/other.lock   # 换清单（演练/测试用）
#
# venv 默认取 <检出>/.venv：本仓裸机部署只认这一个环境
# （run.sh 与 web/deploy/yiban-web.service 同源）。换环境用 --venv，不加新配置键。
#
# 断言的不变式（唯一判据）：requirements.lock 钉住的每个包，在目标 venv 里
# 必须**版本相同**地装好。平台 marker 判为假的条目除外。
#
# 退出码：
#   0 = 一致
#   1 = 有漂移（包缺失，或版本与 lock 不符），**或清单里一个锁定包都没有**
#       （空清单的"一致"是空真，扫空不许当绿——R3-010）
#   2 = 环境/用法错误（venv 解释器缺失、清单缺失、清单出现未知 marker）
#
# 存在的原因（工单 j06f，现象）：pip-audit 审的是 requirements.lock，
# 生产跑的是 venv，两者之间此前没有任何东西对账。
# 2026-10-12 实测：生产 venv 落后 lock 两个含 CVE 修复的包
# （urllib3 2.7.0 对 lock 2.8.0；Werkzeug 3.1.8 对 lock 3.1.9）。
#
# 只读承诺：不装包、不写文件、不联网。
# 判据由目标 venv 自己的解释器读出，只用标准库（importlib.metadata）。
# 部署后立刻可跑；`deploy/prod/install.sh` 同步依赖后也跑它。
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$REPO_ROOT/.venv"
LOCK="$REPO_ROOT/requirements.lock"

while [ $# -gt 0 ]; do
    case "$1" in
        --venv)
            [ $# -ge 2 ] || { echo "check-deps-lock: 参数 --venv 缺取值" >&2; exit 2; }
            VENV="$2"; shift 2 ;;
        --venv=*) VENV="${1#*=}"; shift ;;
        --lock)
            [ $# -ge 2 ] || { echo "check-deps-lock: 参数 --lock 缺取值" >&2; exit 2; }
            LOCK="$2"; shift 2 ;;
        --lock=*) LOCK="${1#*=}"; shift ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "check-deps-lock: 未知参数: $1" >&2; exit 2 ;;
    esac
done

PY=""
for cand in "$VENV/bin/python3" "$VENV/bin/python"; do
    if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
    echo "check-deps-lock: 找不到 venv 解释器：$VENV/bin/python3" >&2
    echo "  这是环境错误，不是漂移。请用 --venv 指定正确的 venv。" >&2
    exit 2
fi
[ -f "$LOCK" ] || { echo "check-deps-lock: 锁定清单缺失：$LOCK" >&2; exit 2; }

# 判据本体跑在目标 venv 的解释器里：它读自己的元数据，不猜、不联网。
# 第三参是本门解析出的 venv 解释器绝对路径：处置提示要与它同口径（见下）。
"$PY" - "$LOCK" "$VENV" "$PY" <<'PYEOF'
import os
import platform
import re
import sys
import importlib.metadata as md

lock_path, venv, venv_py = sys.argv[1], sys.argv[2], sys.argv[3]
on_windows = platform.system() == "Windows"
# marker 白名单：只认 lock 里实际用到的那条平台 marker。
# 出现别的 marker 时本门 fail-closed（退 2），不静默跳过任何条目。
KNOWN_MARKERS = {'platform_system == "Windows"': on_windows}

pins = []
unknown = []
with open(lock_path, encoding="utf-8") as fh:
    for raw in fh:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body, _, marker = line.partition(";")
        body, marker = body.strip(), marker.strip()
        name, sep, want = body.partition("==")
        if not sep or not name.strip() or not want.strip():
            unknown.append((line, "不是 name==version 形态"))
            continue
        if marker:
            if marker not in KNOWN_MARKERS:
                unknown.append((line, marker))
                continue
            if not KNOWN_MARKERS[marker]:
                continue  # 本平台不适用（例：Windows 专属包）
        pins.append((name.strip(), want.strip()))

if unknown:
    print("check-deps-lock: 清单有本门不认识的条目，拒绝放行（fail-closed）：", file=sys.stderr)
    for line, why in unknown:
        print("  %s（%s）" % (line, why), file=sys.stderr)
    print("  处置：扩展 scripts/check-deps-lock.sh 的 marker 白名单后再跑。", file=sys.stderr)
    sys.exit(2)

drift = []

# 防"扫空当绿"（R3-010）：0 个锁定包时"venv 与 lock 一致"是空真。
# 下限 = 1，依据：lock 至少钉一个包才有对账对象；空清单/只有注释的清单
# （通常是路径写错或文件被清空）必须响亮失败，不许静默通过。
if not pins:
    print("check-deps-lock: 锁定清单没有任何 name==version 条目（0 个锁定包）：", file=sys.stderr)
    print("  %s" % lock_path, file=sys.stderr)
    print("  'venv 与 lock 一致'对空清单是空真——拒绝放行（检查清单路径与内容）。",
          file=sys.stderr)
    sys.exit(1)


def norm(name):
    """发行名归一化（PEP 503 形态）：大小写与 -_. 都不参与判等。"""
    return re.sub(r"[-_.]+", "-", name).lower()


# 装了什么，由 venv 的元数据全量枚举得出。
# 为什么不直接 md.version(名字)：目录名形态不同（`charset_normalizer-*.dist-info`）
# 会让按名查找落空，把装好的包误报成漂移。
installed = {}
for dist in md.distributions():
    try:
        declared = dist.metadata["Name"] or dist.name
    except Exception:
        declared = dist.name
    try:
        installed[norm(declared)] = dist.version
    except Exception:
        continue

for name, want in pins:
    have = installed.get(norm(name))
    if have != want:
        drift.append((name, want, have))

if not drift:
    print("check-deps-lock: venv 与 lock 一致（%d 个锁定包，%s）" % (len(pins), venv))
    sys.exit(0)

print("check-deps-lock: venv 与 lock 不一致：%d 处漂移（对比 %d 个锁定包）"
      % (len(drift), len(pins)), file=sys.stderr)
for name, want, have in drift:
    print("  %s: lock=%s venv=%s" % (name, want, have if have else "(未安装)"), file=sys.stderr)
# 处置提示必须抄得动：提示里的入口不可执行时那条命令是 rc=126，不存在时是 rc=127
# （同 install.sh 回滚命令的同一缺陷形状）。
# 故谓词答的是"能不能抄"（+x），不是"在不在"：bin/pip 在但无执行位（如 mode 644）时，
# 门自己走的是解释器分支，提示也必须走 `<解释器> -m pip`。
# 本谓词与 install.sh 的收件谓词（存在即收，故意更宽）**不同**：两者回答不同问题。
pip_path = os.path.join(venv, "bin", "pip")
pip_hint = pip_path if os.access(pip_path, os.X_OK) else "%s -m pip" % venv_py
print("  处置：%s install -r %s" % (pip_hint, lock_path), file=sys.stderr)
sys.exit(1)
PYEOF
