# -*- coding: utf-8 -*-
"""多任务「随机选点、任一成功即停」签到语义测试（2026-08-29）。

覆盖 signin.YibanClient.signin() 在 API 返回多个签到任务时的行为：
- 任务列表先随机打乱（不固定签第一个点位，贴近学生真实行为）；
- 任一任务成功即停止，不再重复提交后续任务；
- 前面的任务失败会继续尝试下一个（随机序）；全部失败才判失败。
"""
import datetime as _dt
import os
import sys
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import signin  # noqa: E402


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
    client = signin.YibanClient.__new__(signin.YibanClient)
    client.account = signin.Account(phone="13800138000", password="secret")
    client.logged_in = True
    client.use_killyiban = False
    client.csrf = "csrf"
    client.phone_model = "Vivo-Test"
    client.phone_code = "C" * 64
    session = mock.Mock()
    session.get.return_value = _FakeResp(_sign_position_data())
    post_iter = iter(post_results)
    session.post.side_effect = lambda *a, **k: _FakeResp(next(post_iter))
    client.session = session
    return client, session


class MultiTaskAnySuccessTest(unittest.TestCase):
    def test_first_success_stops_after_one_submit(self):
        """随机序下首个尝试即成功 → 只提交 1 次，返回成功。"""
        client, session = _make_client([_signin_result(True)])
        with mock.patch.object(signin, "generate_position_in_polygon",
                               return_value=(118.0, 31.0)), \
             mock.patch.object(signin.random, "shuffle", side_effect=lambda lst: None):
            ok, msg, skip, status = client.signin()
        self.assertTrue(ok)
        self.assertIn("签到成功", msg)
        self.assertEqual(session.post.call_count, 1, "首个成功即停，不应提交第二个任务")

    def test_second_hits_after_first_fail(self):
        """随机序下首个失败、次个成功 → 提交 2 次后成功（失败不阻断尝试）。"""
        client, session = _make_client([_signin_result(False), _signin_result(True)])
        with mock.patch.object(signin, "generate_position_in_polygon",
                               return_value=(118.0, 31.0)), \
             mock.patch.object(signin.random, "shuffle", side_effect=lambda lst: None):
            ok, msg, skip, status = client.signin()
        self.assertTrue(ok)
        self.assertIn("签到成功", msg)
        self.assertIn("失败后命中", msg)
        self.assertEqual(session.post.call_count, 2, "首个失败应继续尝试下一个")

    def test_all_fail_returns_failure(self):
        """全部任务失败 → 判失败并列出原因。"""
        client, session = _make_client([_signin_result(False), _signin_result(False)])
        with mock.patch.object(signin, "generate_position_in_polygon",
                               return_value=(118.0, 31.0)), \
             mock.patch.object(signin.random, "shuffle", side_effect=lambda lst: None):
            ok, msg, skip, status = client.signin()
        self.assertFalse(ok)
        self.assertEqual(status, signin.STATUS_FAILED)
        self.assertIn("均失败", msg)
        self.assertEqual(session.post.call_count, 2, "全部失败应尝试完所有任务")

    def test_tasks_are_shuffled_before_signing(self):
        """任务列表在提交前被随机打乱（不固定签第一个点位）。"""
        client, session = _make_client([_signin_result(True)])
        captured = {}

        def fake_shuffle(lst):
            captured["lst"] = list(lst)
            lst[:] = list(reversed(lst))  # 打乱（反转），验证 shuffle 确实被调用

        with mock.patch.object(signin, "generate_position_in_polygon",
                               return_value=(118.0, 31.0)), \
             mock.patch.object(signin.random, "shuffle", side_effect=fake_shuffle):
            ok, _, _, _ = client.signin()
        self.assertTrue(ok)
        names = [p.get("Name") for p in captured["lst"]]
        self.assertEqual(names, ["任务A", "任务B"], "shuffle 必须收到全部任务")
        self.assertEqual(len(captured["lst"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
