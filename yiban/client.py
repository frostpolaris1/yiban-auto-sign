# -*- coding: utf-8 -*-
"""易班客户端外观：凭据托管 / 会话缓存 / 代理 / 设备绑定 / 签到与探针的业务分支。

协议步骤（登录握手、签到两个接口的请求形状）在 `yiban/platform.py`；
本模块负责把**本项目的关注点**组装上去：

| 关注点 | 归属 |
|--------|------|
| WAF 判定 / URL 白名单 / 脱敏诊断 | `yiban/security.py`（以 `ProtocolPolicy` 注入协议层） |
| 会话缓存（少登录 = 少风控暴露面） | 本模块 `_SessionCache`（协议层只通过 restore/save/clear 使用） |
| 凭据内存清零 | `YibanClient._wipe_credentials`（由 `attempt_signin` 的 finally 调用） |
| 三态判定（按 `signPosition` 的 `State` 枚举）/ 窗口校验 / 多任务容错 | 本模块 `YibanClient.signin` |

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

from yiban import platform as yiban_platform
from yiban import security
from yiban.adapters.persistence.accounts import account_still_signable, update_account
from yiban.adapters.protocol_yiban import ParseError, code_is_ok, parse_device_state
from yiban.core import masking
from yiban.core import status as yiban_status
from yiban.geo import generate_position_in_polygon

logger = logging.getLogger("yiban.client")

# ---------------------------------------------------------------------------
# signPosition 的 `State` 判定（工单 yiban-auto-sign-yng6）
# ---------------------------------------------------------------------------
#: 易班 `signPosition` 的 `State` 数值枚举（平台 stateEnum）→ 本项目状态码。
#: 枚举来源：`yiban-apk-reverse/TASK-A-nightattendance-web-api.md` 的 stateEnum，
#: 2026-10-08 经用户确认：
#:   0 可签到 / 1 未签到 / 2 无需签到 / 3 已签到 / 4 已更改 / 5 补签中
#: 逐值写死；值为 None = "尚无结论"，照常往下取点位并提交（0 与 1）。
#:
#: **判定只看 State，不看 `Msg`**：`Msg` 是平台的中文文案，改词、地区/校方配置差异都会
#: 让它静默漂移。2026-10-08 只读采集实拍：`State=4`、`Msg="已更改"`、`Position` 为空
#: ——旧判据是 `"已签到" in Msg`，不命中，于是这个**已签到**的账号落进"Position 为空"
#: 分支被记成 `no_position`（管理员以为平台没开任务）。后果二：`no_position` 不在
#: `CLAIM_DONE_STATUSES` ⇒ 不算了结 ⇒ 补签轮重跑它 ⇒ 同一天多一次真实登录。
#: 注意 `4`（已更改＝班委手动补签）与 `3`（已签到）同归 `already`，**必须落"了结"**。
SIGN_POSITION_STATE_STATUS = {
    0: None,                                  # 可签到：尚未签到，照常取点位并提交
    1: None,                                  # 未签到：同上
    2: yiban_status.STATUS_NO_TASK,           # 无需签到（非签到日）
    3: yiban_status.STATUS_ALREADY,           # 已签到
    4: yiban_status.STATUS_ALREADY,           # 已更改＝班委手动补签＝今日已签到
    5: yiban_status.STATUS_SUPPLEMENTING,     # 补签中：流程进行中，结果未定
}

#: `State=5` 的返回文案：把平台原值写进 message，排障时从状态文件也能看出是哪种结局。
_SUPPLEMENTING_MESSAGE = (
    "补签申请处理中（平台 State=5「补签中」，结果未定），本轮不再提交"
)


def _as_state_int(raw):
    """把 `State` 收敛为 int；收敛失败（缺失/空串/"None"/非数字）返回 None。

    上游数值字段可能发数字也可能发数字字符串（协议库 SPEC §3.1），故这里按同一容忍度
    收敛。不复用 `yiban.adapters.protocol_yiban.position` 的收敛：那要经 `parse_sign_position`，而它对
    候选点是**严格**的（缺必填键即抛 ParseError），会把"点位形状不标准"变成"State 读不到"
    （同一条理由见 `yiban/platform._validate_sign_position`）。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return None
    return None


def _judge_by_state(state):
    """把**已结论**的 `State` 翻成 `signin()` 的返回值；"尚无结论"（0/1）返回 None。

    `skip=True` 表示"不需要重试"（与窗口外跳过同一语义）。**只有"未定"态才用它**：
    - `State=3/4`（已签到）与 `State=2`（无需签到）：`skip=False`——它们本来就进
      `CLAIM_DONE_STATUSES`，执行体先按"了结"短路，`skip` 在那条路径上到不了；
    - `State=5`（补签中）：**结果未定**，故 `skip=True`。平台正在处理补签，我们再登录
      一次改不了结果，只会多一次真实登录，故本轮不提交、不重试，只记状态。领取池侧另按
      保守档收尾：`supplementing` 不在 `yiban.status.RETRYABLE_GIVE_UP_STATUSES` ⇒ 写 `final:`
      前缀（默认档不自动回炉）。**注意 `final:` 不是"当日绝不回炉"**：补签轮把回炉口的
      `include_final` 开关置真（见 `queue_store.requeue_failed`），仍会把它翻回 `pending`
      再领一次；见 `yiban/core/status.py` 该状态码的说明与 `runner.main` 的补签闸门注释。
    """
    status = SIGN_POSITION_STATE_STATUS[state]
    if status is None:
        return None
    if status == yiban_status.STATUS_ALREADY:
        if state == 4:
            # 报文里没有可用的 Position，但平台已判今日已签到：文案写清来历，
            # 管理员看日志/状态文件即可分辨"平台已签到"与"我们签成功"。
            return (True, "今日已签到（平台 State=4「已更改」＝班委手动补签）",
                    False, status)
        return True, "今日已签到（无需重复签到）", False, status
    if status == yiban_status.STATUS_NO_TASK:
        return True, "今日无需签到（非签到日）", False, status
    return False, _SUPPLEMENTING_MESSAGE, True, status


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
        from yiban.adapters.persistence import db
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
        from yiban.adapters.persistence import db
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
        from yiban.adapters.persistence import db
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
        # 密码缓冲用可变 bytearray 持有（str 不可原位清零），单次签到尝试结束由
        # _wipe_credentials 原位清零（attempt_signin 的 finally）
        self.password = bytearray(account.password.encode("UTF-8"))
        # 登录方式：默认"标准 App 特征"流程（真实客户端的请求头与参数取值，
        # 实测可绕过 e003）；旧流程（iOS 伪造 UA）仅在 YIBAN_LEGACY_LOGIN=1 时启用
        self.use_killyiban = os.environ.get("YIBAN_LEGACY_LOGIN", "") != "1"
        if self.use_killyiban:
            self.csrf = secrets.token_hex(16)  # SecureRandom 真随机
            logger.debug(
                f"[{account.phone}] 登录方式: 标准 App 特征（UA=Yiban/AppVersion="
                f"{yiban_platform.YIBAN_APP_VERSION}/SecureRandom CSRF）"
            )
        else:
            self.csrf = secrets.token_hex(16)  # 使用安全随机数替代可预测的时间戳 md5
            logger.debug(f"[{account.phone}] 登录方式: 旧流程（iOS 伪造 UA，YIBAN_LEGACY_LOGIN=1）")
        self.session = requests.Session()
        self.session.headers = dict(
            yiban_platform.KILLYIBAN_HEADERS if self.use_killyiban else yiban_platform.HEADERS
        )
        # 协议核验诊断（默认关闭）：YIBAN_WIRE_DUMP 指向可写目录时挂载线路落盘
        from yiban.infra import wire_dump
        wire_dump.maybe_mount(self.session, account.phone)
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
        # 设备信息：部分学校开启了"设备绑定"，签到时需校验设备型号和唯一识别码。
        # 账号本地配置为空时，签到前会**读回**服务端登记的设备（`_device_identity`）。
        self.phone_model = account.phone_model
        self.phone_code = account.phone_code
        self._registered_device = None      # 服务端登记设备（DeviceState）；None=未读或读失败
        self._registered_device_read = False
        self._registered_device_persisted = False  # 登记值是否已写进账号行（供按需补全端点如实回报）
        self._registered_device_problem = ""  # 设备缺失的原因分类（供拒签文案；不含异常原文）
        self._registered_device_blocked = False  # 设备登记读回被 WAF 拦截（走既有 WAF 文案）
        self.logged_in = False

    @property
    def _session_store(self):
        """会话缓存动作对象（延迟创建，使轻量实例也能直接走登录路径）。"""
        store = getattr(self, "_store", None)
        if store is None:
            store = self._store = _SessionCache(self)
        return store

    def _device_identity(self):
        """本次签到回传的设备身份 `(code, model)`。

        优先级：**账号本地配置 > 服务端登记（读回）**。本地已配置齐全时不读服务端
        （显式配置优先，也省一次请求）；单边配置时用登记值补另一边。
        """
        if self.phone_code and self.phone_model:
            return self.phone_code, self.phone_model
        # getattr 兜底：用 `__new__` 构造的轻量实例（测试/探针路径）没有 __init__ 的字段，
        # 与 `_session_store` 同一惯例——轻量实例也要能直接走这条路径。
        if not getattr(self, "_registered_device_read", False):
            self._registered_device_read = True
            self._registered_device = self._read_registered_device()
        state = getattr(self, "_registered_device", None)
        code = self.phone_code or (state.code if state else "")
        model = self.phone_model or (state.phone_model if state else "")
        return code, model

    def _device_missing_message(self):
        """设备识别码缺失时的拒签说明（`signin()` 是唯一消费点）。

        平台 2026-10-16 起对无登记设备的提交一律回「请使用授权设备进行签到」。
        提交一次必然失败一次，还多留一条服务端失败计数，故本地直接拒签。
        文案只点名**可执行**的补救动作，且在允许来源之内（不伪造设备指纹）：
        用户在易班 App 内完成本机登记与授权；本站侧由签到前自动补全把登记值写进账号，
        故**不再**教用户手填——用户面已无这两个字段的编辑入口（2026-10-16 裁决）。
        """
        cause = getattr(self, "_registered_device_problem", "") or "该账号未登记设备"
        return (
            f"本账号没有可用的设备识别码（{cause}），已拒绝提交——平台对无登记设备的"
            "签到一律回「请使用授权设备进行签到」。补救：请在易班 App 的「设备识别」"
            "页完成本机登记与授权（登记后本程序会在下次签到前自动补全，不必手工填写）。"
        )

    def _read_registered_device(self):
        """读服务端登记的设备。读不到返回 `None`，并记下**原因分类**供拒签文案用。

        设备读取**不是**诊断性前置：平台 10-16 起把设备绑定作为签到前置，读不到
        就没有可回传的 `Code`，提交必然被拒（见 `_device_missing_message`）。
        故"读不到"一律返回 `None`，由 `signin()` 一处裁决是否拒签。
        记下的只是**分类名**（如 `设备登记读取失败`），不含异常原文：异常消息内嵌
        完整 URL（可能带 CSRF）。

        两个例外**必须外溢**，不得降级成"设备缺失"：
        * 网络级异常只收 `requests.RequestException`（连接/超时/非 JSON 响应）；
          其余异常是**我们自己**的缺陷，吞掉它等于把缺陷伪装成"账号没设备"。
        * 信封 `code=999` 抛的 `RuntimeError` 表示**会话已失效**，吞掉它会把
          "清缓存后真重登"那条既有处置路径换成本地拒签——失败分类因此走错档。

        成功路径把落库结果记进 `self._registered_device_persisted`（见
        `_persist_registered_device` 的返回值），供按需补全端点如实回报"已保存/未保存"。
        """
        try:
            resp = yiban_platform.fetch_device_state(self.session, self.csrf, self._policy)
        except requests.RequestException as exc:  # 网络级：不在此处抛出，交 signin() 统一拒签
            self._registered_device_problem = "设备登记读取失败"
            logger.debug(
                f"[{self.account.phone}] 设备登记读回失败：{type(exc).__name__}"
            )
            return None
        if resp.blocked:
            # WAF 拦截是环境问题，不是"账号没设备"——文案与档位都走既有单一真源
            self._registered_device_blocked = True
            self._registered_device_problem = "设备登记接口被风控拦截"
            return None
        if not isinstance(resp.data, dict) or not code_is_ok(resp.data.get("code")):
            self._registered_device_problem = "设备登记接口未正常应答"
            return None
        try:
            state = parse_device_state(resp.data.get("data"))
        except ParseError as exc:
            self._registered_device_problem = f"设备登记响应不合契约（字段 {exc.field}）"
            return None
        if not state.code:
            self._registered_device_problem = "该账号未登记设备"
        if state.code and not state.authorized:
            # 已登记但未授权：上游可能据此拒签，点名**真正的**补救动作。
            logger.warning(
                f"[{self.account.phone}] 账号设备未授权（登记机型 "
                f"{masking.sanitize_text(state.phone_model) or '未知'}）——请在易班 App 的"
                "「设备识别」页完成本机授权，否则可能被上游拒签"
            )
        self._registered_device_persisted = self._persist_registered_device(state)
        return state

    def _persist_registered_device(self, state):
        """把读回的设备登记值落到本地账号行——**注册一次、长期复用**。返回是否**已落库**。

        落库后各轮签到直接走账号本地配置（`_device_identity` 的第一个分支），
        不再发 `getState` 请求。若只是"每轮都读回"，等于把一次登记变成每次上线，
        既多一次请求，又让设备身份随上游变动而漂移。

        账号行 id 为 0（非库来源：JSON/环境变量）时无处可落，跳过；落库失败
        **fail-open**：拿不到持久化不阻断签到，本轮仍用读回值。

        返回值只报"这一对值是否已写进账号行"：`True`=写入成功（含 `update_account`
        的无变化更新）；`False`=跳过（缺一半/无库行）或写失败。按需补全端点据此如实
        回"已保存/未取到"，故返回值**不得**与"读到过值"混为一谈。
        """
        if not state.code or not state.phone_model:
            return False
        account_id = getattr(self.account, "account_id", 0)
        if not account_id:
            return False
        try:
            update_account(account_id, {
                "phone_model": state.phone_model,
                "phone_code": state.code,
            })
        except Exception as exc:  # 诊断性持久化：失败不影响本轮签到
            logger.debug(
                f"[{self.account.phone}] 设备登记落库失败（fail-open，本轮仍用读回值）："
                f"{type(exc).__name__}"
            )
            return False
        # 内存副本同步：同进程后续轮次（重试/多任务）不再重复读
        self.phone_model = state.phone_model
        self.phone_code = state.code
        logger.info(f"[{self.account.phone}] 设备登记已落到本地账号，后续轮次直接复用")
        return True

    def _wipe_credentials(self):
        """凭据内存尽力清零：单次签到尝试结束（成败均然）由 attempt_signin 调用。

        - password 缓冲（bytearray）原位覆写 \\x00——唯一能保证失效的副本；
        - 解除 account/phone_model/phone_code 引用，缩短凭据可回收窗口。
        CPython 局限：不可变对象（str/bytes）无法原位清零，RSA 加密瞬态副本与
        Account.password 本体只能等 GC；core dump / swap 场景仍可能残留。彻底
        消除需全链路换可清零凭据容器（侵入 web/db 存储层，属已知限制）。
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
        outcome = yiban_platform.login_legacy(
            self.session, phone=self.account.phone, password=self.password,
            csrf=self.csrf, policy=self._policy,
        )
        self._after_login(outcome)

    def login_killyiban(self):
        """默认登录方式（KillYiBan 同款），成功置 `logged_in`，失败抛异常。"""
        outcome = yiban_platform.login_killyiban(
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
            # 登录链改过 Origin/Referer，签到前改回 App 域（形状见 yiban/platform.py）
            self.session.headers.update(yiban_platform.APP_SIGN_HEADERS)
        resp = yiban_platform.fetch_sign_position(self.session, self.csrf, self._policy)
        if resp.blocked:
            return False, security.WAF_BLOCKED_MESSAGE, False, yiban_status.STATUS_FAILED
        data = resp.data
        if not isinstance(data, dict):
            # 降级分支（`_parse_api_body` 的 `resp.json()`）可以合法返回 None / list /
            # 标量：此时没有信封，更没有任何签发回执。抛含 HARD_FAIL 词元的 RuntimeError
            # ——与登录链 `envelopes.py` 的"无签发方回执"同形，落硬失败档（1 次尝试、
            # 清缓存），不要在通用档打满 3 次真实登录（工单 ba-p08-07）。
            raise RuntimeError("最终认证失败: 无签发方回执（签到接口响应不是对象）")
        if not code_is_ok(data.get("code")):
            return (False, f"获取签到任务失败: {masking.sanitize_text(data.get('msg'))}",
                    False, yiban_status.STATUS_FAILED)

        data_obj = data.get("data")
        if not isinstance(data_obj, dict):
            # 同一条守卫的另一半：`code` 成功但载荷缺失/非对象 = 伪造成功或无回执。
            raise RuntimeError("最终认证失败: 无签发方回执（signPosition 响应缺 data 对象）")
        msg = data_obj.get("Msg", "")
        # 判据**只看结构化的 State**；`Msg` 只作日志原文（排障用），不再作判据——
        # 中文文本判据会随平台文案改词而静默改变判定（见 SIGN_POSITION_STATE_STATUS）。
        state = _as_state_int(data_obj.get("State"))
        if state in SIGN_POSITION_STATE_STATUS:
            verdict = _judge_by_state(state)
            if verdict is not None:
                return verdict
        else:
            # 未知/缺失 State：**不静默**。把 State 与 Msg 原文一起落 WARN，再走保守回退
            # （继续按 Position 判：空点位 → no_position，有点位 → 照常提交）。
            # 为什么不退回旧的文本判据：两套判据并存时，平台改一个词就会让台账按另一套
            # 记数，而那正是本工单的缺陷形状。
            logger.warning(
                f"[{self.account.phone}] signPosition 的 State 无对应判定，按 Position 保守回退: "
                f"State={masking.sanitize_text(str(data_obj.get('State')))!r} "
                f"Msg={masking.sanitize_text(msg)!r}"
            )

        position_list = data_obj.get("Position", [])
        if not position_list:
            # 走到这里说明 State 判不出结论：`State=0/1`（尚未签到）而平台没给点位，
            # 或 State 缺失/未知。登录成功、signPosition 返回 code=0 但 Position 为空：
            # 必须把 Msg 原文落日志，管理员才能判断是"任务未配置点位"还是"当日任务已关闭"。
            # 状态独立为 STATUS_NO_POSITION——非账号/凭据问题，不按失败告警、不触发补签
            # 重跑（重试预算对 NO_POSITION 只给 1 次，见 engine 的 _retry_budget）。
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
        range_obj = data_obj.get("Range", {})

        # 2. 校验签到时间
        # 注意：这里比的是**易班服务端给的时间戳**（Range.StartTime/EndTime），
        # 取当前时间必须用"真时间戳"语义。`datetime.now().timestamp()` 与
        # `clock.now().timestamp()` 不等价——后者把北京时间当宿主本地时间再转戳，
        # 在 UTC 宿主上会差 8 小时。故此处刻意不走业务钟（与其他取时点不同）。
        now_ts = int(datetime.now().timestamp())  # noqa: DTZ005 真时间戳语义：与易班服务端 Range 时间戳同钟，刻意不走业务钟
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
                f"未在签到时间内（{datetime.fromtimestamp(start_ts)} ~ "  # noqa: DTZ006 真时间戳语义，仅用于提示文案
                f"{datetime.fromtimestamp(end_ts)}）",  # noqa: DTZ006 真时间戳语义，仅用于提示文案
                True,
                yiban_status.STATUS_SKIPPED_WINDOW,
            )

        # 3. 解析多边形点（逐点容错：单个坏点跳过，不拖垮整个签到）。
        # 多任务通常是「同一打卡的多个点位，任取其一即可」：先随机打乱任务顺序
        # （避免固定只签第一个点位形成行为指纹），然后任一任务成功即停（下方 break）。
        random.shuffle(position_list)

        # 设备身份（平台 2026-10-16 起对签到强制校验设备绑定）：
        # 本地未配置时**读回**服务端登记的设备码/机型，签到时照实回传。
        # 取不到设备码则**拒签**（不伪造设备指纹）：平台对无登记设备的提交一律回
        # 「请使用授权设备进行签到」，提交一次必然失败一次，还多留一条失败计数。
        device_code, device_model = self._device_identity()
        if not device_code:
            if getattr(self, "_registered_device_blocked", False):
                # 与 signPosition 被拦截同口径：WAF 是环境问题，文案与档位走既有单一真源
                logger.warning(
                    f"[{self.account.phone}] 设备登记读取被拦截：{security.WAF_BLOCKED_MESSAGE}"
                )
                return False, security.WAF_BLOCKED_MESSAGE, False, yiban_status.STATUS_FAILED
            message = self._device_missing_message()
            logger.warning(f"[{self.account.phone}] {message}")
            return False, message, False, yiban_status.STATUS_FAILED
        if not device_model:
            # 机型缺失不影响设备码这条硬前置，服务端自会裁决；留痕以便定位。
            logger.warning(
                f"[{self.account.phone}] 设备机型为空（账号未填写且服务端未登记）"
                "——签到时 PhoneModel 回传空串，学校若校验机型会被拒"
            )

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
            sign_info = yiban_platform.build_sign_info(lng, lat, position.get("Address", ""))
            # KillYiBan 的 MINI_VERSION 是 "1"，旧流程是 "1.0"（`out_state` 传参）
            submitted = yiban_platform.submit_sign_in(
                self.session, self.csrf,
                phone_code=device_code, phone_model=device_model,
                sign_info=sign_info,
                out_state="1" if self.use_killyiban else "1.0",
                policy=self._policy,
            )
            if submitted.blocked:
                return False, security.WAF_BLOCKED_MESSAGE, False, yiban_status.STATUS_FAILED
            result = submitted.data
            if code_is_ok(result.get("code")) and result.get("data"):
                results_tasks.append((task_name, True, ""))
                # 「随机选点、任一成功即停」：命中任一任务即视为当日已签，
                # 停止提交后续任务（省请求、降风控）
                logger.info(
                    f"[{self.account.phone}] 签到成功，剩余 "
                    f"{len(position_list) - len(results_tasks)} 个任务不再重复提交"
                )
                break
            else:
                err_msg = masking.sanitize_text(result.get("msg", "未知错误"))
                if "授权设备" in err_msg:
                    # 设备身份已改为"读回服务端登记并回传"，此时再教用户配环境变量是错的补救
                    err_msg += "（本账号设备未授权：请在易班 App 的「设备识别」页完成本机授权）"
                results_tasks.append((task_name, False, f"签到失败: {err_msg}"))

        # 6. 汇总各任务结果：命中任一任务即视为当日已签；仅全部失败才判失败
        # （保持重试兜底）
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
            # 登录链改过 Origin/Referer，签到前改回 App 域（形状见 yiban/platform.py）
            self.session.headers.update(yiban_platform.APP_SIGN_HEADERS)
        resp = yiban_platform.fetch_sign_position(self.session, self.csrf, self._policy)
        if resp.blocked:
            return False, security.WAF_BLOCKED_MESSAGE
        data = resp.data
        if not isinstance(data, dict) or not code_is_ok(data.get("code")):
            msg = data.get("msg") if isinstance(data, dict) else ""
            return False, f"获取签到任务失败: {masking.sanitize_text(msg)}"
        return True, "账号健康，可正常签到"
