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
输入：账号列表、探针配置（`YIBAN_PROBE_ENABLE` / `YIBAN_PROBE_TIME` 等，经 .env 传入）。
输出：`sign_events`（stage=probe）、管理员汇总（并入 A 线）与用户预警；退出码口径与
`runner` 一致。
调用谁：`client`（真实登录）、`security`（硬失败词元单一来源）、`alerts`、`state_io`、
`cli_support`、`env_io`（once 自动关闭写 `.env`）、`db`。
谁调用：`runner`（`--probe`）、web 注册/改密路径（`web/services/accounts_data.py`）。
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
from datetime import datetime

from yiban import client as yiban_client
from yiban import clock, egress, security  # egress：与执行体同一套出口模型（M35）
from yiban.engine import alerts, cli_support, state_io
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
PROBE_ENABLE = os.environ.get("YIBAN_PROBE_ENABLE", "").strip().lower() in ("1", "true", "on", "yes")
PROBE_TIME = os.environ.get("YIBAN_PROBE_TIME", "20:00").strip() or "20:00"
# 触发频率：正整数=每 N 天；once=下一次计划时间单次执行（执行后自动关闭）
PROBE_INTERVAL = os.environ.get("YIBAN_PROBE_INTERVAL_DAYS", "1").strip() or "1"

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


def _apply_egress_proxy(client):
    """把 `YIBAN_PROXY` 解析出的出口套到客户端 session 上（读侧出口管控，M35）。

    **为什么这里要再套一次**：`YibanClient.__init__` 只读 `os.environ["YIBAN_PROXY"]`。
    注册/添加账号的在线校验跑在 **web 进程**里，而该进程的环境通常没有这个键（出口是
    给执行体子进程注入的），于是这一次"服务器代用户向易班发起真实登录"就**直连出网**
    ——恰恰是风控暴露面最大、最该受 `YIBAN_PROXY` 管控的一条路径，却绕过了管控。
    经 `egress.resolve(ROLE_SINGLE, ...)` 走与执行体**同一套**出口模型（`ROLE_SINGLE`
    即"读 `YIBAN_PROXY`"那一档），不另造第二套解析。

    解析为空（未配代理）时**不动** session：此时客户端构造期也没配上，保持直连的既有
    行为，不把"没配"变成"显式清空"。

    日志只记 `egress.describe()`（`scheme://host[:port]`，去 userinfo），不落凭据。
    """
    try:
        proxy = egress.resolve(egress.ROLE_SINGLE, env=_egress_env())
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

    出口：登录前先按 `_apply_egress_proxy` 套上 `YIBAN_PROXY`——本函数是**唯一**一条
    在 web 进程里代用户向易班发起的真实登录（探针轮则由执行体注入的出口兜着），
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
    env_path = os.environ.get("YIBAN_ENV_FILE", "").strip() or ".env"
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


def run_probe(accounts):
    """探针模式主流程：对全部账号做只读健康检查。

    - 未到触发时间/频率（或未开启）则直接返回（零请求）→ 返回 `None`，表示**这一轮
      没做检查**（调用方据此返回"跳过"退出码 2，而不是 0）。
    - 结果写入 sign_events（stage=probe，复用 db 写锁 _conn_lock，天然并发安全），
      时间戳为当前时刻，追加在最近签到日志之后。
    - 无法自愈问题：管理员合并预警邮件（复用 A 线 _collect/_flush）+ 对应用户个人
      预警（复用 B 线 send_user_fail_mail，尊重用户开关）。
    - 执行后更新 last_run；once 模式自动关闭探针（.env 写锁）。

    返回**未通过检查的账号数**（硬失败 + 网络类软失败；真跑且全绿为 0）或 `None`
    （跳过）。退出码由调用方按它分族——外部监控原来看不出"探针跳过 / 撞锁 / 真跑
    失败"的区别（一律 0），这是把三种结局分开的可判据。
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
    for acc in accounts:
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
        f"==== 探针模式完成：健康 {healthy_n}，网络类失败 {soft_fail_n}，预警 {len(hard_fail)} ===="
    )
    return len(hard_fail) + soft_fail_n
