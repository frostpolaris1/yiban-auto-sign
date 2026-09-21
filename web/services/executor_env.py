# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""执行体清单的 `.env` 读写与对外展示数据（唯一真源）。

**功能**
并行执行体清单（`YIBAN_EXECUTORS`）的读写：读清单并在旧三键存在时一次性迁移写回
`_executor_rows`、锁内读-改-写 `_mutate_executor_rows`、追加行的槽位号 `_next_executor_slot`、
三处出口写入（`_save_slot_egress` / `_save_row_egress` / `_save_fallback_egress`）与两处
输入校验（`_validated_proxy_value` / `_validated_name`）；外加执行体接口的展示族
（`_executors_window` / `_last_executors` / `_executor_row_payload` / `_executor_activity`）。

**归属**
原 `web/app.py` 的模块级执行体辅助，唯一真源在本模块；`web.app` 只保留名字面与转发
（清单写回用的 `write_env_batch`、窗口族用的 `.env` 路径与 `_sign_window` 由转发处注入）。

**复用**
三个写入接口共用 `_mutate_executor_rows` 的"写锁内读-应用-写回"骨架，`_save_slot_egress`
与 `_validated_proxy_value` 共用同一份 `_is_http_proxy_url` 形状校验（不写第二套）；
清单模型（槽位、类型、出口串）的唯一口径在 `yiban.egress`，本模块只编排。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。凡写 `.env` 的入口都接收调用方传入的 `write_batch`（即 `web.app` 的
`write_env_batch`）：它是既有测试观测每一次落盘的打桩点，本模块另持绑定会让打桩静默失效；
同理 `_executors_window` 的 `.env` 路径与签到窗口解析器由调用方现取传入。读路径
（`read_env` / `load_env_int` / `_env_write_lock` / `_is_http_proxy_url`）复用
`web.services.env_io` 的同域实现，行分隔符判定取 `yiban.infra.env_io` 真源。
"""

import logging

import signin  # 探针/子进程模块（scripts/ 在 sys.path 上，由 web.app 的引导保证）

from web.services.env_io import (
    _env_write_lock,
    _is_http_proxy_url,
    load_env_int,
    read_env,
)
from yiban import egress as yb_egress
from yiban import window as yb_window
from yiban.infra import env_io as _yiban_env_io
from yiban.masking import mask_url_userinfo as _mask_url_userinfo
from yiban.store import db

# 与 web.app 同名的日志通道：清单迁移的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")


# ---------------------------------------------------------------------------
# 展示族（执行体接口读侧）
# ---------------------------------------------------------------------------
def _executors_window(env_file, sign_window):
    """执行体接口口径的**有效签到窗口**（`window.effective_sec` 即容量换算的分母）。

    `edge_*` 未配置按 0 计（与 GET 的原实现逐字一致，故显示值零变化）；容量估算与
    "窗口内不做实测"的拦截都用它，保证页面上显示的窗口与这两个判断同源。

    `.env` 路径与窗口解析器由调用方传入：两者都是 `web.app` 上可被测试改写的模块级名字。
    """
    start, end = sign_window()
    return yb_window.bounds({
        "sign_start": start, "sign_end": end,
        "edge_front_sec": load_env_int(env_file, "YIBAN_WINDOW_EDGE_FRONT_SEC", 0),
        "edge_back_sec": load_env_int(env_file, "YIBAN_WINDOW_EDGE_BACK_SEC", 0),
    })


def _last_executors(day):
    """某个业务日每个账号的归属执行体（**已脱敏**）：`{phone: {role, index, label}}`。

    账号列表要显示"上次实领是谁签的"（day 由调用方给——口径是最近一次有记录的业务日，
    见 `store.claims.latest_claims_day`）：一次取回当日全部 `phone -> owner`（见
    `store.claims.owners_for_day`，**不逐账号查**），再把 owner 折成角色与槽位。
    身份串含主机名，属部署信息，故**只回角色/序号/label**，绝不回 owner 原串。
    库不存在/未初始化 → `{}`（新部署很正常），调用方据此回 `null` 而不是报错。
    """
    out = {}
    for phone, owner in db.claim_owners_for_day(day).items():
        parsed = yb_egress.parse_owner(owner)
        out[phone] = {"role": parsed["role"], "index": parsed["index"],
                      "label": parsed["label"]}
    return out


def _executor_row_payload(row):
    """执行体清单的一行 → 接口项（`GET …/executors` 的 `executors[]`），**已脱敏**。

    `egress` 只回 `egress.describe()` 的描述串（代理可能带 `user:pass@`，绝不回原串）。
    存活：**只有 `worker` 行**有值（四态口径在 `signin.worker_presence`）；
    `fallback` 行的存活归 `fallback.*`（心跳文件与判据不同，套 worker 四态会永远 idle），
    `disabled` 行按要求不报存活——两者都回 **`state: null` / `last_seen_at: null`**
    （字段照给、值为 null，口径已冻结给前端，与 `last_executor` 的 null 用法一致）。
    """
    item = {"slot": row["slot"], "type": row["type"],
            "egress": yb_egress.describe(row["proxy"]),
            "label": yb_egress.executor_label(row["type"], row["slot"]),
            # 行的自定义名：没设就是 **null**（前端据此显示后端给的 `label`，或藏起输入框）。
            # 刻意**不**把 name 折进 label：label 是后端口径（角色中文名），name 是用户输入，
            # 两者混在一起后"清空名字"就再也分不出来了。
            "name": row.get("name") or None,
            "state": None, "last_seen_at": None}
    if row["type"] == yb_egress.TYPE_WORKER:
        item["state"], item["last_seen_at"] = signin.worker_presence(row["slot"])
    return item


def _executor_activity(day):
    """当日领取池归属（**已脱敏**）：按 owner 聚合后折成角色 + 槽位序号。

    为什么必须脱敏：`owner` 形如 `{主机名}:{进程号}:w{序号}`，是部署信息（主机名与
    进程号对攻击者是资产清单）。故**绝不回原串**——用 1-based 槽位号替代它，前端
    拿到的信息量不变（"第 1 个并行执行体做了 10 个"），也看得懂。
    角色解析的唯一口径在 `yiban.egress.parse_owner`；`unknown` 照实回（历史数据里
    兜底与单执行体同前缀，本来就无法追溯，不假装能还原）。

    库不存在/未初始化（新部署很正常）→ `([], 全 0)`，与 `claims.stats` 同口径不抛。
    """
    by_executor = []
    totals = {"claimed": 0, "failed": 0, "done": 0, "total": 0}
    for slot, row in enumerate(db.claim_activity(day), start=1):
        parsed = yb_egress.parse_owner(row.get("owner"))
        by_executor.append({
            "slot": slot,
            "role": parsed["role"],
            "index": parsed["index"],
            "label": parsed["label"],
            "claimed": int(row.get("claimed", 0)),
            "failed": int(row.get("failed", 0)),
            "done": int(row.get("done", 0)),
            "total": int(row.get("total", 0)),
        })
        for key in totals:
            totals[key] += int(row.get(key, 0))
    return by_executor, totals


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def _validated_proxy_value(raw):
    """行出口的校验 + 归一：返回 `(值, 错误信息)`；空/None = 直连（空串）。

    换行在 strip **之前**拦（含尾随换行，与单段写接口同一纪律）；形状校验复用
    `_is_http_proxy_url`（不写第二套）。错误回显先 `_mask_url_userinfo` 脱敏。
    """
    submitted = "" if raw is None else str(raw)
    if _yiban_env_io.has_line_break(submitted):
        return None, "代理配置不能包含换行"
    value = submitted.strip()
    if value and not _is_http_proxy_url(value):
        return None, f"代理地址格式不正确: {_mask_url_userinfo(value)[:40]}"
    return value, None


def _validated_name(raw):
    """行自定义名的校验 + 归一：返回 `(值, 错误信息)`；空/None = 清除自定义名。

    这条值要跟着 `slot/type/proxy` 一起挤进 `.env` 的**同一行**，故换行必须在 strip
    之前拦住（与出口串同一纪律）；超长**明确拒绝**而不是静默截断（截断会让"我明明
    起了这个名字"变成查不出来的困惑）。解析侧另走 `egress.clean_name`（宽容，见其说明）。
    """
    submitted = "" if raw is None else str(raw)
    if _yiban_env_io.has_line_break(submitted):
        return None, "名称不能包含换行"
    value = "".join(ch for ch in submitted.strip() if ch.isprintable()).strip()
    if len(value) > yb_egress.NAME_MAX_LEN:
        return None, f"名称最长 {yb_egress.NAME_MAX_LEN} 个字符"
    return value, None


# ---------------------------------------------------------------------------
# 清单读写（`YIBAN_EXECUTORS`）
# ---------------------------------------------------------------------------
def _save_slot_egress(env_path, key, index, value, write_batch):
    """读-改-写 `.env` 里**一段**出口（`index=None` = 该键只有一段，兜底）。

    读与写在**同一把** `.env` 写锁内完成（复用整条写入用的 `_env_write_lock` 与调用方传入的
    `write_batch`，不另造一套）：否则并发保存时"读到的旧串"会把别人刚写的段盖掉。
    其余段**逐字保留**（切分与合并见 `yiban.egress.replace_slot`）——本接口只改一段，
    不重新校验也不规范化别人的段，免得把"这一段直连"的刻意空位改写成别的意思。

    校验复用整条写入的同一个 `_is_http_proxy_url`（不写第二套形状校验）。
    返回 `(错误信息, 状态码)`；成功为 `(None, None)`。
    """
    if _yiban_env_io.has_line_break(value):
        return "代理配置不能包含换行", 400
    if not _is_http_proxy_url(value):
        # 回显能让用户看出是哪一段写错了，但出口串按契约允许带 `user:pass@`：
        # 必须抹掉 userinfo 再回显（错误文案会进响应、DOM 与日志）。
        return f"代理地址格式不正确: {_mask_url_userinfo(value)[:40]}", 400
    with _env_write_lock(env_path):
        raw = read_env(env_path).get(key, "")
        updated = value if index is None else yb_egress.replace_slot(raw, index, value)
        # 写盘前对整串再查一次换行：新段已校验，但其余段是既有配置，本接口逐字保留它们，
        # 只校验新段拦不住历史载荷被"保"进来（write_env_batch 内还有一道硬校验兜底）
        if _yiban_env_io.has_line_break(updated):
            return "现有代理配置含换行符，请先手工清理该键", 400
        try:
            write_batch(env_path, {key: updated})
        except ValueError as e:
            return str(e), 400
    return None, None


def _executor_rows(env_path, write_batch):
    """读执行体清单（`YIBAN_EXECUTORS`）；清单缺失且存在旧三键时**一次性迁移写回**。

    迁移不是另造一套写盘：读-判-写在同一把 `.env` 写锁内完成，写回走调用方传入的
    `write_batch`（键值校验、行折叠、原子替换都在那里）。**旧键不删**——保留
    一个版本周期，回退读取与手工比对都还靠它们；迁移只"多写一个键"。
    写回失败（只读挂载等）只告警并继续按内存结果服务：读一次配置不该让整个接口 500。
    """
    env = read_env(env_path)
    rows, needs_write = yb_egress.manifest_state(env)
    if not needs_write:
        return rows
    try:
        with _env_write_lock(env_path):
            rows, needs_write = yb_egress.manifest_state(read_env(env_path))
            if needs_write:
                write_batch(env_path, {
                    yb_egress.ENV_MANIFEST: yb_egress.dump_manifest(rows)})
                logger.info("执行体清单：已按旧三键迁移写入 %s（旧键保留）",
                            yb_egress.ENV_MANIFEST)
    except (OSError, ValueError) as e:
        # 已脱敏：这里只打异常本身（不含代理串；write_env_batch 的报错只带键名）
        logger.warning("执行体清单迁移写回失败（按内存结果继续）: %s", e)
    return rows


def _mutate_executor_rows(mutator, env_path, write_batch):
    """在 `.env` 写锁内读清单 → 应用 `mutator(rows)` → 写回清单键（读-改-写原子）。

    `mutator` 返回 `(新行, 结果)`；校验失败抛 ValueError（消息可直接回前端 400）。
    清单缺失时 `manifest_state` 先按旧三键给出行，改动后的整份清单一次写回
    （顺带完成迁移；旧键仍保留）。只按槽位动目标行，其余行逐字保留。
    """
    with _env_write_lock(env_path):
        rows, _ = yb_egress.manifest_state(read_env(env_path))
        new_rows, result = mutator(rows)
        write_batch(env_path, {
            yb_egress.ENV_MANIFEST: yb_egress.dump_manifest(new_rows)})
    return result


def _next_executor_slot(rows):
    """追加行的槽位号：清单最大 + 1，且**跳过保留期内真用过的号**（下标只增不复用）。

    为什么需要这一步：纯函数 `next_slot` 只能给"清单最大值 + 1"，删掉当前最大行之后它会
    把刚空出来的号再发一次，而那个号在领取池（`sign_claims.owner`）里已经有历史——重建的
    执行体会被显示成前任的归属。故这里再按**领取历史**抬一次下限（保留期 14 天，与展示
    口径同窗口）。历史里出现过的号一律不复用，跨主机也一样（同一个库＝同一个部署）。

    库不可用/未初始化时退回"只按清单最大值 + 1"：编号可能重复，但**追加本身绝不能失败**。
    """
    floor = 0
    try:
        for owner in db.claim_owners_since():
            parsed = yb_egress.parse_owner(owner)
            if parsed["role"] == yb_egress.ROLE_WORKER and isinstance(parsed["index"], int):
                floor = max(floor, parsed["index"] + 1)
    except Exception as e:   # 库抖动不影响追加（与领取池的降级纪律一致）
        logging.getLogger("yiban").debug("读取执行体历史失败（追加槽位退回清单口径）: %s", e)
    return max(yb_egress.next_slot(rows), floor)


def _save_row_egress(env_path, slot, value, write_batch):
    """清单模式下只改该行的出口：**其余行逐字保留**，写回清单键（同一把写锁/同一写入函数）。

    与 `_save_slot_egress`（旧逗号列表模式）同纪律：只动目标行，不重排、不规范化
    别的行。槽位不在清单里 → ValueError → 400。
    """
    def _apply(rows):
        return yb_egress.update_row(rows, slot, proxy=value), None

    try:
        _mutate_executor_rows(_apply, env_path, write_batch)
    except ValueError as e:
        return str(e), 400
    return None, None


def _save_fallback_egress(env_path, value, write_batch):
    """清单模式下改兜底行的出口；清单里没有兜底行则**追加一行**（旧接口的写入要生效）。"""
    def _apply(rows):
        fb = yb_egress.fallback_row(rows)
        if fb is None:
            return yb_egress.add_row(rows, yb_egress.TYPE_FALLBACK, value), None
        return yb_egress.update_row(rows, fb["slot"], proxy=value), None

    try:
        _mutate_executor_rows(_apply, env_path, write_batch)
    except ValueError as e:
        return str(e), 400
    return None, None
