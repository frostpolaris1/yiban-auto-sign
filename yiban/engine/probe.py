# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
探针与只读健康检查：非签到时段对全部账号做登录 + 拉任务（不提交签到）。

用途有二：注册/改密时即时验证账号（网页侧复用 `verify_account`），以及定时健康探测
提前发现"图形验证墙 / 校本化失效 / 密码错误"这类无法自愈的问题。探测结果落
`sign_events`（stage=probe），硬失败并入当日管理员汇总邮件，同时按用户开关发个人预警
（措辞用"健康探测"，避免被误读成"当日签到失败"）。

探测本身是**一次真实登录**（与签到同一风控暴露面），因此：一键暂停/周末关闭期间不跑、
与签到进程互斥（入口处持运行锁）、`last_run` **先记账再探测**（双探针并发只放行一个）。

**归属**
`yiban.engine` 的只读健康检查层；`runner --probe` 与网页注册/改密验证都走它。

**复用**
`verify_account`（注册/改密即时验证，网页侧复用）、探针模式判定与结果落库函数；
账号复核 `account_still_signable` 取自 `yiban.store.accounts`。

**通信**
输入：账号列表、探针配置（`YIBAN_PROBE_ENABLE` / `YIBAN_PROBE_TIME` 等，经 .env 传入）、
本进程执行体身份（`YIBAN_EXECUTOR_ID`，缺省 = 单执行体）。
输出：`sign_events`（stage=probe）、管理员汇总（并入 A 线）与用户预警；退出码口径与
`runner` 一致。
调用谁：`client`（真实登录）、`security`（硬失败词元单一来源）、`alerts`、`state_io`、
`cli_support`、`env_io`（once 自动关闭写 `.env`）、`db`、`egress`（按身份解析出口）、
`token_bucket`（出口桶 / 全局 Λ / 每账号 gap 三道闸）。
谁调用：`runner`（`--probe`）、web 注册/改密路径（`web/services/accounts_data.py`）。

**限速（工单 ba-p04-08）**：探测是真实登录，与签到同一风控暴露面，故探针主循环与执行体
走**同一套**三道闸（出口桶 → 全局 Λ → 每账号 gap），且登录量记进**同一个**持久化出口桶
（`egress_state`，桶键 = 本进程执行体身份）。出口也按该身份解析——不再恒取 `ROLE_SINGLE`，
那在清单/多执行体形态下既不是任何 worker 槽位的出口也不是兜底行的出口。等待用阻塞
`sleep`，但**总等待有界**：到点停止本轮剩余账号并留痕，绝不放开限速。
前端调用点：注册与改密表单（`web/static/js/components/account-form.js`、
`web/static/js/pages/my_account.js`）走 `/api/accounts`、`/api/my-accounts` 经本模块做即时验证；
健康探测结果经 `/api/admin/sign-events` 进入仪表盘——验证口径变化会改变注册/改密的
打回提示。
跨模块一律走模块属性访问。
"""
import contextlib
import json
import logging
import os
import re
import time
from datetime import datetime

from yiban import client as yiban_client
from yiban import clock, egress, security  # egress：与执行体同一套出口模型（M35）
from yiban.engine import alerts, cli_support, schedule, state_io, token_bucket
from yiban.infra import env_io
from yiban.masking import mask_phone as _mask_phone
from yiban.masking import mask_url_userinfo as _mask_url_userinfo
from yiban.masking import sanitize_text as _sanitize_text
from yiban.masking import sanitize_url as _sanitize_url
from yiban.store import accounts as accounts_store
from yiban.store import db

logger = logging.getLogger("yiban")

# 客户端外观与运行期账号复核：与兼容壳同名的两个别名（同一对象）
YibanClient = yiban_client.YibanClient
account_still_signable = accounts_store.account_still_signable

# ---- 探针模式 / 注册时账号验证（2026-08-25）----
# 非签到时段对全部账号做只读健康检查（登录+拉任务，不提交签到），提前发现
# 「图形验证墙 / 校本化失效 / 密码错误」等无法自愈问题；注册提交账号时亦可即时验证打回。
# 配置经 web 系统设置写入 .env（YIBAN_PROBE_ENABLE / YIBAN_PROBE_TIME / YIBAN_PROBE_INTERVAL_DAYS /
# YIBAN_ACCOUNT_VERIFY），run.sh / run_probe.sh 加载后经环境变量传入。
# 真值口径单源在 `yiban.infra.env_io.parse_env_flag`（1/true/on/yes，大小写与两侧空白不敏感），
# 与面板 read 侧、run_probe.sh 同口径。
PROBE_ENABLE = env_io.parse_env_flag(os.environ.get("YIBAN_PROBE_ENABLE", ""),
                                     default=False, key="YIBAN_PROBE_ENABLE", log=logger)
PROBE_TIME = os.environ.get("YIBAN_PROBE_TIME", "20:00").strip() or "20:00"
# 触发频率：正整数=每 N 天；once=下一次计划时间单次执行（执行后自动关闭）
PROBE_INTERVAL = os.environ.get("YIBAN_PROBE_INTERVAL_DAYS", "1").strip() or "1"

#: 探针限速的**总等待预算**（秒）：主循环从开始等令牌起计时，越过即停止本轮剩余账号并
#: 留痕。探针有 cadence（每晚/每若干天一轮），故"这轮少探一些"是安全的失败方向；反之
#: 把限速放掉去探完全量，正是本项目红线要防的批量登录。缺省 1 小时，远大于目标规模
#: （1~2 千账号）在出厂速率 1 attempt/s 下的用时（≤ 约 33 分钟），只在速率被压到很低而
#: 账号数又很大时才触发。
PROBE_EGRESS_MAX_SEC = 3600.0

# 探针视为"无法自愈、需预警"的错误特征（复用错误分类思路；网络/Token 等可自愈失败不预警）。
# WAF/风控/挑战解析/非 JSON/假成功家族**不得手抄**：词元来自 `yiban.security.hard_fail_pattern()`
# （与重试档位同一真值源）——此前手抄的词表不含解析失败与非 JSON 文案，探针对该族零预警。
PROBE_HARD_FAIL_RE = re.compile(
    r"图形验证|图片验证|滑块验证|人机验证|captcha"
    r"|校本化|未授权|授权失效|Auth Error|Get Night Attendance Sign Tasks Error"
    r"|登录失败|密码错误|账号或密码"
    r"|授权设备|获取登录入口失败|登录响应异常|最终认证失败"
    r"|(?:" + security.hard_fail_pattern() + r")"
)


def _egress_env():
    """取出口用的环境映射：进程环境打底，`.env` **只补缺**（不覆盖进程环境）。

    为什么两个源都要（处境同 `env_io.resolve_path`，但优先级相反）：执行体由监督进程
    拉起，出口是**按进程注入**的（`yiban/engine/workers.py` 把该槽位的出口写进子进程
    的 `YIBAN_PROXY`）——进程环境必须压过 `.env`，否则每槽位的出口会被 `.env` 里那一个
    值抹平；而 **web 进程不是被 `run.sh` 带着 export 起来的**，进程环境里往往没有这个
    键，只读 `os.environ` 就等于"这条出网不受 `YIBAN_PROXY` 管控"。`.env` 补缺正好
    补上后一种情形。

    走既有读取（`env_io.parse_env_file` / `env_io.env_path`），不新造一套解析。
    """
    try:
        # 注意合并方向：先铺 `.env`、**再让进程环境覆盖它**。反过来写（先 environ 后
        # update）就成了「`.env` 优先」，会把监督进程按槽位注入的出口抹平成 `.env` 里
        # 那一个值——每槽位的出口管控当场失效。
        merged = dict(env_io.parse_env_file(env_io.env_path()))
    except OSError:
        merged = {}
    merged.update(os.environ)
    return merged


def _mono():
    """单调浮点秒：令牌桶的时钟域（与执行体 `_mono` 同口径，不随墙钟跳变漂移）。"""
    return time.monotonic()


def _sleep(sec):
    """阻塞等待：探针是同步路径（无事件循环），放行前的等待直接睡。

    与执行体 `await asyncio.sleep` 同一语义——不自旋、不空烧 CPU。
    """
    time.sleep(sec)


def _executor_identity():
    """本进程的执行体身份串：与执行体同取法（`YIBAN_EXECUTOR_ID`，缺省单执行体）。

    清单/多执行体形态下，监督进程按槽位给子进程注入 `YIBAN_EXECUTOR_ID`
    （`worker-{i}@主机名`），探针子进程据此**与同一槽位的执行体**共用出口与出口桶键；
    单执行体形态（裸机 cron、进程内 `--probe`）无此键，落到 `single@主机名`，与同机
    单执行体的键一致。
    """
    return (os.environ.get("YIBAN_EXECUTOR_ID", "").strip() or egress.single_owner())


def _resolve_egress(identity, env=None):
    """按执行体身份解析出口：身份 →（角色，槽位）→ `egress.resolve`（与执行体同一套）。

    角色判据复用 `egress.parse_owner`（写入与解析同一份口径），出口取值复用
    `egress.resolve`。**不再恒取 `ROLE_SINGLE`**——那在清单/多执行体形态下既不是任何
    worker 槽位的出口也不是兜底行的出口，常等于宿主直连（工单 ba-p04-08）：探针因此跑在
    限速模型与 `egress_state` 记账之外的另一条出口上。
    """
    info = egress.parse_owner(identity)
    return egress.resolve(info["role"], info["index"] or 0, env=env)


def _probe_channels():
    """探针出口桶的突发额度通道数：与执行体同一算式（`schedule.channel_count`）。

    同一出口桶只有一行 `egress_state`（含 `burst`），两侧必须用同一算式算 `burst`，否则
    后落库的一方会把对方的突发额度顶掉。`limiter_from_env` 的缺省通道数只对出厂速率成立，
    故这里显式按执行体口径取 `M = min(16, ceil(rate × avg × 2))`。
    """
    cfg = schedule.planner_config()
    return schedule.channel_count(cfg["bucket_rate"], cfg["avg_attempt_sec"])


def _apply_egress_proxy(client):
    """把本进程身份对应的出口套到客户端 session 上（读侧出口管控，M35）。

    **为什么这里要再套一次**：`YibanClient.__init__` 只读 `os.environ["YIBAN_PROXY"]`。
    注册/添加账号的在线校验跑在 **web 进程**里，而该进程的环境通常没有这个键（出口是
    给执行体子进程注入的），于是这一次"服务器代用户向易班发起真实登录"就**直连出网**
    ——恰恰是风控暴露面最大、最该受 `YIBAN_PROXY` 管控的一条路径，却绕过了管控。

    **出口怎么取**：按**本进程执行体身份**解析（`_resolve_egress`）。web 进程无
    `YIBAN_EXECUTOR_ID` ⇒ 单执行体角色 ⇒ 读 `YIBAN_PROXY`（与改造前逐字一致）；探针在
    清单/多执行体形态下带着槽位身份 ⇒ 取**该槽位自己的出口**（不再恒取 `ROLE_SINGLE`）。
    两条路径共用同一份解析，不另造第二套。

    解析为空（未配代理）时**不动** session：此时客户端构造期也没配上，保持直连的既有
    行为，不把"没配"变成"显式清空"。

    日志只记 `egress.describe()`（`scheme://host[:port]`，去 userinfo），不落凭据。
    """
    try:
        proxy = _resolve_egress(_executor_identity(), env=_egress_env())
    except Exception as e:  # 出口解析不该把注册/改密的主流程带崩
        logger.debug("解析账号校验出口失败（按直连处理）: %s", e)
        return ""
    if not proxy:
        return ""
    client.session.proxies = {"http": proxy, "https": proxy}
    logger.debug("[%s] 账号校验走配置出口: %s", client.account.phone,
                 egress.describe(proxy))
    return proxy


def verify_account(account):
    """只读健康检查（登录 + 拉取任务，不提交签到）。

    供注册时预处理验证（web 端）与探针模式（--probe）复用。
    返回 (ok, message)：ok=False 表示存在无法自愈的问题；message 已脱敏。

    出口：登录前先按 `_apply_egress_proxy` 套上本进程身份对应的出口——本函数是**唯一**
    一条在 web 进程里代用户向易班发起的真实登录（探针轮则由执行体注入的出口兜着），
    两处必须同源，见该函数说明（M35）。
    """
    phone = account.phone
    if not account_still_signable(account):
        # 探针同样会完整登录（与签到同一风控暴露面）：账号已被删除/停用则不发起
        return False, "账号已被删除或停用"
    try:
        client = YibanClient(account)
        try:
            _apply_egress_proxy(client)
            if client.use_killyiban:
                client.login_killyiban()
            else:
                client.login()
            return client.verify()
        finally:
            client._wipe_credentials()
    except Exception as e:
        # 同 attempt_signin——异常消息统一过 _sanitize_url + _mask_url_userinfo 打码
        safe_err = _sanitize_text(_mask_url_userinfo(_sanitize_url(str(e))))
        logger.warning(f"[{phone}] 健康检查失败: {safe_err}", exc_info=False)
        return False, safe_err


def _probe_state_path():
    """探针最近执行日状态文件（由探针进程独占维护，避免频繁写 .env）。"""
    state_dir = env_io.resolve_path("YIBAN_STATE_DIR", "/var/log/yiban")
    return os.path.join(state_dir, "probe-state.json")


def _read_probe_state():
    try:
        # utf-8-sig：容错 Windows 手工编辑留下的 BOM（对齐 _load_cred_state，2026-08-27 审查修复）
        with open(_probe_state_path(), encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_probe_state(state):
    try:
        state_io._write_private_json(_probe_state_path(), state)
    except OSError:
        logger.warning("探针状态文件不可写（不影响本次探测）")


def _update_probe_state_run(today_str):
    """记录探针当日已执行（读-改-写整体持 M12 文件锁，防并发覆盖丢写入）。

    2026-08-27 对抗性审查修复：原实现裸读写，与其它状态文件口径不一致。
    """
    with cli_support._state_file_lock(_probe_state_path()):
        state = _read_probe_state()
        state["last_run"] = today_str
        _write_probe_state(state)


def _health_probe_due(now=None):
    """是否应在本次入口执行健康探针：开启 + 已达触发时间 + 满足频率（once=下一次单次）。"""
    now = now or clock.now()
    if not PROBE_ENABLE:
        return False
    try:
        hh, mm = (int(x) for x in PROBE_TIME.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return False
    except (TypeError, ValueError):
        return False
    if (now.hour, now.minute) < (hh, mm):
        return False
    state = _read_probe_state()
    last = state.get("last_run", "")
    today = now.strftime("%Y-%m-%d")
    if last == today:
        return False
    interval = PROBE_INTERVAL.strip().lower()
    if interval == "once":
        # 单次模式：开启且已到时间且今天未执行 → 本次执行（执行后自动关闭）
        return True
    try:
        n = int(interval)
        if n <= 0:
            return False
    except (TypeError, ValueError):
        return False
    if not last:
        return True
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d")
    except ValueError:
        return True
    return (now.date() - last_dt.date()).days >= n


def _env_update_probe(auto_disable=False):
    """探针执行后更新 .env：once 模式自动关闭 YIBAN_PROBE_ENABLE。

    仅在 once 单次执行后调用；失败只记日志，不影响本次探测结果。
    落盘走 `env_io.write_env_key`：跨进程写锁、同键旧行折叠、逐行校验与原子 0600
    替换单源在 `write_env_keys`。此前就地自写"宽 splitlines + 精确前缀滤行"的
    读-改-写：不认 `KEY = v` 带空格旧行（折不掉、留影子行），还会把注释里潜伏的
    换行族字符拆行实体化成新配置行。
    """
    if not auto_disable:
        return
    env_path = env_io.env_path()
    try:
        env_io.write_env_key(env_path, "YIBAN_PROBE_ENABLE", "0")
    except Exception as e:
        # 升级为 ERROR（2026-08-27 审查）：once 自动关闭失败会让"单次探针"事实变成
        # 每晚全量探测（反复真实登录扩大风控面 + 每日重复告警）；选了 once 的运维
        # 不会回来盯日志，必须醒目留痕提示手动关闭。
        logger.error(
            "探针 once 自动关闭失败，YIBAN_PROBE_ENABLE 仍为开启——单次探针将变成每日重复执行，请手动关闭: %s",
            _sanitize_text(str(e)),
        )


def _sleep_until_admit(wait, deadline):
    """睡 `min(wait, 剩余预算)`；剩余预算 <= 0 即回报"预算用尽"（True）。

    把单次等待夹到 deadline 之内：GCRA 的 `retry_after` 本就小，但夹一刀让**总时长**在
    任何配置下都有界，不让某一次等待把整轮探针拖成长尾。
    """
    budget = deadline - _mono()
    if budget <= 0:
        return True
    _sleep(min(max(0.0, float(wait)), budget))
    return False


def _wait_for_egress(limiter, global_limiter, gap_gate, egress_key, phone, deadline):
    """按执行体同一套三道闸取一次额度：出口桶 → 全局 Λ → 每账号 gap。

    与执行体 `_throttle` **同序、同判据**（便宜且易命中的排最前；三件全过才允许发起
    尝试）。差异两处：本函数是同步路径（`_sleep` 阻塞），且**总等待有 `deadline`**——
    越过即返回 False，调用方停止本轮剩余账号（探针有 cadence，宁可这轮少探，绝不放掉
    限速）。返回 True 表示三道闸全过。
    """
    while not limiter.acquire(egress_key, _mono()):
        if _sleep_until_admit(limiter.bucket(egress_key).retry_after(_mono()), deadline):
            return False
    while not global_limiter.acquire(_mono()):
        # 能进这条说明 Λ 非 None（None 时 acquire 恒放行）
        if _sleep_until_admit(1.0 / global_limiter.lam, deadline):
            return False
    while not gap_gate.allow(phone, _mono()):
        if _sleep_until_admit(gap_gate.gap_sec, deadline):
            return False
    return True


def run_probe(accounts):
    """探针模式主流程：对全部账号做只读健康检查。

    - 未到触发时间/频率（或未开启）则直接返回（零请求）→ 返回 `None`，表示**这一轮
      没做检查**（调用方据此返回"跳过"退出码 2，而不是 0）。
    - 结果写入 sign_events（stage=probe，复用 db 写锁 _conn_lock，天然并发安全），
      时间戳为当前时刻，追加在最近签到日志之后。
    - 无法自愈问题：管理员合并预警邮件（复用 A 线 _collect/_flush）+ 对应用户个人
      预警（复用 B 线 send_user_fail_mail，尊重用户开关）。
    - 每个账号探测前先过出口桶 / 全局 Λ / 每账号 gap 三道闸（与执行体同一套），桶键 =
      本进程执行体身份 ⇒ 登录量记进与执行体**同一个**持久化出口桶；等待用阻塞 sleep，
      但**总等待有 `PROBE_EGRESS_MAX_SEC` 预算**，到点停止本轮剩余账号并留痕（探针有
      cadence，少探一轮是安全方向；放开限速跑完全量是红线）。
    - 执行后更新 last_run；once 模式自动关闭探针（.env 写锁）。

    返回**未通过检查的账号数**（硬失败 + 网络类软失败；真跑且全绿为 0）或 `None`
    （跳过）。因限速预算未探的账号**不计入**返回数——它们既没通过也没失败，是"这轮没
    探到"，会由下一轮 cadence 顺延。退出码由调用方按它分族——外部监控原来看不出
    "探针跳过 / 撞锁 / 真跑失败"的区别（一律 0），这是把三种结局分开的可判据。
    """
    if not PROBE_ENABLE:
        # 探针关闭：完全静默退出（不产生任何日志、不落库、不写状态）
        return None
    if not _health_probe_due():
        # 周期轮询的常态路径（容器调度器每 600s / 宿主 cron */10 都会走到）：
        # "未到触发点"属预期行为而非异常，逐次 INFO 会刷屏（约 144 条/日）。
        # 降为 DEBUG——默认级别下日志只保留签到结果与探针实际执行结果；
        # 需排查轮询是否如期触发时，开 DEBUG 级别即可看到每次尝试轨迹。
        logger.debug("==== 探针模式：已开启，但未到触发时间/频率，本次跳过 ====")
        return None
    # last_run 占位前置——探测开始前先记账，双探针/调度重启并发时
    # 只放行一个（原实现探测结束后才写，两个探针都能通过 _health_probe_due 判定）
    _update_probe_state_run(clock.now().strftime("%Y-%m-%d"))
    logger.info(f"==== 探针模式：对 {len(accounts)} 个账号进行健康检查 ====")
    # 探针确认健康 → 清除熔断暂停（原实现探针与熔断互不相通，
    # 误冻账号即使每晚探针证明凭据可用也要熬到 7 天后半开试探）
    cred_state = state_io._load_cred_state()
    fuse_cleared = False
    now = clock.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    hard_fail = []  # [(Account, message)]
    healthy_n = 0
    soft_fail_n = 0
    unprobed_n = 0
    # ---- 出口限速（工单 ba-p04-08）----
    # 探测是真实登录，与签到同一风控暴露面。三道闸与出口都按**本进程执行体身份**取，
    # 桶键即该身份（`egress_state` 里那一行）——探针登录量因此记进与执行体**同一个**
    # 持久化出口桶，而不是另造第二套计数（跨进程共享限速的根因在此）。出口本身由
    # `verify_account` 内的 `_apply_egress_proxy` 按同一身份解析，两处同源。
    identity = _executor_identity()
    limiter = token_bucket.limiter_from_env(channels=_probe_channels())
    limiter.restore_from_store(identity, now=_mono())  # 装回该出口的速率/TAT：重启后不"重启即全速"
    global_limiter = token_bucket.GlobalLimiter(os.environ.get("YIBAN_GLOBAL_RATE", ""))
    gap_gate = token_bucket.gap_gate_from_env()
    deadline = _mono() + PROBE_EGRESS_MAX_SEC
    for acc in accounts:
        if not _wait_for_egress(limiter, global_limiter, gap_gate, identity, acc.phone,
                                deadline):
            # 到点停止本轮剩余账号：探针有 cadence，少探一轮是安全的；放开限速跑完全量
            # 才是红线。这里必须留痕，否则"探针没跑完"会被误读成"全员健康"。
            unprobed_n = len(accounts) - (healthy_n + len(hard_fail) + soft_fail_n)
            logger.warning(
                "探针限速等待已达本轮预算（%.0fs），停止本轮剩余 %d 个账号（未探完的账号"
                "按当日 cadence 顺延）；已探：健康 %d、硬失败 %d、软失败 %d",
                PROBE_EGRESS_MAX_SEC, unprobed_n, healthy_n, len(hard_fail), soft_fail_n,
            )
            break
        gap_gate.commit(acc.phone, _mono())  # 走到这即"真要发请求"：gap 推进点与尝试一一对应
        ok, message = verify_account(acc)
        hard = (not ok) and bool(PROBE_HARD_FAIL_RE.search(message or ""))
        # 落库：stage=probe（复用 db.add_sign_event，内部 _conn_lock 并发保护）。
        # 记账口径 = 探测结论：未通过（含网络类软失败）一律记 failed——原式
        # `"failed" if hard else "success"` 把软失败涂成 success，探针断网时台账上
        # 仍是"全员可用"（现网 255 行全 success、0 failed）。硬/软的区分由告警分支
        # 与 message 承载，探测行为与预警口径不变。
        try:
            db.add_sign_event(
                ts, acc.phone, "success" if ok else "failed",
                _sanitize_text(message), stage="probe",
            )
        except Exception as e:
            logger.debug("探针日志写入失败（不影响探测）: %s", e)
        if hard:
            hard_fail.append((acc, message))
        elif ok:
            healthy_n += 1
            cred_entry = cred_state.get(acc.phone)
            if isinstance(cred_entry, dict) and cred_entry.get("paused_since"):
                cred_state.pop(acc.phone, None)
                fuse_cleared = True
                logger.info(f"[{_mask_phone(acc.phone)}] ✅ 探针确认凭据健康，解除熔断暂停")
        else:
            # 网络类失败（超时/DNS 等）：不含硬失败特征、通常可自愈，不计入预警，
            # 但必须与「确认健康」区分留痕——否则探针自身故障会被误读为全员健康
            # （2026-08-27 审查修复 P2-8）
            soft_fail_n += 1
            logger.info(
                "探针：账号 %s 网络类失败（不计预警）：%s",
                _mask_phone(acc.phone), _sanitize_text(message),
            )
    # 桶状态落库：探针消费掉的额度写回**同一行** `egress_state`，执行体下一轮（或本轮
    # 重启）`restore_from_store` 即看见——这是"跨进程同一份限速"的落点。写失败由
    # `EgressLimiter.persist` 内部告警，不阻断探针（桶状态是记忆不是业务事实）。
    limiter.persist(identity)
    # 预警（复用 A/B 线邮件机制；用户邮件按「健康探测」措辞，避免误报为当日签到失败）
    if fuse_cleared:
        with contextlib.suppress(Exception):
            state_io._save_cred_state(cred_state, touched={a.phone for a in accounts})
    for acc, message in hard_fail:
        alerts._collect_admin_mail("健康探测预警", [
            ("账号", _mask_phone(acc.phone)),
            ("原因", _sanitize_text(message)),
        ])
        alerts.send_user_fail_mail(acc.owner, acc.phone, message, scenario="probe")
    if soft_fail_n and not hard_fail:
        # 无硬失败时单独提示，避免管理员把「零预警」误读为「全员可用」
        alerts._collect_admin_mail("健康探测提示", [
            ("网络类失败", f"{soft_fail_n} 个账号"),
            ("说明", "超时/连接异常等，通常可自愈，未计入预警"),
        ])
    alerts._flush_admin_mail_summary(phase="健康探测")
    # once 自动关闭（last_run 已在探测开始前置记录）
    if PROBE_INTERVAL.strip().lower() == "once":
        _env_update_probe(auto_disable=True)
        logger.info("==== 探针模式（单次）执行完成，已自动关闭探针 ====")
    logger.info(
        f"==== 探针模式完成：健康 {healthy_n}，网络类失败 {soft_fail_n}，"
        f"预警 {len(hard_fail)}，因限速预算未探 {unprobed_n} ===="
    )
    return len(hard_fail) + soft_fail_n
