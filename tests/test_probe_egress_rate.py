# -*- coding: utf-8 -*-
"""探针必须走与执行体同一套出口限速，并记进同一个持久化出口桶（工单 ba-p04-08）。

标签：B · 调度：领取/队列/执行体
覆盖：`probe.run_probe` 主循环前接出口桶 / 全局 Λ / 每账号 gap 三道闸；探针按**本进程
   执行体身份**解析出口（不再恒取 `ROLE_SINGLE`）；探针登录量落进与**同出口的执行体**
   同一个 `egress_state` 行（桶键 = 出口标识，工单 2cwd），并写回以便执行体重启后装回；
   等待总时长有界。

对应实现：yiban/engine/probe.py（`run_probe`、`verify_account`、`_apply_egress_proxy`、
   `_resolve_egress`、`_executor_identity`）、yiban/engine/token_bucket.py（EgressLimiter /
   GlobalLimiter / AccountGapGate）、yiban/store/queue_store.py（egress_state 读写）。

关键断言（通用工程语义，不为本项目特性豁免）：
1. 探针是"服务器向平台发起的真实登录"，与签到同一风控暴露面；因此它必须与执行体受
   **同一道**限速，且登录量记进**同一个**出口桶——否则跨进程仍是两套限速、平台侧限流
   时当天签到的 AIMD 学不到（信号不落在同一桶键上）。
2. 清单/多执行体形态下探针进程带着 `YIBAN_EXECUTOR_ID`，其出口必须是**该槽位自己的
   出口**（`egress.resolve` 按角色取值），不是恒定的 `ROLE_SINGLE`——那既不是任何 worker
   槽位也不是兜底行的出口，常等于宿主直连。
3. 等待用阻塞 sleep，但**总时长有界**：探针有 cadence，到点即停止本轮剩余账号并留痕，
   绝不放掉限速（失败方向必须是"这轮少探"，不是"放行"）。

依赖：临时 sqlite + 环境变量打桩 + 假客户端；不触网、不真实 sleep（注入假时钟）。
"""

import contextlib
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import db

from yiban import egress
from yiban.engine import probe
from yiban.store import queue_store

TEST_KEY = "a" * 64
SINGLE_PROXY = "http://single-user:pw@127.0.0.1:9001"
WORKER2_PROXY = "http://w2-user:pw@127.0.0.1:9002"


class _ProbeCase(unittest.TestCase):
    """公共基座：临时库 + 隔离的状态/环境变量 + 假网络 + 假时钟。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-probe-rate-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.state_dir = os.path.join(cls.tmp, "state")
        os.makedirs(cls.state_dir, exist_ok=True)
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.state_dir,
        })

    @classmethod
    def tearDownClass(cls):
        cls._close_conn()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_EXECUTOR_ID", "YIBAN_PROXY",
                  "YIBAN_PROXY_LIST", "YIBAN_GLOBAL_RATE", "YIBAN_EGRESS_RATE",
                  "YIBAN_EXECUTORS"):
            os.environ.pop(k, None)

    @staticmethod
    def _close_conn():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def setUp(self):
        self._close_conn()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        # 每个用例从干净环境开始：只留库/环境文件/状态目录三条定位键
        for k in ("YIBAN_EXECUTOR_ID", "YIBAN_PROXY", "YIBAN_PROXY_LIST",
                  "YIBAN_GLOBAL_RATE", "YIBAN_EGRESS_RATE"):
            os.environ.pop(k, None)
        self._patches = [
            mock.patch.object(probe, "PROBE_ENABLE", True),
            mock.patch.object(probe, "PROBE_INTERVAL", "1"),
            mock.patch.object(probe, "_health_probe_due", return_value=True),
            mock.patch.object(probe, "_update_probe_state_run"),
            mock.patch.object(probe.state_io, "_load_cred_state", return_value={}),
            mock.patch.object(probe.alerts, "_collect_admin_mail"),
            mock.patch.object(probe.alerts, "send_user_fail_mail"),
            mock.patch.object(probe.alerts, "_flush_admin_mail_summary"),
            mock.patch.object(probe.db, "add_sign_event"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._close_conn()

    # ---- 假时钟：把 sleep 变成"推进一个可读的浮点秒"，用例零真实等待 ----
    def _install_fake_clock(self, start=0.0):
        clock = {"t": float(start)}

        def _mono():
            return clock["t"]

        def _sleep(sec):
            clock["t"] += max(0.0, float(sec))

        self.addCleanup(mock.patch.object(probe, "_mono", _mono).stop)
        self.addCleanup(mock.patch.object(probe, "_sleep", _sleep).stop)
        mock.patch.object(probe, "_mono", _mono).start()
        mock.patch.object(probe, "_sleep", _sleep).start()
        return clock

    def _accounts(self, n, owner="o@example.com"):
        return [SimpleNamespace(phone="1380000%04d" % i, owner=owner)
                for i in range(n)]

    def _verified_ok_client(self):
        """假客户端：登录那一刻把 session 上挂的出口记下来，供断言。"""
        seen = {}

        class _Fake:
            def __init__(self, account):
                self.account = account
                self.session = SimpleNamespace(proxies={})
                self.use_killyiban = False

            def login(self):
                seen.setdefault(self.account.phone, dict(self.session.proxies))
                return True

            login_killyiban = login

            def verify(self):
                return True, "账号健康，可正常签到"

            def _wipe_credentials(self):
                pass

        return _Fake, seen


class ProbeEgressIdentityTest(_ProbeCase):
    """缺陷二：探针出口必须按本进程执行体身份解析，不再恒取 ROLE_SINGLE。"""

    def test_probe_uses_worker_slot_egress_not_single(self):
        os.environ["YIBAN_EXECUTOR_ID"] = "worker-2@host"
        os.environ["YIBAN_PROXY"] = SINGLE_PROXY
        os.environ["YIBAN_PROXY_LIST"] = (
            "http://w0:0,http://w1:1," + WORKER2_PROXY)
        fake, seen = self._verified_ok_client()
        with mock.patch.object(probe, "YibanClient", fake), \
                mock.patch.object(probe, "account_still_signable", lambda a: True):
            probe.run_probe(self._accounts(1))
        self.assertEqual(
            seen.get("13800000000"),
            {"http": WORKER2_PROXY, "https": WORKER2_PROXY},
            "多执行体形态下探针必须走它自己槽位的出口，而不是恒取 ROLE_SINGLE",
        )

    def test_verify_account_single_form_still_reads_yiban_proxy(self):
        """web 注册/改密（无 YIBAN_EXECUTOR_ID）仍按单执行体读 YIBAN_PROXY。"""
        os.environ.pop("YIBAN_EXECUTOR_ID", None)
        os.environ["YIBAN_PROXY"] = SINGLE_PROXY
        os.environ["YIBAN_PROXY_LIST"] = "http://w0:0,http://w1:1"
        fake, seen = self._verified_ok_client()
        with mock.patch.object(probe, "YibanClient", fake), \
                mock.patch.object(probe, "account_still_signable", lambda a: True):
            probe.verify_account(SimpleNamespace(phone="13900000000"))
        self.assertEqual(seen.get("13900000000"),
                         {"http": SINGLE_PROXY, "https": SINGLE_PROXY})


class ProbeSharesExecutorBucketTest(_ProbeCase):
    """缺陷一：探针登录量必须记进与执行体同一个持久化出口桶（同一出口键）。

    工单 2cwd 后桶键是**出口标识**（`egress.egress_identity`），不再是执行体身份：同一出口的
    探针与执行体因此共桶；不同出口的执行体仍各持一桶。
    """

    def test_probe_consumes_and_persists_same_key_as_executor(self):
        os.environ["YIBAN_EXECUTOR_ID"] = "single@host"   # 单执行体、未配出口 = 直连
        key = egress.DIRECT_EGRESS
        # 执行体已落过**出口级**桶状态：速率 1.0、突发 1
        self.assertTrue(queue_store.save_egress_state(key, 1.0, 1, 0.0))
        clock = self._install_fake_clock(0.0)
        with mock.patch.object(probe, "verify_account", return_value=(True, "ok")) as va:
            probe.run_probe(self._accounts(3))
        self.assertEqual(va.call_count, 3)
        row = queue_store.load_egress_state(key)
        self.assertIsNotNone(row, "探针必须把速率写回出口标识那个键（直连 = direct）")
        self.assertAlmostEqual(row["rate"], 1.0, places=9,
                               msg="探针不喂风控/成功信号，出口级速率不该被它改写")
        self.assertAlmostEqual(row["tat"], 0.0, places=9,
                               msg="tat 不落库（多进程不共享令牌位置）")
        # 执行体侧重新装回：看见同一出口级速率（否则跨进程不是同一份速率）
        from yiban.engine import token_bucket
        fresh = token_bucket.EgressLimiter()
        self.assertTrue(fresh.restore_from_store(key, now=clock["t"]))
        self.assertAlmostEqual(fresh.rate, 1.0, places=9)

    def test_probe_writes_under_outlet_identity_not_executor_identity(self):
        """探针不得按执行体身份另造桶键：非直连出口时键 = 该出口的标识。"""
        os.environ["YIBAN_EXECUTOR_ID"] = "worker-2@host"
        os.environ["YIBAN_PROXY_LIST"] = "http://w0:0,http://w1:1," + WORKER2_PROXY
        outlet = egress.egress_identity(WORKER2_PROXY)
        self._install_fake_clock(0.0)
        with mock.patch.object(probe, "verify_account", return_value=(True, "ok")):
            probe.run_probe(self._accounts(2))
        self.assertIsNotNone(queue_store.load_egress_state(outlet))
        self.assertIsNone(queue_store.load_egress_state("worker-2@host"),
                          "桶键是出口标识，不是执行体身份——不得按身份另造一份")


class ProbeThreeGatesTest(_ProbeCase):
    """三道闸（出口桶 / 全局 Λ / 每账号 gap）必须全部接在探针主循环上。"""

    def test_run_probe_attaches_limiter_global_and_gap(self):
        self._install_fake_clock(0.0)
        from yiban.engine import token_bucket
        calls = {"limiter": 0, "global": 0, "gap": 0}
        real_limit = token_bucket.limiter_from_env
        real_global = token_bucket.GlobalLimiter
        real_gap = token_bucket.gap_gate_from_env

        def _limit(*a, **k):
            calls["limiter"] += 1
            return real_limit(*a, **k)

        def _global(*a, **k):
            calls["global"] += 1
            return real_global(*a, **k)

        def _gap(*a, **k):
            calls["gap"] += 1
            return real_gap(*a, **k)

        with mock.patch.object(probe.token_bucket, "limiter_from_env", _limit), \
                mock.patch.object(probe.token_bucket, "GlobalLimiter", _global), \
                mock.patch.object(probe.token_bucket, "gap_gate_from_env", _gap), \
                mock.patch.object(probe, "verify_account", return_value=(True, "ok")):
            probe.run_probe(self._accounts(2))
        self.assertEqual(calls, {"limiter": 1, "global": 1, "gap": 1},
                         "出口桶 / 全局 Λ / gap 三道闸必须都接上")


class ProbeBoundedWaitTest(_ProbeCase):
    """等待总时长有界：到点停止本轮剩余账号，绝不放掉限速（失败方向=少探）。"""

    def test_hitting_budget_stops_remaining_accounts(self):
        os.environ["YIBAN_EXECUTOR_ID"] = "single@host"     # 未配出口 = 直连
        # 显式清单 = 1 个 worker（无兜底）⇒ 同出口执行体数 n=1 ⇒ 份额 = 出口级速率（不缩水）
        os.environ["YIBAN_EXECUTORS"] = egress.dump_manifest(
            [{"slot": 0, "type": "worker", "proxy": ""}])
        queue_store.save_egress_state(egress.DIRECT_EGRESS, 1.0, 1, 0.0)
        self._install_fake_clock(0.0)
        with mock.patch.object(probe, "PROBE_EGRESS_MAX_SEC", 2.5), \
                mock.patch.object(probe, "verify_account",
                                  return_value=(True, "ok")) as va, \
                self.assertLogs("yiban", level="WARNING") as cm:
            probe.run_probe(self._accounts(10))
        # rate=1/s、burst=1：预算 2.5s 内最多放行 3 次（t=0,1,2），第 4 次到点放弃
        self.assertLess(va.call_count, 10, "到点必须停止，不得把 10 个账号全跑完")
        self.assertEqual(va.call_count, 3,
                         "预算 2.5s、1 次/s 恰好放行 3 次（t=0/1/2），第 4 次超预算")
        self.assertTrue(any("预算" in m or "限速" in m for m in cm.output),
                        "停止必须留痕，不能静默少探")


class ProbeTimeWindowTest(_ProbeCase):
    """F2：探针时刻落在签到窗口内时告警（探针不计入出口预算分母）。"""

    def test_overlap_detection(self):
        cfg = {"sign_start": (6, 30), "sign_end": (7, 50)}
        for text, want in (("06:30", True), ("06:40", True), ("07:49", True),
                           ("07:50", False), ("05:00", False), ("20:00", False),
                           ("bad", False), ("25:00", False)):
            with self.subTest(text=text):
                self.assertEqual(
                    probe._probe_time_overlaps_sign_window(text, cfg=cfg), want)

    def test_cross_midnight_window(self):
        cfg = {"sign_start": (23, 0), "sign_end": (1, 0)}
        self.assertTrue(probe._probe_time_overlaps_sign_window("23:30", cfg=cfg))
        self.assertTrue(probe._probe_time_overlaps_sign_window("00:30", cfg=cfg))
        self.assertFalse(probe._probe_time_overlaps_sign_window("12:00", cfg=cfg))

    def test_run_probe_warns_when_time_inside_window(self):
        os.environ["YIBAN_EXECUTOR_ID"] = "single@host"
        self._install_fake_clock(0.0)
        with mock.patch.object(probe, "PROBE_TIME", "06:40"), \
                mock.patch.object(probe, "verify_account", return_value=(True, "ok")), \
                self.assertLogs("yiban", level="WARNING") as cm:
            probe.run_probe(self._accounts(1))
        self.assertTrue(any("落在签到窗口内" in m for m in cm.output), cm.output)


if __name__ == "__main__":
    unittest.main()
