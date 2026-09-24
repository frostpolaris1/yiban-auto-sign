# -*- coding: utf-8 -*-
"""状态集合的单一事实源：JSON 状态码全集、了结/未了结划分、「今日不必再签」三套词表。

标签：D · 状态词汇与账号生命周期
覆盖：`ALL_STATUSES` 与状态常量全集及展示映射一致、`CONCLUDED_JSON_STATUSES` 只排除
    pending、pending 属未了结但不算结论、窗口外跳过状态仍触发补签；三套"了结"词汇的
    口径差异（认领表侧只认 done、`queue_store.SETTLED_STATES` 认 done+skipped、
    `TASKS_OPEN_STATES` 与 `TASKS_SETTLED_STATES` 合起来必须覆盖全词表）；
    `is_concluded_status` 对空串/缺键/未知值/首尾空白的判定；补签轮剔除与
    `state_io.has_undone_accounts_today`（领取池当日有行时以池为准，无行才回退状态文件）；
    窗口外跳过预筛不得改写未知状态。
对应实现：集合与判定的**权威定义全在 `yiban/status.py`**（`ALL_STATUSES`、
    `CONCLUDED_JSON_STATUSES`、`UNDONE_STATUSES`、`CLAIM_DONE_STATUSES`、
    `TASKS_OPEN_STATES`/`TASKS_SETTLED_STATES`、`is_concluded_status`）；领取池词表在
    `yiban/store/queue_store.py`（`STATE_DONE`/`STATE_SKIPPED`/`SETTLED_STATES`/
    `OPEN_STATES`）；当日未了结判定在 `state_io.has_undone_accounts_today`。
    本文件不持有第二份集合定义。
关键断言：**集合成员逐字不变**（实现可换、成员不许变）——补签轮剔除谁、领取池把谁记
    done 全靠这几个集合；空串/缺 `status` 键是「无记录」而非「结论」，把 `failed`
    判成无记录会让窗口外跳过覆盖真实结论、连带吞掉失败告警。
依赖：纯进程内（临时状态文件 + 字典构造），不触网、不起子进程、不需 bash/docker。

集合定义分散时，改一处忘一处表现为**漏签**或**同一账号被真实登录两次**（后者踩上游
风控红线），故收口到 `yiban/status.py` 一处。
"""
import contextlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from yiban import clock
from yiban import status as yiban_status
from yiban.engine import round as round_mod
from yiban.engine import state_io
from yiban.store import claims as claims_mod
from yiban.store import queue_store

#: 用例里的固定手机号（本文件不碰库，仅作状态文件的键）
PHONE = "13800000001"


def _acc(phone=PHONE):
    """账号替身：`_second_run_drop_done` 只读 `.phone`。"""
    return SimpleNamespace(phone=phone)


class StatusSetsTest(unittest.TestCase):
    """状态码全集与了结/未了结划分的代数关系。"""

    def test_all_statuses_covers_every_status_constant(self):
        """全集无重复、无遗漏——新增 `STATUS_*` 时必须同步本元组。"""
        names = {n for n in dir(yiban_status) if n.startswith("STATUS_")}
        self.assertEqual(
            set(yiban_status.ALL_STATUSES),
            {getattr(yiban_status, n) for n in names},
            "状态码全集漏了某个 STATUS_*，差集定义就会跟着漏",
        )
        self.assertEqual(len(yiban_status.ALL_STATUSES), 12)
        self.assertEqual(len(set(yiban_status.ALL_STATUSES)), 12, "全集内不得有重复串")

    def test_all_statuses_matches_display_maps(self):
        """两张映射表的键并集即全部状态码。

        `SYMBOL` 无 `pending`、`ICON` 无 `no_position`/`global_paused` 是刻意的
        （服务不同消费方，合并会改变前端可见表现），故只断言并集，不断言相等。
        """
        self.assertEqual(set(yiban_status.ALL_STATUSES),
                         set(yiban_status.SYMBOL) | set(yiban_status.ICON))
        self.assertEqual(set(yiban_status.ALL_STATUSES) - set(yiban_status.SYMBOL),
                         {yiban_status.STATUS_PENDING})

    def test_concluded_excludes_only_pending(self):
        """「只有 `pending` 不算结论」的可观察后果，按消费方的判据断言。

        1. 窗口收尾的 CAS 判据 `_has_conclusion` 对 `pending` 为假、对其余 11 个状态码
           为真——若把 `pending`（排计划写下的"打算什么时候签"）当成记录，窗口外起跑的
           整轮零请求账号会一个都进不了 `results`，汇总把它们算成失败并退出码 1；
        2. `pending` 不在「已了结」三态里 ⇒ 该账号仍会被补签轮纳入。

        「还要不要再签」与「有没有留下事实」是两个问题，故两处判定刻意不同向。
        """
        self.assertFalse(state_io._has_conclusion({"status": yiban_status.STATUS_PENDING}))
        self.assertFalse(yiban_status.is_concluded_status(yiban_status.STATUS_PENDING))
        for st in yiban_status.ALL_STATUSES:
            if st == yiban_status.STATUS_PENDING:
                continue
            with self.subTest(status=st):
                self.assertTrue(state_io._has_conclusion({"status": st}),
                                "除 pending 外的状态码都留下了事实，窗口外跳过不得覆盖")
        self.assertNotIn(yiban_status.STATUS_PENDING, yiban_status.CLAIM_DONE_STATUSES,
                         "pending 不是「已了结」⇒ 补签轮仍要纳入它")
        self.assertIn(yiban_status.STATUS_PENDING, yiban_status.UNDONE_STATUSES)

    def test_pending_is_undone_but_not_concluded(self):
        """`pending` 只是计划（"打算什么时候签"），不是事实。"""
        self.assertNotIn(yiban_status.STATUS_PENDING, yiban_status.CONCLUDED_JSON_STATUSES)
        self.assertIn(yiban_status.STATUS_PENDING, yiban_status.UNDONE_STATUSES)

    def test_unfinished_skip_statuses_still_trigger_second_run(self):
        """窗口外/窗口缺失/无点位仍属「未了结」（仍触发补签轮），同时算「已有结论」
        （窗口外跳过不得覆盖它们），且不属于「已了结」——三者都不得顺手挪动。"""
        for st in (yiban_status.STATUS_SKIPPED_WINDOW, yiban_status.STATUS_SKIPPED_NORANGE,
                   yiban_status.STATUS_NO_POSITION):
            with self.subTest(status=st):
                self.assertIn(st, yiban_status.UNDONE_STATUSES)
                self.assertIn(st, yiban_status.CONCLUDED_JSON_STATUSES)
                self.assertNotIn(st, yiban_status.CLAIM_DONE_STATUSES)


class SettledVocabularyRegressionTest(unittest.TestCase):
    """三套词表的「了结」取值：实现收口到共享定义后，成员一字不变。"""

    def test_claims_settled_is_done_only(self):
        self.assertEqual(set(claims_mod.SETTLED_STATES), {claims_mod.STATE_DONE})

    def test_queue_store_settled_is_done_and_skipped(self):
        self.assertEqual(set(queue_store.SETTLED_STATES),
                         {queue_store.STATE_DONE, queue_store.STATE_SKIPPED})

    def test_claim_done_statuses_is_the_shared_object(self):
        """补签轮剔除用的集合必须是**同一对象**，不是内容相同的副本。"""
        self.assertIs(round_mod._CLAIM_DONE_STATUSES, yiban_status.CLAIM_DONE_STATUSES)
        self.assertEqual(set(yiban_status.CLAIM_DONE_STATUSES),
                         {yiban_status.STATUS_SUCCESS, yiban_status.STATUS_ALREADY,
                          yiban_status.STATUS_NO_TASK})

    def test_open_states_are_disjoint_from_settled(self):
        """两个池的「未了结」与「了结」互斥（同一行不可能又了结又开放），
        且池侧两套常量都与 `yiban.status` 的共享定义同值。"""
        self.assertEqual(set(claims_mod.SETTLED_STATES) & set(claims_mod.OPEN_STATES), set())
        self.assertEqual(set(queue_store.SETTLED_STATES) & set(queue_store.OPEN_STATES), set())
        self.assertIs(queue_store.SETTLED_STATES, yiban_status.TASKS_SETTLED_STATES)
        self.assertEqual(set(queue_store.OPEN_STATES), yiban_status.TASKS_OPEN_STATES)

    def test_tasks_open_and_settled_cover_the_whole_vocabulary(self):
        """领取池六态 = 开放态 ∪ 了结态（无第三种归属，也没有漏掉的 state）。"""
        self.assertEqual(
            set(yiban_status.TASKS_OPEN_STATES) | set(yiban_status.TASKS_SETTLED_STATES),
            set(queue_store.STATES))
        self.assertEqual(set(yiban_status.TASKS_OPEN_STATES) & set(yiban_status.TASKS_SETTLED_STATES),
                         set())
        self.assertEqual(set(queue_store.OPEN_STATES) | set(queue_store.SETTLED_STATES),
                         set(queue_store.STATES))


class HasConclusionTest(unittest.TestCase):
    """`_has_conclusion` 逐格口径（窗口收尾「仅当无结论」写入的前提）。"""

    def test_empty_or_missing_status_is_no_record(self):
        """空串 / 纯空白 / 缺 `status` 键 / 非 dict = 无记录，不是结论。"""
        for entry in ({}, {"status": ""}, {"status": "   "}, {"message": "半截条目"},
                      None, "pending", []):
            with self.subTest(entry=entry):
                self.assertFalse(state_io._has_conclusion(entry))

    def test_pending_is_plan_not_conclusion(self):
        self.assertFalse(state_io._has_conclusion({"status": yiban_status.STATUS_PENDING}))

    def test_every_known_status_is_a_conclusion(self):
        """12 个状态码里除 `pending` 外都是结论——**含未了结状态**。

        `failed` 是事实，窗口外跳过不得覆盖它（覆盖会让 `has_real_failure` 变 False、
        失败告警被吞）。这正是本集合与 `UNDONE_STATUSES` 重叠、却都把 `pending`
        排除在外的原因：前者答"有没有留下事实"，后者答"还要不要再签"。
        """
        for st in yiban_status.CONCLUDED_JSON_STATUSES:
            with self.subTest(status=st):
                self.assertTrue(state_io._has_conclusion({"status": st}))

    def test_unknown_status_counts_as_conclusion(self):
        """12 个状态码之外的串按「已有结论」处理（排除法判据，失效方向偏安全）。

        状态文件的 `status` 可能是本进程不认识的串（新增状态码而读侧未同步、外部工具
        手写）。判「无记录」会让窗口外跳过覆盖掉这个真实结果且不报警；判「有结论」只会
        少写一次跳过——本判据服务的是 CAS 与窗口收尾预筛，故取后者。
        """
        for value in ("unknown_xyz", "SUCCESS", "SKIPPED", "GLOBAL_PAUSED"):
            with self.subTest(status=value):
                self.assertTrue(state_io._has_conclusion({"status": value}))
                self.assertTrue(yiban_status.is_concluded_status(value))
                self.assertNotIn(value, yiban_status.CONCLUDED_JSON_STATUSES,
                                 "枚举集只含已知常量，不等于谓词的定义域")

    def test_surrounding_whitespace_is_normalized(self):
        self.assertTrue(state_io._has_conclusion({"status": " success "}))


class SecondRunDropDoneTest(unittest.TestCase):
    """补签轮定向剔除的判据不变（读状态文件，不是读领取池）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-status-sets-")
        self._env = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        self.state_path = os.path.join(
            self.tmp, f"sign-state-{clock.now().strftime('%Y-%m-%d')}.json")

    def tearDown(self):
        if self._env is None:
            os.environ.pop("YIBAN_STATE_DIR", None)
        else:
            os.environ["YIBAN_STATE_DIR"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_state(self, payload):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    def _kept(self):
        return [a.phone for a in state_io._second_run_drop_done([_acc()])]

    def test_settled_statuses_are_dropped(self):
        for st in (yiban_status.STATUS_SUCCESS, yiban_status.STATUS_ALREADY,
                   yiban_status.STATUS_NO_TASK):
            with self.subTest(status=st):
                self._write_state({PHONE: {"status": st}})
                self.assertEqual(self._kept(), [],
                                 "当日已了结的账号不得再被真实登录一次")

    def test_unsettled_statuses_are_kept(self):
        for st in (yiban_status.STATUS_FAILED, yiban_status.STATUS_PAUSED,
                   yiban_status.STATUS_NO_POSITION, yiban_status.STATUS_SKIPPED_WINDOW):
            with self.subTest(status=st):
                self._write_state({PHONE: {"status": st}})
                self.assertEqual(self._kept(), [PHONE])

    def test_pending_is_kept_for_second_run(self):
        """`pending` 只是排计划写下的"打算什么时候签"，不算已了结 ⇒ 补签轮仍纳入它。"""
        self._write_state({PHONE: {"status": yiban_status.STATUS_PENDING}})
        self.assertEqual(self._kept(), [PHONE])

    def test_missing_state_file_keeps_everything(self):
        """状态文件缺失/损坏按「无记录」处理：宁可多跑一轮，不可漏签。"""
        self.assertEqual(self._kept(), [PHONE])


class WindowSkipPrefilterAgreementTest(unittest.TestCase):
    """窗口收尾的预筛与「仅当无结论才写」的 CAS 对同一状态串必须给同一判定。

    预筛决定账号是否带着已有结论透传进 `results`；CAS 决定落盘是否覆盖。两处判据不一致
    时，同一个账号既进不了 `results`（汇总按未执行失败计、退出码 1、发失败邮件），状态
    文件又被改写成 `skipped_window`——真实结果与失败告警一起消失。
    """

    DAY = "2026-09-16"
    #: 窗口已关闭的时刻（默认窗口 06:30~07:50，截止保护按 sign_end 减 edge_back）
    CLOSED_AT = datetime(2026, 9, 16, 8, 5)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-status-sets-skip-")
        self._env = dict(os.environ)
        os.environ.update({"YIBAN_STATE_DIR": self.tmp,
                           "YIBAN_SIGN_START": "06:30",
                           "YIBAN_SIGN_END": "07:50"})
        os.environ.pop("YIBAN_SECOND_RUN", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state_path(self):
        return os.path.join(self.tmp, f"sign-state-{self.DAY}.json")

    def _run_window_closed(self, recorded_status):
        """按给定的当日记录跑一轮「窗口已关闭」的执行，返回 `(results, 落盘状态)`。"""
        with open(self._state_path(), "w", encoding="utf-8") as f:
            json.dump({PHONE: {"status": recorded_status}}, f, ensure_ascii=False)
        with mock.patch.object(clock, "now", return_value=self.CLOSED_AT), \
                mock.patch.object(round_mod.time, "sleep"):
            results = round_mod.run_queue_retry(
                [_acc()], "", 0, 0, schedule={PHONE: self.CLOSED_AT}, cred_state={})
        with open(self._state_path(), encoding="utf-8") as f:
            return results, json.load(f)[PHONE]["status"]

    def test_unknown_status_survives_window_skip(self):
        results, stored = self._run_window_closed("GLOBAL_PAUSED")
        self.assertEqual(stored, "GLOBAL_PAUSED",
                         "看不懂的状态串也是结果，窗口外跳过不得覆盖它")
        self.assertEqual(results[PHONE][3], "GLOBAL_PAUSED",
                         "预筛必须判它「已有结论」并透传，而不是当成本轮未执行")

    def test_no_record_is_still_marked_as_window_skip(self):
        """反方向：空串是「无记录」，窗口外跳过必须写得进去（否则漏记一轮）。"""
        results, stored = self._run_window_closed("")
        self.assertEqual(stored, yiban_status.STATUS_SKIPPED_WINDOW)
        self.assertEqual(results[PHONE][3], yiban_status.STATUS_SKIPPED_WINDOW)


class HasUndoneAccountsTest(unittest.TestCase):
    """`has_undone_accounts_today` 按源择一的行为不变。"""

    DAY = "2026-09-16"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-status-sets-gate-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_state(self, payload):
        with open(os.path.join(self.tmp, f"sign-state-{self.DAY}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    @contextlib.contextmanager
    def _pool(self, total, open_=0):
        """打桩领取池当日计数：`total` 为 0 表示池里当日无行（回退状态文件）。"""
        with mock.patch.object(state_io.db, "is_initialized", return_value=True), \
                mock.patch.object(state_io.db, "claim_stats",
                                  return_value={"total": total, "open": open_}):
            yield

    def _undone(self):
        return state_io.has_undone_accounts_today(state_dir=self.tmp, day=self.DAY)

    def test_claims_rows_win_over_state_file(self):
        """池里当日有行即以池为准：状态文件的 failed 不改变结果。"""
        self._write_state({PHONE: {"status": yiban_status.STATUS_FAILED}})
        with self._pool(1, 0):
            self.assertFalse(self._undone())
        with self._pool(1, 1):
            self.assertTrue(self._undone())

    def test_falls_back_to_state_file_without_claims_rows(self):
        """池里当日无行（还没人领过）才回退状态文件。"""
        self._write_state({PHONE: {"status": yiban_status.STATUS_SUCCESS}})
        with self._pool(0):
            self.assertFalse(self._undone())
        self._write_state({PHONE: {"status": yiban_status.STATUS_SKIPPED_WINDOW}})
        with self._pool(0):
            self.assertTrue(self._undone())

    def test_pool_unavailable_is_fail_safe(self):
        """无库且状态文件缺失 → 判「有未了结」（宁多跑一轮，不漏签）。"""
        with mock.patch.object(state_io.db, "is_initialized", return_value=False):
            self.assertTrue(self._undone())

    def test_pool_error_falls_back_to_state_file(self):
        """池读取异常同样回退状态文件，绝不把「读不到」当「已了结」。"""
        self._write_state({PHONE: {"status": yiban_status.STATUS_SUCCESS}})
        with mock.patch.object(state_io.db, "is_initialized", return_value=True), \
                mock.patch.object(state_io.db, "claim_stats",
                                  side_effect=RuntimeError("库抖动")):
            self.assertFalse(self._undone())


if __name__ == "__main__":
    unittest.main(verbosity=2)
