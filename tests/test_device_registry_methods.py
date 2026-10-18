# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""设备登记读回/落库两条方法的行为用例（`_read_registered_device` / `_persist_registered_device`）。

标签：D · 状态词汇与账号生命周期

覆盖：两条方法各自的**契约**（不是签到编排的复演——编排由
    `tests/test_device_identification.py` 钉）。本文件钉的是"按需补全"端点直接依赖的
    两件事：① 落库的**返回值**如实（调用方据此回"已保存/未取到"）；② **空值永不落库**
    （读不到时不得把库里既有设备值冲掉）。

预期行为（先于实现写下，按通用工程语义）：
1. `_persist_registered_device(state)` 成功写入返回 True；机型或识别码任一为空 ⇒
   不调用 `update_account`（不写半对、不写空值）并返回 False；账号无库行（id=0）⇒
   同上；落库抛异常 ⇒ fail-open 返回 False，且内存副本**不**更新；
2. `_read_registered_device()` 读失败（网络异常）⇒ 返回 None 且**不调用** `update_account`
   ——这是"读不到不许冲库"的最内层判据；读成功 ⇒ 返回解析出的 `DeviceState` 并落库一次。

打桩目标：`yiban.client.update_account`（落库出口）与 `yiban.platform.fetch_device_state`
（上游出口）。不触网。
"""

import os
import unittest
from unittest import mock

import requests

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import signin  # noqa: E402

CODE = "abcd" + "0" * 56 + "wxyz"
MODEL = "Example-EX1000"


def _state(code=CODE, model=MODEL, authorized=True):
    return signin.yiban_client.parse_device_state(
        {"authorize": authorized, "Code": code, "PhoneModel": model, "UpdateTime": 1})


def _client(*, phone_code="", phone_model="", account_id=0):
    """轻量客户端：绕过 `__init__`（与 `test_device_identification.py` 同惯例）。"""
    client = signin.YibanClient.__new__(signin.YibanClient)
    client.account = signin.Account(phone="13800138000", password="secret",
                                    account_id=account_id)
    client.phone_model = phone_model
    client.phone_code = phone_code
    client.logged_in = True
    client.use_killyiban = False
    client.csrf = "csrf"
    client.session = object()
    return client


class PersistRegisteredDeviceTest(unittest.TestCase):
    """`_persist_registered_device` 的返回值与"空值不落库"契约。"""

    def test_success_writes_both_fields_and_returns_true(self):
        client = _client(account_id=42)
        with mock.patch.object(signin.yiban_client, "update_account") as upd:
            wrote = client._persist_registered_device(_state())
        self.assertIs(wrote, True)
        upd.assert_called_once()
        _args, _kwargs = upd.call_args
        fields = _args[1] if len(_args) > 1 else _kwargs
        self.assertEqual(fields.get("phone_code"), CODE)
        self.assertEqual(fields.get("phone_model"), MODEL)
        self.assertEqual(client.phone_code, CODE, "成功后同步内存副本")
        self.assertEqual(client.phone_model, MODEL)

    def test_missing_code_writes_nothing(self):
        client = _client(account_id=42)
        with mock.patch.object(signin.yiban_client, "update_account") as upd:
            wrote = client._persist_registered_device(_state(code=""))
        self.assertIs(wrote, False)
        upd.assert_not_called()

    def test_missing_model_writes_nothing(self):
        client = _client(account_id=42)
        with mock.patch.object(signin.yiban_client, "update_account") as upd:
            wrote = client._persist_registered_device(_state(model=""))
        self.assertIs(wrote, False)
        upd.assert_not_called()

    def test_without_account_row_writes_nothing(self):
        client = _client(account_id=0)
        with mock.patch.object(signin.yiban_client, "update_account") as upd:
            wrote = client._persist_registered_device(_state())
        self.assertIs(wrote, False)
        upd.assert_not_called()

    def test_write_failure_is_fail_open_and_keeps_memory_clean(self):
        client = _client(account_id=42)
        with mock.patch.object(signin.yiban_client, "update_account",
                               side_effect=RuntimeError("库锁了")):
            wrote = client._persist_registered_device(_state())
        self.assertIs(wrote, False)
        self.assertEqual(client.phone_code, "", "落库失败不得把未持久化的值写进内存副本")


class ReadRegisteredDeviceTest(unittest.TestCase):
    """`_read_registered_device` 的"失败不写库"不变式。"""

    def test_read_failure_never_touches_account_row(self):
        """红线：读失败返回 None，且不调用 `update_account`（库里既有值不被冲）。"""
        client = _client(account_id=42)
        with mock.patch.object(signin.yiban_platform, "fetch_device_state",
                               side_effect=requests.exceptions.ConnectionError("网络炸了")), \
             mock.patch.object(signin.yiban_client, "update_account") as upd:
            state = client._read_registered_device()
        self.assertIsNone(state)
        upd.assert_not_called()

    def test_read_success_persists_once_and_returns_state(self):
        client = _client(account_id=42)
        envelope = {"code": 0, "msg": "", "data": {
            "authorize": True, "Code": CODE, "PhoneModel": MODEL, "UpdateTime": 1}}
        with mock.patch.object(signin.yiban_platform, "fetch_device_state",
                               return_value=signin.yiban_platform.SignResponse(data=envelope)), \
             mock.patch.object(signin.yiban_client, "update_account") as upd:
            state = client._read_registered_device()
        self.assertIsNotNone(state)
        self.assertEqual(state.code, CODE)
        self.assertEqual(state.phone_model, MODEL)
        self.assertEqual(upd.call_count, 1, "读成功只落库一次")


if __name__ == "__main__":
    unittest.main()
