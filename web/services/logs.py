# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""签到日志族与账号状态族：日志行可见性、按天日志读写解析、状态文件与熔断暂停。

**功能**
日志页、日志导出与账号卡「最近记录」共用的日志读取链路：行可见性判定
`_log_line_visible`、尾部倒读 `_tail_lines`（上限 `_LOG_TAIL_BYTES`）、按天文件路径
`log_path_for`、逐行解析 `parse_sign_log` / `_log_lines_for`、日期串形状校验
`_is_valid_date_str`、最近有日志的日期 `_today_has_logs` / `_most_recent_log_date`、
出站脱敏 `_mask_log_phones`；以及账号状态族的读取与熔断记录清理：按日状态文件
`load_sign_state`、账密故障暂停集合 `_cred_paused_phones`、凭据变更后清熔断
`clear_fuse_pause` / `clear_fuse_on_cred_change`。

**归属**
原 `web/app.py` 的模块级日志与状态辅助，唯一真源在本模块；`web/app.py` 只保留名字面与
转发，把它自己持有、而本模块需要的模块级名字——日志文件路径 `LOG_FILE`、状态目录
`STATE_DIR`、倒读实现 `_tail_lines`——在调用时刻现取后注入。

**复用**
`parse_sign_log` 与 `_log_lines_for` 共用同一条可见性规则（`_log_line_visible`）与同一份
行正则 `SIGN_LOG_RE`，避免两处各写一遍必然漂移；`_today_has_logs` / `_most_recent_log_date`
复用 `_log_lines_for` 的整读判定；`clear_fuse_on_cred_change` 复用 `clear_fuse_pause`。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。日志目录、状态目录与倒读实现都作为显式参数接收：它们在 `web.app` 上是会被测试
改写、也会随运行方式变化的模块级名字（既有测试直接赋值 `web.app.LOG_FILE` 来换日志目录，
又在 `web.app` 上打桩 `_tail_lines` 来替换解析输入），本模块另持一份绑定会让这些改写静默
失效。熔断状态文件的读写一律走 `yiban.cred_state` 的唯一入口，不自建第二套。
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta

from yiban import clock, cred_state
from yiban import status as yiban_status
from yiban.masking import mask_phone as _mask_phone
from yiban.masking import mask_phones_in_text as _mask_phones_in_text

# 与 web.app 同名的日志通道：熔断清理失败的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")


# ---------------------------------------------------------------------------
# 日志行可见性
# ---------------------------------------------------------------------------
def _log_line_visible(level, logger_name):
    """日志页 / 导出 / 账号卡「最近记录」显示哪些行。

    - `yiban` 及其**子模块**（`yiban.*`）：全部级别。签到链路的细节都在这些 logger 下
      （`yiban.fyiban.protocol` 的登录成功、`yiban.client` 的生成定位与签到成功、`yiban.engine.*`
      的逐账号判定），漏掉它们页面就只剩结果；DEBUG 也是部署自己开的级别，开了就该看得到。
    - 其它组件（werkzeug / mailer / notify 等）：仅 WARNING 以上——它们的 INFO 与签到无关
      （请求日志、发送成功），全量入列会把日志页灌满、把故障留痕冲走。
    """
    if logger_name == "yiban" or logger_name.startswith("yiban."):
        return True
    return level in ("WARNING", "ERROR", "CRITICAL")


# 日志格式（与 signin.py 相同）
# 行格式: [2026-08-07 06:40:04] [INFO] yiban: [手机号] ✅ 签到成功
# logger 名允许点分（`yiban.client` / `yiban.fyiban.protocol` …）：旧正则用 `(\w+)`，匹配不到
# 带点的名字，签到链路的**细节行**（登录成功 / 生成定位 / 签到成功）因此整行被丢弃，日志页只剩
# 汇总与结果。
SIGN_LOG_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}) [\d:]+\] \[(\w+)\] ([\w.]+): (.*)")

#: 日志倒读上限 2MB（约 2 万行）
_LOG_TAIL_BYTES = 2 * 1024 * 1024


def _tail_lines(path, max_bytes=_LOG_TAIL_BYTES):
    """从文件尾部读取最多 max_bytes 的完整文本行：大日志避免整读入内存。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # 丢弃首个不完整行
                raw = f.read()
            else:
                raw = f.read()
    except OSError:
        return []
    return raw.decode("utf-8", errors="replace").splitlines()


def parse_sign_log(path, tail_lines):
    """解析签到日志：返回最近日志行列表（可见性口径见 `_log_line_visible`）。

    只返回日志行，不返回"日志符号 → 图标"这类派生状态：账号状态的事实源是 sign-state
    文件（`load_sign_state`，`/api/accounts`），日志符号与前端状态码语义不符，透传会把
    前端图标/统计卡污染。`parse_sign_log` 与 `_log_lines_for` 共用同一条可见性规则，
    避免两处各写一遍必然漂移。

    倒读实现由调用方传入（`web.app` 的 `_tail_lines`）：它是本函数的既有打桩点，
    测试以它替换解析输入。
    """
    recent = []
    for line in tail_lines(path):
        m = SIGN_LOG_RE.match(line.strip())
        if not m:
            continue
        _date, level, logger_name, _msg = m.groups()
        if not _log_line_visible(level, logger_name):
            continue
        recent.append(line.strip())
    return recent


def _is_valid_date_str(s):
    """YYYY-MM-DD 格式且为真实日历日期（2026-13-99 这类非法值拒绝）。"""
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def log_path_for(log_file, date_str=None):
    """按天日志文件路径：{log_file 目录}/sign-YYYY-MM-DD.log（date_str 缺省=今天）。

    日志按天分文件：每天一个文件，按日期查看 = 直接读对应文件；
    run.sh / signin.py / 手动签到子进程均写入当天文件（保留 `LOG_FILE` 配置的目录）。

    `log_file` 由调用方传入（`web.app` 的 `LOG_FILE`）——它可被测试直接赋值改写，
    也会随 `--config` 变化。
    """
    date_str = date_str or clock.now().strftime("%Y-%m-%d")
    return os.path.join(os.path.dirname(log_file), f"sign-{date_str}.log")


def _log_lines_for(date_str, path_for, tail_lines):
    """读取指定日期日志的行（行首日期过滤防跨天残留；可见性口径见 `_log_line_visible`）。

    文件缺失/不可读返回空列表（历史日期无日志是正常状态，不报错）。
    路径与倒读实现由调用方传入，故日志目录切到别处的既有打桩面继续生效。
    """
    prefix = f"[{date_str} "
    out = []
    for line in tail_lines(path_for(date_str)):
        if not line.startswith(prefix):
            continue
        m = SIGN_LOG_RE.match(line.strip())
        if not m:
            continue
        _, level, logger_name, _msg = m.groups()
        if not _log_line_visible(level, logger_name):
            continue
        out.append(line.strip())
    return out


# 最近日志日期缓存 {date: "YYYY-MM-DD"}：轮询每 10s 调用，避免每次都扫描 30 天文件
_most_recent_log_cache = {"history_date": None, "checked_day": ""}


def _today_has_logs(path_for, tail_lines):
    """今天是否有 yiban 签到日志行（整读当天文件判定，不依赖文件尾部）。

    只扫文件尾部会误判：尾部一旦被其他 logger（如 web 每日清理循环的 yiban.db 告警）
    刷屏就会得出「今天无日志」而回退到历史日期。按天文件体积有限，整读开销可忽略；
    判定口径与 `_log_lines_for` 一致（logger=yiban 且非 DEBUG）。
    """
    return bool(_log_lines_for(clock.now().strftime("%Y-%m-%d"), path_for, tail_lines))


def _most_recent_log_date(max_days, path_for, tail_lines):
    """查找最近有日志的日期（从今天往前最多 max_days 天）。返回 YYYY-MM-DD。

    每次先检查今天（开销小，今天有新日志立即生效）；无日志时用历史缓存（每天只扫一次）。
    """
    today = clock.now().strftime("%Y-%m-%d")
    # 今天有日志 → 直接返回今天（并更新缓存）
    if _today_has_logs(path_for, tail_lines):
        _most_recent_log_cache["history_date"] = today
        return today
    # 今天无日志：跨天重置缓存，重新扫描历史
    if _most_recent_log_cache["checked_day"] != today:
        _most_recent_log_cache["checked_day"] = today
        _most_recent_log_cache["history_date"] = None
        for i in range(1, max_days + 1):
            d = (clock.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            # 整读判定（与 _today_has_logs 同口径）：尾部被其他 logger 刷屏时
            # 同样会漏判，统一用 _log_lines_for 保证正确性（按天文件体积有限）
            if _log_lines_for(d, path_for, tail_lines):
                _most_recent_log_cache["history_date"] = d
                break
    return _most_recent_log_cache["history_date"] or today


# ---------------------------------------------------------------------------
# 出站脱敏
# ---------------------------------------------------------------------------
def _mask_log_phones(line):
    """日志行内全部 11 位手机号脱敏（/api/logs 与 /api/my-logs 共用，防展示层漏出 PII）。

    直接复用输出面兜底同一实现 `mask_phones_in_text`：只认 `[11 位]` 方括号形态会漏过
    中文逗号分隔等其它位置的裸号，展示/导出层与落盘面必须是同一个号码口径。
    """
    return _mask_phones_in_text(line)


# ---------------------------------------------------------------------------
# 按日签到状态
# ---------------------------------------------------------------------------
def load_sign_state(state_dir, date_str=None):
    """读取按日结构化状态文件：{phone: {status, message, time, task}}。

    缺失/损坏/目录不存在时回退读旧格式按日文件（sign-daily，符号 → 状态码）：
    覆盖部署过渡期（sign-state 尚未生成）与历史日期查看场景。
    两者都无 → 返回空 dict（前端回退显示待签 ⏳）。

    `state_dir` 由调用方传入（`web.app` 的 `STATE_DIR`）——它是可被参数覆盖、
    也会随运行方式变化的模块级名字。
    """
    date_str = date_str or clock.now().strftime("%Y-%m-%d")
    path = os.path.join(state_dir, f"sign-state-{date_str}.json")
    try:
        # utf-8-sig：兼容 Windows 记事本/手工编辑可能写入的 UTF-8 BOM（BOM 会让 json.load 抛错）
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            return data
    except (OSError, ValueError):
        pass
    # 回退：sign-daily（旧版符号 ✅/❌/➖）→ 状态码
    daily_path = os.path.join(state_dir, f"sign-daily-{date_str}.json")
    try:
        with open(daily_path, encoding="utf-8-sig") as f:
            daily = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(daily, dict):
        return {}
    sym_map = {"✅": yiban_status.STATUS_SUCCESS,
               "❌": yiban_status.STATUS_FAILED,
               "➖": yiban_status.STATUS_NO_TASK}
    return {
        phone: {"status": sym_map.get(sym, yiban_status.STATUS_PENDING),
                "message": "", "task": "default"}
        for phone, sym in daily.items()
    }


# ---------------------------------------------------------------------------
# 账密故障暂停（熔断）
# ---------------------------------------------------------------------------
def _cred_paused_phones():
    """处于「账密故障暂停」（熔断/半开试探中）的手机号集合，供设置页容量拆解展示。

    数据源：STATE_DIR/cred-state.json（signin 维护，{phone: {fail_days, last_fail,
    paused_since, probe_date}}）。判定口径与 signin 一致：`paused_since` 非空即暂停中。
    **必须容错**：该文件由签到进程按"无暂停=文件不存在"语义维护，随时可能缺失、被删或
    半写；设置页不能因为一个可选状态文件读不出来就 500，故一切异常都退化为空集合
    （展示层显示 0，判定逻辑不受影响——本函数只服务显示，绝不参与配额判定）。
    utf-8-sig 容错 Windows 手工编辑留下的 BOM（与 signin._load_cred_state 同口径）。
    """
    data = cred_state.read()
    if not data:
        return set()
    return {
        str(phone)
        for phone, rec in data.items()
        if isinstance(rec, dict) and str(rec.get("paused_since", "") or "").strip()
    }


def clear_fuse_pause(phone):
    """账号凭据变更（改密码/编辑）后清除熔断暂停记录，使其立即恢复签到。

    经 `yiban.cred_state` 的唯一入口（整段读-改-写持跨进程锁）。自己读整个文件、删一条、
    再整体写回且**完全不持锁**会与签到进程收尾保存并发：按自己的读取结果重写会抹掉对方
    写入的其他账号记录。文件不存在时无需清除，静默返回——用户每次编辑账号都会走到这里，
    按 I/O 失败告警会刷屏。
    """
    try:
        cred_state.clear(phone)
    except Exception as e:
        # 留痕：裸吞会让"改密后仍暂停"无从排查
        logger.warning("清除账密熔断暂停状态失败，该账号可能仍处暂停: %s [%s]",
                       _mask_phone(phone), e)


def clear_fuse_on_cred_change(old_phone, old_password, clean):
    """仅凭据（密码/手机号）实际变更时清除熔断计数；只改备注/状态等不清。

    此前任意编辑都触发 clear_fuse_pause → fail_days 清零 → 熔断永不跳闸。
    改绑清旧号条目（账号主体已迁移），改密清当前号条目（立即恢复签到资格）。
    """
    if old_phone != clean["phone"]:
        clear_fuse_pause(old_phone)
    if clean["password"] != old_password:
        clear_fuse_pause(clean["phone"])
