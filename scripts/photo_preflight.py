#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
签到窗口前的只读预演：登录 → 读回设备登记（getState）→ 拉取 signPosition，
逐个账号打印平台当前给出的判定输入。

**为什么**
平台要求上传签到照片时，`signPosition` 返回空 `Position`，本程序落 `no_position`。
运维看到 `no_position` 分不清"学校没开任务"与"要照片、我们不做"。
窗口前跑一次本脚本，即可知道有多少账号会被照片关挡住。

**只读边界**
本脚本不调用 `submit_sign_in`，也不调用 `YibanClient.signin`（签到提交触网点唯一性
门禁会红）。账号装载走 `load_accounts(migrate=False)`：独立只读连接，不建库、不建表、
不迁移、不切 WAL；因此库未初始化，登录会话缓存也不会落库。手机号一律掩码，口令不
打印、不落盘。除登录握手外，平台请求只走两个只读接口：
`device/student/index/getState` 与 `nightAttendance/.../signPosition`。

**归属**
运维侧脚本（`scripts/`）。登录链的唯一实现在 `yiban/client.py`，两个只读接口的唯一
实现在 `yiban/platform.py`；本脚本只做参数解析、账号装载与输出，不另写登录链。

**退出码**
0 = 全部账号探测完成；1 = 至少一个账号探测异常（登录失败 / 接口非 0 / WAF / 解析
失败 / 指定手机号未找到）；2 = 环境或凭据来源不可用（账号装载失败或装载到 0 个账号）。

**已知边界**
`IsNeedPhoto` 的取值语义**未证**（实拍只观测到 2）。脚本按原值输出，并用"照片"字样
与"非零"两条启发式给出提示；提示**不是平台契约**，也不改变任何签到判定。
"""
import argparse
import logging
import os
import sys
from datetime import datetime

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from yiban import logging_ext  # noqa: E402
from yiban import platform as yiban_platform  # noqa: E402
from yiban.adapters.protocol_yiban import (  # noqa: E402
    ParseError,
    parse_device_state,
    parse_sign_position,
)
from yiban.client import (  # noqa: E402
    YibanClient,
    _as_state_int,
    _looks_like_photo_required,
)
from yiban.core import masking  # noqa: E402
from yiban.engine.accounts import load_accounts  # noqa: E402

LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"


def _configure_logging():
    """日志只到 WARNING，并经输出面脱敏（登录失败消息可能内嵌裸号与令牌）。"""
    logging.basicConfig(level=logging.WARNING, format=LOG_FORMAT)
    formatter = logging_ext.MaskingFormatter(LOG_FORMAT)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


def _safe(text):
    """平台可控文本进入终端前脱敏（换行转义、抹凭据、遮裸号）。"""
    return masking.sanitize_text("" if text is None else str(text))


def _raw(value):
    """原值展示：缺失打印 `<缺失>`，其余按 str（数值不带引号）。"""
    return "<缺失>" if value is None else _safe(value)


def _format_range(range_obj):
    """`Range` 起止：epoch 原值与本地时区可读时间；缺失打印 `<缺失>`。"""
    if not isinstance(range_obj, dict):
        return "<缺失>"
    start = range_obj.get("StartTime")
    end = range_obj.get("EndTime")
    if start is None and end is None:
        return "<缺失>"
    if start is None or end is None:
        return f"{_raw(start)}..{_raw(end)}"

    def _readable(ts):
        try:
            # 本地时区可读时间：Range 是平台 epoch 秒，展示用本地钟（与 client 同口径）。
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: DTZ006
        except (TypeError, ValueError, OSError):
            return "?"

    return f"{_raw(start)}..{_raw(end)}（{_readable(start)} ~ {_readable(end)} 本地时区）"


def _read_device(client):
    """只读设备登记（getState）。返回 `(ok, 描述)`；不落库、不打印设备码。

    网络级异常按账号异常处置；其余异常（含 code=999 的会话失效）向上抛出，
    由 `main` 的边界按类型报告——吞掉它们会把我们自己的缺陷伪装成"没设备"。
    """
    try:
        resp = yiban_platform.fetch_device_state(
            client.session, client.csrf, client._policy
        )
    except requests.RequestException as exc:
        return False, f"设备登记读取失败（{type(exc).__name__}）"
    if resp.blocked:
        return False, "设备登记接口被 WAF 拦截"
    if not isinstance(resp.data, dict) or resp.data.get("code") != 0:
        return False, "设备登记接口未正常应答"
    try:
        state = parse_device_state(resp.data.get("data"))
    except ParseError as exc:
        return False, f"设备登记响应不合契约（字段 {exc.field}）"
    registered = "是" if state.code else "否"
    authorized = "是" if state.authorized else "否"
    return True, f"已登记={registered} 已授权={authorized}"


def _verdict(positions, state, is_need_photo, remark):
    """结论行：**展示用启发式**，不参与任何签到判定。"""
    if positions == 0:
        if _looks_like_photo_required(is_need_photo, remark):
            return "无点位；平台要求上传照片（启发式）⇒ 本程序不实现，今日无法签成"
        return "无点位；未观察到照片信号（请结合 State/Msg 判断）"
    if _as_state_int(state) in (0, 1):
        return "有点位；可进入正常签到流程"
    if _as_state_int(state) is None:
        return "有点位；State 无法收敛（原值见上），须人工判断"
    return f"有点位；State={_raw(state)} 由平台判定（本脚本不判定）"


def _probe(account):
    """单账号只读探测。返回 `(ok, 文本行列表)`。"""
    phone = masking.mask_phone(account.phone)
    lines = [f"[{phone}] {_safe(account.name) or '(未命名)'}"]
    client = YibanClient(account)
    try:
        if client.use_killyiban:
            client.login_killyiban()
        else:
            client.login()
        # 与 `YibanClient.signin` 逐字同序：旧流程的登录链改过 Origin/Referer，
        # 拉任务前必须改回 App 域；标准 App 特征流程保持 KILLYIBAN_HEADERS 不动。
        if not client.use_killyiban:
            client.session.headers.update(yiban_platform.APP_SIGN_HEADERS)

        device_ok, device_desc = _read_device(client)
        lines.append(f"  设备：{device_desc}")
        # 设备读回失败不提前返回：照片关的判据在 signPosition，先把它拿到；
        # 账号异常仍由返回值记一笔（退出码 1）。

        resp = yiban_platform.fetch_sign_position(
            client.session, client.csrf, client._policy
        )
        if resp.blocked:
            lines.append("  signPosition：请求被 WAF 拦截")
            return False, lines
        if not isinstance(resp.data, dict) or resp.data.get("code") != 0:
            msg = resp.data.get("msg") if isinstance(resp.data, dict) else resp.data
            lines.append(f"  signPosition：接口未正常应答 Msg={_safe(msg)!r}")
            return False, lines
        data_obj = resp.data.get("data")
        if not isinstance(data_obj, dict):
            lines.append("  signPosition：响应缺 data 对象")
            return False, lines

        # 字段口径复用协议库解析（契约只有一份）；空点位/缺时间窗与
        # `platform._validate_sign_position` 同口径不解析，直接按原值展示。
        cfg = None
        raw_positions = data_obj.get("Position")
        raw_range = data_obj.get("Range")
        eligible = (
            isinstance(raw_positions, list)
            and raw_positions
            and isinstance(raw_range, dict)
            and "StartTime" in raw_range
            and "EndTime" in raw_range
        )
        if eligible:
            try:
                cfg = parse_sign_position(data_obj)
            except ParseError as exc:
                lines.append(f"  signPosition：解析失败（字段 {exc.field}），按原值展示")
        if cfg is not None:
            state = cfg.state
            msg = cfg.msg
            positions = len(cfg.positions)
            is_need_photo = cfg.is_need_photo
            remark = cfg.remark
            range_obj = cfg.time_window
        else:
            state = data_obj.get("State")
            msg = data_obj.get("Msg")
            positions = len(raw_positions) if isinstance(raw_positions, list) else 0
            is_need_photo = data_obj.get("IsNeedPhoto")
            remark = data_obj.get("Remark")
            range_obj = raw_range

        if isinstance(range_obj, dict):
            range_text = _format_range(range_obj)
        elif range_obj is None:
            range_text = "<缺失>"
        else:  # TimeWindow 对象
            range_text = (
                f"{range_obj.start_time}..{range_obj.end_time}"
                f"（{datetime.fromtimestamp(range_obj.start_time).strftime('%Y-%m-%d %H:%M:%S')}"  # noqa: DTZ006
                f" ~ {datetime.fromtimestamp(range_obj.end_time).strftime('%Y-%m-%d %H:%M:%S')}"  # noqa: DTZ006
                " 本地时区）"
            )

        lines.extend([
            f"  State={_raw(state)} Msg={_safe(msg)!r} Position={positions}",
            f"  Range={range_text}",
            f"  IsNeedPhoto={_raw(is_need_photo)} Remark={_safe(remark)!r}",
            f"  结论：{_verdict(positions, state, is_need_photo, remark)}",
        ])
        return device_ok, lines
    finally:
        # 与 attempt_signin 同口径：本次探测结束立即清零/解除凭据副本。
        client._wipe_credentials()


def _load(args):
    """装载账号；返回 `(accounts, error, code)`。

    code：0 = 正常；1 = 指定手机号未找到（账号异常族）；2 = 环境或凭据来源不可用。
    """
    try:
        accounts = load_accounts(migrate=False)
    except Exception as exc:  # 装载边界：任何来源故障都归环境族，类型留在消息里
        return [], f"账号装载失败（{type(exc).__name__}）", 2
    if args.phone:
        wanted = set(args.phone)
        matched = [a for a in accounts if a.phone in wanted]
        missing = wanted - {a.phone for a in matched}
        if missing:
            return matched, "指定手机号未找到：" + ", ".join(
                masking.mask_phone(p) for p in sorted(missing)
            ), 1
        accounts = matched
    if not accounts:
        return [], "未装载到任何账号（检查 YIBAN_DB_FILE / YIBAN_ACCOUNTS_JSON / .env）", 2
    return accounts[: args.limit], "", 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="签到窗口前只读预演：打印每个账号的 signPosition 判定输入。"
    )
    parser.add_argument("--limit", type=int, default=10,
                        help="最多探测的账号数（默认 10）")
    parser.add_argument("--phone", action="append", default=[],
                        help="只探测指定手机号（可重复；默认从库里按顺序取）")
    args = parser.parse_args(argv)
    if args.limit <= 0:
        print("--limit 必须大于 0", file=sys.stderr)
        return 2

    _configure_logging()
    accounts, load_error, load_code = _load(args)
    if load_code == 2:
        print(f"预演中止：{load_error}", file=sys.stderr)
        return 2

    print("=== 签到前只读预演（不做任何签到提交）===")
    print(f"计划探测账号数：{len(accounts)}")
    anomalies = 0
    for account in accounts:
        try:
            ok, lines = _probe(account)
        except Exception as exc:
            ok = False
            lines = [
                f"[{masking.mask_phone(account.phone)}] 探测异常：{type(exc).__name__}",
                f"  {_safe(exc)!r}",
            ]
        for line in lines:
            print(line)
        if not ok:
            anomalies += 1
    print(f"=== 完成：{len(accounts)} 个账号，异常 {anomalies} 个 ===")
    if load_error:  # 指定手机号未找到：账号异常族（环境已装载成功）
        print(load_error, file=sys.stderr)
        anomalies += 1
    return 1 if anomalies else 0


if __name__ == "__main__":
    sys.exit(main())
