# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""HRW（Rendezvous Hashing）确定性分工：账号 → 虚分片 → 执行体，纯函数、无中心。

**功能**：把账号划分给执行体，全程不需要任何协调。两步：`vshard_of` 按 `(phone, day)`
把账号钉进 v 个虚分片之一，`owner_of` 再对每个分片用 argmax 选出归属执行体。

**归属**：`yiban.engine` 的调度基元层。计划层按分片批量写 `sign_tasks.owner`，执行体用
`shards_of` 拼 `WHERE vshard IN (...)` 领自己名下的行；本模块自身零依赖、零状态。
调用点：`planner.build_plan` 用 `v_for` / `vshard_of` / `owner_of`，`executor_v3` 用
`shards_of` / `vshard_of`——两者都在 `YIBAN_SCHEDULER_V3`（缺省 0）的 v3 路径上。

**复用**：`v_for` 按账号规模选分片数；`assignment` 一次算出全表归属（运维核对/对账用）；
`shards_of` 是执行体启动第一批查询分片集的唯一来源。

**通信**：输入是字符串/整数（`phone`、`day`、执行体身份串），输出是整数 / tuple / dict。
不读时钟、不读环境、不落库、不缓存，`day` 一律由调用方显式传入——因此同一组输入在任何
进程、任何时刻、任何调用顺序下都得到同一结果，崩溃恢复与重放都不会漂移。
打分纪律：`hashlib.blake2b` 前 8 字节，**禁用内置 `hash()`**（字符串哈希每进程随机化，
一旦误用，执行体之间对同一账号的 owner 判断会分叉 ⇒ 重复登录）。

**规模**：每执行体期望 `V/K` 个分片（3 万账号 / 16 执行体 ≈1875 账号/执行体）。分片数
服从多项分布，相对标准差 `√((K−1)/V)`（K=8、V=256 时约 16.5%）——虚分片带来的是
"增删执行体只搬约 1/K"与按分片批量领取，不是精确均分。
"""
import hashlib

#: 虚分片总数缺省值。分片越细，执行体增删时被搬动的账号比例越接近 1/K。
V_DEFAULT = 256
#: 小规模站点用的更小 V（见 `v_for`）：纯计算量层面的省，不改变归属语义
V_SMALL = 64
V_MEDIUM = 128


def _h(*parts):
    """HRW 打分：把各段以 `\\x1f` 连接后取 blake2b 前 8 字节的无符号整数。

    取 64 位整数而非先取模：`vshard_of` 的取模与 `owner_of` 的打分比较都直接吃它，
    64 位空间下平局概率可忽略。`\\x1f`（ASCII 单元分隔符）不会出现在手机号/日期/执行体
    身份串里，用它连接可避免 `("12", "3")` 与 `("1", "23")` 撞成同一串。
    """
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big")


def vshard_of(phone, day, v=V_DEFAULT):
    """账号所属虚分片 `H(phone ‖ day) mod v`，返回 0..v-1。

    `day` 进哈希输入：当天内完全确定（可落库、可重放、崩溃恢复不漂移），跨天则重排，
    避免"永远是同一批账号落在同一执行体"的偏斜。
    """
    return _h(phone, day) % v


def owner_of(vshard, executors, day):
    """虚分片归属 `argmax_e H(vshard ‖ day ‖ executor_e)`；`executors` 为空返回 `""`。

    排序后再 `max`：`max` 返回首个最大值，排序即把平局判给字典序最小的执行体——同一
    分片集合无论调用方以什么顺序传入，归属都相同（无中心、零通信的共识就建立在这条上）。
    增删执行体只影响得分被超过的那约 1/K 个分片，其余分片归属不动（迁移量最小）。
    """
    if not executors:
        return ""
    return max(sorted(executors), key=lambda e: _h(vshard, day, e))


def assignment(executors, day, v=V_DEFAULT):
    """一次算出全部分片归属 `{vshard: owner}`，键恰为 0..v-1（每个分片必有主，无空洞）。

    代价 O(V·K) 次哈希（256×K，K≤64 时微秒~毫秒级），故不加缓存——纯函数缓存只会带来
    失效问题。`executors` 为空时全部值取 `""`（还没有执行体，计划先落库也可）。
    """
    ordered = sorted(executors)          # 排一次序，省下逐片重排
    return {vshard: owner_of(vshard, ordered, day) for vshard in range(v)}


def shards_of(executor, executors, day, v=V_DEFAULT):
    """某执行体名下的分片集（升序 tuple）。

    执行体启动第一批查询就用它拼 `WHERE vshard IN (...)`：走 `sign_tasks(day, vshard, ...)`
    索引、与别的执行体零竞争、也不碰别人的行。返回空 tuple 表示本轮不该领活（无执行体或
    自己没分到片），不是故障。
    """
    return tuple(vshard for vshard, owner in assignment(executors, day, v).items()
                 if owner == executor)


def v_for(n_accounts):
    """虚分片数按规模选：n<500→64，n<3000→128，否则 256。

    小 N 时用小 V 只是省点哈希；分片逻辑本身不特判——单执行体（K=1）自然拿到全部分片，
    行为退化为"全包"，不需要另一条代码路径。
    """
    if n_accounts < 500:
        return V_SMALL
    if n_accounts < 3000:
        return V_MEDIUM
    return V_DEFAULT
