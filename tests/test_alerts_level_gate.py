# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""A 线告警分级名册门禁：每处生产调用点都声明级别，或登记在册并写明理由。

判据（`ba-p04-02`）：汇总邮件按 `MAIL_SUMMARY_MAX_ENTRIES` 封顶。首版按插入顺序
切片，于是轮末产生的轮级告警被轮中累积的逐账号明细挤出正文。

名册的两次漏计都出在"没数全调用点"：控制器预检只列了转发层的一部分；本路第一轮
又漏了 `probe` 与 `schedule` 的两枚。故本门禁把"哪些调用点、各判哪一级"写成可执行
名册——**新增调用点**（含在已覆盖函数里新增）或**改动某点的级别**而不登记，即红，
逼出同批的分级判断。

扫描面：`yiban/ web/ scripts/ docker/` 的 AST 调用节点。注释与 docstring 里的同名
文字不是调用点，故不在扫描面内（预检的 grep 名册含 3 处 docstring 命中，须靠 AST
剔除）。键 = `(相对路径, 主题实参)`；主题实参取字面量值，不是字面量时取源码文本
（转发层把形参原样透传，故键是形参名）。

标签：H · 通知：邮件与推送
覆盖：A 线告警调用点的分级名册（两向钉：未登记即红、名册过期即红）。
对应实现：`yiban/engine/alerts.py` 的 `ALERT_LEVEL_CRITICAL` / `ALERT_LEVEL_NORMAL`
    与 `_select_summary_entries`。
关键断言：高级别点必须写 `ALERT_LEVEL_CRITICAL` 常量名（不许把常量值抄进代码）；
    普通级点必须**不传** `level`（缺省即 NORMAL，且理由逐键写在册）；
    转发层必须写 `level=level`（缺了它等于把调用方声明的级别吃掉）。
依赖：只读源码。无 DB、无网络、无桩。
"""
import ast
import io
import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 运行时目录：生产调用点只可能住在这四处（tests/ 不在面内——测试侧不算生产事实）
RUNTIME_DIRS = ("yiban", "web", "scripts", "docker")

#: 两条 A 线写入口：`_collect_admin_mail` 是唯一写点，`notify_admin_entry` 是它的转发层
TARGET_FUNCS = ("_collect_admin_mail", "notify_admin_entry")

CRITICAL = "critical"        # 必须显式 `level=...ALERT_LEVEL_CRITICAL`
DEFAULT = "default"          # 不传 `level` ⇒ NORMAL：逐账号明细
PASSTHROUGH = "passthrough"  # 转发调用方声明的级别

#: 调用点所在的生产文件（量具自证用）：扫描空转时下面那条断言会红，而不是恒绿
ROSTER_FILES = (
    "yiban/engine/alerts.py",
    "yiban/engine/executor_v3.py",
    "yiban/engine/probe.py",
    "yiban/engine/round.py",
    "yiban/engine/runner.py",
    "yiban/engine/schedule.py",
    "yiban/store/claims.py",
)

#: 键 → (声明形态, 理由)。理由逐键写死：判级依据必须是说出来的，不是沉默的缺省。
ROSTER = {
    # -- 转发层：形参 subject/entry 原样透传，级别由调用方给 --
    ("yiban/engine/alerts.py", "subject"): (
        PASSTHROUGH, "notify_admin_entry 的写点；级别是它自己的形参，它只负责透传"),
    # -- 逐账号明细（NORMAL）：每账号一条，量随账号数放大 --
    ("yiban/engine/alerts.py", "易班签到耗时告警"): (
        DEFAULT, "单账号慢信号，逐账号一条；量随账号数放大即普通明细"),
    ("yiban/engine/executor_v3.py", "易班签到失败"): (
        DEFAULT, "v3 最终放弃，逐账号一条"),
    ("yiban/engine/round.py", "易班签到失败"): (
        DEFAULT, "v2 放弃与窗口不足两处放弃，均逐账号一条"),
    ("yiban/engine/probe.py", "健康探测预警"): (
        DEFAULT, "逐账号一条。探针在自身进程内收集并收尾（runner 的 --probe 分支"
                 "先于签到路径返回），同进程内只有这一族条目，无轮末关键告警可被它挤出；"
                 "被截断的主题与条数由尾部名册点名，不静默"),
    ("yiban/engine/probe.py", "健康探测提示"): (
        DEFAULT, "一轮至多一条，但只在零硬失败时产生（与逐账号预警互斥），"
                 "自愈类软失败不需要当机处理"),
    # -- 轮级关键告警（CRITICAL）：一轮至多一条，且它意味着"当天可能无签" --
    # 主题是局部变量 `title`：零成功与"部分成功但补签后未了结"两支共用一个写点，
    # 故键取形参名（两个标题都由它承载），理由逐支写在下面。
    ("yiban/engine/alerts.py", "title"): (
        CRITICAL, "零成功（当日签到异常告警）与部分成功但补签后未了结"
                  "（签到窗口异常告警）两支，均轮末产生、落在列表尾部，正是被挤出的那条"),
    ("yiban/engine/runner.py", "易班签到容量超载"): (
        CRITICAL, "窗口已过 / 超载，本轮不发起或跑不完：当天可能全量漏签"),
    ("yiban/engine/runner.py", "易班签到出口预算不足"): (
        CRITICAL, "声明出口数不够 K 个执行体的产出速率：实际吞吐受出口限制，"
                  "窗口内可能跑不完（容量预检高估方向的兜底，与『容量超载』同档）"),
    ("yiban/engine/executor_v3.py", "易班队列读不通：本轮已放弃领取"): (
        CRITICAL, "补货循环连续读不通到上界即停止领取，一轮至多一条；"
                  "库锁/磁盘坏时当天可能零签到，处置须当机进行（查库锁与磁盘空间、"
                  "恢复后跑补签轮），与同族『容量超载』同档。别单（queue，ba-p01-01）"
                  "新增调用点，本单（ba-p04-02）同批定级"),
    ("yiban/engine/schedule.py", "签到窗口配置异常"): (
        CRITICAL, "窗口非法或回退默认：实际签到时刻与配置不符"),
    ("yiban/engine/schedule.py", "签到窗口缓冲已收缩"): (
        CRITICAL, "有效窗口被压缩到窗口宽度的 80%：与配置不符，且重试空间随之减少"),
    ("yiban/store/claims.py", "签到领取池不可用"): (
        CRITICAL, "领取池不可用 ⇒ 本执行体拒绝执行签到，一轮至多一条"),
}


def _callee(node):
    """调用目标名（`x.y.z` 取末段），取不到返回 None。"""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _subject_key(node, src):
    """主题实参：字面量取其值，其它取源码文本（转发层是形参名）。"""
    if not node.args:
        return "<无实参>"
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return (ast.get_source_segment(src, first) or "<取不到>").strip()


def _declared(node, src):
    """该调用声明的级别形态。未知写法原样回报，比较时必不相等。"""
    for kw in node.keywords:
        if kw.arg != "level":
            continue
        text = (ast.get_source_segment(src, kw.value) or "").strip()
        if text == "level":
            return PASSTHROUGH
        if text.endswith("ALERT_LEVEL_CRITICAL"):
            return CRITICAL
        return text
    return DEFAULT


def scan_call_sites():
    """→ {(相对路径, 主题实参): [声明形态, ...]}，含全部运行时调用点。"""
    found = {}
    for root in RUNTIME_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(BASE, root)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, BASE).replace(os.sep, "/")
                with io.open(path, encoding="utf-8") as f:
                    src = f.read()
                for node in ast.walk(ast.parse(src, filename=path)):
                    if not isinstance(node, ast.Call) or _callee(node) not in TARGET_FUNCS:
                        continue
                    found.setdefault((rel, _subject_key(node, src)), []).append(
                        _declared(node, src))
    return found


class AlertLevelRosterTest(unittest.TestCase):
    """双向钉：源码里每一处调用点都在册，册里每一条也都还在源码里。"""

    def test_scan_reaches_the_live_call_sites(self):
        """量具自证：扫描真取到了生产调用点与它们的文件，而不是空转（空转的门禁恒绿）。"""
        found = scan_call_sites()
        self.assertGreaterEqual(len(found), len(ROSTER_FILES),
                                f"扫描只拿到 {len(found)} 个键：量具空转？")
        self.assertEqual(sorted({f for f, _s in found}), sorted(ROSTER_FILES),
                         "调用点文件名册与预期不符（改名或新增文件都要登记）")

    def test_every_call_site_declares_a_registered_level(self):
        found = scan_call_sites()
        unregistered = sorted(k for k in found if k not in ROSTER)
        stale = sorted(k for k in ROSTER if k not in found)
        self.assertEqual(
            unregistered, [],
            "未登记级别的 A 线调用点（新增调用点必须同批定级并登记理由）："
            + "; ".join(f"{f} 主题={s} 实际={found[(f, s)]}" for f, s in unregistered))
        self.assertEqual(
            stale, [],
            "名册已过期（源码里已无此调用点，删掉名册行或改回）："
            + "; ".join(f"{f} 主题={s}" for f, s in stale))
        wrong = []
        for key, declared in sorted(found.items()):
            want = ROSTER[key][0]
            if set(declared) != {want}:
                wrong.append(f"{key[0]} 主题={key[1]} 期望={want} 实际={sorted(set(declared))}")
        self.assertEqual(wrong, [], "级别与名册不符：" + "; ".join(wrong))

    def test_every_entry_states_its_reason(self):
        """判级依据必须写下来：缺理由的登记行等于没判。"""
        missing = [f"{f} 主题={s}" for (f, s), (_lv, why) in ROSTER.items()
                   if len((why or "").strip()) < 8]
        self.assertEqual(missing, [], "登记行缺理由：" + "; ".join(missing))


if __name__ == "__main__":
    unittest.main(verbosity=2)
