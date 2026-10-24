# -*- coding: utf-8 -*-
"""异步校验的看门狗只计外呼时间，不计任何排队的秒数（R3-013）。

对应实现：`yiban/attempt/jobs.py` 的 `run()`——看门狗在**同账号互斥与全局席位都拿到
之后**才起表；两个等待各有界，超时只落任务"稍后重试"，不改账号状态。

为什么钉这条：旧实现把定时器起在 `seat.acquire` 之前，排队超过 `OUTBOUND_TIMEOUT`
（30 秒）的任务被判"外呼超时"，账号连带置为 rejected。全局席位只有 2 席，
同账号互斥还会叠加等待——排队越久越容易被判死，而这段时间一次外呼都没发生。

归属：`yiban/attempt/jobs.py` 的收口路径②（排队超时）与③（外呼超时）的边界。
"""
import contextlib
import threading
import time
import unittest
from unittest import mock

from yiban.attempt import jobs

PHONE = "13900001111"
USER = "watchdog@example.com"


class WatchdogScopeTest(unittest.TestCase):
    """排队不消耗外呼预算；外呼卡死仍准时收口。"""

    def setUp(self):
        self.seat = threading.BoundedSemaphore(1)
        self.account_lock = threading.Lock()
        self.rejected = []
        self.jobs_state = {}
        self.lock = threading.Lock()

    def _hooks(self):
        return dict(
            seat=self.seat,
            verify_one=lambda clean: None,
            record_failure=lambda fails, phone, err, ts: "other",
            mask_phone=lambda p: p,
            reject_account=lambda phone, reason, account_id, prev: self.rejected.append(reason),
            queue_full=lambda: False,
        )

    def _account_hooks(self):
        """同一账号互斥：`threading.Lock` 等价于宿主的按手机号信号量。"""
        hooks = self._hooks()
        hooks["acquire_account"] = lambda phone, timeout=None: self.account_lock.acquire(
            timeout=timeout)
        hooks["release_account"] = lambda phone: self.account_lock.release()
        return hooks

    def _fake_store(self):
        def _create(account_id, phone, username, prev_status="pending"):
            with self.lock:
                self.jobs_state = {"id": 1, "status": "running", "error": None}
            return 1, "2026-10-24 00:00:00"

        def _claim(job_id):
            with self.lock:
                self.jobs_state["status"] = "running"
            return True

        def _finish(job_id, error=None):
            with self.lock:
                self.jobs_state["error"] = error
                self.jobs_state["status"] = "rejected" if error else "done"
            return True

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(jobs.store, "create", _create))
        stack.enter_context(mock.patch.object(jobs.store, "claim", _claim))
        stack.enter_context(mock.patch.object(jobs.store, "finish", _finish))
        return stack

    def _wait_terminal(self, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.jobs_state.get("status") != "running":
                    return
            time.sleep(0.02)
        self.fail("任务未在预算内落到终态")

    def _run_job(self, hooks, *, outbound_timeout=0.2, gate_wait):
        """跑一条任务并等到终态；全部打桩在返回时恢复。"""
        with self._fake_store(), mock.patch.object(jobs, "_hooks", {}), \
                mock.patch.object(jobs, "OUTBOUND_TIMEOUT", outbound_timeout), \
                mock.patch.object(jobs, "GATE_WAIT", gate_wait):
            jobs.configure(**hooks)
            self.assertIsNotNone(jobs.start({"phone": PHONE, "status": "pending"},
                                            USER, 1, {}, None))
            self._wait_terminal()

    def test_seat_queue_time_does_not_consume_outbound_budget(self):
        """排队 3 倍外呼预算后拿到席位，任务仍应正常完成。"""
        self.seat.acquire()  # 先占满：后台线程必须排队
        with self._fake_store(), mock.patch.object(jobs, "_hooks", {}), \
                mock.patch.object(jobs, "OUTBOUND_TIMEOUT", 0.2):
            jobs.configure(**self._hooks())
            self.assertIsNotNone(jobs.start({"phone": PHONE, "status": "pending"},
                                            USER, 1, {}, None))
            time.sleep(0.6)  # 3 倍预算：旧实现此时已判"外呼超时"
            self.seat.release()
            self._wait_terminal()
        self.assertEqual(self.rejected, [], "排队期间不得判超时")
        self.assertEqual(self.jobs_state["status"], "done")
        self.assertIsNone(self.jobs_state["error"])

    def test_account_queue_time_does_not_consume_outbound_budget(self):
        """等同一账号的另一个外呼结束，不消耗外呼预算，也不改账号状态。"""
        self.account_lock.acquire()  # 先占住账号权：后台线程必须排队
        with self._fake_store(), mock.patch.object(jobs, "_hooks", {}), \
                mock.patch.object(jobs, "OUTBOUND_TIMEOUT", 0.2):
            jobs.configure(**self._account_hooks())
            self.assertIsNotNone(jobs.start({"phone": PHONE, "status": "pending"},
                                            USER, 1, {}, None))
            time.sleep(0.6)  # 3 倍预算：旧实现此时已判"外呼超时"
            with self.lock:
                self.assertEqual(self.jobs_state.get("status"), "running",
                                 "排队期间任务不得被看门狗收口")
            self.account_lock.release()
            self._wait_terminal()
        self.assertEqual(self.rejected, [], "排队期间不得把账号判 rejected")
        self.assertEqual(self.jobs_state["status"], "done")
        self.assertIsNone(self.jobs_state["error"])

    def test_seat_wait_timeout_does_not_reject_account(self):
        """等席位超时只让任务稍后重试，账号状态不变。"""
        self.seat.acquire()
        self._run_job(self._hooks(), gate_wait=0.2)
        self.assertEqual(self.rejected, [], "席位繁忙不是账号问题，不得判 rejected")
        self.assertIn("稍后重试", self.jobs_state["error"])

    def test_account_wait_timeout_does_not_reject_account(self):
        """等同一账号超时只让任务稍后重试，账号状态不变。"""
        self.account_lock.acquire()
        self._run_job(self._account_hooks(), gate_wait=0.2)
        self.assertEqual(self.rejected, [], "账号互斥等待超时不是账号问题")
        self.assertIn("稍后重试", self.jobs_state["error"])

    def test_verify_exception_settles_job_without_touching_account(self):
        """外呼实现抛异常：任务落终态，账号状态不变。"""
        def _boom(clean):
            raise RuntimeError("boom")
        hooks = self._hooks()
        hooks["verify_one"] = _boom
        self._run_job(hooks, outbound_timeout=5.0, gate_wait=5.0)
        self.assertEqual(self.jobs_state["status"], "rejected")
        self.assertIn("校验异常", self.jobs_state["error"])
        self.assertEqual(self.rejected, [], "内部异常不是账号问题")

    def test_half_registered_account_hooks_are_refused(self):
        """只登记 acquire 会让账号权永久泄漏：配置期直接拒绝。"""
        with mock.patch.object(jobs, "_hooks", {}), self.assertRaises(ValueError):
            jobs.configure(acquire_account=lambda phone, timeout=None: True)

    def test_watchdog_start_failure_settles_job_and_releases(self):
        """计时器起不来时任务落终态，且席位与账号权都还回去。"""
        with self._fake_store(), mock.patch.object(jobs, "_hooks", {}), \
                mock.patch.object(jobs.threading, "Timer", side_effect=RuntimeError("no timer")):
            jobs.configure(**self._account_hooks())
            self.assertIsNotNone(jobs.start({"phone": PHONE, "status": "pending"},
                                            USER, 1, {}, None))
            self._wait_terminal()
        self.assertEqual(self.jobs_state["status"], "rejected")
        self.assertIn("校验启动失败", self.jobs_state["error"])
        self.assertEqual(self.rejected, [], "启动失败不是账号问题")
        self.assertTrue(self.seat.acquire(timeout=0), "席位必须已释放")
        self.seat.release()
        self.assertFalse(self.account_lock.locked(), "账号权必须已释放")

    def test_outbound_call_still_times_out(self):
        """外呼本身卡住时，看门狗仍在 `OUTBOUND_TIMEOUT` 后收口（边界未放宽）。"""
        stop = threading.Event()

        def _hang(clean):
            stop.wait(3.0)
            return None
        hooks = self._hooks()
        hooks["verify_one"] = _hang
        with self._fake_store(), mock.patch.object(jobs, "_hooks", {}), \
                mock.patch.object(jobs, "OUTBOUND_TIMEOUT", 0.2):
            jobs.configure(**hooks)
            worker = threading.Thread(
                target=jobs.run,
                args=(1, {"phone": PHONE, "status": "pending"}, USER, 1, "pending", {}),
                daemon=True)
            worker.start()
            self._wait_terminal()
        stop.set()
        worker.join(timeout=3.0)
        self.assertFalse(worker.is_alive(), "挂起线程必须能被回收")
        self.assertEqual(self.jobs_state["status"], "rejected")
        self.assertEqual(self.rejected, ["外呼超时：校验未在预算内完成"])
        self.assertIn("超时", self.jobs_state["error"])


if __name__ == "__main__":
    unittest.main()
