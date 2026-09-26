# 兜底撤除前置 checklist（07:12 固定轮）2026-09-26

> **是什么**：撤除「07:12 固定补签轮」这条恢复腿之前必须逐条关闭的 6 条前置的判定台账
> **状态**：定稿（2026-09-26）
> **范围**：覆盖 6 条前置的现状、证据指针与缺口；不覆盖撤除动作本身（**撤除未排期、未执行**），不覆盖 `YIBAN_FALLBACK_ENABLE` 的使用文档（见 `docs/dev/cli.md`）
> **读者**：要动 `run.sh` 补签段 / 宿主 cron 的人必读；要判断 v3 灰度是否可放行的人选读；实现者可略过
> **基线**：`repair/m3-batch1` @ `d88ed15`（2026-09-26）

**关键词**：兜底撤除, 07:12 固定轮, 补签轮, 前置条件, SECOND_DONE_MARKER, 有效轮次, fallback-removal

## 速览

- 6 条前置里 **4 条已闭**（常驻真在跑、rc 已修、让位不整段停摆、v3 有回炉口），**2 条仍开**（单进程吞吐判据、四条契约迁移）。
- 仍开的两条一个是**测量任务**（没有实测数字就不许谈撤除），一个是**治理性变更**（发布门禁"有效轮次"定义，需用户点头）。
- **撤除动作未排期、未执行**：07:12 那条 cron 与 `run.sh` 的进程内补签轮照旧保留，它是"主进程消失后当日仍能恢复"的唯一腿。
- 坑：现网"89 个账号、余量 23 分钟"这个余量是**用没实测过的单账号周期算出来的**（见 §5 与 `must-fix-list.md` MF-56③），别拿它当"单进程够用"的证据。

## 目录

1. 结论：撤除未排期，两条前置在闸口
2. 前置一（已闭）：常驻真在跑——开关 + cron 件
3. 前置二（已闭）：rc 已修——非零退出不得写成 SUCCESS
4. 前置三（已闭）：让位不整段停摆——同一账号让位
5. 前置四（开）：单进程吞吐判据——要先有实测数字
6. 前置五（已闭）：v3 有回炉口——`sign_tasks.failed` 当日可回炉
7. 前置六（开・需用户点头）：四条契约迁移
8. 撤除动作本身的执行前检查
9. 证据索引（可复跑的命令）

---

## 1. 结论：撤除未排期，两条前置在闸口

本节结论：**6 条前置 4 闭 2 开，且两条开项各自卡住一类授权**——一条卡证据（没实测数字），一条卡用户点头（治理性变更）。

| # | 前置 | 状态 | 判据是否成立 | 关键证据 |
|---|------|------|--------------|----------|
| 1 | 常驻真在跑（开关 + cron 件） | 已闭 | 开关置 1 ⇒ 启动即告警并真拉起；cron 件在仓且有独立用例 | `run.sh:_check_fallback_wiring`、`scripts/yiban-fallback.sh` |
| 2 | rc 已修 | 已闭 | 任何非零（含负数/信号/未知码）折进真失败 1；封存前置=库内事实 | `yiban/engine/workers.py:run_worker_supervisor`、`run.sh:_seal_second_done_marker` |
| 3 | 让位不整段停摆 | 已闭 | 有池部署：全量轮持锁期间兜底照常接"该轮没碰的账号" | `yiban/engine/workers.py:run_fallback_worker`、`_await_pool_event` |
| 4 | 单进程吞吐判据 | **开** | 无实测数字，且现有探针与引擎不同式 | 见 §5；`must-fix-list.md` MF-56③⑥ |
| 5 | v3 有回炉口 | 已闭 | `sign_tasks.failed` 当日可被显式路径回炉，沿用 v2 档位协议 | `yiban/store/queue_store.py:requeue_failed`、`yiban/engine/executor_v3.py:_tier_prefix` |
| 6 | 四条契约迁移 | **开** | 4 条全部未迁（撤除未执行，迁移无从谈起）；其中 1 条需用户点头 | 见 §7 |

07:12 固定轮的身份见登记表 `docs/dev/must-fix-list.md`（MF-43 ③）：**既非冗余也非坏件**——它是唯一"主进程消失后才动"的恢复腿（`flock` 即存活判据）。它历史上"从不触发"是被坏 rc 弹开的（前置二已闭），不是它自己不需要。

## 2. 前置一（已闭）：常驻真在跑——开关 + cron 件

本节结论：`YIBAN_FALLBACK_ENABLE=1` 的语义已从"只是 .env 里一个键"兑现为**真拉起 + 真告警**，且存活判据看心跳新鲜度而不是"文件在不在"。

| 分项 | 现状 | 落点 |
|------|------|------|
| 开关 → 真拉进程 | 已闭 | `run.sh:_check_fallback_wiring`：拿到锁、确实要跑本轮时核对一次；不在跑 ⇒ stderr + 当日日志**双声音** ⇒ `nohup "$PY" -m yiban.cli sign --fallback … 9<&-` 拉起（`9<&-` 是防子进程继承本轮全局运行锁的关键） |
| 存活判定口径 | 已闭 | `yiban/engine/state_io.py:fallback_alive`——按心跳文件内时间戳，超过 `2 × YIBAN_FALLBACK_INTERVAL` 视为已停；被 kill -9 的进程不会清心跳，故不会谎报"在跑" |
| cron 件 | 已在仓 | `scripts/yiban-fallback.sh`（薄包装：判开关 + 日志与 `run.sh` 同源），模板 `5 6 * * *` 写在文件头注释；独立用例 `tests/test_yiban_fallback_sh.py` |
| 清单 `type=fallback` 行 | 不参与并行派发（设计如此） | `yiban/egress.py:launch_slots` 只数 worker 行；兜底进程由上面那条开关路径负责，避免"清单里一行 = 每天多一个常驻" |
| 常驻的醒法 | 已闭（见 §4） | 事件驱动：扫空后按池事件签名短轮询，`_POOL_WATCH_SEC` 为粒度、`YIBAN_FALLBACK_INTERVAL` 为上限 |

残留事实（不是缺陷，是部署面）：现网 `cron.d` **没有** fallback 条目（该判断已被否证清单固定：`docs/dev/must-fix-list.md` "已否证清单"一条）。也就是说今天"常驻真在跑"靠的是 `run.sh` 每次触发时的自举核对，而不是宿主自己挂了那条 cron。撤除 07:12 之前要确认：**还剩 `run.sh` 的哪一次触发来做这个核对**——07:12 那条恰恰是当天第二次核对点。〔记入待办 T2〕

## 3. 前置二（已闭）：rc 已修——非零退出不得写成 SUCCESS

本节结论：`SUCCESS` 与补签判定消费的是**同一份真实 rc**，且封存标记的前置是库内事实，不是子执行体自报。

| 验收不变量 | 落点 | 用例 |
|------|------|------|
| 任何非零（负数/信号/契约外未知码）不得归 0 | `workers.run_worker_supervisor` 汇总支 + `_settled_child_code`（只认 0/1/2/3/10 作证） | `tests/test_second_round_rc_contract.py:SupervisorRcAggregationTest` |
| 收尾标记单点写（监督进程写，子执行体不写） | `run_worker_supervisor` 末尾唯一写入口；`runner.main` 按 `YIBAN_EXECUTOR_ID` 关停子执行体侧写 | `SchedMarkerChildGuardTest` |
| "无未了结"取**池 ∪ 状态文件**并集（整批没领不得判"没活"） | `state_io.has_undone_accounts_today` | `UndoneFactsUnionTest` |
| 封存前置 = `--second-run-check` 判 0；判 10 不封存留给后继触发；判定不可得不封存 + 双声音 + 成功轮升 1 | `run.sh:_seal_second_done_marker` / `SEAL_CHECK_RC` | `RunShSealGateTest` |
| 真信号杀的整链收场（主证据） | 真 bash `run.sh` + 真 `run_worker_supervisor` + 真 OS 子进程 + 真 SIGKILL | `RunShRealSigkillDrillTest`：状态文件无 SUCCESS、marker 不封存、`sched-run` 不落盘、判定仍答 10 |

## 4. 前置三（已闭）：让位不整段停摆——同一账号让位

本节结论：全量轮持锁期间兜底**不再整段休眠**；让位粒度从"整轮"收窄到"同一账号"，失败高峰时段（06:31–07:28）它真的在接活。

- 有池部署：让位 = 对"该轮正在飞的那一行"领不动（`claims.try_claim` 同一事务的在飞未过期拒 / done 拒 / `retry:` 放行三条既有条款），不需要全局锁。
- 无池部署（纯状态文件）：账号级互斥不存在，**保留**整段停摆 + 盲间隔（第一红线不松动）。
- 醒得够快：扫空后轮询 `claims.fallback_event(day, exclude_owner)` 的池事件签名（只数兜底接得动的 `retry:` 档，剔除自己的弃权），签名一变即回炉再扫；读不到签名按无事件处理（事件驱动是延迟优化，不是正确性依赖，最坏退化为原 60s 节律）。
- 用例：`tests/test_fallback_event_yield.py`（活体反例 + 签名六条）、`tests/test_fallback_gates.py:YieldPerAccountTest` / `FallbackEventDrivenWakeTest`。

## 5. 前置四（开）：单进程吞吐判据——要先有实测数字

本节结论：这一条**不是代码问题而是测量任务**——今天没有任何一个可信数字能支撑"一个进程就能在窗口内把当天签完，所以不需要 07:12 那一次重新触发"。

判据要成立，需要同时给出下面四项，缺一不算：

| 项 | 要求 | 现状 |
|----|------|------|
| ① 账号数 N | 以**部署记录**为准（登记表记 89），并说明口径 = "会发起签到的账号"（`yiban/store/accounts.py:signs_in`） | 89 是登记件数字，非当日快照 |
| ② 单账号周期 t 的实测分位数 | 同一台机器、同一出口、真实网络下的 p50/p95（含重试与风控退避），不是设计假设 | **未测**。登记表 MF-56③ 明写容量输入量 `1.87~3s` 不是实测；"余量 23 分钟"就是这个未实测 t 推出来的 |
| ③ 判据式 | `N × (t_p95 + gap) ≤ eff_hi − eff_lo − 储备`，其中 `eff_lo/eff_hi` 取 `yiban/window.py:bounds`（默认 06:30~07:50 各让 60s ⇒ 4680s），`gap` 取引擎同款间隔下限（`min_exec_gap`/`exec_gap_min`/`gap_max`），储备建议 ≥ 窗口 20% | 未成文 |
| ④ 被杀后的续得住 | 撤除 ③ 之后当日只剩常驻兜底这条腿，故还要证明："单进程中途崩溃/被 OOM 杀掉时，兜底在窗口剩余时间内吃得下未了结核"——按最坏发生时刻（如 07:00）验算一次 | 未测 |

取证手段的两个坑（必须在动手前解决，否则测出来的数字不可用）：

1. `scripts/loadtest/capacity_probe.py` 与引擎**不是同一个式子**，且造号时窗口写死 ⇒ 白天跑全落 `skipped_window` 仍报"够用"（登记表 MF-56⑥）。要么把探针改成与 `yiban/engine/schedule.py:capacity_of` 同源，要么改用引擎自己的实测来源。
2. 用引擎自己的留痕做分位数更直接：`sign_events` 的每次尝试耗时 + `sched-run-<日期>.json` 的收尾时刻（同一天多轮要看 `sign-status`/`SECOND_DONE_MARKER` 的有无）。这条路径不需要新工具，但需要**现网连续若干天的真实轮次**，且这些轮次必须在修好 rc 之后（旧轮次的"成功"不可信）。

判据未成立 ⇒ **撤除动作不得执行**。

## 6. 前置五（已闭）：v3 有回炉口——`sign_tasks.failed` 当日可回炉

本节结论：开了 `YIBAN_SCHEDULER_V3` 之后失败账号不再只能等第二天；回炉沿用 v2 的 `retry:`/`final:` 档位协议，没有另造一套。

- 写侧：`executor_v3._tier_prefix(status)` 在三处 failed 收尾打档位前缀（判据只认 `claims.RETRYABLE_GIVE_UP_STATUSES`；凭据熔断类落 `final:` 保守档）。
- 回炉口：`queue_store.requeue_failed(day, shards, include_final=…)`，`run_executor_v3` 轮首自动接默认档、`final:` 只由有界显式路径放行（补签轮/手动），常驻兜底不传 `requeue_final`（无界循环不是显式路径）。
- 协议唯一源核验：档位字面量只定义在 `yiban/store/claims.py`；写路径逐行走 `requeue_task` 原语。
- 用例：`tests/test_v3_same_day_requeue.py`（注入 failed ⇒ 当日领到）。
- 与灰度的关系：`docs/dev/must-fix-list.md` "v3 灰度批的两个硬前置"是"两池互斥"与本条（回炉口），本条已闭；另一条的落地判据不在本文范围。

## 7. 前置六（开・需用户点头）：四条契约迁移

本节结论：撤除 07:12 = 撤除"补签轮"这个概念的一次落地，它牵动 4 条对外/跨层契约；**4 条全部未迁**，其中第 4 条是治理性变更，**必须用户点头**，技术方案不得单方面改。

| # | 契约 | 现消费方 | 撤除后的去处 | 需用户点头 | 状态 |
|---|------|----------|--------------|-----------|------|
| 1 | `state_io.need_second_run`（"当日是否收尾"的唯一判据） | `run.sh:_need_second_round`、`run.sh:_seal_second_done_marker`（经 `--second-run-check`）、容器调度器 SECOND 闸门 | 迁移给"调度器收尾/常驻兜底的轮末"，或删除；删了它就是死代码 | 是（判据归属变更） | 未开始 |
| 2 | `run.sh` 退出码 **10 = 需要补跑**（对外契约） | `docs/dev/cli.md` §3、`workers._SECOND_RUN_CHECK_NEED`、监督进程透出、若干用例 | 同步文档 + 测试 + 消费方；**码值不新增**是既有纪律，撤除时按"停用"处理而非改义 | 是（对外契约变更） | 未开始 |
| 3 | `_is_second_run` 的告警口径 | `alerts._maybe_alert_zero_success` 等按"是否补签轮"选措辞/是否告警 | 需要替代信号（例如"窗口内最后一次尝试之后"）——**没有替代信号就撤 = 当日零成功静默** | 是（告警语义变更） | 未开始 |
| 4 | 发布门禁"有效轮次"定义 | `docs/dev/release-gate.md` §3（仓内唯一定义件；交接提示词 `PROMPT.md §6.10` 是它的同名引用，该件不在本仓）——定义**直接依赖 `run.sh` 完整执行 + 退出码 0**，生产每日两条 cron（首签 + 收尾/补签） | 若改成"一个窗口一个常驻进程"，"轮次"要重新定义 ⇒ 影响 `main` 分支推送门槛 | **是，且这条是治理性变更：必须用户点头才动** | 未开始 |

四条的出处是 `docs/dev/reviewfix-scheduler-redesign-20260922.md`（"取消补签轮会牵动的四个契约"一节）与 `docs/dev/reviewfix-10k-algorithm-20260922.md` §4 的退场推论；登记表 `must-fix-list.md`（MF-43）把"四条契约迁移"列为撤除前置第 6 条。

## 8. 撤除动作本身的执行前检查

本节结论：真正动手撤除时，下面每条都要有可指的证据，缺一就不撤。**本文只落这份 checklist，不执行任何撤除**。

- [ ] §5 的四项判据全部成文并给出复现命令；
- [ ] §7 的四条契约逐条迁移完（含文档与测试同步），第 4 条有用户明确点头的记录；
- [ ] 宿主 cron 的 07:12 条目**移除前**先确认 `run.sh` 自举核对（§2 残留事实）在撤除后仍有当天触发点；
- [ ] 常驻兜底在现网的开关状态（`YIBAN_FALLBACK_ENABLE`）经只读取证确认——登记表的 `[需确认：现网 fallback 开关状态（.env 不可读）]` 至今未闭合；
- [ ] 撤除后第一周做一次真实巡检：当日 `sched-run`/`sign-status`/未了结核三项对齐（`docs/dev/ops-inspector` 族的口径）。

## 9. 证据索引（可复跑的命令）

本节结论：本文件的每条"已闭"都能用下面两条命令在本地复跑；判据未成的 §5/§7 不在此列（它们要的是现网取证，不是本地用例）。

```bash
# 前置二/一：rc 契约 + 封存闸门 + 兜底接线 + 真信号杀演练（需 bash 与 POSIX 信号）
TZ=Asia/Shanghai python -m pytest tests/test_second_round_rc_contract.py -q -p no:randomly
TZ=UTC           python -m pytest tests/test_second_round_rc_contract.py -q -p no:randomly
# 前置三/五：让位与事件驱动、v3 当日回炉
python -m pytest tests/test_fallback_event_yield.py tests/test_fallback_gates.py \
                 tests/test_v3_same_day_requeue.py -q -p no:randomly
```

---

## 明确缺陷

| 编号 | 缺陷 | 影响 | 处置 |
|------|------|------|------|
| D1 | §5 的判据 ③ 里"储备 ≥ 窗口 20%"是本文件建议值，不是既有契约 | 引用者可能把它当成已批准的阈值 | 标注为建议；定稿前由用户或运维确认后再作为判据（待办 T1） |
| D2 | "四条契约迁移"的第 4 条引用件 `PROMPT.md §6.10` 不在本仓，仓内同义件是 `docs/dev/release-gate.md` §3 | 按引用名 grep 会 0 命中，误判"定义不存在" | 本文件已写明两处对应关系；引用时以 `release-gate.md` §3 为准 |
| D3 | 账号数 89 取自登记表的登记数字，未做当日快照核对 | 判据 ① 的 N 可能已变 | 与 D1 同期做现网只读取证时一并复核（待办 T1） |

## 待办

| 编号 | 事项 | 处置时点 | 状态 |
|------|------|---------|------|
| T1 | 现网只读取证：账号数 N、单账号周期 p50/p95、`capacity_probe` 与引擎式子同源化（或改走 `sign_events` 耗时分布） | 谈撤除之前；v3 灰度收口时一并 | 未开始 |
| T2 | 确认撤除 07:12 后 `YIBAN_FALLBACK_ENABLE` 的启动核对还剩哪个触发点，并把结论回写 §2 | T1 完成时 | 未开始 |
| T3 | 取用户对"四条契约迁移"（尤其第 4 条"有效轮次"定义）的点头记录，逐条落迁移任务 | 用户裁决时 | 未开始 |
| T4 | 撤除动作本身的执行前检查（§8 五条）逐条打勾后方可改 `run.sh` / 宿主 cron | 撤除批开工前 | 未开始 |
