# -*- coding: utf-8 -*-
"""signPosition 判据改用结构化 `State` 的守卫（工单 yiban-auto-sign-yng6）。

标签：D · 状态词汇与账号生命周期
覆盖：
  ① `State` 逐值映射：3 已签到 / 4 已更改 → `already`；2 无需签到 → `no_task`；
     5 补签中 → `supplementing`；0 可签到 / 1 未签到 → 照常取点位并提交；
  ② `State=4` **落了结**：真队列（`sign_tasks`）跑一轮后该账号记 `done`，且状态文件里
     记 `already` 时补签轮定向剔除去掉它；
  ③ `State=5` 的口径：不进了结、不进未了结、弃权落 `final:` 保守档（默认回炉口不复活，
     只有显式路径放行），且**绝不提交签到**；
  ④ `Msg` 文本不再作判据：`Msg` 与 `State` 冲突时以 `State` 为准；
  ⑤ 未知/缺失 `State`：出声（WARN 带 `State` 与 `Msg` 原文）+ 按 `Position` 保守回退，
     且**不许**退回旧的文本判据；
  ⑥ 突变验证：把源码里 `4: STATUS_ALREADY` 那一行删掉，本文件的守卫断言必须翻面。
对应实现：`yiban/client.py`（`SIGN_POSITION_STATE_STATUS` / `_as_state_int` /
    `_judge_by_state` / `YibanClient.signin`）、`yiban/status.py`（词表与三处集合口径）、
    `yiban/engine/executor_v3.py`（`_tier_prefix` / 了结收尾）、`yiban/store/queue_store.py`
    （`requeue_failed` 的档位过滤）。
关键断言：`State=4` 是 2026-10-08 只读采集的实拍形态（`Msg="已更改"`、`Position` 空）。
    旧实现按 `"已签到" in Msg` 判，两个文本判据都不命中，于是这个**已签到**的账号落进
    "Position 为空"分支被记成 `no_position`：台账错（管理员以为平台没开任务），且
    `no_position` 不算了结 ⇒ 补签轮重跑它 ⇒ **同一账号同一天多一次真实登录**。
    同一次采集确认 `State=5`（补签中）此前没有任何归属。
依赖：进程内响应桩（`session.get` / `session.post` 打桩）、临时 sqlite（`sign_tasks`）
    + 假时钟 + 替身限速三件套；不触网、不起子进程、不需 bash/docker。
"""
import asyncio
import contextlib
import datetime
import importlib.util
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import requests

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402
import signin  # noqa: E402

from yiban import client as yiban_client  # noqa: E402
from yiban import clock, egress  # noqa: E402
from yiban import status as yiban_status  # noqa: E402
from yiban.engine import executor_v3, state_io, token_bucket  # noqa: E402
from yiban.store import claims as claims_mod  # noqa: E402
from yiban.store import clock_meta, queue_store  # noqa: E402

TEST_KEY = "a" * 64
PHONE = "13800000001"
DAY = "2026-09-16"          # 周三，避开周末门
START = datetime.datetime(2026, 9, 16, 6, 40, 0)
OWNER = "single@testhost"
RUNTIME_OWNER = egress.runtime_owner(OWNER)

#: 一个形状合法的候选点位（只含客户端消费的键：Name / Points / Address）。
_POSITION = {
    "Name": "任务A",
    "Points": ["118.0,31.0", "118.1,31.0", "118.1,31.1", "118.0,31.1"],
    "Address": "点A",
    "LngLat": "118.05,31.05",
}

#: `State=4` 实拍形态的 `Msg` 原文（判据不许再看它，但日志与回退要用）。
_CAPTURED_MSG = "已更改"


# ---------------------------------------------------------------------------
# 响应桩：真 `YibanClient.signin()` 的判定
# ---------------------------------------------------------------------------
def _sign_position_payload(*, state=None, msg="", positions=None):
    """构造 signPosition 成功信封。

    `state=None` 表示**该键缺失**（用于"未知/缺失 State ⇒ 出声 + 回退"的用例）。
    """
    now = int(datetime.datetime.now().timestamp())
    data = {
        "Msg": msg,
        "IsNeedPhoto": 2,
        "Position": [_POSITION] if positions is None else positions,
        "Range": {"StartTime": now - 3600, "EndTime": now + 3600},
    }
    if state is not None:
        data["State"] = state
    return {"code": 0, "data": data}


def _stub_response(envelope):
    """响应桩：`is_blocked` 与信封解析都只读 `.text`。"""
    return SimpleNamespace(
        text=json.dumps(envelope, ensure_ascii=False), status_code=200, headers={}
    )


def _new_client(cls):
    """构造一个"已登录"的客户端（不跑登录、不触网）。"""
    client = cls.__new__(cls)
    client.account = signin.Account(phone=PHONE, password="p")
    client.logged_in = True
    client.use_killyiban = True
    client.csrf = "csrf-token"
    client.phone_model = "Vivo-Test"
    client.phone_code = "C" * 64
    client.session = requests.Session()
    client.session.headers = dict(signin.KILLYIBAN_HEADERS)
    return client


def _run_signin(payload, *, cls=None):
    """用响应桩跑一次真 `signin()`，返回 `(verdict 四元组, 是否提交过签到)`。"""
    client = _new_client(cls or signin.YibanClient)
    client.session.get = mock.Mock(return_value=_stub_response(payload))
    client.session.post = mock.Mock(return_value=_stub_response(
        {"code": 0, "data": {"Id": "1"}}))
    with mock.patch.object(signin.random, "shuffle", side_effect=lambda lst: None):
        verdict = client.signin()
    return verdict, bool(client.session.post.called)


class StateJudgementTest(unittest.TestCase):
    """`State` 逐值判定：映射表写死的每一格都要能被观察。"""

    def test_state_3_and_4_are_already(self):
        for state in (3, 4):
            with self.subTest(state=state):
                verdict, posted = _run_signin(
                    _sign_position_payload(state=state, positions=[]))
                self.assertTrue(verdict[0], verdict[1])
                self.assertEqual(verdict[3], yiban_status.STATUS_ALREADY)
                self.assertIn("已签到", verdict[1])
                self.assertFalse(posted, "平台已判今日已签到，不得再提交一次签到")

    def test_state_4_is_the_captured_shape(self):
        """实拍形态：State=4、Msg="已更改"、Position 为空 → 记 already（不是 no_position）。"""
        verdict, _posted = _run_signin(
            _sign_position_payload(state=4, msg=_CAPTURED_MSG, positions=[]))
        self.assertEqual(verdict[3], yiban_status.STATUS_ALREADY,
                         "State=4（已更改＝班委手动补签）就是今日已签到")
        self.assertNotEqual(verdict[3], yiban_status.STATUS_NO_POSITION)
        self.assertIn("已更改", verdict[1], "平台原值要留在 message 里，便于排障")

    def test_state_2_is_no_task(self):
        verdict, posted = _run_signin(
            _sign_position_payload(state=2, msg="今日无需签到", positions=[]))
        self.assertTrue(verdict[0])
        self.assertEqual(verdict[3], yiban_status.STATUS_NO_TASK)
        self.assertFalse(posted)

    def test_state_0_and_1_take_the_normal_position_branch(self):
        """0 可签到 / 1 未签到：没有结论，照常取点位并提交（真实提交一次）。"""
        for state in (0, 1):
            with self.subTest(state=state):
                verdict, posted = _run_signin(_sign_position_payload(state=state))
                self.assertTrue(posted, "State=0/1 必须照常提交签到")
                self.assertEqual(verdict[3], yiban_status.STATUS_SUCCESS)

    def test_state_5_is_supplementing_and_never_submits(self):
        """补签中：结果未定 ⇒ 不提交、不重试、只记状态；点位暂时可用同样不许提交。"""
        for positions in ([], [_POSITION]):
            with self.subTest(positions=len(positions)):
                verdict, posted = _run_signin(
                    _sign_position_payload(state=5, msg="补签中", positions=positions))
                self.assertEqual(verdict[3], yiban_status.STATUS_SUPPLEMENTING)
                self.assertFalse(posted, "补签中不得提交一次真实签到")
                self.assertTrue(verdict[2], "补签中不重试（skip=True，与窗口外跳过同语义）")

    def test_state_accepts_numeric_string(self):
        """上游数值字段可能以字符串下发（SPEC §3.1）：字符串 "4" 同样判 already。"""
        verdict, _posted = _run_signin(
            _sign_position_payload(state="4", msg=_CAPTURED_MSG, positions=[]))
        self.assertEqual(verdict[3], yiban_status.STATUS_ALREADY)

    def test_msg_text_is_no_longer_a_judgement(self):
        """`Msg` 只作日志原文：与 `State` 冲突时一律以 `State` 为准。"""
        # State=0（可签到）+ Msg="已签到"：旧实现会记 already，现在必须照常提交
        verdict, posted = _run_signin(
            _sign_position_payload(state=0, msg="今日已签到"))
        self.assertTrue(posted, "State=0 该照常签到，不许被 Msg 文本挡住")
        self.assertEqual(verdict[3], yiban_status.STATUS_SUCCESS)
        # State=2（无需签到）+ Msg="已签到"：结论取 State
        verdict2, posted2 = _run_signin(
            _sign_position_payload(state=2, msg="今日已签到", positions=[]))
        self.assertEqual(verdict2[3], yiban_status.STATUS_NO_TASK)
        self.assertFalse(posted2)

    def test_unknown_state_falls_back_loudly_without_text_judgement(self):
        """未知 State：WARN 带 State 与 Msg 原文，然后按 Position 保守回退。

        `State=6` 是"将来平台新增一枚枚举"的替身；回退**不允许**退回 `Msg` 文本判据
        ——两套判据并存正是本工单的缺陷形状，故此处刻意让 `Msg="已签到"` 与 `State`
        冲突，结论必须是 `no_position`（空点位）而不是 `already`。
        """
        with self.assertLogs(yiban_client.logger, level="WARNING") as cm:
            verdict, posted = _run_signin(
                _sign_position_payload(state=6, msg="已签到", positions=[]))
        logged = "\n".join(cm.output)
        self.assertIn("6", logged, "WARN 必须带上 State 原值")
        self.assertIn("已签到", logged, "WARN 必须带上 Msg 原文")
        self.assertEqual(verdict[3], yiban_status.STATUS_NO_POSITION)
        self.assertFalse(posted)

    def test_missing_state_falls_back_loudly_and_still_signs(self):
        """State 键缺失同样出声（不静默回退），有点位时照常走签到分支。"""
        with self.assertLogs(yiban_client.logger, level="WARNING") as cm:
            verdict, posted = _run_signin(_sign_position_payload(msg=""))
        self.assertIn("State", "\n".join(cm.output))
        self.assertTrue(posted, "State 缺失时按 Position 保守回退：有点位就照常签到")
        self.assertEqual(verdict[3], yiban_status.STATUS_SUCCESS)


class SupplementingVocabularyTest(unittest.TestCase):
    """`supplementing` 的三处集合口径与展示格。"""

    def test_not_in_done_and_not_in_undone(self):
        self.assertNotIn(yiban_status.STATUS_SUPPLEMENTING,
                         yiban_status.CLAIM_DONE_STATUSES,
                         "补签中结果未定，不许当'今日已了结'")
        self.assertNotIn(yiban_status.STATUS_SUPPLEMENTING,
                         yiban_status.UNDONE_STATUSES,
                         "补签中不进未了结：否则补签轮天天为它多跑一次真实登录")
        self.assertNotIn(yiban_status.STATUS_SUPPLEMENTING,
                         claims_mod.RETRYABLE_GIVE_UP_STATUSES,
                         "补签中不得落'默认可再领'档（那是'可自由再签'）")

    def test_display_and_symbol_are_complete(self):
        entry = yiban_status.DISPLAY[yiban_status.STATUS_SUPPLEMENTING]
        for key in ("symbol", "text", "legend", "tone"):
            self.assertTrue(entry[key], f"显示表缺 {key}")
        self.assertIn("补签", entry["text"])
        self.assertNotEqual(entry["tone"], "ok", "补签中不得渲染成已完成")


# ---------------------------------------------------------------------------
# 真队列（sign_tasks）上的后果：落了结 / 保守档
# ---------------------------------------------------------------------------
class _FakeClock:
    """`_now`/`_sleep`/`_mono` 共用一份推进（与 test_executor_v3 同口径）。"""

    def __init__(self, t=START):
        self.t = t
        self.mono = 10_000.0
        self.sleeps = []

    def now(self):
        return self.t

    async def sleep(self, sec):
        self.sleeps.append(sec)
        self.t += datetime.timedelta(seconds=sec)
        self.mono += sec
        await asyncio.sleep(0)


class _PermissiveLimiter:
    manual = False

    def bucket(self, egress_key):
        return SimpleNamespace(retry_after=lambda now: 0.0)

    def acquire(self, egress_key, now):
        return True

    def on_success(self, egress_key):
        pass

    def on_risk_signal(self, egress_key, now):
        pass

    def persist(self, egress_key, stamp=None):
        return True

    def restore_from_store(self, egress_key, now=None):
        return False


class _PermissiveGate:
    gap_sec = 10.0

    def allow(self, phone, now):
        return True

    def commit(self, phone, now):
        pass


async def _never(_ctx):
    await asyncio.Event().wait()


def _ts(**kw):
    return (START + datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _cfg():
    return {
        "order": "sequence", "dist": "uniform",
        "edge_front_sec": 60, "edge_back_sec": 60, "block_cap": 15,
        "mu_min_pct": 40, "mu_max_pct": 60, "sigma_min_pct": 15, "sigma_max_pct": 25,
        "min_exec_gap": 5, "avg_attempt_sec": 3, "retry_min_interval": 60,
        "exec_gap_min": 10, "allow_time_pref": 0,
        "sign_start": (6, 30), "sign_end": (7, 50),
        "bucket_rate": 1.0, "executors": [OWNER],
    }


class _V3Base(unittest.TestCase):
    """临时 `sign_tasks` 库 + 假时钟 + 替身三件套；`attempt_signin` 由用例打桩。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-state-v3-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_EXECUTOR_ID": OWNER,
        })

    @classmethod
    def tearDownClass(cls):
        cls._close_conn()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_EXECUTOR_ID"):
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
            path = self.db_file + suffix
            if os.path.exists(path):
                os.remove(path)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        clock_meta.set_meta(executor_v3.V_META_KEY_PREFIX + DAY, 8)
        self.fc = _FakeClock()
        self._patches = [
            mock.patch.object(executor_v3, "_now", self.fc.now),
            mock.patch.object(executor_v3, "_sleep", self.fc.sleep),
            mock.patch.object(executor_v3, "_mono", lambda: self.fc.mono),
            mock.patch.object(clock, "now", self.fc.now),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._stop_patches)

    def _stop_patches(self):
        for p in reversed(self._patches):
            with contextlib.suppress(Exception):
                p.stop()
        self._close_conn()

    # ---- 夹具 ----
    def _add_claimed(self, phone=PHONE):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (phone, DAY, 0, RUNTIME_OWNER, _ts(seconds=-1), 5, "claimed", 0,
             _ts(seconds=30), "", 1, _ts(seconds=-60)))
        conn.commit()

    def _row(self, phone=PHONE):
        row = db.get_conn().execute(
            "SELECT * FROM sign_tasks WHERE phone=? AND day=?", (phone, DAY)).fetchone()
        return dict(row) if row else None

    def _run_round(self, verdict, *, requeue_final=False):
        """跑一轮真 v3 执行体（`attempt_signin` 打桩成给定结论），返回实际尝试的账号。"""
        calls = []

        def _fake_attempt(acc):
            calls.append(acc.phone)
            return verdict

        async def _refill(queue, shards, ctx):
            queue.put_nowait((executor_v3.PRIORITY_ORDER_BASE, _ts(seconds=-1), PHONE,
                              0, 1))
            for _ in range(ctx.m):
                queue.put_nowait((executor_v3.SENTINEL_PRIORITY, "", "", 0, 0))

        acc = SimpleNamespace(phone=PHONE, user_paused=False, owner="u@" + PHONE)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               side_effect=_fake_attempt), \
                mock.patch.object(executor_v3, "_persist_loop", _never), \
                mock.patch.object(executor_v3, "_refiller", _refill), \
                mock.patch.object(executor_v3, "_make_limiter",
                                  lambda channels: _PermissiveLimiter()), \
                mock.patch.object(executor_v3, "_make_global_limiter",
                                  lambda: token_bucket.GlobalLimiter("")), \
                mock.patch.object(executor_v3, "_make_gap_gate", lambda: _PermissiveGate()):
            executor_v3.run_executor_v3([acc], day=DAY, cfg=_cfg(),
                                        rng=random.Random(7), cred_state={},
                                        requeue_final=requeue_final)
        return calls


class State4SettlesForTheDayTest(_V3Base):
    """`State=4` 的后果：队列记 `done`，补签轮定向剔除它。"""

    def test_state4_verdict_settles_the_row_as_done(self):
        verdict, posted = _run_signin(
            _sign_position_payload(state=4, msg=_CAPTURED_MSG, positions=[]))
        self.assertFalse(posted)
        self._add_claimed()
        self._run_round(verdict)
        row = self._row()
        self.assertEqual(row["state"], "done",
                         "State=4 判 already ⇒ 队列记 done（补签轮据 done 剔除它）")
        self.assertFalse(row["result"].startswith(claims_mod.RESULT_FINAL_PREFIX))

    def test_state4_already_is_dropped_by_the_supplement_round(self):
        """宿主/容器补签轮的剔除判据：状态文件里是 already ⇒ 不再真实登录一次。"""
        path = os.path.join(self.tmp, f"sign-state-{DAY}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({PHONE: {"status": yiban_status.STATUS_ALREADY}}, f,
                      ensure_ascii=False)
        kept = state_io._second_run_drop_done([SimpleNamespace(phone=PHONE)])
        self.assertEqual(kept, [], "已了结的账号不得被补签轮重跑一遍完整登录")


class SupplementingTierTest(_V3Base):
    """`State=5` 的后果：弃权落 `final:` 保守档，默认回炉口不复活它。"""

    def test_supplementing_verdict_settles_as_final_tier(self):
        verdict, posted = _run_signin(
            _sign_position_payload(state=5, msg="补签中", positions=[]))
        self.assertFalse(posted, "补签中不许提交签到")
        self._add_claimed()
        self._run_round(verdict)
        row = self._row()
        self.assertEqual(row["state"], "failed", "补签中不是了结：队列不记 done")
        self.assertTrue(row["result"].startswith(claims_mod.RESULT_FINAL_PREFIX),
                        "补签中属保守档：默认不自动回炉，只有显式路径才可再领")
        self.assertEqual(executor_v3._tier_prefix(yiban_status.STATUS_SUPPLEMENTING),
                         claims_mod.RESULT_FINAL_PREFIX)

    def test_default_round_cannot_reclaim_it(self):
        """默认轮（含兜底常驻）不得把补签中当成"可自由再签"：回炉口不放行。

        `claim_batch` 只取 `pending`，故"默认档不复活 `final:` 行"就是"默认轮领不到它"
        的全部依据（见 `queue_store.requeue_failed` 的说明）；本用例直接问回炉口。
        """
        self._add_claimed()
        self._run_round((False, "补签申请处理中（结果未定）", True,
                         yiban_status.STATUS_SUPPLEMENTING))
        self.assertEqual(queue_store.requeue_failed(DAY, (0,), include_final=False), 0,
                         "final: 档不得被默认回炉口复活")
        row = self._row()
        self.assertEqual(row["state"], "failed")
        self.assertTrue(row["result"].startswith(claims_mod.RESULT_FINAL_PREFIX))

    def test_explicit_path_is_the_documented_boundary(self):
        """本行钉的是 `final:` 档的**既有语义**（显式路径放行），不是对 `State=5` 策略的
        拍板：补签轮（`requeue_final=True`）会把它翻回 `pending` 再领一次。该残留已写进
        交付报告，等用户拍板"补签中要不要彻底不重试"。"""
        self._add_claimed()
        self._run_round((False, "补签申请处理中（结果未定）", True,
                         yiban_status.STATUS_SUPPLEMENTING))
        self.assertEqual(queue_store.requeue_failed(DAY, (0,), include_final=True), 1)


class StateMappingMutationTest(unittest.TestCase):
    """突变验证：删掉源码里 `4: STATUS_ALREADY` 那一行，本文件的守卫必须翻面。

    做法 = 把真源码读进来、只删那一行、写到临时文件后按模块加载（其余 import 仍指向
    真包）。若 `State=4` 在突变体下仍判 `already`，说明本文件的守卫钉的不是那张映射表
    （例如另有一处文本兜底），下面的断言即红。
    """

    @staticmethod
    def _mutant_client():
        src_path = os.path.join(BASE, "yiban", "client.py")
        with open(src_path, encoding="utf-8") as f:
            lines = f.read().splitlines(keepends=True)
        kept = [ln for ln in lines
                if not ln.strip().startswith("4: yiban_status.STATUS_ALREADY")]
        assert len(kept) == len(lines) - 1, "源码里找不到 `4: STATUS_ALREADY` 那一行"
        tmp = tempfile.mkdtemp(prefix="yiban-state-mutant-")
        path = os.path.join(tmp, "client_mutant.py")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(kept))
        spec = importlib.util.spec_from_file_location("yiban_client_mutant", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["yiban_client_mutant"] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_dropping_the_state4_row_flips_the_verdict(self):
        mutant = self._mutant_client()
        client = _new_client(mutant.YibanClient)
        client.session.get = mock.Mock(return_value=_stub_response(
            _sign_position_payload(state=4, msg=_CAPTURED_MSG, positions=[])))
        client.session.post = mock.Mock()
        with self.assertLogs(yiban_client.logger, level="WARNING"):
            verdict = client.signin()
        self.assertNotEqual(
            verdict[3], yiban_status.STATUS_ALREADY,
            "突变体（缺 State=4 行）仍判 already ⇒ 本文件的守卫钉不住那张映射表")
        self.assertEqual(verdict[3], yiban_status.STATUS_NO_POSITION,
                         "缺该行后落回 Position 回退分支——正是本工单修复前的缺陷形态")
        self.assertFalse(client.session.post.called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
