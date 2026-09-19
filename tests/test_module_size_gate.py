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
    "yiban/store/db.py": (None, (
        "SQLite 数据访问层（连接/迁移/各表 CRUD/清理）。已按 M4 计划从 scripts/db.py "
        "移入 store（旧路径只剩兼容壳）；verify_jobs 与 accounts 已按表迁出"
        "（见 yiban/store/），剩余部分继续按表迁，不一次性重构的原因：迁移需与冻结的"
        "历史迁移函数共存（迁移不可变），批量搬动会同时动 schema 与读写路径，风险高。"
    )),
    "yiban/store/audit_chain.py": (None, (
        "审计链域（2026-09-19 从 db.py 第二刀迁出）：HMAC 哈希链写入/校验、全表重链留痕、"
        "库外锚点族、审计密钥来源与缓存。四块服务于同一条协议与同一份取证状态——锚点校验"
        "要读清理留痕、体检要汇总链/锚点/留痕全部信号，拆开就得把这份状态改成跨模块传递"
        "（门禁判据①「同一件事」成立、②不成立）。下一步：若继续膨胀需要再拆，按「密钥来源"
        "与缓存（_audit_key 族，只依赖 connection/env_io/env_lock）」与「锚点文件（record/"
        "verify/anchor 族）」切成两个模块；当前不拆，避免为搬家再动 db 门面与打桩面"
        "（db._audit_hash = 替身 / db._AUDIT_KEY_CACHE = None 必须落在真定义点）。"
    )),
    # scripts/signin.py 已按"执行一轮"的边界切分为 yiban/engine/*（最大 round.py 507 行），
    # 旧路径只剩兼容壳（约 130 行），故不再登记。
    "yiban/egress.py": (None, (
        "出口分配 + 执行体清单模型（2026-09-17 加入 `YIBAN_EXECUTORS` 后 618 行）。"
        "两半**互相咬合**：清单的兜底/回退读取要用 `resolve`（清单优先、旧三键回退），"
        "而 `resolve` 又要读清单——按「拆开」办就得让两个模块来回传「当前清单」，"
        "通信成本高于收益（门禁判据②）。已定好的拆法：把清单模型（parse/dump/行增删改/"
        "迁移/name）整体搬到新模块 `yiban/executors.py`，`resolve` 侧用局部导入绕开循环"
        "（与 `engine/schedule.py` 里 `alerts` 的局部导入同法）；**清单模型下次变更时一起做**，"
        "不为了十几行做一次纯搬家（搬动会同时动 web/runner/测试几十处调用点）。"
    )),
    # yiban/cli.py：命令行统一入口（argparse 装配 + 七个子命令实现 + 单行 JSON 字段契约）。
    # 整块服务于同一件事——命令行面与它的退出码契约。拆开只增加跨文件对偶：新增子命令
    # 必须同时改"解析器"与"handler"，两个文件互为唯一调用方，没有独立变更轴（门禁判据②③）。
    # 当前 700 余行，其中约 45 行是给 agent 看的字段契约表。下一步：若超过 900 行，按
    # 「透传 sign/probe」与「只读运维子命令（config/capacity/state/db/version）」一分为二。
    "yiban/cli.py": (None, (
        "命令行统一入口：argparse 装配 + 七个子命令实现 + 单行 JSON 字段契约。整块服务于"
        "同一件事——命令行面与其退出码（每加一个子命令都要同时改解析与分发，拆成两个"
        "文件只增加跨文件对偶，没有独立变更轴）。当前 700 余行（含约 45 行字段契约表与"
        "逐条硬约定说明）；若增长到 900 行以上，按「透传 sign/probe」与「只读运维子命令"
        "（config/capacity/state/db/version）」切成两个模块。"
    )),
    "web/static/js/components/settings-executors.js": (780, (
        "执行体分区组件（规模 KPI + 清单表 + 行内设置弹窗 + 每个写操作的口令门 + 写明细口径的注释）。"
        "三块服务于同一个屏与**同一份接口响应**：lastData 被 KPI、清单渲染、行弹窗三处读，"
        "banner/focusAfterPaint/rowName/putRow 等助手三处共用——拆开等于把这份共享状态改成跨模块协议"
        "（门禁判据②），而任何接口字段变动仍要同时改多处（判据③不成立）。"
        "2026-09-17 已按上一版登记的下一步抽出「容量实测与建议」（搬去 settings-quota.js）；"
        "随后按后端交付（docs/refactor/90）给三种写操作（追加行/删行/改行）补了口令门，"
        "并加了「只改名不打门」的分支，涨到 706 行 → 上限由 700 提到 780。"
        "再涨就先切行内设置弹窗：openRow 及其独有助手（infoTip/linkBtn/ROW_HELP），"
        "届时要把它依赖的 lastData/putRow/banner 三样显式注入。"
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
