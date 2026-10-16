# -*- coding: utf-8 -*-
"""设备识别码：签到前从服务端登记**读回**并照实回传。

标签：D · 状态词汇与账号生命周期
覆盖：`YibanClient.signin()` 的设备身份来源。平台 2026-10-16 起强制校验设备绑定：
    签到时 `Code`/`PhoneModel` 必须与账号**已登记设备**一致，否则上游回「请使用授权设备」。

预期行为（先于实现写下，按通用工程语义，不为现状豁免）：
1. 账号未配置设备时，签到前读 `GET /device/student/index/getState`，把登记值
   （`Code`/`PhoneModel`）照实回传——**读回不是伪造**：真实客户端用原生桥生成一次
   并登记，此后各处回传同一个值；
2. 本地已配置设备时**以本地为准且不读服务端**（显式配置优先；省一次请求）；
3. 读回失败（网络异常/非 0 信封/畸形）**fail-open**：诊断性前置不得中断签到，
   服务端自会裁决；
4. `authorize=False`（已登记未授权）仍提交，并留下点名真实补救动作的告警；
5. 上游回「授权设备」时的提示必须指向**真正的补救动作**（在易班 App 内完成设备授权），
   不得再教用户去配环境变量（读回之后那不是缺配置）。

打桩目标：设备读取走真实调用链，用**按 URL 路由的假会话**区分 `getState` 与
`signPosition`（比打桩 `fetch_device_state` 更强：连"发到哪里"一起验了）。
依赖：纯进程内 fake session（按 URL 路由的 get/post 桩），不触网。
"""
import datetime as _dt
import os
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import signin  # noqa: E402

SIGN_POSITION_PATH = "/nightAttendance/student/index/signPosition"
SIGN_IN_PATH = "/nightAttendance/student/index/signIn"

#: 服务端登记的设备（合成值；真实值是指纹，不入仓库）
REGISTERED_CODE = "d" * 64
REGISTERED_MODEL = "Example-EX1000"


def _sign_position_data():
    now = int(_dt.datetime.now().timestamp())
    return {
        "code": 0,
        "data": {
            "State": 0,
            "Msg": "",
            "Position": [{
                "Id": "pos-A", "Type": "campus", "Title": "任务A", "Name": "任务A",
                "LngLat": "118.05,31.05",
                "Points": ["118.0,31.0", "118.1,31.0", "118.1,31.1", "118.0,31.1"],
                "Address": "点A",
            }],
            "Range": {"StartTime": now - 3600, "EndTime": now + 3600},
        },
    }


class _Resp:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.text = ""
        self.headers = {}

    def json(self):
        return self._json


class _RoutingSession:
    """按 URL 路由的会话桩：记录每次请求，便于断言"读没读服务端登记"。"""

    def __init__(self, device_state=None, device_error=None):
        self.device_state = device_state
        self.device_error = device_error
        self.get_urls = []
        self.post_bodies = []

    def get(self, url, **kwargs):
        self.get_urls.append(url)
        if url.endswith(SIGN_POSITION_PATH):
            return _Resp(_sign_position_data())
        if url.endswith("/device/student/index/getState"):
            if self.device_error is not None:
                raise self.device_error
            return _Resp(self.device_state)
        raise AssertionError("未预期的 GET: " + url)

    def post(self, url, **kwargs):
        self.post_bodies.append(kwargs.get("data", ""))
        return _Resp({"code": 0, "data": True})

    @property
    def headers(self):
        return {}


def _make_client(session, *, phone_code="", phone_model="", account_id=0):
    client = signin.YibanClient.__new__(signin.YibanClient)  # 绕过 __init__：要一个已登录的壳
    client.account = signin.Account(phone="13800138000", password="secret",
                                    account_id=account_id)
    client.logged_in = True
    client.use_killyiban = False
    client.csrf = "csrf"
    client.phone_model = phone_model
    client.phone_code = phone_code
    client.session = session
    return client


def _device_state_body(authorize=True, code=REGISTERED_CODE, model=REGISTERED_MODEL):
    return {"code": 0, "msg": "", "data": {
        "authorize": authorize, "Code": code, "PhoneModel": model, "UpdateTime": 1761517844,
    }}


def _run(client):
    with mock.patch.object(signin.yiban_client, "generate_position_in_polygon",
                           return_value=(118.0, 31.0)), \
         mock.patch.object(signin.random, "shuffle", side_effect=lambda lst: None):
        return client.signin()


def _submitted_form_fields(body):
    """把提交体解析成字段字典（值不做 URL 解码，够断言 Code/PhoneModel 即可）。"""
    fields = {}
    for part in str(body).split("&"):
        if "=" in part:
            key, _, value = part.partition("=")
            fields[key] = value
    return fields


class DeviceRegistryReadTest(unittest.TestCase):
    """行为 1 / 2：读回登记并在提交里照实回传；本地配置优先且不读服务端。"""

    def test_unconfigured_account_reads_registry_and_submits_registered_code(self):
        session = _RoutingSession(_device_state_body())
        client = _make_client(session)
        ok, _msg, _skip, _status = _run(client)
        self.assertTrue(ok)
        self.assertTrue(any(u.endswith("/device/student/index/getState") for u in session.get_urls),
                        "未配置设备时应读一次服务端登记")
        fields = _submitted_form_fields(session.post_bodies[-1])
        self.assertEqual(fields.get("Code"), REGISTERED_CODE, "必须回传登记的设备码")
        self.assertEqual(fields.get("PhoneModel"), REGISTERED_MODEL, "必须回传登记的机型")

    def test_locally_configured_device_wins_and_skips_registry(self):
        session = _RoutingSession(_device_state_body())
        client = _make_client(session, phone_code="L" * 64, phone_model="Local-Model")
        ok, _msg, _skip, _status = _run(client)
        self.assertTrue(ok)
        self.assertFalse(any("getState" in u for u in session.get_urls),
                         "本地已配置设备时不应再读服务端（显式配置优先，省一次请求）")
        fields = _submitted_form_fields(session.post_bodies[-1])
        self.assertEqual(fields.get("Code"), "L" * 64)
        self.assertEqual(fields.get("PhoneModel"), "Local-Model")


class DeviceRegistrationPersistenceTest(unittest.TestCase):
    """行为 6（用户 2026-10-16 追加）：**注册一次、长期本地保存**，不每轮拉取。"""

    def test_first_run_persists_and_second_run_skips_registry(self):
        """首轮读回并落库；同实例第二轮不再发请求（内存副本已复用）。"""
        session = _RoutingSession(_device_state_body())
        client = _make_client(session, account_id=42)
        with mock.patch.object(signin.yiban_client, "update_account") as upd:
            _run(client)
        self.assertEqual(upd.call_count, 1, "读回后应落库一次")
        _args, kwargs = upd.call_args
        fields = _args[1] if len(_args) > 1 else kwargs
        self.assertEqual(fields.get("phone_code"), REGISTERED_CODE)
        self.assertEqual(fields.get("phone_model"), REGISTERED_MODEL)
        # 第二轮：账号副本已就绪，不得再读服务端登记（签到本身仍会取 signPosition）
        def _state_reads():
            return [u for u in session.get_urls if u.endswith("/device/student/index/getState")]

        self.assertEqual(len(_state_reads()), 1, "首轮只读一次登记")
        _run(client)
        self.assertEqual(len(_state_reads()), 1, "落库复用后不应再发 getState")

    def test_no_account_row_skips_persist(self):
        """非库来源账号（account_id=0）无处可落 → 不尝试落库，签到照常。"""
        session = _RoutingSession(_device_state_body())
        client = _make_client(session)  # account_id 默认 0
        with mock.patch.object(signin.yiban_client, "update_account") as upd:
            ok, _m, _s, _st = _run(client)
        self.assertTrue(ok)
        upd.assert_not_called()

    def test_persist_failure_is_fail_open(self):
        """落库失败不得中断签到（诊断性持久化）。"""
        session = _RoutingSession(_device_state_body())
        client = _make_client(session, account_id=42)
        with mock.patch.object(signin.yiban_client, "update_account",
                              side_effect=RuntimeError("库锁了")):
            ok, _m, _s, _st = _run(client)
        self.assertTrue(ok, "落库失败应 fail-open，本轮仍用读回值")


class DeviceRegistryFailOpenTest(unittest.TestCase):
    """行为 3：读回失败不得中断签到。"""

    def test_read_failure_still_signs(self):
        session = _RoutingSession(device_error=RuntimeError("网络炸了"))
        client = _make_client(session)
        ok, _msg, _skip, _status = _run(client)
        self.assertTrue(ok, "设备登记读回失败属诊断性前置，不能把签到一起拖死")

    def test_non_zero_envelope_still_signs(self):
        session = _RoutingSession({"code": 500, "msg": "服务端错误", "data": None})
        client = _make_client(session)
        ok, _msg, _skip, _status = _run(client)
        self.assertTrue(ok, "非 0 信封同样 fail-open")

    def test_unregistered_device_still_signs_with_empty_code(self):
        session = _RoutingSession(_device_state_body(authorize=False, code="", model=""))
        client = _make_client(session)
        ok, _msg, _skip, _status = _run(client)
        self.assertTrue(ok, "账号没有登记设备时仍按现状提交（服务端裁决）")
        fields = _submitted_form_fields(session.post_bodies[-1])
        self.assertEqual(fields.get("Code", ""), "")


class DeviceAuthorizationWarningTest(unittest.TestCase):
    """行为 4：已登记未授权 → 提交并点名补救动作。"""

    def test_unauthorized_device_warns_with_app_remedy(self):
        session = _RoutingSession(_device_state_body(authorize=False))
        client = _make_client(session)
        with self.assertLogs("yiban.client", level="WARNING") as logs:
            ok, _msg, _skip, _status = _run(client)
        self.assertTrue(ok)
        joined = "\n".join(logs.output)
        self.assertIn("未授权", joined, "应告警提醒设备未授权")
        self.assertIn("设备识别", joined, "补救动作必须点名易班 App 内的设备识别页")
        fields = _submitted_form_fields(session.post_bodies[-1])
        self.assertEqual(fields.get("Code"), REGISTERED_CODE, "未授权也要照实回传登记码")


class DeviceRejectionHintTest(unittest.TestCase):
    """行为 5：上游拒签时的提示指向真正的补救动作。"""

    def test_rejection_hint_points_to_app_authorization(self):
        session = _RoutingSession(_device_state_body())
        client = _make_client(session)
        session.post = lambda url, **kw: _Resp({"code": 500, "msg": "请使用授权设备进行签到", "data": None})
        ok, msg, _skip, _status = _run(client)
        self.assertFalse(ok)
        self.assertIn("授权设备", msg)
        self.assertNotIn("YIBAN_PHONE_CODE", msg,
                         "读回机制落地后，再教用户配环境变量就是错的补救动作")
        self.assertIn("设备识别", msg, "提示应指向易班 App 内的设备识别页")


if __name__ == "__main__":
    unittest.main()
