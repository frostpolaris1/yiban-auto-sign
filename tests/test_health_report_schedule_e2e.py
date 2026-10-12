# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""issue #23：告警通道健康报告显式发送时刻（钟点锚定）的端到端行为。

**先写的预期行为**（写实现之前落笔，先证红）：
1. 两枚新键都未配置 ⇒ 例行判定与改前逐位一致（只有 `_HEALTH_REPORT_WEEKDAY` 那一天发），
   非例行日零发送、零降级痕迹、零 .env 迁移；
2. 配置「时:分 + 星期几」⇒ 清理线程在配置时刻（±唤醒粒度）触发，一周恰好一封；
3. 键写非法值（`99:99` / 星期越界）⇒ 加载器口径告警 + 回落缺省（等同未配置），
   报告不会因为写错一个字符而静默消失；
4. 设置页保存链可写入并即时生效（`.env` 原子写 → 加载器读回 → 下一次判定用新值）。

标签：H · 通知：邮件与推送
覆盖：`web/app.py` 的 `_channel_health_report_due` 钟点锚定与 `_daily_purge_loop` 唤醒节拍、
    `config/registry.json` 的两枚新键、`POST /api/settings` 的写入与档位门禁。
对应实现：判据在 `web/app.py`；后端设置写链在 `web/routes/settings_api.py` 与
    `web/services/env_io.py`；配置取值走 `yiban/config_loader`（名册是唯一定义点）。
关键断言：**未配置零迁移**要逐位对拍改前的式子（一周 × 分钟粒度全扫），不是抽样；
    配置后「到点才发、不到点不发」用注入时钟钉，不真等一周。
依赖：复用 `tests/test_rekey_key_source.py` 的 `_B14AlertGateBase` 夹具（隔离 .env/DB +
    内置主管理员会话 + 记录式 send_notification），故须在仓库根跑 pytest；不连 SMTP。
"""
import contextlib
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from test_rekey_key_source import (  # noqa: E402  （同目录夹具，须在 sys.path 补好后导入）
    _B14AlertGateBase,
    _write_env,
)

from web.services.env_io import (  # noqa: E402
    health_report_time_str,
    health_report_weekday_setting,
)
from yiban import config_loader  # noqa: E402

TIME_KEY = "YIBAN_HEALTH_REPORT_TIME"
WEEKDAY_KEY = "YIBAN_HEALTH_REPORT_WEEKDAY"
#: 改前的例行播报日（0=周一）——用例里的对拍基准按这个字面量写，不去读被测常量，
#: 否则"把常量改错"会让对拍两边一起错。
LEGACY_WEEKDAY = 0

#: 健康快照：`_channel_health_degraded` 只看这几个结构化字段（与 `_alert_channel_status`
#: 同键名）。完整键集是为了让 `_channel_status_lines` 也能直接吃它。
HEALTHY_STATUS = {
    "mail_flag_on": True, "mail_usable": True, "mail_state": "ok",
    "mail_state_detail": "", "mail_self_notify": True, "mail_recipients": 2,
    "mail_user": "alert@test.local", "mail_admin_to": "admin@test.local",
    "mail_error": "", "push_usable": False, "push_configured": False,
    "push_ever_configured": False, "push_type": "", "push_secret_masked": "",
    "push_urgent_only": False, "push_error": "",
    "daily_max": None, "daily_remaining": None,
    "urgent_daily_max": None, "urgent_daily_remaining": None,
}


@pytest.fixture(autouse=True)
def _clear_report_config_from_process_env(monkeypatch):
    """逐用例把两枚键从**进程环境**摘掉：加载器层序是「进程环境 → `.env` → 名册缺省」。

    宿主机若导出过 `YIBAN_HEALTH_REPORT_TIME` / `YIBAN_HEALTH_REPORT_WEEKDAY`（`.env`
    里写的值会被进程环境层压过去），"未配置"与"我配成 X"两类用例都会静默测到另一个
    取值——用例仍然绿，测的却不是目标。**必须逐用例覆盖全部路径**：只在 `_set_config()`
    里 pop 挡不住走 HTTP `_save` 的用例（它们不经过那个函数）。与 conftest 对
    `YIBAN_MAIL_ENABLE` 的隔离同一层，故用 autouse fixture 而不是散在各处。
    """
    monkeypatch.delenv(TIME_KEY, raising=False)
    monkeypatch.delenv(WEEKDAY_KEY, raising=False)


class _HealthScheduleBase(_B14AlertGateBase):
    """本文件所有用例的公共工具：注入时钟、清配置、逐分钟驱动清理线程的判定。

    进程环境隔离由模块级 autouse fixture `_clear_report_config_from_process_env` 承担
    （逐用例、覆盖全部路径，含走 HTTP 的用例），此处不重复做。
    """

    def _set_config(self, time_value=None, weekday_value=None):
        """把两枚新键写进（或从）隔离 `.env` 里——None = 该键不出现（未配置）。"""
        lines = [ln for ln in _read_lines(self.env_file)
                 if not ln.strip().startswith((TIME_KEY + "=", WEEKDAY_KEY + "="))]
        if time_value is not None:
            lines.append(f"{TIME_KEY}={time_value}")
        if weekday_value is not None:
            lines.append(f"{WEEKDAY_KEY}={weekday_value}")
        _write_env(self.env_file, lines)

    def _clear_config(self):
        self._set_config()

    def _due_at(self, moment, status=HEALTHY_STATUS, exhausted=False, since=None,
                unconfigured_hm=None):
        """在指定时刻判闸门：时刻经 `yiban.clock.now` 注入，额度状态按需打桩。

        `since` = 上一次唤醒时刻，走清理线程的区间判定（不传 = 单点判定）。
        `unconfigured_hm` = 时刻键未配置时的锚点（不传 = 无时刻门，旧签名语义）。
        """
        with mock.patch.object(self.webapp.clock, "now", return_value=moment), \
                mock.patch.object(self.webapp.notify, "has_pending_exhaustion_notice",
                                  return_value=exhausted), \
                mock.patch.object(self.webapp.notify, "budget_exhausted_today",
                                  return_value=False):
            return self.webapp._channel_health_report_due(
                status, since=since, unconfigured_hm=unconfigured_hm)

    def _drive_ticks(self, start, end, step_minutes=5, status=HEALTHY_STATUS,
                     unconfigured_hm="auto"):
        """按清理线程的唤醒节拍驱动一轮「判定 + 发送」，返回 (发送时刻列表, 判定为真次数)。

        与清理线程**同一姿势**：`since` 传上一跳时刻（区间补发生效），锚点取首跳钟点
        （`unconfigured_hm="auto"`）——按旧签名不传锚点会测到生产上不存在的路径。
        走的是真实的 `_send_channel_health_report`（含 app_meta 去重），只把通道快照打桩——
        「一周一封」与「未配置零迁移」因此是被实测的，不是被绕过的。
        """
        sent, due_calls = [], 0
        anchor = (start.hour, start.minute) if unconfigured_hm == "auto" else unconfigured_hm
        box = {"t": start, "prev": None}
        with mock.patch.object(self.webapp.clock, "now", side_effect=lambda: box["t"]), \
                mock.patch.object(self.webapp, "_alert_channel_status",
                                  return_value=dict(status)), \
                mock.patch.object(self.webapp.notify, "has_pending_exhaustion_notice",
                                  return_value=False), \
                mock.patch.object(self.webapp.notify, "budget_exhausted_today",
                                  return_value=False):
            while box["t"] <= end:
                if self.webapp._channel_health_report_due(
                        since=box["prev"], unconfigured_hm=anchor):
                    due_calls += 1
                    before = len(self.alerts)
                    self.webapp._send_channel_health_report()
                    if len(self.alerts) > before:
                        sent.append(box["t"])
                box["prev"], box["t"] = box["t"], box["t"] + timedelta(minutes=step_minutes)
        return sent, due_calls


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_lines(path):
    with open(path, encoding="utf-8-sig") as f:
        return [ln for ln in f.read().splitlines() if ln.strip()]


MONDAY = datetime(2026, 9, 21, 0, 0, 0)
assert MONDAY.weekday() == LEGACY_WEEKDAY


class UnconfiguredIsBitForBitLegacyTest(_HealthScheduleBase):
    """行为一：两键都未配置 ⇒ 到达时刻与改前逐位一致（= 进程启动钟点落在例行日）。

    改前循环是"启动 60s 后首轮，此后每 24h 一醒"：例行报告落在周一，到达时刻 = 启动钟点
    （k×86400 秒与首跳同钟点）。节拍收细到 5 分钟后，若把未配置分支判成"当天唤醒即发"，
    未配置的部署会从启动钟点漂到**零点后第一跳**——那不是零迁移。本例把锚点钉在启动钟点。
    """

    def test_no_new_keys_present_after_seed(self):
        """前置：隔离 .env 里本来就没有这两枚键（否则"未配置"测的是别的场景）。"""
        text = "\n".join(_read_lines(self.env_file))
        self.assertNotIn(TIME_KEY, text)
        self.assertNotIn(WEEKDAY_KEY, text)

    def test_arrival_is_the_start_clock_time_not_just_after_midnight(self):
        """未配置 + 首跳钟点 20:00 ⇒ 报告必须落在周一 20:00，不是周一 00:00。

        这是本单最关键的一条零迁移断言：节拍收细不能把未配置的部署的到达时刻挪走。
        """
        self._clear_config()
        start = datetime(2026, 9, 20, 20, 0)          # 周日 20:00 = 首跳钟点（非午夜）
        sent, _due = self._drive_ticks(start, start + timedelta(days=7) - timedelta(minutes=5))
        self.assertEqual(len(sent), 1, f"一周应恰好一封：{sent}")
        self.assertEqual(sent[0].strftime("%Y-%m-%d %H:%M"), "2026-09-21 20:00",
                         "到达时刻必须是启动钟点落在例行日的那一刻，不是零点后第一跳")

    def test_gate_matches_legacy_arrival_over_a_full_week(self):
        """未配置 + 首跳钟点 20:00：一周 × 每分钟对拍「例行日 且 已过启动钟点」。"""
        self._clear_config()
        anchor = (20, 0)
        mismatches = []
        for day in range(7):
            for minutes in range(0, 24 * 60, 7):
                moment = MONDAY + timedelta(days=day, minutes=minutes)
                expected = (moment.weekday() == LEGACY_WEEKDAY
                            and (moment.hour, moment.minute) >= anchor)
                got = self._due_at(moment, unconfigured_hm=anchor)
                if got != expected:
                    mismatches.append((moment.strftime("%a %H:%M"), expected, got))
        self.assertEqual(mismatches, [],
                         "未配置时例行判定必须与改前的到达时刻逐位一致")

    def test_unconfigured_non_routine_day_sends_nothing(self):
        """非例行日：零发送、零降级告警、零迁移成本。"""
        self._clear_config()
        # 周二 20:00 → 周日 23:55，逐 5 分钟驱动一遍（锚点 = 首跳钟点 20:00）
        sent, _due = self._drive_ticks(MONDAY + timedelta(days=1, hours=20),
                                       MONDAY + timedelta(days=6, hours=23, minutes=55))
        self.assertEqual(sent, [], "未配置且非例行日不得发出任何一封")
        self.assertEqual(self.alerts, [], "不得产生任何告警（含降级痕迹那一路）")

    def test_unconfigured_routine_day_sends_once_and_writes_no_config(self):
        """例行日本身照发一封——但 .env 不被这两枚键写入（零迁移）。"""
        self._clear_config()
        before = _read_lines(self.env_file)
        start = datetime(2026, 9, 21, 9, 0)           # 周一 09:00 = 首跳钟点
        sent, _due = self._drive_ticks(start, start + timedelta(hours=23, minutes=55))
        self.assertEqual(len(sent), 1, f"周一应恰好一封：{sent}")
        self.assertEqual(sent[0], start, "首跳钟点即到点：周一一开轮就发")
        self.assertEqual(_read_lines(self.env_file), before,
                         "未配置的运行不得顺手把键写进 .env")


class ConfiguredClockAnchorTest(_HealthScheduleBase):
    """行为二：配置「时:分 + 星期几」⇒ 钟点锚定，一周恰好一封。"""

    def test_configured_time_gates_before_and_after_the_moment(self):
        self._set_config(time_value="09:00")
        self.assertFalse(self._due_at(datetime(2026, 9, 21, 8, 59)),
                         "不到点不得判「该发」")
        self.assertTrue(self._due_at(datetime(2026, 9, 21, 9, 0)),
                        "到点即判「该发」")
        self.assertTrue(self._due_at(datetime(2026, 9, 21, 23, 59)),
                        "同一天过了点仍算「该发」（当天未发过时由去重标记兜底）")

    def test_configured_weekday_moves_the_routine_day(self):
        self._set_config(time_value="09:00", weekday_value="2")
        self.assertFalse(self._due_at(datetime(2026, 9, 21, 9, 0)), "周一不再是例行日")
        self.assertTrue(self._due_at(datetime(2026, 9, 23, 9, 0)), "周三 09:00 是例行点")
        self.assertFalse(self._due_at(datetime(2026, 9, 23, 8, 0)), "周三不到点不发")

    def test_weekday_only_config_uses_the_start_clock_time_on_that_day(self):
        """只配星期几（不配时刻）是合法用法：那一天按**启动钟点**发（与未配置同一锚点）。"""
        self._set_config(weekday_value="4")
        anchor = (9, 0)
        self.assertFalse(self._due_at(datetime(2026, 9, 23, 9, 0), unconfigured_hm=anchor),
                         "周三不是配置的星期")
        self.assertFalse(self._due_at(datetime(2026, 9, 25, 8, 59), unconfigured_hm=anchor),
                         "周五不到启动钟点不发")
        self.assertTrue(self._due_at(datetime(2026, 9, 25, 9, 0), unconfigured_hm=anchor),
                        "周五的启动钟点即到点")

    def test_weekday_only_config_reaches_the_restored_arrival(self):
        """只配星期五 + 首跳钟点 20:00：一周恰好一封，落在周五 20:00。"""
        self._set_config(weekday_value="4")
        start = datetime(2026, 9, 20, 20, 0)          # 周日 20:00 = 首跳钟点
        sent, _due = self._drive_ticks(start, start + timedelta(days=8))
        friday = [t for t in sent if t.weekday() == 4]
        self.assertEqual(len(sent), 1, f"一周应恰好一封：{sent}")
        self.assertEqual(friday[0].strftime("%Y-%m-%d %H:%M"), "2026-09-25 20:00")

    def test_exactly_one_send_per_week_and_it_lands_on_the_configured_minute(self):
        """一周逐 5 分钟驱动：恰好一封，且落在配置时刻的唤醒粒度内。"""
        self._set_config(time_value="09:00")
        start = MONDAY
        sent, _due = self._drive_ticks(start, start + timedelta(days=7) - timedelta(minutes=5))
        self.assertEqual(len(sent), 1, f"一周必须恰好一封：{sent}")
        delta = sent[0] - datetime(2026, 9, 21, 9, 0)
        self.assertGreaterEqual(delta, timedelta(0), "不得早于配置时刻发出")
        self.assertLessEqual(delta, timedelta(minutes=5),
                             "到达时间必须落在清理线程唤醒粒度内（±5 分钟）")

    def test_moment_in_the_last_tick_gap_is_caught_across_midnight(self):
        """配置时刻落在跨零点那一跳的空隙里时，区间判定必须把它补上。

        单点判定只在"当前 >= 配置时刻且星期命中"时放行：配置 23:58、任务末跳 23:56、
        下一跳 00:01（已换日）。没有任何一跳命中单点判据 ⇒ 整周的周报被静默丢掉，
        而这是一份"通道被拆也看得见"的安全证据。
        """
        self._set_config(time_value="23:58")
        last_tick = datetime(2026, 9, 21, 23, 56)
        next_tick = datetime(2026, 9, 22, 0, 1)
        self.assertFalse(self._due_at(last_tick), "23:56 还没到 23:58")
        self.assertFalse(self._due_at(next_tick), "单点判定在跨日后确实判不出来")
        self.assertTrue(self._due_at(next_tick, since=last_tick),
                        "区间判定：配置时刻落在 (上次唤醒, 本次] 里 ⇒ 必须补发")
        # 补发只发生一次：再下一跳里配置时刻已不在区间内
        self.assertFalse(self._due_at(datetime(2026, 9, 22, 0, 6), since=next_tick))
        # 配置星期之后，区间判定同样只认配置的那一天
        self._set_config(time_value="23:58", weekday_value="2")
        self.assertFalse(self._due_at(next_tick, since=last_tick),
                         "配置到周三时，周一的跨零点不得触发")

    def test_degraded_day_sends_at_the_day_opening_tick(self):
        """降级/额度耗尽的加发按**北京日期开闸**：当天第一跳就发，不看星期也不看时刻。

        与例行锚点（首跳钟点栅格）是两套语义，刻意不对齐并在此钉住：改前 24h 一醒时它落在
        启动钟点，节拍收细后跨日运行的进程落在零点后的第一跳。承诺未破——进程当天首次起来
        时"第一跳"就是启动后 60 秒那一刻。本用例把现状钉住：不为对齐它再改行为。
        """
        self._clear_config()
        degraded = dict(HEALTHY_STATUS, mail_usable=False)
        start = MONDAY + timedelta(days=2)            # 周三 00:00：非例行日、非首跳钟点
        sent, _due = self._drive_ticks(start, start + timedelta(hours=6), status=degraded)
        self.assertEqual(sent[:1], [start], "降级日必须在当天第一跳就发")
        self.assertEqual(len(sent), 1, f"当天仍只一封：{sent}")

    def test_degraded_channel_still_sends_off_routine_day_and_off_time(self):
        """加发逻辑不受影响：通道降级 / 额度耗尽当天就发，不看星期与时刻。"""
        self._set_config(time_value="09:00", weekday_value="2")
        degraded = dict(HEALTHY_STATUS, mail_usable=False)
        self.assertTrue(self._due_at(datetime(2026, 9, 21, 3, 0), status=degraded),
                        "周一 03:00 通道降级仍须当天发")
        self.assertTrue(self._due_at(datetime(2026, 9, 21, 3, 0), exhausted=True),
                        "额度耗尽同样当天发")

    def test_change_takes_effect_on_the_next_tick_without_restart(self):
        """改完即生效（下一轮判定）：不必重启进程。"""
        self._set_config(time_value="09:00")
        self.assertFalse(self._due_at(datetime(2026, 9, 21, 8, 0)))
        self._set_config(time_value="07:30")
        self.assertTrue(self._due_at(datetime(2026, 9, 21, 8, 0)),
                        "改成更早的时刻后，同一进程的下一轮判定就该放行")


class InvalidConfigFallsBackTest(_HealthScheduleBase):
    """行为三：键写非法值 ⇒ 加载器口径告警 + 回落缺省（报告不会静默消失）。"""

    def test_invalid_time_warns_and_is_treated_as_unconfigured(self):
        self._set_config(time_value="99:99")
        with self.assertLogs("yiban.config_loader", level="WARNING") as logs:
            resolved = config_loader.resolve(TIME_KEY, env_file=self.env_file)
        self.assertIsNone(resolved, "解不出的时刻按「未设」处理，缺省为 null")
        self.assertTrue(any(TIME_KEY in m for m in logs.output),
                        "加载器必须点名该键告警，不静默")
        # 回落缺省 = 未配置 = 改前行为（周一即发），报告不会因此消失
        self.assertTrue(self._due_at(datetime(2026, 9, 21, 3, 0)))
        self.assertFalse(self._due_at(datetime(2026, 9, 22, 3, 0)))

    def test_out_of_range_weekday_warns_and_falls_back_to_monday(self):
        self._set_config(weekday_value="9")
        with self.assertLogs("yiban.config_loader", level="WARNING") as logs:
            config_loader.resolve(WEEKDAY_KEY, env_file=self.env_file)
        self.assertTrue(any("域" in m for m in logs.output),
                        "越界值必须触发名册域告警")
        self.assertTrue(self._due_at(datetime(2026, 9, 21, 3, 0)),
                        "越界星期回落缺省（周一）——不得让报告从此再不发出")
        self.assertFalse(self._due_at(datetime(2026, 9, 23, 3, 0)))

    def test_non_canonical_time_is_echoed_after_normalisation(self):
        """手写 `9:00` 同样是合法配置（解析口径与引擎同一处）：回显归一为 `09:00`。

        回显若原样返回 `9:00`，前端的形状校验会把它判成非法值 ⇒ 界面显示"未配置"、
        而报告其实已按 09:00 锚定：显示值与生效值两段不等。
        """
        self._set_config(time_value="9:00")
        self.assertFalse(self._due_at(datetime(2026, 9, 21, 8, 59)))
        self.assertTrue(self._due_at(datetime(2026, 9, 21, 9, 0)))
        c = self._client()
        self._login(c, "admin", "MasterPass#2026")
        self.assertEqual(c.get("/api/settings").get_json()["health_report_time"], "09:00")

    def test_loader_reads_the_configured_values(self):
        self._set_config(time_value="09:00", weekday_value="2")
        self.assertEqual(config_loader.resolve(TIME_KEY, env_file=self.env_file), "09:00")
        self.assertEqual(config_loader.resolve(WEEKDAY_KEY, env_file=self.env_file), 2)


class SettingsEntryTest(_HealthScheduleBase):
    """行为四：设置页入口可配置保存，保存后 .env 原子写生效，加载器读到新值。"""

    def _save(self, body):
        c = self._client()
        token = self._login(c, "admin", "MasterPass#2026")
        payload = dict(body, confirm_password="MasterPass#2026")
        return c.post("/api/settings", json=payload, headers=self._csrf(token))

    def test_get_settings_exposes_the_two_fields(self):
        c = self._client()
        self._login(c, "admin", "MasterPass#2026")
        data = c.get("/api/settings").get_json()
        self.assertIn("health_report_time", data)
        self.assertIn("health_report_weekday", data)
        self.assertEqual(data["health_report_time"], "", "未配置回显空串")
        # 星期回显的是**配置值**：空串 = 没配。界面靠它分辨"配了周一"与"没配"，
        # 否则关掉固定发送时删不掉残留的星期键（面板显示已关闭而报告照残留星期发）。
        self.assertEqual(data["health_report_weekday"], "", "未配置回显空串（不是缺省周一）")
        # 变更判定的现值必须与上一条回显**同一口径**：否则"提交一个等于回显的值"
        # 会被判成没改 ⇒ 键永不落盘，而回显仍是空串 ⇒ 每次打开设置页都有一条永远
        # 清不掉的脏标记（保存成功了，界面却说还有未保存的修改）。
        self.assertEqual(
            self.webapp._settings_effective_values(self.env_file)["health_report_weekday"],
            "", "变更判定的现值口径与 GET 回显同源")

    def test_master_saves_and_loader_reads_back(self):
        r = self._save({"health_report_time": "09:00", "health_report_weekday": "2"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        text = "\n".join(_read_lines(self.env_file))
        self.assertIn(f"{TIME_KEY}=09:00", text)
        self.assertIn(f"{WEEKDAY_KEY}=2", text)
        self.assertEqual(config_loader.resolve(TIME_KEY, env_file=self.env_file), "09:00")
        self.assertEqual(config_loader.resolve(WEEKDAY_KEY, env_file=self.env_file), 2)
        c = self._client()
        self._login(c, "admin", "MasterPass#2026")
        data = c.get("/api/settings").get_json()
        self.assertEqual(data["health_report_time"], "09:00")
        self.assertEqual(data["health_report_weekday"], "2")
        self.assertTrue(self._due_at(datetime(2026, 9, 23, 9, 0)), "保存后判定即按新值")
        self.assertFalse(self._due_at(datetime(2026, 9, 21, 9, 0)))

    def test_invalid_time_rejected_and_nothing_written(self):
        r = self._save({"health_report_time": "99:99"})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        text = "\n".join(_read_lines(self.env_file))
        self.assertNotIn(TIME_KEY, text, "非法值不得落盘")
        self.assertNotIn(WEEKDAY_KEY, text)

    def test_out_of_range_weekday_rejected(self):
        r = self._save({"health_report_weekday": "9"})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_malformed_weekday_is_rejected_with_400_not_500(self):
        """非 ASCII 数字（"²"）等写法必须走 400 拒绝，不得把解析异常漏成 500。

        `str.isdigit()` 对上标数字也为真，而 `int("²")` 抛 ValueError——只查 isdigit
        就会让一个畸形请求变成服务端 500（对照：时刻键的解析包在 try 里）。
        """
        for bad_value in ("²", "³", "9", "abc", "0x2", "3 3", "-1", "1.5"):
            with self.subTest(value=bad_value):
                r = self._save({"health_report_weekday": bad_value})
                self.assertEqual(r.status_code, 400,
                                 f"{bad_value!r} 应 400：{r.get_data(as_text=True)}")
        r = self._save({"health_report_time": "²:00"})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_non_master_is_forbidden(self):
        """普通管理员提交新键：与其它 A 档键同一条 403 口径（先拒，不落盘）。"""
        self._save({"health_report_time": "09:00"})
        before = "\n".join(_read_lines(self.env_file))
        self.webapp.db.create_user(
            "admin2@test.local",
            self.webapp.generate_password_hash("PlainAdmin#2026x"), role="admin")
        c2 = self._client()
        t2 = self._login(c2, "admin2@test.local", "PlainAdmin#2026x")
        for field, value in (("health_report_time", "07:00"),
                             ("health_report_weekday", "3")):
            r = c2.post("/api/settings", json={field: value}, headers=self._csrf(t2))
            self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertEqual("\n".join(_read_lines(self.env_file)), before,
                         "403 不得留下任何 .env 写入")

    def test_audit_detail_renders_unset_in_human_text(self):
        """审计明细里"未配置"必须写在**新值位与旧值位两处**。

        只钉新值位的写法会空转：`changes.append((_k, _old or "-", _new))` 那类"在
        `_settings_value_text` 之前就把空串兜底成 `-`"的写法，新值位照旧是人话，唯一红得
        了的只有旧值位。故两个方向都钉：未配置 → 配好（旧值位是未配置）、配好 → 清空
        （新值位是未配置）。
        """
        # 方向一：旧值位 = 未配置（起点是"两键都没配"）
        r = self._save({"health_report_time": "09:00", "health_report_weekday": "2"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        detail = self._audit_rows("settings_save")[-1]["detail"]
        self.assertIn("健康报告发送时刻=未配置→09:00", detail,
                      f"旧值位没写成人话：{detail}")
        self.assertIn("健康报告发送星期=未配置→周三", detail,
                      f"旧值位没写成人话：{detail}")
        # 方向二：新值位 = 未配置（本用例清空两键）
        r = self._save({"health_report_time": "", "health_report_weekday": ""})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        detail = self._audit_rows("settings_save")[-1]["detail"]
        self.assertIn("健康报告发送时刻=09:00→未配置", detail,
                      f"新值位没写成人话：{detail}")
        self.assertIn("健康报告发送星期=周三→未配置", detail,
                      f"新值位没写成人话：{detail}")

    def test_saving_the_switch_on_pair_is_reflected_by_the_echo(self):
        """开开关提交的成对取值必须真的落盘并回显。

        落盘被"现值口径不一致"静默跳过时（例如回显空串、现值当周一），界面提交的
        "周一"不会写进 .env，而下次 GET 又回空串 ⇒ 每次打开设置页都有一条永远清不掉的
        "未保存的修改"（保存成功了，界面却说还没保存）。
        """
        self._set_config()
        r = self._save({"health_report_time": "09:00", "health_report_weekday": "0"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c = self._client()
        self._login(c, "admin", "MasterPass#2026")
        data = c.get("/api/settings").get_json()
        self.assertEqual(data["health_report_time"], "09:00")
        self.assertEqual(data["health_report_weekday"], "0",
                         "提交的星期（含缺省周一）必须落盘并回显")

    def test_clearing_the_time_restores_legacy_behaviour(self):
        self._save({"health_report_time": "09:00"})
        r = self._save({"health_report_time": ""})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        text = "\n".join(_read_lines(self.env_file))
        self.assertNotIn(f"{TIME_KEY}=", text, "清空 = 删键（回落到未配置）")
        self.assertTrue(self._due_at(datetime(2026, 9, 21, 3, 0)),
                        "删键后回到「周一即发」，不是「从此不发」")
        self.assertFalse(self._due_at(datetime(2026, 9, 22, 3, 0)))

    def test_clearing_the_pair_removes_the_residual_weekday(self):
        """关掉"固定发送"必须把时刻与星期**成对**删掉。

        只删时刻而留下星期，例行日会静默改到残留的那一天——面板显示已关闭，
        行为却不是未配置，这正是设置页要消灭的"看不见的事实"。
        """
        self._save({"health_report_time": "09:00", "health_report_weekday": "2"})
        self.assertTrue(self._due_at(datetime(2026, 9, 23, 9, 0)), "前置：已按周三 09:00")
        r = self._save({"health_report_time": "", "health_report_weekday": ""})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        text = "\n".join(_read_lines(self.env_file))
        self.assertNotIn(f"{TIME_KEY}=", text)
        self.assertNotIn(f"{WEEKDAY_KEY}=", text, "残留的星期键必须一并清除")
        self.assertFalse(self._due_at(datetime(2026, 9, 23, 9, 0)), "周三不再是例行日")
        self.assertTrue(self._due_at(datetime(2026, 9, 21, 3, 0)), "回到「周一即发」")


class ReaderContractTest(unittest.TestCase):
    """取值口径与控件契约（纯函数 / 源码级，不启 app、不碰 DB）。"""

    def test_weekday_reader_normalises_like_the_runtime(self):
        """手写 `00` / `+3` 必须与运行期同解。

        读得比运行期更严就会出现"运行期按周一发、界面却显示未配置"的分叉——正是本单
        在消灭的那类病（运行期走 `config_loader` 的 `int()` 归一）。
        """
        cases = {
            "00": "0", "+3": "3", " 5 ": "5", "6": "6",
            "": "", "9": "", "abc": "", "²": "", "3.0": "",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    health_report_weekday_setting({"YIBAN_HEALTH_REPORT_WEEKDAY": raw}),
                    want)
        self.assertEqual(health_report_weekday_setting({}), "")

    def test_time_reader_normalises_like_the_runtime(self):
        for raw, want in (("9:00", "09:00"), ("09:00", "09:00"), (" 6:05 ", "06:05"),
                          ("99:99", ""), ("", ""), ("abc", "")):
            with self.subTest(raw=raw):
                self.assertEqual(
                    health_report_time_str({"YIBAN_HEALTH_REPORT_TIME": raw}), want)
        self.assertEqual(health_report_time_str({}), "")

    def test_healthcard_fills_the_time_only_on_user_toggle(self):
        """时刻初值只补在开关的 change 事件上，不得补在派生 ref 的 watch 上。

        设置是异步到达的（props.settings 先是 `{}`），等真设置回来时开关状态会从
        false 变 true——挂在 watch 上就等于"打开设置页"这个动作本身把"只配了星期"的
        存量配置改写成"09:00 发"。本仓 vitest 无组件挂载夹具，故这一条按源码契约钉；
        它的行为面（只配星期时提交为空）由 `model.spec.ts` 的同名用例钉住。
        """
        src = _read(os.path.join(BASE, "frontend", "src", "settings", "HealthCard.vue"))
        self.assertIn('@change="onReportFixedToggle"', src,
                      "开关的初值补点没挂在 change 事件上")
        self.assertNotIn("watch(() => form.value.reportFixed", src,
                         "初值补点挂回了 watch：设置异步到达时会改写存量配置")


class ConfigRereadLatchTest(_HealthScheduleBase):
    """坏键值只告警一次（每键每 `.env` 状态），且改完立刻重读。"""

    def test_bad_config_warns_once_per_env_state_not_every_tick(self):
        """坏键值不得每跳告警：节拍 300s 时一个坏键一天能刷几百行日志。"""
        self._set_config(time_value="99:99", weekday_value="9")
        with self.assertLogs(level="WARNING") as logs:
            self.webapp._channel_health_report_due(HEALTHY_STATUS)
            first_round = len(logs.output)
            for _ in range(12):
                self.webapp._channel_health_report_due(HEALTHY_STATUS)
            self.assertEqual(len(logs.output), first_round,
                             "同一 .env 状态下不得每跳重复告警（坏键值被放大成日志刷屏）")
        self.assertGreaterEqual(first_round, 2,
                                "坏键值仍须告警（加载器点名 + 星期的回落说明）")

    def test_new_bad_value_warns_again(self):
        """换了一个新的坏值必须再告警——闩只挡"同一状态重复告警"，不挡新事实。"""
        self._set_config(time_value="99:99")
        with self.assertLogs(level="WARNING") as logs:
            self.webapp._channel_health_report_due(HEALTHY_STATUS)
            n1 = len(logs.output)
            self._set_config(time_value="25:00")
            self.webapp._channel_health_report_due(HEALTHY_STATUS)
            self.assertGreater(len(logs.output), n1, "换了新的坏值应重新告警")


class _StopLoop(Exception):
    """打断清理线程的 while True：由 `time.sleep` 打桩在指定轮次抛出。"""


class PurgeLoopCadenceTest(_HealthScheduleBase):
    """校验者：唤醒节拍被钉住（钟点锚定靠它），且每日清理不得跟着变成每轮一跑。

    循环体是 `create_app` 里的闭包（`target=_daily_purge_loop`），取不到属性；这里把
    `threading.Thread` 换成记录器、放开 `YIBAN_DISABLE_PURGE_LOOP` 跑一次真实
    `create_app`，取回**真身**再驱动——不另写一份循环体来对着测（那测的是副本）。
    """

    def _capture_loop(self):
        self.webapp._purge_loop_started = False
        holder = {}

        class _Recorder:
            def __init__(self, target=None, daemon=None, name=None, **_kw):
                holder["target"] = target
                holder["name"] = name

            def start(self):
                holder["started"] = True

        with mock.patch.object(self.webapp.threading, "Thread", _Recorder), \
                mock.patch.dict(os.environ, {"YIBAN_DISABLE_PURGE_LOOP": ""}):
            self.webapp.create_app()
        self.assertTrue(holder.get("started"), "清理线程未被启动（接线断了）")
        self.assertEqual(holder.get("name"), "daily-purge")
        return holder["target"]

    def _drive_loop(self, moments, cleanup_counter, due=False, attempts=None):
        """喂一串「当前时刻」驱动循环，多出的一轮 sleep 处打断，返回 sleep 参数列表。

        `due=True` 把闸门钉成"该发"（降级日的形状）；`attempts` 传列表时按"投递失败"
        计一次尝试——不落 app_meta 的已播标记，重试资格因此整天保持。
        """
        loop = self._capture_loop()
        sleeps = []
        it = iter(moments)

        def _sleep(sec):
            sleeps.append(sec)
            if len(sleeps) > len(moments):
                raise _StopLoop()

        patches = [
            mock.patch.object(self.webapp.time, "sleep", side_effect=_sleep),
            mock.patch.object(self.webapp.clock, "now", side_effect=lambda: next(it)),
            mock.patch.object(self.webapp.db, "run_daily_cleanup",
                              side_effect=lambda: cleanup_counter.append(1)),
            mock.patch.object(self.webapp, "_channel_health_report_due",
                              return_value=due),
        ]
        if attempts is not None:
            patches.append(mock.patch.object(
                self.webapp, "_send_channel_health_report",
                side_effect=lambda *a, **kw: (attempts.append(1), False)[1]))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with self.assertRaises(_StopLoop):
                loop()
        return sleeps

    def test_tick_is_bounded_so_the_clock_anchor_can_be_hit(self):
        """节拍上界按**公布的到达精度**（±5 分钟）收紧，不是宽到 600 秒也算过。"""
        sleeps = self._drive_loop([MONDAY, MONDAY + timedelta(minutes=5)], [])
        self.assertEqual(sleeps[0], 60, "首轮延迟保持既有 60 秒")
        self.assertLessEqual(self.webapp._HEALTH_REPORT_TICK_SEC, 300,
                             "唤醒间隔超过 5 分钟即与公布的到达精度不符")
        self.assertGreater(sleeps[1], 0)
        self.assertLessEqual(sleeps[1], self.webapp._HEALTH_REPORT_TICK_SEC)

    def test_documented_arrival_precision_matches_the_tick(self):
        """三处文案里的分钟数必须等于 `_HEALTH_REPORT_TICK_SEC`（防文档与常量脱钩）。"""
        minutes = self.webapp._HEALTH_REPORT_TICK_SEC // 60
        for rel in ("README.md", ".env.example",
                    os.path.join("frontend", "src", "settings", "HealthCard.vue")):
            with self.subTest(doc=rel):
                self.assertIn(f"{minutes} 分钟", _read(os.path.join(BASE, rel)),
                              f"{rel} 没写到达精度 {minutes} 分钟（改节拍要同步改文案）")

    def test_daily_cleanup_runs_once_a_day_not_once_a_tick(self):
        counter = []
        moments = [MONDAY + timedelta(minutes=5 * i) for i in range(4)]
        moments += [MONDAY + timedelta(days=1, minutes=5 * i) for i in range(3)]
        self._drive_loop(moments, counter)
        self.assertEqual(len(counter), 2,
                         "同一天内多轮唤醒只跑一次清理，跨日才再跑一次")

    def _drive_loop_real_gate(self, moments, attempts):
        """驱动循环但走**真实**闸门（通道钉成健康）：钉跨零点补发的端到端行为。

        时刻用可变盒子喂（不按调用次数消费迭代器）：真实闸门一轮里会读多次
        `clock.now()`，按次分发的迭代器会把时刻分配错位。
        """
        loop = self._capture_loop()
        box = {"t": moments[0]}
        sleeps = []

        def _sleep(sec):
            sleeps.append(sec)
            if len(sleeps) > len(moments):
                raise _StopLoop()
            box["t"] = moments[len(sleeps) - 1]      # 下一轮的"当前时刻"

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                self.webapp.time, "sleep", side_effect=_sleep))
            stack.enter_context(mock.patch.object(
                self.webapp.clock, "now", side_effect=lambda: box["t"]))
            stack.enter_context(mock.patch.object(
                self.webapp.db, "run_daily_cleanup", side_effect=lambda: None))
            stack.enter_context(mock.patch.object(
                self.webapp, "_alert_channel_status",
                return_value=dict(HEALTHY_STATUS)))
            stack.enter_context(mock.patch.object(
                self.webapp.notify, "has_pending_exhaustion_notice",
                return_value=False))
            stack.enter_context(mock.patch.object(
                self.webapp.notify, "budget_exhausted_today", return_value=False))
            stack.enter_context(mock.patch.object(
                self.webapp, "_send_channel_health_report",
                side_effect=lambda *a, **kw: (attempts.append(box["t"]), True)[1]))
            with self.assertRaises(_StopLoop):
                loop()
        return sleeps

    def test_late_evening_moment_is_not_lost_to_the_midnight_gap(self):
        """端到端：末跳落在配置时刻之前、下一跳已跨日时，周报在零点后的第一跳补发。"""
        self._set_config(time_value="23:58")
        attempts = []
        self._drive_loop_real_gate([
            datetime(2026, 9, 21, 23, 51),
            datetime(2026, 9, 21, 23, 56),
            datetime(2026, 9, 22, 0, 1),
            datetime(2026, 9, 22, 0, 6),
        ], attempts)
        self.assertEqual(len(attempts), 1, f"必须恰好补发一封：{attempts}")
        self.assertGreaterEqual(attempts[0], datetime(2026, 9, 21, 23, 58),
                                "不得早于配置时刻发出")

    def test_configured_moment_in_the_midnight_gap_is_covered_by_ticks_too(self):
        """区间补发在**逐跳驱动**下也生效（`_drive_ticks` 传 since，与清理线程同一姿势）。"""
        self._set_config(time_value="23:58")
        start = datetime(2026, 9, 21, 23, 46)         # 周一 23:46 = 首跳钟点
        sent, _due = self._drive_ticks(start, start + timedelta(days=1))
        self.assertEqual(len(sent), 1, f"跨零点那一跳必须补发且只补一封：{sent}")
        self.assertEqual(sent[0].strftime("%Y-%m-%d %H:%M"), "2026-09-22 00:01",
                         "补发落在零点后的第一跳")

    def test_failed_delivery_is_not_retried_on_every_tick(self):
        """投递失败不得把"当日可重试"放大成每 5 分钟一次。

        节拍收细到 5 分钟后，"闸门恒判该发 + 投递失败不落标记"的组合若没有尝试闩，
        一个降级日会做 ~288 次外发尝试与同量降级审计痕迹。跨日应重新获得一次尝试资格。
        """
        attempts = []
        moments = [MONDAY + timedelta(minutes=5 * i) for i in range(6)]
        moments += [MONDAY + timedelta(days=1, minutes=5 * i) for i in range(3)]
        self._drive_loop(moments, [], due=True, attempts=attempts)
        self.assertEqual(len(attempts), 2,
                         f"一个进程日内至多尝试一次，跨日再获得一次：{len(attempts)} 次")


if __name__ == "__main__":
    unittest.main()
