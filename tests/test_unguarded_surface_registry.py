# -*- coding: utf-8 -*-
"""无门面登记表：`docs/refactor/140` §六 的每一项都要在库内有处置与证据。

标签：A · 判据：覆盖显性化

**为什么要有它**：140 §六 列了 15 项"这一维当前无覆盖"。普查稿在 `docs/` 下、**有意
不入库**（过程稿）。若只在过程稿里登记，事实会随文档消失，下一个人要么重新普查一遍，
要么把"已经覆盖了"误当"还是缺口"（本批实测：§六 里已有 3 项被别的批次覆盖了，清单
过期）。**读者拿不到的东西，不能当依据**（AGENTS.md §十.24 的同族）。

**本门钉的性质（三条）**：
  ① **§六 的每一项都有处置**：15 项逐项登记，每项给 `covered` / `open` / `blocked`
     三种判定之一，并附**可核验的探针**（不是散文）。
  ② **探针的观察值必须与登记值一致**：谁把某个缺口补上了、或某个"已覆盖"的事实漂了，
     探针的观察值就变 ⇒ 红，逼人回来改登记。
  ③ **开放缺口必须带工单**：`open` / `blocked` 的项必须写明工单或说明为什么本批不能做。

**探针是"事实观察"，不是"性质断言"**：它回答"今天的形状是什么样"，不断言形状对错。
断言"形状对不对"住在各自的专门口里（如 `test_docker_image_inputs_gate.py`）。

**本门不挡什么**：它**不**替 §六 的每一项建行为门。它保证"每一项都有处置、处置与事实
一致"。真正的判据在各专门口里；本门只是那张**不许静默漂移的账**。

对应实现：`docs/refactor/140-test-double-fidelity-census.md` §六（过程稿，不入库）＋
    本仓 tracked 面（探针逐个现量）。
关键断言：钉"§六 逐项有处置、处置可核验"两条性质；不钉任何用例行为。
依赖：读 tracked 文件与 `git ls-files`；不跑子进程、不触网。整文件在本机执行，无 skip。
"""
import os
import re
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: §六 的项号全集（普查稿 §六 有 15 项；派单写"12 项"，现量 15——按现量取）。
SIX_SECTION_ITEMS = frozenset(range(1, 16))

COVERED, OPEN, BLOCKED = "covered", "open", "blocked"


# ---- 探针（事实观察）----
def _tracked(pattern):
    """跟踪面里匹配该路径的文件（`git ls-files`）。"""
    out = subprocess.run(["git", "ls-files", pattern], cwd=BASE,
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError("前置就绪：`git ls-files` 跑不起来（rc=%s，stderr=%r）"
                            % (out.returncode, (out.stderr or "")[-300:]))
    return [x.strip() for x in (out.stdout or "").splitlines() if x.strip()]


def _read(rel):
    with open(os.path.join(BASE, *rel.split("/")), encoding="utf-8") as f:
        return f.read()


def _grep_tracked(needle, exclude=()):
    """跟踪面里含该字面量的文件（排除给定路径）。"""
    files = _tracked("*")
    hits = []
    for rel in files:
        if rel in exclude:
            continue
        path = os.path.join(BASE, *rel.split("/"))
        try:
            with open(path, encoding="utf-8") as f:
                if needle in f.read():
                    hits.append(rel)
        except (OSError, UnicodeDecodeError):
            continue
    return hits


def probe_live_claim_xproc():
    """§六-1：现役 `sign_tasks` 的跨进程争用是否有真载体。"""
    rel = "tests/test_live_claim_cross_process_contention_e2e.py"
    if not _tracked(rel):
        return "absent"
    body = _read(rel)
    if "subprocess.Popen" not in body:
        return "no-subprocess"
    m = re.search(r"def _race\(self, mode, n=(\d+)\)", body)
    return "races-%s-processes" % (m.group(1) if m else "?")


def probe_mask_parity_node_gate():
    """§六-4：前后端脱敏对拍是否挂在 node 上，而 pytest 门禁 job 是否不装 node。"""
    body = _read("tests/test_web_mask_email_parity.py")
    gated = "skipUnless(NODE" in body
    ci = _read(".github/workflows/ci.yml")
    verify = ci.split("  dependency-audit:")[0]        # `verify` job 的段落
    return (gated, "setup-node" in verify)


def probe_engine_round_load():
    """§六-6：引擎级一轮负载（N 账号过真执行体）是否有载体。"""
    rel = "tests/test_multi_executor_drill_e2e.py"
    if not _tracked(rel):
        return "absent"
    m = re.search(r"ACCOUNTS = (\d+)", _read(rel))
    return "drill-e2e-%s-accounts" % (m.group(1) if m else "?")


def probe_deploy_gate_real_carriers():
    """§六-9：闸门的**真载体**在不在跟踪面里（CI 作业 ＋ 接线元测试）。

    "真载体"＝会真的把它跑起来的东西：`.github/workflows/deploy-gate.yml`（CI 作业，
    README 默认索引形态）与 `tests/test_deploy_gate_wiring.py`（10 用例接线守卫，
    dev-verify 全量与 nightly 也在跑）。文档里提到它的名字不算载体。
    """
    return bool(_tracked(".github/workflows/deploy-gate.yml")
                and _tracked("tests/test_deploy_gate_wiring.py"))


def probe_rate_limit_concurrency():
    """§六-10：进程内限速状态的并发维是否有真载体（真 HTTP 服务）。"""
    rel = "tests/test_web_rate_limit_concurrency_e2e.py"
    if not _tracked(rel):
        return "absent"
    body = _read(rel)
    return "real-http" if "gunicorn" in body else "no-http-server"


def probe_fake_window_always_true():
    """§六-11：假上游把 `signPosition` 的时间窗写死成恒真？"""
    body = _read("yiban/adapters/protocol_fake/__init__.py")
    return ("now - 3600" in body and "now + 3600" in body)


def probe_ci_wordset_drift():
    """§六-15：`--ci` 的目标集是否单源（`-k` 词集已退役，车道名册是唯一事实源）。

    2026-10-17 vckk 阶段 2-a 之前：`--ci` 用 `-k "security or …"` 关键词子集，脚本与
    入库契约文档各写一份、已漂移（故本项当年登记 BLOCKED）。现在子集整体换成
    `tests/lane-roster.tsv`，`-k` 词集不应再出现在任何一面；本探针判两件事——
    旧词集确已退役 ＋ 名册被脚本与文档共同引用。
    """
    script = _read("scripts/dev-verify.sh")
    doc = _read("docs/dev/dev-verify.md")
    legacy = re.search(r'-k "(security[^"]*)"', script) or re.search(r'-k "(security[^"]*)"', doc)
    if legacy:
        return "legacy-wordset-still-present"
    return ('LANE_ROSTER_REL="tests/lane-roster.tsv"' in script,
            "车道名册" in doc)


#: 逐项登记：`项号 -> (判定, 处置说明, 工单/理由, 探针, 期望观察值)`。
#: `covered` 的项附"被谁覆盖"；`open` 的项附"为什么本批没建门"；`blocked` 附硬边界。
REGISTRY = {
    1: (COVERED,
        "现役 sign_tasks 的跨进程争用：已有真子进程竞位载体"
        "（`tests/test_live_claim_cross_process_contention_e2e.py`，3 进程同刻抢同一行，"
        "claim/互斥/收尸三条各有牙）。普查清单写它'无用例'已过期",
        "本批现量复核",
        probe_live_claim_xproc, "races-3-processes"),
    2: (COVERED,
        "兜底腿肘界取值大小：本批新建 `tests/test_fallback_elbow_discrimination_e2e.py`"
        "（行为半证取值有判别力，静态半证接线用了那个常量）",
        "本批新建",
        lambda: _tracked("tests/test_fallback_elbow_discrimination_e2e.py") != [],
        True),
    3: (COVERED,
        "镜像内容 = Dockerfile ∩ .dockerignore：本批新建"
        "`tests/test_docker_image_inputs_gate.py`（两个输入在同一判据里对上）",
        "本批新建",
        lambda: _tracked("tests/test_docker_image_inputs_gate.py") != [],
        True),
    4: (OPEN,
        "前后端脱敏口径对拍整类挂在 `skipUnless(NODE)` 上，而 pytest 门禁 job（`verify`）"
        "**不装 node**（`setup-node` 只在 `frontend` job 里）⇒ 这条对拍靠 runner 镜像自带 "
        "node 才跑。本批不建门：把它做成「必须跑」需要给 `verify` job 加 setup-node"
        "（改 `.github/workflows/ci.yml`，本批硬边界外）",
        "140 §六-4；与 plo2 的'必须运行的检查清单'同族",
        probe_mask_parity_node_gate, (True, False)),
    5: (COVERED,
        "模块级字面日期腐坏：本批新建 `tests/test_module_date_constant_gate.py`"
        "（常量名必须登记类别 ＋ 总量不得静默增长）。"
        "**口径订正**：普查写'88 文件/368 处'，现量是 27 文件/30 处（368 是**所有**日期"
        "字面量，含内联串与实参）",
        "本批新建 ＋ 现量订正",
        lambda: _tracked("tests/test_module_date_constant_gate.py") != [],
        True),
    6: (COVERED,
        "引擎级一轮负载：`tests/test_multi_executor_drill_e2e.py` 已入库（本批现量确认"
        "tracked），真监督进程 ＋ 真执行体 ＋ 假上游跑一轮。**量小**（4 账号），"
        "不是大 N 压测",
        "本批现量复核（onem 那三件已入库）",
        probe_engine_round_load, "drill-e2e-4-accounts"),
    7: (COVERED,
        "真跨进程文件锁争用：本批新建 `tests/test_cross_process_file_lock_e2e.py`"
        "（3 独立进程抢同一把 `locks.file_lock`，哨兵探重叠，绕过锁即红）",
        "本批新建",
        lambda: _tracked("tests/test_cross_process_file_lock_e2e.py") != [],
        True),
    8: (COVERED,
        "失败注入/WAF 形态：本批新建 `tests/test_fake_upstream_fault_knobs_gate.py`"
        "（每枚旋钮要么有主、要么显式登记；12 枚今日无人消费已登记）",
        "本批新建",
        lambda: _tracked("tests/test_fake_upstream_fault_knobs_gate.py") != [],
        True),
    9: (COVERED,
        "按 README 从零干净部署：PR #120（2026-10-16 合入）已把闸门接进 CI——"
        "真载体＝`.github/workflows/deploy-gate.yml`（跑 README 默认索引形态）＋ "
        "`tests/test_deploy_gate_wiring.py`（10 用例接线守卫，dev-verify 全量与 nightly "
        "也在跑）；干净环境验收 29 项 FAIL=0、零手动调整。普查写'零载体'已过期",
        "工单 c9uz；PR #120（独立审查两轮：首轮不放行守卫两洞，复审放行）",
        probe_deploy_gate_real_carriers, True),
    10: (COVERED,
         "32 线程共享一份进程内限速状态：已有真 HTTP 载体"
         "（`tests/test_web_rate_limit_concurrency_e2e.py`，gunicorn `-w 1 --threads 32` "
         "同形 ＋ 有牙对照）。普查清单写'零载体'已过期",
         "本批现量复核",
         probe_rate_limit_concurrency, "real-http"),
    11: (OPEN,
         "窗口外不签／边界让位：假上游把 `signPosition` 的 `Range` 窗写死成 `now±3600`"
         "（恒真）⇒ 以它为上游的 e2e 里'窗口外'这一维被短路。本批不建门："
         "改假上游属硬边界（`yiban/adapters/protocol_fake/__init__.py` 不许动），"
         "而只建一条'这条硬编码还在'的登记门不增加判别力",
         "140 §五 S12；与 §六-12 同根（编造被当证据）",
         probe_fake_window_always_true, True),
    12: (COVERED,
         "故障注入面：同 §六-8 的门（`tests/test_fake_upstream_fault_knobs_gate.py`）"
         "按档记账，6 枚故障旋钮今日无人消费的 5 枚已登记",
         "本批新建",
         lambda: _tracked("tests/test_fake_upstream_fault_knobs_gate.py") != [],
         True),
    13: (COVERED,
         "CJK 分片脚本的 `rmtree` 防护白名单：**本批修好**——"
         "`tests/test_web_font_closure.py` 原先在 `setUpClass` 里 `raise SkipTest`"
         "（fonttools 在任何一轨都不存在 ⇒ 门在所有环境都不执行），"
         "现改为缺依赖时挂替身包照跑",
         "本批订正",
         lambda: re.search(r"^\s*raise unittest\.SkipTest",
                           _read("tests/test_web_font_closure.py"), re.M) is None,
         True),
    14: (COVERED,
         "跳过面自身无门：本批新建 `tests/test_skip_visibility_gate.py`"
         "（跳过面逐枚登记 ＋ 永不运行的族显式登记 ＋ 反陈旧）",
         "本批新建",
         lambda: _tracked("tests/test_skip_visibility_gate.py") != [],
         True),
    15: (COVERED,
         "`--ci` 目标集的单一真源：原 `--ci` 用 `-k` 关键词子集，脚本与入库契约文档"
         "两份定义已漂移（原登记 BLOCKED）。2026-10-17 vckk 阶段 2-a 把子集整体换成"
         " `tests/lane-roster.tsv`（车道名册），`-k` 词集退役；名册漏归类/漂移由"
         " `tests/test_gate_lane_roster.py` 对真实扫描与 pytest 收集对账，红得出来",
         "本批（vckk 2-a）",
         probe_ci_wordset_drift, (True, True)),
}


class UnguardedSurfaceRegistryTest(unittest.TestCase):
    """§六 逐项有处置，处置与事实一致。"""

    def test_six节的每一项都有处置(self):
        missing = sorted(SIX_SECTION_ITEMS - set(REGISTRY))
        extra = sorted(set(REGISTRY) - SIX_SECTION_ITEMS)
        self.assertEqual(missing, [],
                         "§六 的这些项没有处置登记：%s" % missing)
        self.assertEqual(extra, [], "登记表里有超出 §六 项号范围的键：%s" % extra)

    def test_每条登记的探针与登记值一致(self):
        drift = {}
        for item, (verdict, _why, _ticket, probe, expect) in sorted(REGISTRY.items()):
            self.assertIn(verdict, (COVERED, OPEN, BLOCKED),
                          "§六-%d 的判定不是三种之一：%r" % (item, verdict))
            got = probe()
            if got != expect:
                drift[item] = {"verdict": verdict, "expected": expect, "observed": got}
        self.assertEqual(
            drift, {},
            "这些项的探针观察值变了——事实漂了，登记表要同批订正：%s" % drift)

    def test_开放与受阻项必须带工单或理由(self):
        for item, (verdict, why, ticket, _probe, _expect) in sorted(REGISTRY.items()):
            self.assertTrue(why and len(why) > 10,
                            "§六-%d 缺处置说明（要写清'为什么本批不建门/谁覆盖了'）" % item)
            if verdict in (OPEN, BLOCKED):
                self.assertTrue(ticket, "§六-%d 判定为 %s 却没有工单/理由" % (item, verdict))

    def test_已覆盖项必须能指出承重者(self):
        """`covered` 的说明里必须点名一个 tracked 文件（承重者），不许只写散文。"""
        for item, (verdict, why, _ticket, _probe, _expect) in sorted(REGISTRY.items()):
            if verdict != COVERED:
                continue
            named = re.findall(r"`(tests/[^`]+\.py)`", why)
            self.assertTrue(named, "§六-%d 判 covered，但说明里没点名承重文件" % item)
            for rel in named:
                self.assertTrue(_tracked(rel),
                                "§六-%d 点名的承重者 %s 不在跟踪面里" % (item, rel))

    def test_探针面非空且可调用(self):
        self.assertGreater(len(REGISTRY), 0, "登记表为空：门在空转")
        for item, (_v, _w, _t, probe, _e) in sorted(REGISTRY.items()):
            self.assertTrue(callable(probe), "§六-%d 的探针不可调用" % item)
            self.assertIsNotNone(probe(), "§六-%d 的探针返回 None" % item)


if __name__ == "__main__":
    unittest.main(verbosity=2)
