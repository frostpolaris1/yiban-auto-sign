# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""HRW（Rendezvous Hashing）确定性分工：账号 → 虚分片 → 执行体，纯函数、无中心。

**算法溯源**
- Rendezvous Hashing（又称 HRW / Highest Random Weight Hashing）：
  David Thaler、Chinya Ravishankar，密歇根大学 CSE-TR-316-96（1996），
  正式论文 IEEE/ACM ToN 6(1) 1998。本模块即该算法：`vshard_of` 是账号→虚分片的取模步，
  `owner_of` 的 `argmax_e H(vshard‖day‖executor)` 是归属步。二者论文给出了**最小扰动界**
  ——增删一个执行体时仅归属被超过的约 1/K 分片重映射，这是 `owner_of` 取代"按取余轮流分"
  的全部依据（ketama 一致性哈希环的搬移比例同为 ~1/K，差别在本算法的查找是无中心的，
  不依赖任何协调状态）。

**功能**：把账号划分给执行体，全程不需要任何协调。两步：`vshard_of` 按 `(phone, day)`
把账号钉进 v 个虚分片之一，`owner_of` 再对每个分片用 argmax 选出归属执行体。

**归属**：`yiban.engine` 的调度基元层。计划层按分片批量写 `sign_tasks.owner`，执行体用
`shards_of` 拼 `WHERE vshard IN (...)` 领自己名下的行；本模块自身零依赖、零状态。
调用点：`planner.build_plan` 用 `v_for` / `vshard_of` / `owner_of`，`executor_v3` 用
`shards_of` / `vshard_of`——两者都在台账单池化后的唯一生产执行路径上。

**复用**：`v_for` 给出当日虚分片数（已定档）；`assignment` 一次算出全表归属（运维核对/对账用）；
`shards_of` 是执行体启动第一批查询分片集的唯一来源。

**通信**：输入是字符串/整数（`phone`、`day`、执行体身份串），输出是整数 / tuple / dict。
不读时钟、不读环境、不落库、不缓存，`day` 一律由调用方显式传入——因此同一组输入在任何
进程、任何时刻、任何调用顺序下都得到同一结果，崩溃恢复与重放都不会漂移。
打分纪律：`hashlib.blake2b` 前 8 字节，**禁用内置 `hash()`**（字符串哈希每进程随机化，
一旦误用，执行体之间对同一账号的 owner 判断会分叉 ⇒ 重复登录）。

**规模**：分片数已**定档 64**（`v_for`），每执行体期望 `V/K` 个分片（K=8 时约 8 片）。
分片数服从多项分布，账号数的相对波动约 `√(K(1−1/K)/V)`——K=2 约 ±12.5%、K=4 ±22%、
K=8 ±33%。虚分片带来的是"增删执行体只搬约 1/K"与按分片批量领取，不是精确均分。
定档只改"账号落进哪个桶"，每账号每天仍是一行，数据量不变。
"""
import hashlib

#: 虚分片总数（定档值）。分片越细，执行体增删时被搬动的账号比例越接近 1/K；
#: 取 64 的依据、恢复办法与改档后要补的测试都写在 `v_for` 的 docstring 里。
V_DEFAULT = 64


def _h(*parts):
    """HRW 打分：把各段以 `\\x1f` 连接后取 blake2b 前 8 字节的无符号整数。

    取 64 位整数而不是先取模：`vshard_of` 的取模与 `owner_of` 的打分比较都直接吃它，
    64 位空间下平局概率可忽略。
    """
    # \\x1f（ASCII 单元分隔符）不会出现在手机号/日期/执行体身份串里，用它连接可避免
    # ("12","3") 与 ("1","23") 撞成同一串——换成分隔符为空或普通连字符都会引入这类碰撞
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big")


def u01(*parts):
    """`[0,1)` 均匀量：`_h` 取高 53 位（低 11 位丢弃，换来 2^53 个可精确表示的分点）。

    归一化只在这一处：分位、槽内相位、每日参数都从它派生，同一串输入在任何进程、
    任何时刻都得同一值——"计划是纯函数、崩溃可重放"的底座就在这里。
    """
    return (_h(*parts) >> 11) / float(1 << 53)


def vshard_of(phone, day, v=V_DEFAULT):
    """账号所属虚分片 `H(phone ‖ day) mod v`，返回 0..v-1。

    `day` 进哈希输入是刻意的：当天内完全确定（可落库、可重放、崩溃恢复不漂移），跨天则重排，
    避免"永远是同一批账号落在同一执行体"的偏斜。
    """
    return _h(phone, day) % v  # 取模收在最后：先模会丢掉高位、把平局概率放大


def owner_of(vshard, executors, day):
    """虚分片归属 `argmax_e H(vshard ‖ day ‖ executor_e)`；`executors` 为空返回 `""`。

    增删执行体只影响得分被超过的那约 1/K 个分片，其余归属不动（迁移量最小）——这是它取代
    "按取余轮流分"的全部理由。
    """
    if not executors:
        return ""
    # 排序后再 max：max 返回首个最大值，排序即把平局判给字典序最小的执行体——同一分片集合
    # 无论调用方以什么顺序传入，归属都相同（无中心、零通信的共识就建立在这条上）
    return max(sorted(executors), key=lambda e: _h(vshard, day, e))


def assignment(executors, day, v=V_DEFAULT):
    """一次算出全部分片归属 `{vshard: owner}`，键恰为 0..v-1（每个分片必有主，无空洞）。

    代价 O(V·K) 次哈希（64×K，K≤64 时微秒~毫秒级），故不加缓存——纯函数缓存只会带来
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
    """当日虚分片数 V：**定档 64**（2026-09 起），恒定返回 `V_DEFAULT`，不再按规模分档。

    `n_accounts` 只是为保住既有调用点（`planner.build_plan`、`executor_v3` 的影子对账与
    建计划兜底）与下面的恢复路径，不必改签名；定档后它不参与选择。

    ① **为什么定 64**：目标规模是 1~2 千账号，这个量级实测 K=1（单执行体）就够，K≥2 属
    冗余形态；而 64 恰是定档前小站（n<500）的缺省值，故小站部署定档前后**行为逐位不变**。
    V 只是模数：每账号每天仍写一行、只改进"落进哪个桶"，因此零 schema 变更、零数据迁移，
    当日已写行由 `executor_v3._stored_v` / `_max_vshard` 兜底放宽。分片本身是领取围栏柱的
    载体（`claim_batch` 只领自己分片集内的行），故 V 只能定档、不能拆掉——V=1 会让 K≥2 的
    第二个执行体一个桶都分不到。单执行体（K=1）自然拿到全部分片，行为退化为"全包"，不需要
    另一条代码路径。

    ② **恢复指引**：若账号数已到 500 以上**且**执行体数 K≥4，或实测领取分摊明显不均
    （某执行体长期空转、另一侧积压），把本函数改回按账号数分档——旧口径是 n<500→64、
    n<3000→128、否则 256。选档公式：账号数相对波动 ≈ `√(K(1−1/K)/V)`，V=64 时 K=2 约
    ±12.5%、K=4 ±22%、K=8 ±33%（K=1 无波动）。重启条件见 `docs/dev/scheduler-v3.md` §9。

    ③ **改档后必须补的测试**：`tests/test_hrw.py` 两处 σ 包络按新 V **重算**（正态多项分布
    近似，照抄上一档的容差必然误报）、`test_v_for_thresholds` 钉死新分档的边界两侧取值、
    V 不变量跨档抽查（`tests/test_executor_v3.py::VInvariantTest`）。
    """
    return V_DEFAULT
