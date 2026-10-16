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
3. 取不到设备码（读失败 / 非 0 信封 / 该账号未登记设备）**拒签**：不提交、不伪造，
   文案只点名 App 内登记授权这条可执行动作（本站侧靠自动补全 · 用户面无可编辑入口）；
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

import requests

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
    def __init__(self, json_data, status_code=200, text=""):
        self._json = json_data
        self.status_code = status_code
        # `text` 是 WAF 与信封两条判据的输入面（`policy.is_blocked` 与 `parse_api_envelope` 都读它）
        self.text = text
        self.headers = {}

    def json(self):
        return self._json


class _RoutingSession:
    """按 URL 路由的会话桩：记录每次请求，便于断言"读没读服务端登记"。"""

    def __init__(self, device_state=None, device_error=None, device_text=""):
        self.device_state = device_state
        self.device_error = device_error
        # `device_text` 只喂给 getState 的响应体：WAF 判据与信封 `code=999` 都从它读
        self.device_text = device_text
        self.get_urls = []
        self.post_bodies = []

    def get(self, url, **kwargs):
        self.get_urls.append(url)
        if url.endswith(SIGN_POSITION_PATH):
            return _Resp(_sign_position_data())
        if url.endswith("/device/student/index/getState"):
            if self.device_error is not None:
                raise self.device_error
            return _Resp(self.device_state, text=self.device_text)
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


class DeviceMissingCodeRefusalTest(unittest.TestCase):
    """行为 3（2026-10-16 修订）：取不到设备码就**拒签**——不提交，也不伪造。

    口径变更的根因：设备读取从"诊断性前置"变成**签到前置**。平台对无登记设备的提交
    一律回「请使用授权设备进行签到」，提交一次必然失败一次，还多留一条服务端失败计数。
    故三条取不到码的路径（读失败 / 非 0 信封 / 该账号未登记设备）一律本地拦截。
    """

    def test_read_failure_refuses_and_never_submits(self):
        session = _RoutingSession(
            device_error=requests.exceptions.ConnectionError("网络炸了"))
        client = _make_client(session)
        ok, msg, _skip, status = _run(client)
        self.assertFalse(ok, "取不到设备码必须拒签")
        self.assertEqual(session.post_bodies, [], "拒签时不得提交签到（明知必被拒）")
        self.assertEqual(status, signin.STATUS_FAILED)
        self.assertIn("设备识别", msg, "文案必须点名易班 App 内的设备识别页")
        self.assertIn("自动补全", msg,
                      "补救动作只能指向 App 内登记；本站侧靠自动补全，不再教用户手填"
                      "（用户面已无这两个字段的编辑入口）")

    def test_non_zero_envelope_refuses(self):
        session = _RoutingSession({"code": 500, "msg": "服务端错误", "data": None})
        client = _make_client(session)
        ok, _msg, _skip, _status = _run(client)
        self.assertFalse(ok, "非 0 信封同样取不到码 ⇒ 拒签")
        self.assertEqual(session.post_bodies, [])

    def test_unregistered_device_refuses(self):
        session = _RoutingSession(_device_state_body(authorize=False, code="", model=""))
        client = _make_client(session)
        ok, msg, _skip, _status = _run(client)
        self.assertFalse(ok, "账号没有登记设备时拒签")
        self.assertEqual(session.post_bodies, [])
        self.assertIn("未登记设备", msg, "文案要说清是哪一类缺失")

    def test_missing_code_is_never_fabricated(self):
        """红线：任何取不到码的路径都不许凭空造一个设备码。

        判据是**不变式**——提交体里的 `Code` 只有两个允许来源：服务端登记值，
        或账号里人填的值。取不到就没有提交体可查（拒签）。
        """
        session = _RoutingSession(
            device_error=requests.exceptions.ConnectionError("网络炸了"))
        client = _make_client(session)
        _run(client)
        self.assertEqual(session.post_bodies, [], "取不到码时不存在提交体")
        self.assertEqual(client.phone_code, "", "不得凭空写入设备码")
        # 反面：登记值存在时，提交体里的 Code 必须**逐字**等于登记值
        session2 = _RoutingSession(_device_state_body(code="f" * 64))
        client2 = _make_client(session2)
        _run(client2)
        self.assertEqual(_submitted_form_fields(session2.post_bodies[-1]).get("Code"),
                         "f" * 64, "唯一合法的码来源是登记值本身，不做任何加工")


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


class DeviceReadLoudFailureTest(unittest.TestCase):
    """行为 7（2026-10-16 独立审查整改）：读取失败必须**分类正确**，不许一律降级成"设备缺失"。

    两条不变式：环境问题（WAF 拦截）与会话失效各有**既有的**处置路径
    （WAF 文案与风控重试档、清缓存后真重登）。把这两类吞进"设备缺失"会静默换掉
    处置动作——那是比"没读到设备"严重得多的错。
    """

    def test_waf_blocked_read_uses_the_single_waf_message(self):
        """getState 被 WAF 拦截 → 走既有 WAF 文案（单一真源），不是设备缺失文案。"""
        session = _RoutingSession({"code": 0, "data": {}},
                                  device_text="您的访问存在风险访问，已被拦截")
        client = _make_client(session)
        ok, msg, _skip, status = _run(client)
        self.assertFalse(ok)
        self.assertEqual(msg, signin.security.WAF_BLOCKED_MESSAGE,
                         "WAF 拦截必须复用 security 的单一文案，不得自造第二份")
        self.assertEqual(session.post_bodies, [], "被拦截时不得提交签到")
        self.assertEqual(status, signin.STATUS_FAILED)

    def test_session_expired_read_stays_loud(self):
        """getState 回 code=999（会话失效）→ 必须外溢，交既有"清缓存后真重登"路径。

        判据是**不变式**：会话失效在任何一层都不许被吞成"设备缺失"——那会把
        「清缓存重登」换成「本地拒签」，失败分类与处置动作一起走错。
        """
        session = _RoutingSession({"code": 999, "msg": "", "data": None},
                                  device_text='{"code": 999, "msg": "", "data": null}')
        client = _make_client(session)
        with self.assertRaisesRegex(RuntimeError, "会话失效"):
            _run(client)


if __name__ == "__main__":
    unittest.main()
