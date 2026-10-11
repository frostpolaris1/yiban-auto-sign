# -*- coding: utf-8 -*-
"""校验者：同出口 K 进程并发取额，**窗口内合计 ≤ λ**（工单 `yiban-auto-sign-2cwd` 修法 C）。

标签：B · 调度：领取/队列/执行体

覆盖（**真子进程 + 屏障**，非套内单进程模拟）：
- `egress.outlet_executor_count` 数出同出口执行体数 n；每个子进程据此把出口级速率 λ 均分成
  自身份额 λ/n（`token_bucket.limiter_from_env(shares=n)`）；
- K 个共用单出口的子进程各驱动同一套 `now` 网格取额度，**合计放行数 ≤ λ×窗口（加初始小突发
  容差）**——这正是审查者实测的「两进程同键同时取额 admitted=2」形状要咬的反例：桶键改成
  出口标识只让状态共行，不把各进程各吃一份 λ 的问题消掉；预算均分才消掉它；
- 对照：n=1 时子桶速率 = 出口级 λ（预算不缩水），故单执行体部署行为不变。

对应实现：`yiban/egress.py`（`outlet_executor_count` / `egress_identity`）、
`yiban/engine/token_bucket.py`（`EgressLimiter.shares` / `share_rate` / `limiter_from_env`）、
`yiban/engine/executor_v3.py`（`_Ctx` 传 `shares`）、`yiban/engine/probe.py`（`shares`）。

依赖：`sys.executable` 起真子进程；`now` 由固定网格注入（不依赖真实时钟），故合计数是确定的。
"""

import os
import subprocess
import sys
import tempfile
import unittest

from yiban import egress
from yiban.engine import token_bucket

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 子进程脚本：按真实启动路径算份额（`outlet_executor_count`），驱动同一套 `now` 网格取额。
#: 输出一行 `放行数 份额数n 子桶速率`，供父进程断言。
_CHILD = r"""
import os, sys, time
from yiban import egress
from yiban.engine import token_bucket

slot = int(os.environ["CHILD_SLOT"])
identity = egress.egress_identity(egress.resolve(egress.ROLE_WORKER, slot))
shares = egress.outlet_executor_count(identity)
lim = token_bucket.limiter_from_env(channels=1, shares=shares)
# 屏障：等父进程放行（全部子进程就位后同时起跑）
barrier = os.environ["BARRIER_FILE"]
deadline = time.time() + 30.0
while not os.path.exists(barrier):
    if time.time() > deadline:
        sys.stderr.write("barrier timeout\n")
        sys.exit(3)
    time.sleep(0.005)
now = 0.0
step = 0.01
window = float(os.environ["WINDOW"])
admits = 0
while now <= window:
    if lim.acquire(identity, now):
        admits += 1
    now += step
sys.stdout.write("%d %d %.9f\n" % (admits, shares, lim.bucket(identity).rate))
"""


class BudgetSplitE2ETest(unittest.TestCase):
    """K 个真子进程共用单出口：合计放行数受出口预算约束。"""

    K = 3
    RATE = 4.0
    WINDOW = 20.0
    #: 初始小突发容差（每进程首条即时放行）：合计允许 `λ×窗口 + 2K`。
    SLACK = 2 * K

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-egress-budget-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        manifest = egress.dump_manifest(
            [{"slot": i, "type": "worker", "proxy": ""} for i in range(self.K)])
        env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
        env.update({
            "YIBAN_ACCOUNTS_KEY": "a" * 64,
            "YIBAN_ENV_FILE": os.path.join(self.root, ".env"),
            "YIBAN_DB_FILE": os.path.join(self.root, "yiban.db"),
            "YIBAN_STATE_DIR": self.root,
            "YIBAN_EXECUTORS": manifest,          # K 个 worker，出口全为空 = 同一出口 direct
            "YIBAN_EGRESS_RATE": str(self.RATE),  # 显式写入 = 人工接管（速率不被 AIMD 改）
            "YIBAN_MIN_EXEC_GAP": "5",
            "WINDOW": str(self.WINDOW),
            "BARRIER_FILE": os.path.join(self.root, "go"),
            "PYTHONPATH": BASE,
            "PYTHONIOENCODING": "utf-8",
        })
        self.env = env

    def _run_children(self):
        """起 K 个真子进程，全部就位后放屏障，收回它们的一行结果。"""
        procs = []
        for i in range(self.K):
            child_env = dict(self.env, CHILD_SLOT=str(i),
                             YIBAN_EXECUTOR_ID="worker-%d@testhost" % i)
            procs.append(subprocess.Popen(
                [sys.executable, "-c", _CHILD], cwd=BASE, env=child_env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace"))
        # 全部 Popen 完成后再放屏障：确保"同时起跑"
        open(self.env["BARRIER_FILE"], "w").close()
        results = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, f"子进程失败: {err[-500:]}")
            parts = out.split()
            self.assertEqual(len(parts), 3, f"子进程输出异常: {out!r} {err[-300:]}")
            results.append((int(parts[0]), int(parts[1]), float(parts[2])))
        return results

    def test_same_outlet_k_processes_total_admits_within_lambda(self):
        results = self._run_children()
        total = sum(r[0] for r in results)
        # 每个子进程都按真实启动路径数出了 n=K，并把出口级 λ 均分成 λ/K
        for _admits, shares, bucket_rate in results:
            self.assertEqual(shares, self.K, "同出口执行体数 n 必须是 K")
            self.assertAlmostEqual(bucket_rate, self.RATE / self.K, places=9,
                                   msg="子桶速率必须是出口级 λ ÷ n（预算均分）")
        bound = self.RATE * self.WINDOW + self.SLACK
        self.assertLessEqual(
            total, bound,
            f"同出口 {self.K} 进程合计放行 {total} 条，超过 λ×窗口 + 小突发（{bound:.0f}）；"
            "预算均分失效 = 该出口实际速率被进程数放大")

    def test_single_executor_keeps_full_rate(self):
        """对照：n=1 时份额 = 出口级 λ（预算不缩水），单执行体部署行为不变。"""
        self.K = 1
        # setUp 已按 K=3 写好清单；本条自己重设 K=1 的清单与份额
        manifest = egress.dump_manifest([{"slot": 0, "type": "worker", "proxy": ""}])
        self.env["YIBAN_EXECUTORS"] = manifest
        results = self._run_children()
        admits, shares, bucket_rate = results[0]
        self.assertEqual(shares, 1)
        self.assertAlmostEqual(bucket_rate, self.RATE, places=9)
        # 网格是浮点累加，落点会有 ±1~2 的抖动；只要**接近满速**就证明预算没被均分缩水
        # （均分后（shares=3）同一窗口只有 ≈ 1/3 的量）。
        self.assertGreater(admits, 0.9 * self.RATE * self.WINDOW,
                           "单执行体应吃到完整出口速率（明显多于均分后的份额）")


class InProcessGridTest(unittest.TestCase):
    """同进程两个限速器共享同一出口：各吃 λ/2，网格合计 ≈ λ×窗口（份额算法的内存对照）。"""

    def test_in_process_grid_total_within_lambda(self):
        window = 20.0
        total = 0
        for _ in range(2):
            lim = token_bucket.EgressLimiter(rate=4.0, burst=1, shares=2)
            now = 0.0
            while now <= window:
                if lim.acquire("direct", now):
                    total += 1
                now += 0.01
        self.assertLessEqual(total, 4.0 * window + 4)


class OutletExecutorCountTest(unittest.TestCase):
    """`outlet_executor_count`：只数**真会拉起**的执行体（兜底按开关，停用不计）。"""

    def test_counts_workers_and_enabled_fallback_on_same_outlet(self):
        env = {
            egress.ENV_MANIFEST: egress.dump_manifest([
                {"slot": 0, "type": "worker", "proxy": ""},
                {"slot": 1, "type": "worker", "proxy": ""},
                {"slot": 2, "type": "worker", "proxy": "http://127.0.0.1:3128"},
                {"slot": 3, "type": "fallback", "proxy": ""},
            ]),
            egress.ENV_FALLBACK_ENABLE: "1",
        }
        self.assertEqual(egress.outlet_executor_count(egress.DIRECT_EGRESS, env=env), 3,
                         "两个直连 worker + 开关打开的直连兜底 = 3")
        self.assertEqual(
            egress.outlet_executor_count(egress.egress_identity("http://127.0.0.1:3128"),
                                         env=env), 1)

    def test_disabled_fallback_is_not_counted(self):
        """兜底开关未设=关（registry 缺省 false）⇒ 兜底不计入预算分母（F1）。"""
        env = {egress.ENV_MANIFEST: egress.dump_manifest([
            {"slot": 0, "type": "worker", "proxy": ""},
            {"slot": 3, "type": "fallback", "proxy": ""},
        ])}
        self.assertEqual(egress.outlet_executor_count(egress.DIRECT_EGRESS, env=env), 1,
                         "兜底未声明开启：只数 worker，n=1")

    def test_disabled_rows_do_not_count(self):
        env = {egress.ENV_MANIFEST: egress.dump_manifest([
            {"slot": 0, "type": "worker", "proxy": ""},
            {"slot": 1, "type": "disabled", "proxy": ""},
        ])}
        self.assertEqual(egress.outlet_executor_count(egress.DIRECT_EGRESS, env=env), 1,
                         "停用行不拉起、不消费，故不计入预算分母")

    def test_no_manifest_fallback_disabled_is_n1(self):
        """默认部署（无清单、兜底关）⇒ n=1：出口速率**不**被静默减半（复审 F1）。"""
        self.assertEqual(egress.outlet_executor_count(egress.DIRECT_EGRESS, env={}), 1)

    def test_no_manifest_fallback_enabled_counts_it(self):
        """无清单、兜底开 ⇒ n 含兜底（1 并行 + 1 兜底，均直连 ⇒ n=2）。"""
        env = {egress.ENV_FALLBACK_ENABLE: "1"}
        self.assertEqual(egress.outlet_executor_count(egress.DIRECT_EGRESS, env=env), 2)


if __name__ == "__main__":
    unittest.main()
