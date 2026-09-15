# -*- coding: utf-8 -*-
"""模块化门禁：默认 600 行目标 + 超限必须写明工程理由。

`PROMPT.md` §5.2 第 4/7 条禁止"继续做超长文件"与"堆砌代码"。但**红线不是目的**：
真正的判据是"拆了是否更好维护"。若两个功能本就相似相通，硬拆会把原本一次函数调用
变成跨模块协议 + 注入/回调，反而更难读、更容易错——那种情况下**不拆才是对的**。

因此本门禁的规则是：

1. 未登记的文件 ≤ `LIMIT`（600 行）；
2. 登记（`OVERSIZED`）的文件必须给出**工程理由**：为什么它现在这么大、为什么不拆
   （或不立刻拆）、下一步怎么处理。理由写不出 20 字以上就失败——**要么拆，要么说清楚**；
3. 登记项可以给硬上限（`limit`）也可以放弃（`None`，表示"按理由判断，允许增长"）；
4. 任何文件超过 `HARD_CAP` 一律失败（防真正的失控堆砌，与逐行较劲无关）；
5. 登记表不得指向已不存在的文件。

**判断某处该不该拆时，按顺序问自己**：
① 两部分是否服务于同一件事（同一状态机/同一表/同一协议）？是→不拆；
② 拆分是否要求把共享状态、回调或异常类型在模块间来回传递？是→**慎重**，通信成本可能
   高于收益；
③ 是否存在"独立变更轴"（一端改动不需要另一端改动）？是→拆；
④ 是否有第二个调用方（复用）？是→拆。
"""
import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 目标上限（`45` §3.1）
LIMIT = 600
# 绝对上限：与逐行较劲无关，只拦"失控式堆砌"
HARD_CAP = 12000

# 超限但**经工程判断暂不拆/无法简单拆**的文件：(行数上限 or None, 理由)
# 理由需包含：它是什么、为什么现在这样、下一步。
OVERSIZED = {
    "web/app.py": (None, (
        "Flask 工厂 + 全部路由 + 渲染辅助，M5 计划拆为 routes/services/security/render。"
        "当前未拆的工程原因：路由函数大量共享 create_app 内的闭包状态（_file_lock 保护的"
        "读改写序列、按会话的限速表），先拆会把共享状态改成跨模块注入，收益低于风险；"
        "M5 已有既定拆法（蓝图 + 服务层），届时按依赖自然切分。"
    )),
    "scripts/db.py": (None, (
        "SQLite 数据访问层（连接/迁移/各表 CRUD/清理）。M4 计划拆为 store/*。"
        "已按表迁出 verify_jobs 与 accounts（见 yiban/store/）；剩余部分继续按表迁，"
        "不一次性重构的原因：迁移需与冻结的历史迁移函数共存（迁移不可变），"
        "批量搬动会同时动 schema 与读写路径，风险高。"
    )),
    "scripts/signin.py": (None, (
        "签到引擎 + CLI 入口（协议层取自 FYIBAN，调度为原创）。M3 计划拆为 yiban/*（引擎）"
        "与兼容壳。当前未拆的工程原因：引擎部分的状态机、重试预算、看板与熔断彼此共享"
        "大量运行时状态（attempts/results/cred_state/schedule），先抽一部分会把它们变成"
        "跨模块参数传递；M3 会连同 CLI 收口一起按'执行一轮'的边界切分。"
    )),
    "scripts/notify.py": (None, (
        "通知聚合（webhook + 邮件 + 节流 + 台账）。M1④ 计划拆为 yiban/notify/*："
        "台账读写、通道、节流是三条独立变更轴，属'该拆'；排在引擎拆分之后做，"
        "避免与 signin 的调用点改动叠加冲突。"
    )),
    # 工具脚本（非运行时模块，不参与模块化拆分），只设上限防继续膨胀
    "scripts/build_cjk_font_slices.py": (900, "构建期工具：字体分片生成脚本，一次性运行"),
    "scripts/rekey_accounts.py": (800, "运维工具：密钥轮换脚本，与本项目运行时解耦"),
    "scripts/loadtest/concurrency_probe.py": (800, "压测工具：并发探针，非运行时路径"),
}

# 扫描范围：运行时与共享代码（不含测试、构建产物、第三方）
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
    def test_unlisted_files_within_target(self):
        violations = []
        for rel, full in _iter_scanned():
            n = _count_lines(full)
            if rel in OVERSIZED or n <= LIMIT:
                continue
            violations.append(f"  {rel}: {n} 行 > {LIMIT} 行")
        if violations:
            self.fail(
                "文件超过目标规模且未登记（PROMPT.md §5.2 第 4/7 条）：\n"
                + "\n".join(violations)
                + "\n请二选一：按上文判据拆开，或在 OVERSIZED 里写明工程理由与下一步。"
            )

    def test_oversized_entries_explain_themselves(self):
        """允许增长（limit=None）的登记项必须给出实质理由——"要么拆，要么说清楚"。

        工具脚本那类只给数字上限的条目是"防继续膨胀"，不参与拆分，无需长理由。
        """
        thin = []
        for rel, (limit, reason) in OVERSIZED.items():
            need = 20 if limit is None else 1
            if len(str(reason).strip()) < need:
                thin.append(f"  {rel}: 理由过短（{reason!r}）")
        if thin:
            self.fail("超限登记缺少工程理由：\n" + "\n".join(thin))

    def test_oversized_hard_limits_are_kept(self):
        """给了硬上限的登记项（工具脚本）不得越线。"""
        violations = []
        for rel, (limit, _) in OVERSIZED.items():
            full = os.path.join(BASE, rel)
            if limit is None or not os.path.exists(full):
                continue
            n = _count_lines(full)
            if n > limit:
                violations.append(f"  {rel}: {n} 行 > 登记上限 {limit}")
        if violations:
            self.fail("工具脚本超出登记上限：\n" + "\n".join(violations))

    def test_no_file_runs_away(self):
        """绝对上限：拦"失控式堆砌"，与逐行红线无关。"""
        violations = [
            f"  {rel}: {_count_lines(full)} 行 > 绝对上限 {HARD_CAP}"
            for rel, full in _iter_scanned()
            if _count_lines(full) > HARD_CAP
        ]
        if violations:
            self.fail("单文件失控膨胀（请按判据拆分）：\n" + "\n".join(violations))

    def test_entries_exist(self):
        """登记表不得指向已不存在的文件（拆走后请删除条目）。"""
        stale = [rel for rel in OVERSIZED
                 if not os.path.exists(os.path.join(BASE, rel))]
        if stale:
            self.fail("登记表指向已不存在的文件（拆分完成后请删除条目）：" + ", ".join(stale))


if __name__ == "__main__":
    unittest.main(verbosity=2)
