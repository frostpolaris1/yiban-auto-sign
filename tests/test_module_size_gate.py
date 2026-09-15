# -*- coding: utf-8 -*-
"""模块化门禁：单文件规模上限（`PROMPT.md` §5.2 第 4/7 条禁止项的守卫）。

`PROMPT.md` 明令禁止"不遵循模块化规则，继续做超长文件"与"堆砌代码，制造新的长文件"，
而此前**没有任何自动化门禁**拦这件事——重构期间很容易一边拆旧文件、一边把新代码
继续堆进巨文件里，拆完发现总量没变。

判据（`45` §3.1）：拆分后无单文件超过 **600 行**。当前三个巨文件（`web/app.py` /
`scripts/db.py` / `scripts/signin.py`）尚未拆完，故设**只减不增的白名单**：

- 白名单内的文件：不得超过其**当前记录值**（只允许变小，不允许变大）；
- 白名单外的文件：不得超过 600 行；
- 每完成一个拆分里程碑，把白名单里的对应条目删掉或下调——**这是里程碑的验收动作**，
  不做就等于没拆完（对应 `45` §3.1"不是建议，是判据"）。

白名单记的是一个"天花板"而不是精确行数：删几行不需要改测试，加一行就会失败——
失败时请拆文件，而不是抬天花板。
"""
import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 目标上限（`45` §3.1）
LIMIT = 600

# 未拆完的巨文件：只允许变小（值 = 记录时的行数上限）。
# 拆分里程碑完成后必须删除对应条目——留着就等于这条不再被约束。
GRANDFATHERED = {
    "web/app.py": 8007,              # M5 拆为 routes/services/security
    "scripts/db.py": 3715,           # M4 拆为 store/*
    "scripts/signin.py": 3640,       # M3 拆为 yiban/*（引擎）+ 兼容壳
    "scripts/notify.py": 950,        # M1④ 拆为 yiban/notify/*
    # 下面几个是工具脚本，不是运行时模块（不参与模块化拆分），但仍设上限防继续膨胀
    "scripts/build_cjk_font_slices.py": 757,
    "scripts/rekey_accounts.py": 693,
    "scripts/loadtest/concurrency_probe.py": 690,
}

# 扫描范围：运行时与共享代码（不含测试、构建产物、第三方）
SCAN_FILES = ["*.py"]
SCAN_DIRS = ["scripts", "web", "docker", "yiban"]


def _iter_scanned():
    for d in SCAN_DIRS:
        root_dir = os.path.join(BASE, d)
        if not os.path.isdir(root_dir):
            continue
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [x for x in dirnames
                           if x not in ("__pycache__", "vendor", "node_modules", ".venv")]
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, BASE).replace(os.sep, "/")
                yield rel, full


def _count_lines(path):
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


class ModuleSizeGateTest(unittest.TestCase):
    def test_no_file_exceeds_its_ceiling(self):
        violations = []
        for rel, full in _iter_scanned():
            n = _count_lines(full)
            ceiling = GRANDFATHERED.get(rel, LIMIT)
            if n > ceiling:
                if rel in GRANDFATHERED:
                    violations.append(
                        f"  {rel}: {n} 行 > 记录上限 {ceiling} —— 巨文件只允许变小，"
                        f"请拆文件而不是抬上限"
                    )
                else:
                    violations.append(
                        f"  {rel}: {n} 行 > {LIMIT} 行 —— 请拆分模块（45 §3.1）"
                    )
        if violations:
            self.fail(
                "单文件规模超限（PROMPT.md §5.2 禁止第 4/7 条）：\n" + "\n".join(violations)
            )

    def test_grandfathered_entries_still_exist(self):
        """白名单条目不得指向已不存在的文件——那说明它已拆走，条目应删除。"""
        stale = [rel for rel in GRANDFATHERED
                 if not os.path.exists(os.path.join(BASE, rel))]
        if stale:
            self.fail(
                "白名单指向已不存在的文件（拆分完成后请删除该条目）：" + ", ".join(stale)
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
