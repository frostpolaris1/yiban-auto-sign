# -*- coding: utf-8 -*-
"""多任务「随机选点、任一成功即停」签到语义。

标签：D · 状态词汇与账号生命周期
覆盖：`YibanClient.signin()` 在 API 返回多个签到任务时的行为——任务列表先随机打乱、
    任一成功即停不重复提交、前面失败会继续尝试下一个、全部失败才判失败。
对应实现：`yiban/client.py` 的 `YibanClient.signin`（客户端外观层）。
关键断言：`session.post` 的**调用次数**就是"即停"的证据（首个成功=1 次、先败后成=2 次、
    全败=2 次即尝试完所有任务）；随机打乱用假 shuffle 捕获实参，证明收到的是全部任务。
依赖：纯进程内 fake session（get/post 桩 + `_FakeResp`），不触网；签到窗口用
    `_dt.datetime.now()` 现算，故任何时刻跑都落在窗口内。

**打桩目标说明**：定位生成与签到的调用点在 `yiban/client.py`，故
`generate_position_in_polygon` 必须打在 `yiban.client` 上——`signin` 里那份是
**同一对象的转发**，打在它上面不会影响客户端内部的调用（静默失效，断言照样过）。
"""
import datetime as _dt
import os
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import signin  # noqa: E402

from yiban import client as yiban_client  # noqa: E402  # 打桩目标：调用点在客户端外观层


def _sign_position_data():
    """构造 signPosition 返回体：2 个任务、当前时间在签到窗口内。"""
    now = int(_dt.datetime.now().timestamp())
    return {
        "code": 0,
        "data": {
            "Msg": "",
            "Position": [
                {
                    "Name": "任务A",
                    "Points": ["118.0,31.0", "118.1,31.0", "118.1,31.1", "118.0,31.1"],
                    "Address": "点A",
                },
                {
                    "Name": "任务B",
                    "Points": ["118.2,31.2", "118.3,31.2", "118.3,31.3", "118.2,31.3"],
                    "Address": "点B",
                },
            ],
            "Range": {"StartTime": now - 3600, "EndTime": now + 3600},
        },
    }


def _signin_result(ok):
    return {"code": 0, "data": True} if ok else {"code": 1, "msg": "失败原因", "data": None}


class _FakeResp:
    def __init__(self, json_data):
        self._json = json_data
        self.text = ""

    def json(self):
        return self._json


def _make_client(post_results):
    """构造已登录 YibanClient：session.get 返回 2 任务数据，session.post 按序返回结果。"""
    client = signin.YibanClient.__new__(signin.YibanClient)  #绕过 __init__：真构造会去登录，这里要的是一个已登录的壳
    client.account = signin.Account(phone="13800138000", password="secret")
    client.logged_in = True
    client.use_killyiban = False
    client.csrf = "csrf"
    client.phone_model = "Vivo-Test"
    client.phone_code = "C" * 64
    session = mock.Mock()
    session.get.return_value = _FakeResp(_sign_position_data())
    post_iter = iter(post_results)
    session.post.side_effect = lambda *a, **k: _FakeResp(next(post_iter))  #post 按序弹结果：不数返回值、只数调用次数，才测得出"即停"
    client.session = session
    return client, session


class MultiTaskAnySuccessTest(unittest.TestCase):
    def test_first_success_stops_after_one_submit(self):
        """随机序下首个尝试即成功 → 只提交 1 次，返回成功。"""
        client, session = _make_client([_signin_result(True)])
        with mock.patch.object(yiban_client, "generate_position_in_polygon",
                               return_value=(118.0, 31.0)) as geo, \
             mock.patch.object(signin.random, "shuffle", side_effect=lambda lst: None):
            ok, msg, _skip, _status = client.signin()
        geo.assert_called()
        self.assertTrue(ok)
        self.assertIn("签到成功", msg)
        self.assertEqual(session.post.call_count, 1, "首个成功即停，不应提交第二个任务")

    def test_second_hits_after_first_fail(self):
        """随机序下首个失败、次个成功 → 提交 2 次后成功（失败不阻断尝试）。"""
        client, session = _make_client([_signin_result(False), _signin_result(True)])
        with mock.patch.object(yiban_client, "generate_position_in_polygon",
                               return_value=(118.0, 31.0)) as geo, \
             mock.patch.object(signin.random, "shuffle", side_effect=lambda lst: None):
            ok, msg, _skip, _status = client.signin()
        geo.assert_called()
        self.assertTrue(ok)
        self.assertIn("签到成功", msg)
        self.assertIn("失败后命中", msg)
        self.assertEqual(session.post.call_count, 2, "首个失败应继续尝试下一个")

    def test_all_fail_returns_failure(self):
        """全部任务失败 → 判失败并列出原因。"""
        client, session = _make_client([_signin_result(False), _signin_result(False)])
        with mock.patch.object(yiban_client, "generate_position_in_polygon",
                               return_value=(118.0, 31.0)) as geo, \
             mock.patch.object(signin.random, "shuffle", side_effect=lambda lst: None):
            ok, msg, _skip, status = client.signin()
        geo.assert_called()
        self.assertFalse(ok)
        self.assertEqual(status, signin.STATUS_FAILED)
        self.assertIn("均失败", msg)
        self.assertEqual(session.post.call_count, 2, "全部失败应尝试完所有任务")

    def test_tasks_are_shuffled_before_signing(self):
        """任务列表在提交前被随机打乱（不固定签第一个点位）。"""
        client, _session = _make_client([_signin_result(True)])
        captured = {}

        def fake_shuffle(lst):
            captured["lst"] = list(lst)
            lst[:] = list(reversed(lst))  # 打乱（反转），验证 shuffle 确实被调用  #反转而非随机：顺序可预测，才能反证实现没有偷偷固定签第一个点位

        with mock.patch.object(yiban_client, "generate_position_in_polygon",
                               return_value=(118.0, 31.0)) as geo, \
             mock.patch.object(signin.random, "shuffle", side_effect=fake_shuffle):
            ok, _, _, _ = client.signin()
        geo.assert_called()
        self.assertTrue(ok)
        names = [p.get("Name") for p in captured["lst"]]
        self.assertEqual(names, ["任务A", "任务B"], "shuffle 必须收到全部任务")
        self.assertEqual(len(captured["lst"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
