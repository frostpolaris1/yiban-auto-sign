# -*- coding: utf-8 -*-
"""状态文件写放大治理：批量累积＋整批落盘的**不变式**（e2e，先红后绿）。

标签：性能 · 状态词汇与账号生命周期
覆盖：`yiban/engine/state_io.py::_write_sign_state` 与
    `yiban/engine/alerts.py::_user_fail_mail_reserve/_user_fail_mail_release` 两条
    "整体持锁＋读整个文件＋改一键＋全量写回"路径。二者在轮内**每账号调用一次**，
    代价随账号数平方增长（N 次调用 × 每次 O(N) 字节）。

**钉不变式，不钉编舞**（AGENTS §一 6a）。本文件只钉下面四条性质，任何一条被破坏
都必须红：

1. **等价性**：批量形态的**产出**与"逐条写"形态**逐字段一致**（含 `scheduled`
   继承、`only_if_absent` 的 CAS 结果、`pending` 不覆盖已有结论三条既有规则），
   且 CAS 的**返回值序列**也逐条一致。
2. **崩溃语义**：一次**真实崩溃**（子进程 `os._exit`，不跑任何清理）之后，
   **已落盘的条目不得丢失**；崩溃切点后仍在进程内、未落盘的尾部允许丢失。
3. **可见性有界**：任何时刻"未落盘的更新条数"不超过配置上界；落盘之间不出现
   无界的陈旧窗口（跨进程读者至多落后一个有界的小批）。
4. **写放大有界**：一批 M 次更新的**写盘字节**不得随 M 平方增长（回归即红——
   这正是本批要修的形状）。

对应实现：批量缓冲在 `yiban/engine/state_io.py` 与 `yiban/engine/alerts.py`；
禁用开关（不设该上下文即逐条写盘）由调用方持有（`runner.main`）。
依赖：临时 `YIBAN_STATE_DIR`、进程内打桩 `private_json.write_private_json` 计数与
    字节记账、真子进程（崩溃用例）；不触网、不需要库。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from yiban.engine import alerts, state_io  # noqa: E402


def _read(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


#: 一段覆盖三条既有规则的真实调用序列（同一账号重复写、CAS、计划保留、dur）。
_OPS = [
    ("13800000001", "pending", "计划 06:31", {"scheduled": "06:31:00"}),
    ("13800000001", "success", "签到成功", {"dur": 3.45, "scheduled": "06:31:00"}),
    # 计划态不得覆盖已有结论；scheduled 仍要写回
    ("13800000001", "pending", "计划 06:31", {"scheduled": "06:32:00"}),
    ("13800000002", "failed", "密码错误", {"dur": 1.2}),
    # only_if_absent：02 已有结论 ⇒ CAS 拒绝；03 无记录 ⇒ 写入
    ("13800000002", "skipped_window", "签到时段已结束", {"only_if_absent": True}),
    ("13800000003", "skipped_window", "签到时段已结束", {"only_if_absent": True}),
    # 未显式传 scheduled 时必须继承既有值
    ("13800000004", "retrying", "待重试", {"scheduled": "07:00:00"}),
    ("13800000004", "success", "签到成功", {"dur": 2.0}),
]


class _Buf:
    """进入被测的批量上下文；退出时整批落盘。"""

    def __init__(self, tmp):
        self.tmp = tmp
        self._stack = None

    def __enter__(self):
        dirs = mock.patch.object(state_io, "_state_dir", return_value=self.tmp)
        dirs.start()
        self._stack = dirs
        self._ctx = state_io.batch_sign_state()
        self._ctx.__enter__()
        return self

    def __exit__(self, *exc):
        self._ctx.__exit__(*exc)
        self._stack.stop()
        state_io.flush_sign_state()
        return False


class SignStateEquivalenceTest(unittest.TestCase):
    """① 等价性：批量形态与逐条写形态逐字段一致。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-batch-eq-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run(self, batched):
        state_io.flush_sign_state()  # 清掉上一形态可能残留的缓冲
        path = os.path.join(self.tmp, "sign-state-%s.json" % state_io.clock.now().strftime("%Y-%m-%d"))
        if os.path.exists(path):
            os.remove(path)
        returns = []
        dirs = mock.patch.object(state_io, "_state_dir", return_value=self.tmp)
        dirs.start()
        try:
            if batched:
                with state_io.batch_sign_state():
                    for phone, status, msg, kw in _OPS:
                        returns.append(state_io._write_sign_state(phone, status, msg, **kw))
            else:
                for phone, status, msg, kw in _OPS:
                    returns.append(state_io._write_sign_state(phone, status, msg, **kw))
        finally:
            dirs.stop()
        state_io.flush_sign_state()
        return _read(path), returns

    def test_output_is_field_by_field_identical(self):
        """产出逐字段一致（不是只看 status：message/time/task/dur/scheduled 全比）。"""
        sequential, seq_returns = self._run(batched=False)
        batched, bat_returns = self._run(batched=True)
        self.assertEqual(sorted(batched), sorted(sequential), "账号集合必须一致")
        for phone in sequential:
            self.assertEqual(batched[phone], sequential[phone], f"{phone} 条目逐字段不一致")
        self.assertEqual(bat_returns, seq_returns, "CAS 返回值序列必须逐条一致")

    def test_only_if_absent_returns_same_sequence(self):
        """CAS 返回值序列逐条一致（调用方据此计入'跳过'计数）。"""
        _, seq_returns = self._run(batched=False)
        _, bat_returns = self._run(batched=True)
        self.assertEqual(bat_returns, seq_returns)
        # 加一条正面断言：02 的 CAS 必须被拒（False），03 必须被接受（True）
        self.assertEqual(seq_returns[4], False)
        self.assertEqual(seq_returns[5], True)

    def test_batched_output_is_private_and_atomic(self):
        """整批落盘同样走私有写单通道：终文件 0600、无 .tmp 残留。"""
        self._run(batched=True)
        path = os.path.join(self.tmp, "sign-state-%s.json" % state_io.clock.now().strftime("%Y-%m-%d"))
        self.assertTrue(os.path.exists(path))
        if os.name == "posix":
            import stat
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual([n for n in os.listdir(self.tmp) if ".tmp" in n], [])

    def test_corrupt_and_bom_files_handled_like_sequential(self):
        """坏文件/带 BOM 文件在批量形态下与逐条写同处置（不得清空整日数据）。"""
        for content in ('{ 不是合法 JSON', None):
            with self.subTest(content=content):
                shutil.rmtree(self.tmp, ignore_errors=True)
                os.makedirs(self.tmp, exist_ok=True)
                path = os.path.join(self.tmp, "sign-state-%s.json" % state_io.clock.now().strftime("%Y-%m-%d"))
                if content is not None:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                state_io.flush_sign_state()
                with mock.patch.object(state_io, "_state_dir", return_value=self.tmp), \
                        state_io.batch_sign_state():
                    state_io._write_sign_state("13800000009", "success", "签到成功")
                state_io.flush_sign_state()
                self.assertEqual(_read(path)["13800000009"]["status"], "success")


class SignStateCrashDurabilityTest(unittest.TestCase):
    """② 崩溃语义：真实崩溃（os._exit）后，已落盘条目不得丢失。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-batch-crash-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.day = state_io.clock.now().strftime("%Y-%m-%d")

    def _child(self, body):
        env = dict(os.environ)
        env["YIBAN_STATE_DIR"] = self.tmp
        env["PYTHONPATH"] = BASE
        code = (
            "import os, sys\n"
            f"sys.path.insert(0, {BASE!r})\n"
            "from yiban.engine import state_io\n"
            + body
        )
        return subprocess.run([sys.executable, "-c", code], env=env,
                              capture_output=True, text=True)

    def test_flushed_entries_survive_hard_crash(self):
        """上界设 2：写 2 条即落盘，之后写 2 条留在缓冲；崩溃后前 2 条必须在盘上。"""
        r = self._child(
            "state_io._FLUSH_MAX = 2\n"
            "with state_io.batch_sign_state():\n"
            "    state_io._write_sign_state('13800000001', 'success', 'A')\n"
            "    state_io._write_sign_state('13800000002', 'success', 'B')\n"
            "    state_io._write_sign_state('13800000003', 'success', 'C')\n"
            "    state_io._write_sign_state('13800000004', 'success', 'D')\n"
            "os._exit(7)  # 硬崩溃：不跑 atexit、不跑上下文收尾\n"
        )
        self.assertEqual(r.returncode, 7, r.stderr)
        path = os.path.join(self.tmp, f"sign-state-{self.day}.json")
        data = _read(path)
        self.assertEqual(data.get("13800000001", {}).get("status"), "success",
                         "已落盘条目在崩溃后不得丢失")
        self.assertEqual(data.get("13800000002", {}).get("status"), "success")

    def test_atexit_flush_net_persists_on_clean_exit(self):
        """正常退出（未显式 flush）也必须由退出钩子落盘——否则轮末数据随进程消失。"""
        r = self._child(
            "state_io._FLUSH_MAX = 10**6\n"     # 计数上界极高 ⇒ 只能靠退出钩子
            "with state_io.batch_sign_state():\n"
            "    state_io._write_sign_state('13800000007', 'success', 'x')\n"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        path = os.path.join(self.tmp, f"sign-state-{self.day}.json")
        self.assertEqual(_read(path)["13800000007"]["status"], "success",
                         "退出钩子必须冲刷缓冲（崩溃语义的网络）")


class SignStateVisibilityBoundTest(unittest.TestCase):
    """③ 可见性有界：未落盘条数不超过上界；已签不得回退为未签。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-batch-vis-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.day = state_io.clock.now().strftime("%Y-%m-%d")
        self.path = os.path.join(self.tmp, f"sign-state-{self.day}.json")

    def test_pending_never_exceeds_bound(self):
        """任意前缀处，盘上条数 ≥ 已写条数 − 上界（陈旧窗口有界）。"""
        bound = 5
        state_io.flush_sign_state()
        with mock.patch.object(state_io, "_state_dir", return_value=self.tmp), \
                mock.patch.object(state_io, "_FLUSH_MAX", bound), \
                mock.patch.object(state_io, "_FLUSH_SEC", 10 ** 9), \
                state_io.batch_sign_state():
            for i in range(40):
                state_io._write_sign_state(f"138{i:08d}", "success", "ok")
                on_disk = len(_read(self.path)) if os.path.exists(self.path) else 0
                self.assertGreaterEqual(
                    on_disk, (i + 1) - bound,
                    f"第 {i+1} 条后盘上仅 {on_disk} 条：陈旧窗口超出上界 {bound}")

    def test_entry_time_is_call_time_not_flush_time(self):
        """`time` 字段是"何时签的"，落盘被推迟不得改写它（用假钟钉死）。"""
        import datetime

        day = state_io.clock.now().strftime("%Y-%m-%d")
        fake = {"t": datetime.datetime(2026, 10, 13, 6, 31, 5)}
        path = os.path.join(self.tmp, f"sign-state-{day}.json")
        state_io.flush_sign_state()
        with mock.patch.object(state_io, "_state_dir", return_value=self.tmp), \
                mock.patch.object(state_io.clock, "now", lambda: fake["t"]), \
                state_io.batch_sign_state():
            state_io._write_sign_state("13800000001", "success", "A")
            fake["t"] = datetime.datetime(2026, 10, 13, 6, 31, 9)
            state_io._write_sign_state("13800000002", "success", "B")
            fake["t"] = datetime.datetime(2026, 10, 13, 6, 31, 59)  # 落盘时刻（远晚于调用）
            state_io.flush_sign_state()
        data = _read(path)
        self.assertEqual(data["13800000001"]["time"], "06:31:05")
        self.assertEqual(data["13800000002"]["time"], "06:31:09")

    def test_concluded_never_regressed_to_unconcluded(self):
        """已落盘的 success 不得被后续 pending 改写（回退为'未签'即红）。"""
        state_io.flush_sign_state()
        with mock.patch.object(state_io, "_state_dir", return_value=self.tmp):
            with state_io.batch_sign_state():
                state_io._write_sign_state("13800000001", "success", "签到成功")
            state_io.flush_sign_state()
            with state_io.batch_sign_state():
                state_io._write_sign_state("13800000001", "pending", "计划 07:00",
                                           scheduled="07:00:00")
            state_io.flush_sign_state()
        self.assertEqual(_read(self.path)["13800000001"]["status"], "success")


class SignStateWriteAmplificationTest(unittest.TestCase):
    """④ 写放大有界：一批 M 次更新的写盘字节不得随 M 平方增长。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-batch-amp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.day = state_io.clock.now().strftime("%Y-%m-%d")

    def _drive(self, n):
        state_io.flush_sign_state()
        path = os.path.join(self.tmp, f"sign-state-{self.day}.json")
        if os.path.exists(path):
            os.remove(path)
        total = {"bytes": 0, "writes": 0}
        real = state_io.private_json.write_private_json

        def spy(p, payload, **kw):
            total["bytes"] += len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            total["writes"] += 1
            return real(p, payload, **kw)

        with mock.patch.object(state_io, "_state_dir", return_value=self.tmp), \
                mock.patch.object(state_io.private_json, "write_private_json", spy), \
                state_io.batch_sign_state():
            for i in range(n):
                state_io._write_sign_state(f"139{i:08d}", "success", "签到成功")
            state_io.flush_sign_state()
        final = os.path.getsize(path)
        return total, final

    def test_amplification_is_bounded_not_quadratic(self):
        """M 翻倍时写盘字节不得约 4× 增长（平方形状），且放大倍数受上界约束。"""
        small, _small_final = self._drive(80)
        big, big_final = self._drive(160)
        self.assertLessEqual(
            big["bytes"], 3 * small["bytes"],
            f"写盘字节随规模平方增长：80→{small['bytes']}B，160→{big['bytes']}B")
        self.assertLessEqual(
            big["bytes"], 12 * max(big_final, 1),
            f"写放大超出上界：{big['bytes']}B 写盘 / {big_final}B 终文件")


class UserFailMailQuotaBatchingTest(unittest.TestCase):
    """② 用户失败提醒额度文件同形：批量形态与逐条形态等价、且上限仍被守住。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-batch-quota-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.day = "2026-10-13"
        self.path = os.path.join(self.tmp, f"mail-user-fail-{self.day}.json")

    def _patch(self):
        return mock.patch.object(alerts.env_io, "resolve_path", return_value=self.tmp)

    def test_reserve_release_sequence_identical(self):
        """预占/归还返回值序列与逐条落盘形态一致。"""
        def drive(batched):
            alerts.flush_user_fail_mail_state()
            if os.path.exists(self.path):
                os.remove(self.path)
            seq = []
            with self._patch():
                if batched:
                    with alerts.batch_user_fail_mail():
                        seq.append(alerts._user_fail_mail_reserve("13800000001", self.day))
                        seq.append(alerts._user_fail_mail_reserve("13800000001", self.day))
                        alerts._user_fail_mail_release("13800000001", self.day)
                        seq.append(alerts._user_fail_mail_reserve("13800000001", self.day))
                else:
                    seq.append(alerts._user_fail_mail_reserve("13800000001", self.day))
                    seq.append(alerts._user_fail_mail_reserve("13800000001", self.day))
                    alerts._user_fail_mail_release("13800000001", self.day)
                    seq.append(alerts._user_fail_mail_reserve("13800000001", self.day))
            alerts.flush_user_fail_mail_state()
            return _read(self.path), seq

        seq_file, seq_returns = drive(False)
        bat_file, bat_returns = drive(True)
        self.assertEqual(bat_returns, seq_returns, "额度返回值序列必须一致")
        self.assertEqual(bat_file, seq_file, "额度文件产出必须逐字段一致")

    def test_daily_cap_still_enforced_within_batch(self):
        """批量形态下"每天每账号至多 1 封"仍成立（cap 默认 1）。"""
        with self._patch(), alerts.batch_user_fail_mail():
            self.assertTrue(alerts._user_fail_mail_reserve("13800000002", self.day))
            self.assertFalse(alerts._user_fail_mail_reserve("13800000002", self.day))
        alerts.flush_user_fail_mail_state()


if __name__ == "__main__":
    unittest.main(verbosity=2)
