# -*- coding: utf-8 -*-
"""易班客户端外观：凭据托管 / 会话缓存 / 代理 / 设备绑定 / 签到与探针的业务分支。

协议步骤（登录握手、签到两个接口的请求形状）在 `yiban/fyiban/protocol.py`
（第三方隔离层）；本模块负责把**本项目的关注点**组装上去：

| 关注点 | 归属 |
|--------|------|
| WAF 判定 / URL 白名单 / 脱敏诊断 | `yiban/security.py`（以 `ProtocolPolicy` 注入协议层） |
| 会话缓存（少登录 = 少风控暴露面） | 本模块 `_SessionCache`（协议层只通过 restore/save/clear 使用） |
| 凭据内存清零（C-SIGN-04） | `YibanClient._wipe_credentials`（由 `attempt_signin` 的 finally 调用） |
| 三态判定 / 窗口校验 / 多任务容错 | `YibanClient.signin` |

`scripts/signin.py` 以 `YibanClient` 之名转发本类（**同一对象**，不是第二份实现），
故既有调用点与测试（`patch.object(signin.YibanClient, ...)`）行为不变。
"""
import json
import logging
import os
import random
import secrets
from datetime import datetime

import requests
from requests.utils import cookiejar_from_dict, dict_from_cookiejar

from yiban import masking, security
from yiban import status as yiban_status
from yiban.fyiban import headers as fyiban_headers
from yiban.fyiban import protocol as fyiban_protocol
from yiban.fyiban import waf as fyiban_waf
from yiban.fyiban.algo import generate_position_in_polygon
from yiban.store.accounts import account_still_signable

logger = logging.getLogger("yiban.client")


class _SessionCache:
    """会话缓存的存取（协议层注入 `session_store` 的动作实现）。

    我们的登录是 OAuth 会话 Cookie 流程（非 access_token）：登录握手完成后认证态落在
    `session.cookies` + `csrf` 上。缓存序列化 cookie jar + csrf，下次签到先探针判活复用
    ——减少登录频率 = 降低风控触发面。仅 db 已初始化（数据库账号模式）时启用；
    CI 的环境变量账号模式不建缓存。
    """

    def __init__(self, client):
        self._client = client

    def restore(self):
        """命中则把缓存 cookies 装回会话并返回缓存的 CSRF 令牌；未命中返回 None。

        任何缓存读失败都按未命中处理，绝不阻断正常登录。会话是否仍有效由
        `login_killyiban` 第 1 步的 OAuth 探针判定。
        """
        from yiban.store import db
        client = self._client
        if not db.is_initialized():
            return None
        try:
            cached = db.get_session_cache(client.account.phone)
        except Exception as e:
            logger.debug(f"[{client.account.phone}] 读取会话缓存失败（按未命中处理）: "
                         f"{masking.sanitize_text(e)}")
            return None
        if not cached:
            return None
        try:
            cookies = json.loads(cached["cookies"])
        except (TypeError, ValueError):
            logger.warning(f"[{client.account.phone}] 会话缓存 cookies 非合法 JSON，已清除")
            self.clear()
            return None
        client.session.cookies = cookiejar_from_dict(cookies)
        return cached["csrf"]

    def save(self, session, csrf):
        """完整登录成功后保存 cookie jar + csrf（密文落库）；失败仅告警不影响签到。"""
        from yiban.store import db
        client = self._client
        if not db.is_initialized():
            return
        if not account_still_signable(client.account):
            # 账号在登录过程中被删除/停用：不落库。session_cache 的清理全按现存账号行
            # 的 phone 驱动，为已消失的账号写入会留下**永久孤儿**凭据缓存。
            logger.debug(f"[{client.account.phone}] 账号已删除/停用，不保存会话缓存")
            return
        cookies = dict_from_cookiejar(session.cookies)
        if not cookies:
            return  # 空会话无复用价值，不落库
        try:
            db.set_session_cache(client.account.phone, json.dumps(cookies), csrf)
        except Exception as e:
            logger.warning(f"[{client.account.phone}] 保存会话缓存失败（不影响签到）: "
                           f"{masking.sanitize_text(e)}")

    def clear(self):
        """清除本账号会话缓存（探针判死 / 风控类失败联动清除）。"""
        from yiban.store import db
        client = self._client
        if not db.is_initialized():
            return
        try:
            db.clear_session_cache(client.account.phone)
        except Exception as e:
            logger.debug(f"[{client.account.phone}] 清除会话缓存失败: "
                         f"{masking.sanitize_text(e)}")


class YibanClient:
    """易班客户端：封装登录与签到流程。"""

    #: 安全策略（WAF/白名单/脱敏）。**无状态**，故按类共享：用 `__new__` 构造的
    #: 轻量实例（测试与探针路径）也必须能直接用。若将来它需要按账号保存状态，
    #: 必须改为实例属性并在 `__init__` 中赋值。
    _policy = security.ProtocolPolicy()

    def __init__(self, account):
        self.account = account
        # C-SIGN-04：密码缓冲用可变 bytearray 持有（str 不可原位清零），
        # 单次签到尝试结束由 _wipe_credentials 原位清零（attempt_signin finally）
        self.password = bytearray(account.password.encode("UTF-8"))
        # 登录方式：默认 KillYiBan 同款流程（真实 App 特征，与同作者 FYIBAN 同源，实测绕过 e003）；
        # 旧流程（Auto-Test 继承的 iOS 伪造 UA）仅在 YIBAN_LEGACY_LOGIN=1 时启用（GitHub Actions 等场景备选）
        self.use_killyiban = os.environ.get("YIBAN_LEGACY_LOGIN", "") != "1"
        if self.use_killyiban:
            self.csrf = secrets.token_hex(16)  # SecureRandom 真随机
            logger.debug(
                f"[{account.phone}] 登录方式: 标准 App 特征（UA=Yiban/AppVersion="
                f"{fyiban_headers.YIBAN_APP_VERSION}/SecureRandom CSRF）"
            )
        else:
            self.csrf = secrets.token_hex(16)  # 使用安全随机数替代可预测的时间戳 md5
            logger.debug(f"[{account.phone}] 登录方式: 旧流程（iOS 伪造 UA，YIBAN_LEGACY_LOGIN=1）")
        self.session = requests.Session()
        self.session.headers = dict(
            fyiban_headers.KILLYIBAN_HEADERS if self.use_killyiban else fyiban_headers.HEADERS
        )
        # 代理配置：GitHub Actions 海外 IP 可能被易班 WAF 地域风控拦截
        proxy = os.environ.get("YIBAN_PROXY", "").strip()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
            # 日志只记录 scheme://host:port，绝不落 userinfo（账号密码）明文
            logger.debug(f"[{account.phone}] 已启用代理: {security.url_desc(proxy)}")
        else:
            logger.debug(
                f"[{account.phone}] 未配置代理，如遇 WAF 拦截可配置 YIBAN_PROXY"
            )
        # 设备信息：部分学校开启了"设备绑定"，签到时需校验设备型号和唯一识别码
        self.phone_model = account.phone_model
        self.phone_code = account.phone_code
        self.logged_in = False

    @property
    def _session_store(self):
        """会话缓存动作对象（延迟创建，使轻量实例也能直接走登录路径）。"""
        store = getattr(self, "_store", None)
        if store is None:
            store = self._store = _SessionCache(self)
        return store

    def _wipe_credentials(self):
        """凭据内存尽力清零（C-SIGN-04）：单次签到尝试结束（成败均然）由 attempt_signin 调用。

        - password 缓冲（bytearray）原位覆写 \\x00——唯一能保证失效的副本；
        - 解除 account/phone_model/phone_code 引用，缩短凭据可回收窗口。
        CPython 局限：不可变对象（str/bytes）无法原位清零，RSA 加密瞬态副本与
        Account.password 本体只能等 GC；core dump / swap 场景仍可能残留。彻底
        消除需全链路换可清零凭据容器（侵入 web/db 存储层，标注为已知限制）。
        """
        pwd = getattr(self, "password", None)
        if isinstance(pwd, bytearray):
            pwd[:] = b"\x00" * len(pwd)
        self.password = None
        self.phone_model = None
        self.phone_code = None
        self.account = None

    # ---- 登录 ----
    def login(self):
        """旧流程登录（YIBAN_LEGACY_LOGIN=1），成功置 `logged_in`，失败抛异常。"""
        outcome = fyiban_protocol.login_legacy(
            self.session, phone=self.account.phone, password=self.password,
            csrf=self.csrf, policy=self._policy,
        )
        self._after_login(outcome)

    def login_killyiban(self):
        """默认登录方式（KillYiBan 同款），成功置 `logged_in`，失败抛异常。"""
        outcome = fyiban_protocol.login_killyiban(
            self.session, phone=self.account.phone, password=self.password,
            csrf=self.csrf, policy=self._policy, session_store=self._session_store,
        )
        self._after_login(outcome)

    def _after_login(self, outcome):
        """登录成功后的统一收尾：会话里生效的 CSRF 才是后续请求要用的令牌。"""
        self.csrf = outcome.csrf
        self.logged_in = True

    # ---- 会话缓存（供既有调用方与测试直接使用；动作实现见 _SessionCache）----
    def _restore_session_cache(self):
        return bool(self._session_store.restore())

    def _save_session_cache(self):
        self._session_store.save(self.session, self.csrf)

    def _clear_session_cache(self):
        self._session_store.clear()

    # ---- 反爬挑战（实现见 yiban/fyiban/waf.py）----
    def _is_ydclearance_challenge(self, resp):
        return fyiban_waf.looks_like_challenge(
            resp.text, resp.headers.get("Set-Cookie", "")
        )

    def _solve_ydclearance(self, text):
        """纯 Python 解析易盾 WAF 挑战（实现与来源见 yiban/fyiban/waf.py）。

        白名单是**本项目的安全策略**，以参数注入解析器——第三方层不内联安全校验。
        """
        return fyiban_waf.solve_ydclearance(text, allow_url=security.is_fyiban_url)

    # ---- 签到 ----
    def signin(self):
        """执行签到，返回 (success: bool, message: str, skip: bool, status: str)。

        skip=True 表示当前不在签到时间窗口内，不需要重试。
        """
        if not self.logged_in:
            # 与 attempt_signin 保持一致：按配置选择登录流程（防止直接调 signin() 时走错）
            if self.use_killyiban:
                self.login_killyiban()
            else:
                self.login()

        # 1. 获取签到位置范围
        if not self.use_killyiban:
            # 登录链改过 Origin/Referer，签到前改回 App 域（形状见 fyiban/headers.py）
            self.session.headers.update(fyiban_headers.APP_SIGN_HEADERS)
        resp = fyiban_protocol.fetch_sign_position(self.session, self.csrf, self._policy)
        if resp.blocked:
            return False, security.WAF_BLOCKED_MESSAGE, False, yiban_status.STATUS_FAILED
        data = resp.data
        if data.get("code") != 0:
            return (False, f"获取签到任务失败: {masking.sanitize_text(data.get('msg'))}",
                    False, yiban_status.STATUS_FAILED)

        data_obj = data["data"]
        msg = data_obj.get("Msg", "")
        if "已签到" in msg:
            return True, "今日已签到（无需重复签到）", False, yiban_status.STATUS_ALREADY
        if "今日无需签到" in msg:
            return True, "今日无需签到（非签到日）", False, yiban_status.STATUS_NO_TASK

        position_list = data_obj.get("Position", [])
        if not position_list:
            # 2026-08-31 公测：登录成功、signPosition 返回 code=0 但 Position 为空。
            # 此前只报笼统一句"未找到签到位置数据"，Msg 原文被吞，管理员无从判断
            # 是"任务未配置点位"还是"当日任务已关闭"。落一条带 Msg 的日志供取证。
            # 2026-09-01：状态独立为 STATUS_NO_POSITION——非账号/凭据问题，不按失败
            # 告警、不触发补签重跑（NO_POSITION_MAX_ATTEMPTS=1，见 _retry_budget）。
            logger.warning(
                f"[{self.account.phone}] signPosition 无可用点位: "
                f"Msg={masking.sanitize_text(msg)!r} Range={'有' if data_obj.get('Range') else '无'}"
            )
            return (
                False,
                "未找到签到位置数据（易班未返回该账号的签到点位，非账号密码问题）",
                False,
                yiban_status.STATUS_NO_POSITION,
            )
        # 多任务 shuffle 改造（2026-08-29）后首个点位不再特殊：
        # 点位统一由下方遍历全部任务处理，此处不再取 position_list[0]。
        range_obj = data_obj.get("Range", {})

        # 2. 校验签到时间
        # 注意：这里比的是**易班服务端给的时间戳**（Range.StartTime/EndTime），
        # 取当前时间必须用"真时间戳"语义。`datetime.now().timestamp()` 与
        # `clock.now().timestamp()` 不等价——后者把北京时间当宿主本地时间再转戳，
        # 在 UTC 宿主上会差 8 小时。故此处刻意不走业务钟（与其他取时点不同）。
        now_ts = int(datetime.now().timestamp())
        start_ts = int(range_obj.get("StartTime", 0))
        end_ts = int(range_obj.get("EndTime", 0))
        if not start_ts or not end_ts:
            # 签到时间窗口缺失（Range 为空），视为 skip，不直接提交
            return False, "签到时间窗口缺失（无 Range），已跳过", True, \
                yiban_status.STATUS_SKIPPED_NORANGE
        if not (start_ts <= now_ts <= end_ts):
            # 不在签到时间窗口内，标记为 skip（不需要重试）
            return (
                False,
                f"未在签到时间内（{datetime.fromtimestamp(start_ts)} ~ "
                f"{datetime.fromtimestamp(end_ts)}）",
                True,
                yiban_status.STATUS_SKIPPED_WINDOW,
            )

        # 3. 解析多边形点（逐点容错：单个坏点跳过，不拖垮整个签到）
        # 修复了「只签 position_list[0]」导致的漏签，改为遍历全部任务。
        # 2026-08-29 用户裁决：多任务通常为「同一打卡的多个点位，任取其一即可」——
        # 先随机打乱任务顺序（避免固定只签第一个点位，贴近学生真实行为、降低固定
        # 点位指纹），然后任一任务成功即停（下方 break），不再重复提交。
        random.shuffle(position_list)
        results_tasks = []  # [(task_name, ok, err_msg)]
        for position in position_list:
            task_name = str(position.get("Name", "") or f"任务{len(results_tasks) + 1}")
            polygon = []
            for p in position.get("Points", []):
                try:
                    parts = str(p).split(",")
                    if len(parts) >= 2:
                        polygon.append((float(parts[0]), float(parts[1])))
                except (TypeError, ValueError):
                    continue

            if not polygon:
                results_tasks.append((task_name, False, "签到范围点解析失败"))
                continue

            # 4. 在多边形内生成随机点
            lng, lat = generate_position_in_polygon(polygon)
            logger.info(
                f"[{self.account.phone}] 生成定位: ({lng},{lat}) "
                f"地址: {masking.sanitize_text(position.get('Address', ''))}"
            )

            # 5. 构建签到数据并提交
            sign_info = fyiban_protocol.build_sign_info(lng, lat, position.get("Address", ""))
            if not self.phone_model or not self.phone_code:
                logger.warning(
                    f"[{self.account.phone}] 未配置设备信息（YIBAN_PHONE_MODEL/YIBAN_PHONE_CODE），"
                    "如学校开启了设备绑定，签到将失败"
                )
            # KillYiBan 用 MINI_VERSION="1"，原脚本用 "1.0"
            submitted = fyiban_protocol.submit_sign_in(
                self.session, self.csrf,
                phone_code=self.phone_code, phone_model=self.phone_model,
                sign_info=sign_info,
                out_state="1" if self.use_killyiban else "1.0",
                policy=self._policy,
            )
            if submitted.blocked:
                return False, security.WAF_BLOCKED_MESSAGE, False, yiban_status.STATUS_FAILED
            result = submitted.data
            if result.get("code") == 0 and result.get("data"):
                results_tasks.append((task_name, True, ""))
                # 2026-08-29 用户裁决：多任务「随机选点、任一成功即停」——命中任一
                # 任务即视为当日已签，停止提交后续任务（省请求、降风控）
                logger.info(
                    f"[{self.account.phone}] 签到成功，剩余 "
                    f"{len(position_list) - len(results_tasks)} 个任务不再重复提交"
                )
                break
            else:
                err_msg = masking.sanitize_text(result.get("msg", "未知错误"))
                if "授权设备" in err_msg:
                    err_msg += "（请配置 YIBAN_PHONE_MODEL 和 YIBAN_PHONE_CODE 环境变量）"
                results_tasks.append((task_name, False, f"签到失败: {err_msg}"))

        # 6. 汇总各任务结果（2026-08-29 语义：多任务「随机选点、任一成功即停」——
        # 命中任一任务即视为当日已签；仅全部失败才判失败，保持重试兜底）
        ok_tasks = [t for t in results_tasks if t[1]]
        fail_tasks = [t for t in results_tasks if not t[1]]
        if ok_tasks:
            if fail_tasks:
                # 前面任务失败、后续任务命中（随机序）——留痕失败原因，仍判成功
                detail = "; ".join(f"{n}: {m}" for n, _ok, m in fail_tasks[:3])
                logger.warning(
                    f"[{self.account.phone}] 已成功签到（前面 {len(fail_tasks)} 个任务失败后命中）: {detail}"
                )
                return True, f"签到成功（{len(fail_tasks)} 个任务失败后命中）", False, \
                    yiban_status.STATUS_SUCCESS
            return True, "签到成功", False, yiban_status.STATUS_SUCCESS
        parts = "; ".join(f"{n}: {m}" for n, _ok, m in fail_tasks[:3])
        return False, f"{len(fail_tasks)} 个任务均失败: {parts}", False, yiban_status.STATUS_FAILED

    def verify(self):
        """只读健康检查（登录后）：拉取签到位置，**不提交签到**。

        用于注册时预处理验证与探针模式。返回 (ok, message)：
        - ok=True：账号可正常签到（能登录且能拉到任务，含校本化授权正常）
        - ok=False：存在无法自愈的问题（登录失败/校本化失效/图形验证/WAF 等，
          message 已脱敏，供用户可见提示或探针预警）
        """
        if not self.logged_in:
            if self.use_killyiban:
                self.login_killyiban()
            else:
                self.login()
        if not self.use_killyiban:
            # 登录链改过 Origin/Referer，签到前改回 App 域（形状见 fyiban/headers.py）
            self.session.headers.update(fyiban_headers.APP_SIGN_HEADERS)
        resp = fyiban_protocol.fetch_sign_position(self.session, self.csrf, self._policy)
        if resp.blocked:
            return False, security.WAF_BLOCKED_MESSAGE
        data = resp.data
        if data.get("code") != 0:
            return False, f"获取签到任务失败: {masking.sanitize_text(data.get('msg'))}"
        return True, "账号健康，可正常签到"
