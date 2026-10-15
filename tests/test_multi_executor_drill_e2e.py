# -*- coding: utf-8 -*-
"""多执行体协调面的**靶场 e2e**：两条不变式 ＋ 有牙证明。

标签：B · 调度：领取/队列/执行体
覆盖：本靶场（`scripts/drill_multi_exec.py`）在**本地、零外联**的环境里拉起真实的
   监督进程与真实执行体子进程（真值路径 `workers.run_worker_supervisor` →
   `executor_v3` → `signin`），子进程的全部 https 请求经传输层改写钉回回环假上游
   （`tests/fake_yiban_server.py --no-tls`）。四条：
   ① 两个执行体领同一批账号时 I1／I2 绿、账号全部了结、无多余真登录；
   ② 在某执行体的在飞行上 SIGKILL 后，它的在飞行被收尸（回退待领 ＋ `epoch+1`），
      其余账号不重不丢，且**恢复轮**能把被收尸的账号领走签成；
   ③ 以旧 owner ＋ 旧 epoch 发收尾写被 `settle_tasks` fence（0 行命中、行不变）；
   ④ 慢执行体持有在飞行时，兜底身份带 60s 肘界领不到该行（让位判向）。
对应实现：yiban/engine/workers.py（run_worker_supervisor、_reap_dead_worker）、
   yiban/store/queue_store.py（claim_batch / settle_tasks / reap_abandoned）、
   yiban/engine/executor_v3.py（领取/收尾/肘界）。
关键断言（**不变式**，不是编舞）：
   I1 —— 同一账号在同一时刻不得被两个执行体持有。判据：`sign_tasks` 一行的 `state`
   连续两次采样都是 `claimed`，而 `owner` 变了（持有者还在飞，另一个执行体把它抢走）。
   I2 —— 任何时刻都不该有"无人持有且不可再领"的账号（那等于漏签）。判据：一轮结束后
   不得留下 `state='claimed'` 的行。
   有牙证明分两段、**顺序不可换**：先由**哨兵**断言变异真的贴上了（标记 ＋ 行为），
   再断言不变式因此变红——否则红的时候分不清"变异没贴"还是"不变式没破"（CI 实测过）。
   确定性来自**屏障 ＋ 同伴**：目标执行体领到后卡在可注入的持有闸门上等放行，驱动侧
   见到就绪标记才动手（杀它／让同伴抢它那一行），不靠调度运气。
依赖：临时 sqlite ＋ 真子进程（POSIX：SIGKILL ／ `/proc` 查进程环境）；本地假上游；
   传输层改写结构性保证零外联。非 POSIX 平台整组 skip。
"""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import drill_multi_exec as drill  # noqa: E402

_POSIX = (os.name == "posix"
          and hasattr(__import__("signal"), "SIGKILL")
          and os.path.isdir("/proc"))


@unittest.skipUnless(_POSIX, "SIGKILL 与 /proc 查进程环境仅 POSIX 可用")
class MultiExecutorDrillInvariantsTest(unittest.TestCase):
    """两条不变式 ＋ 四个旋钮的靶场 e2e。"""

    #: 用例规模压到最小：2 执行体 × 4 账号，判据不变、墙钟最短。
    EXECUTORS = 2
    ACCOUNTS = 4

    def _run(self, **kw):
        kw.setdefault("executors", self.EXECUTORS)
        kw.setdefault("accounts", self.ACCOUNTS)
        # 全量并发跑测（-n auto）时机器争用重：靶场拉起的是**真子进程**，超时给足，
        # 否则夹具被饿死会让"注入没跑到"伪装成"不变式没红"。
        kw.setdefault("timeout_sec", 300)
        return drill.run_drill(**kw)

    def test_baseline_two_executors_invariants_green(self):
        """两个执行体领同一批账号：I1／I2 绿、全部了结、无多余真登录。"""
        res = self._run()
        self.assertTrue(res["i1_ok"], "I1 必须绿：无并发双持有；违规=%s" % res["i1_violations"])
        self.assertTrue(res["i2_ok"], "I2 必须绿：无残留 claimed；残留=%s" % res["i2_claimed_left"])
        self.assertEqual(res["done_count"], self.ACCOUNTS, "每个账号都应签成")
        self.assertLessEqual(res["signin_excess"], 0,
                             "正常一轮每账号恰好一次真登录（无超额=%s）" % res["signin_excess"])
        self.assertEqual(res["i2_claimed_left"], [])

    def test_kill_reaps_recovers_stale_write_and_yields(self):
        """SIGKILL 在飞行持有者：收尸（回退待领＋epoch+1）、旧代写被 fence、恢复轮不丢。"""
        res = self._run(kill_at="claim", stale_write=True)
        self._assert_barrier_ready(res)
        kill = res["kill"]
        self.assertIsNotNone(kill.get("killed_pid"), "杀点必须真的命中一个执行体子进程")
        phone = kill["killed_phone"]
        self.assertIsNotNone(phone, "杀点必须在一个在飞行上命中")
        # 收尸证据：第一轮结束时该行已回退待领，且换代 +1、不再归任何执行体。
        first = res["first_round_rows"][phone]
        self.assertEqual(first["state"], "pending",
                         "被杀执行体的在飞行必须被收尸回退待领（不是留 claimed）")
        self.assertEqual(int(first["epoch"]), int(kill["killed_epoch"]) + 1,
                         "收尸必须换代 +1（fence 迟到旧代写）")
        self.assertEqual(first["owner"], "", "回退后的行不再归任何执行体")
        # 迟到旧代写：用被杀者旧 owner ＋ 旧 epoch，必须被 fence（0 行、行不变）。
        stale = res["stale_write"]
        self.assertTrue(stale.get("attempted"), "迟到旧代写注入必须真的发出去")
        self.assertEqual(stale["rows_affected"], 0, "旧代写必须 0 行命中（被 epoch fence）")
        self.assertTrue(stale["fenced"], "旧代写必须被 fence：行不变")
        # 不丢：恢复轮把被收尸的账号领走，全量签成；两条不变式全程仍绿。
        self.assertEqual(res["done_count"], self.ACCOUNTS,
                         "恢复轮必须把被收尸的账号领走（不丢）；pending=%s"
                         % res.get("first_round_pending"))
        self.assertTrue(res["i1_ok"] and res["i2_ok"], "恢复全程两条不变式仍须绿")
        self.assertLessEqual(res["signin_excess"], 0)
        # 让位判向：慢持有者在飞时，兜底带 60s 肘界领不到它的行。
        probe = res.get("elbow_probe") or {}
        self.assertTrue(probe.get("ran"), "持有期探针必须跑到")
        self.assertEqual(probe["fallback_claimed_rows"], [],
                         "兜底带 60s 肘界不得领走刚到的行")
        self.assertTrue(probe["no_yield_violation"])

    # ---- 有牙证明：对协调逻辑变异，两条不变式必须红 ----
    #
    # 两段断言**分开**，顺序不可换：
    # ① 哨兵：变异真的贴上并改变了行为——不过就报"**变异未生效**"，与不变式无关；
    # ② 不变式：因此变红。
    # 这样红的时候一眼可分"变异没贴"与"不变式没被打破"（CI 实测过这两种被混成一条）。

    def test_teeth_i2_reap_off_leaves_claimed_row(self):
        """变异①：去掉轮末收尸 ⇒ 被杀执行体的在飞行留在 claimed ⇒ I2 红。"""
        res = self._run(kill_at="claim", mutate="reap-off", recover=False)
        self._assert_barrier_ready(res)
        self._assert_mutation_applied(res, "reap-off")
        self.assertFalse(res["i2_ok"], "收尸已成空操作，I2 必须红")
        self.assertTrue(res["i2_claimed_left"],
                        "必须留下 state='claimed' 的行（漏签形态）")
        self.assertFalse(res["terminal_or_pending"],
                         "留下的 claimed 行不是可再领态；应有账号不可再领")
        left = {r["phone"] for r in res["i2_claimed_left"]}
        self.assertIn(res["kill"]["killed_phone"], left,
                      "留下的必须正是被杀执行体那一行")

    def test_teeth_i1_claim_steal_double_holds(self):
        """变异②：去掉领取互斥（可抢在飞行）⇒ 同一账号被两个执行体持有 ⇒ I1 红。

        确定性来自**屏障 ＋ 同伴**：屏障让真执行体 A 持着在飞行并卡住；同伴进程（带
        放宽后的领取谓词、用执行体 B 的身份）去抢**A 正持有的那一行**。同伴抢成功后
        监控必然看到 `claimed(A)` → `claimed(B)` 的换手——不再指望两条腿恰好撞上。
        """
        res = self._run(mutate="claim-steal")
        self._assert_barrier_ready(res)
        self._assert_mutation_applied(res, "claim-steal")
        probe = res["mutation_probe"]
        self.assertEqual(probe.get("state_before_peer_claim"), "claimed",
                         "哨兵前提：目标行在同伴动手前**本就被持有**（否则'抢'只是合法领取）")
        self.assertFalse(res["i1_ok"], "领取互斥已不在，I1 必须红；违规=%s"
                         % res["i1_violations"])
        viol_phones = {v["phone"] for v in res["i1_violations"]}
        self.assertIn(res.get("barrier_phone"), viol_phones,
                      "违规必须落在屏障观测到的那一行（确定性：同伴抢的就是这一行）")

    def _assert_barrier_ready(self, res):
        """屏障必须就绪：它是本组确定性断言的**前提**。

        不就绪时不许继续往下断言——那时"不变式绿"或"杀点没命中"都不是被测性质
        的结论，而是前提没搭起来。**先报这一条**，红的时候一眼看出卡在哪。
        """
        self.assertTrue(
            res.get("barrier_ready"),
            "屏障未就绪：目标执行体没能在 %s 秒内'领到一行并持着在飞行'——"
            "本组断言的确定性前提不成立，后面每条断言都不作数"
            % drill.HOLD_MAX_SEC)

    def _assert_mutation_applied(self, res, mutate):
        """哨兵断言：变异已生效。不过则报**变异未生效**（与不变式红与否无关）。"""
        probe = res.get("mutation_probe") or {}
        self.assertIn("mutation_applied", res, "靶场未回哨兵结论（mutation_applied 缺失）")
        self.assertTrue(
            res["mutation_applied"],
            "变异未生效：DRILL_MUTATE=%s 未真正改变被测函数（哨兵结论=%s）——"
            "此时不变式绿是**假绿**，与'不变式没被打破'是两件事"
            % (mutate, probe))


if __name__ == "__main__":
    unittest.main(verbosity=2)
