# 测试项目清单（按功能分组，2026-09-29 按实收集刷新）

> 计数口径：**pytest 实际收集数**（含参数化展开；subTest 不计入收集数）。
> 当前：**182 个测试文件 / 3414 个收集用例**。
> 复现：
> ```bash
> wsl -e bash -c "cd /mnt/d/code/yiban-wt-s6a && ~/.venv-yiban-wsl/bin/python -m pytest tests/ \
>   -p no:randomly --collect-only -q 2>&1 | tail -1"
> ```
> 分组取自各文件模块 docstring 的 `标签：<字母> · <组名>` 行；描述优先沿用旧表，
> 新增文件取模块 docstring 首句（截断 70 字）。全量实跑 passed/skipped 见批 6c-3 报告。

## 怎么只跑一组

```bash
wsl -e bash -c "cd /mnt/d/code/yiban-wt-s6a && ~/.venv-yiban-wsl/bin/python -m pytest \
  tests/test_planner.py tests/test_hrw.py -q -p no:randomly"      # 指定文件
wsl -e bash -c "cd /mnt/d/code/yiban-wt-s6a && ~/.venv-yiban-wsl/bin/python -m pytest \
  tests/ -q -p no:randomly -k mine_or_planner_or_hrw"             # 按关键字
```

## A · 调度：计划与分片

| 文件 | 用例 | 覆盖 |
|---|---:|---|
| `test_time_prefs.py` | 69 | 调度 v2（S2/S3）自选时间片全链路测试：db 层 + 调度层 + API 层。 |
| `test_planner.py` | 52 | `yiban/engine/planner.py`（双粒度分片 Planner）与 `schedule.capacity_accounts_v3` 的契约用例。 |
| `test_effective_window.py` | 27 | 有效窗口单一口径（排计划 / 判关闭 / 算容量同源）。 |
| `test_schedule_v2.py` | 22 | 调度 v2（S1 demo）build_schedule 统一填充框架测试。 |
| `test_hrw.py` | 11 | `yiban.engine.hrw` 的契约用例：纯函数 HRW 分工（虚分片 + argmax 归属）。 |
| `test_slot_geometry.py` | 11 | 自选片几何的准绳一致性：`schedule._slot_to_bi` / web `_pref_slots` 与 `_schedule_blocks` 同源。 |
| `test_window_edge_clamp.py` | 11 | 缓冲过大时收缩缓冲、保留窗口（`yiban/window.bounds` 的退化处置）。 |

小计 **7** 文件 / **203** 用例

## B · 调度：领取/队列/执行体

| 文件 | 用例 | 覆盖 |
|---|---:|---|
| `test_executor_v3.py` | 91 | yiban/engine/executor_v3.py`：v3 执行体核心（台账单池化后的唯一生产执行体）。 标签：B · 调度：领取/队列 |
| `test_egress_and_executors_api.py` | 70 | 出口分配（`yiban/egress.py`）与执行体接口（`/api/scheduler/executors`）的断言。 |
| `test_scheduler_gate.py` | 56 | 修复回归测试（对抗性审查 2026-08-29）。 |
| `test_executor_manifest.py` | 46 | **执行体清单**（`YIBAN_EXECUTORS`）的模型、迁移等价与行接口断言。 |
| `test_container_scheduler.py` | 43 | 容器内签到调度器（docker/scheduler.py）回归测试。 |
| `test_schedule_retry.py` | 38 | 重试重排与补签时刻：重试落点、槽位时刻与宿主脚本契约。 |
| `test_token_bucket.py` | 31 | `yiban/engine/token_bucket.py` 的契约用例：GCRA/TAT 令牌桶、AIMD、全局 Λ、gap 门、EWMA。 |
| `test_run_sh_workers.py` | 27 | `run.sh` 的多执行体开关（`YIBAN_WORKERS`）外壳行为。 |
| `test_claims.py` | 25 | 签到领取池与账号级租约（`yiban/store/claims.py`，v17）的行为与并发断言。 |
| `test_fallback_gates.py` | 25 | 兜底常驻执行体的三道门与窗口边界（2026-09-17 实测缺陷的钉版回归）。 |
| `test_sign_round_guards.py` | 25 | 签到轮守卫回归（2026-09-08）。 |
| `test_store_queue.py` | 25 | `yiban/store/queue_store.py`：sign_tasks 的批量领取 / 批量收尾 / 重排 / 当日计数。 |
| `test_claims_mutex.py` | 22 | phone, day)` 互斥的 store 层收口：领取租约、owner 身份、v3 重排门、purge 时钟守卫。 标签：B · 调度： |
| `test_v3_same_day_requeue.py` | 22 | v3 当日失败任务回炉口与兜底腿的 v2/v3 分流（补签链灰度硬前置的反例钉）。 标签：B · 调度：领取/队列/执行体 覆盖：v3 弃权 |
| `test_scheduler_exception_surface.py` | 21 | 容器调度器异常面与锁目录 fail-closed 回归。 标签：B · 调度：领取/队列/执行体 覆盖：签到子进程 Popen 抛 OSEr |
| `test_queue_recovery.py` | 19 | v3 崩溃恢复的队列访问层：`queue_store.reap_expired` 与死主分片接管的领取链。 标签：B · 调度：领取/队列/ |
| `test_scheduler_env_probe.py` | 19 | 对抗性审查修复回归测试（v0.24.3，2026-08-27）。 |
| `test_second_round_rc_contract.py` | 19 | 补签链的 rc 契约与封存前置：监督进程不得把异常退出归 0，标记单点写，封存看库内事实。 标签：B · 调度：领取/队列/执行体 覆盖：` |
| `test_host_exit_semantics.py` | 18 | 回归测试：宿主 run.sh 补签闸门不被「部分成功」吞掉。 |
| `test_multi_executor_engine.py` | 18 | 多执行体的**引擎侧**行为断言：领取池进入执行循环后的分派与了结语义。 |
| `test_claims_fencing.py` | 17 | fencing epoch：领取自增、**所有终态写**都带 `epoch=?`，以及 `try_claim` 的 fail-closed。 |
| `test_claims_heartbeat.py` | 16 | 在领账号的心跳与轮末收尸：`claims.touch` 的生产调用者、周期、降级、库信号分档。 标签：B · 调度：领取/队列/执行体 覆盖 |
| `test_executor_write_guard.py` | 14 | 执行体写操作的**口令门**与行的**自定义名**（2026-09-17）。 |
| `test_run_lock_fail_closed.py` | 10 | 运行锁三条 fail-open 的收敛：拿不到锁一律拒跑，四个调用点都检查返回值。 标签：B · 调度：领取/队列/执行体 覆盖：`_acq |
| `test_waf_failure_tier.py` | 10 | WAF/挑战解析失败必须落**显式不可重试档**（总尝试 1 + 清会话），判据单一真值源。 标签：B · 调度：领取/队列/执行体 覆盖： |
| `test_fallback_event_yield.py` | 9 | 兜底"失败即入队"的事件源与"同一账号让位"的仲裁面：都由**任务队列（sign_tasks）**的行迁移承担。 标签：B · 调度：领取/ |
| `test_claims_cross_round.py` | 7 | 领取池的跨轮上限：弃权原因分档与「显式路径才可再领」。 标签：B · 调度：领取/队列/执行体 覆盖：`claims.give_up` 的原 |
| `test_only_single_process.py` | 6 | 手动签到 `--only` 的派发收敛与 terminate 进程树：单进程、无孤儿。 标签：B · 调度：领取/队列/执行体 覆盖：`ru |
| `test_yiban_fallback_sh.py` | 5 | 兜底常驻执行体外壳（`scripts/yiban-fallback.sh`）的行为断言。 |
| `test_supervisor_recursion_guard.py` | 4 | 多执行体的**子进程不得再当监督进程**（2026-09-17 对抗性审查 H1 的钉版回归）。 |
| `test_claims_reap_e2e.py` | 3 | 领取面两步显式处置的 e2e：心跳停摆后的租约接管，与轮末对死亡执行体的收尸。 标签：B · 调度：领取/队列/执行体 覆盖：真子进程领取后 |

小计 **31** 文件 / **761** 用例

## C · 存储：迁移与库完整性

| 文件 | 用例 | 覆盖 |
|---|---:|---|
| `test_store_boundary.py` | 98 | store 层拆分边界：门面读写转发 + 表级 CRUD 真实可用。 |
| `test_db_integrity.py` | 46 | Task 3：DB 层数据完整性与迁移修复测试。 |
| `test_session.py` | 38 | 会话缓存表族与会话恢复：有效期判定、凭据加密、重启后恢复。 |
| `test_migrations_fail_closed.py` | 35 | 迁移 fail-closed：分级拒绝启动、迁移记录表、断点重跑、tmp 权限、守恒断言。 验收不变量逐条（task-5 brief）： - |
| `test_sign_events.py` | 16 | 签到事件表 `sign_events`：写入、按天聚合与前端统计口径。 |
| `test_ledger_check.py` | 13 | `scripts/ledger_check.py` 的契约用例：三项检查、退出码 `0/1/2`、白名单补项。 |
| `test_db_migrations.py` | 12 | Phase 0：通用幂等迁移框架测试。 |
| `test_migrations_v20.py` | 12 | v20 backfill：把 `sign-state-*.json` 里的**终态**补进 `sign_tasks`。 |
| `test_migrations_v18.py` | 8 | v18 迁移：持久化任务队列 `sign_tasks` + 出口令牌桶状态 `egress_state`。 |
| `test_migrations_v19.py` | 8 | v19 迁移：`sign_claims` 补 fencing token 列 `epoch`（`sign_tasks.epoch` 兜底补齐）。 |
| `test_db_owner_constraint.py` | 7 | Phase 2：每人限 1 账号 DB 约束 + 可区分错误码测试。 |
| `test_migration_compat.py` | 3 | 升级兼容硬门：旧代码打开新库（零写入）与「既有表零变更」。 |

小计 **12** 文件 / **296** 用例

## D · 状态词汇与账号生命周期

| 文件 | 用例 | 覆盖 |
|---|---:|---|
| `test_state_gc.py` | 37 | 按天状态文件的清理策略（`yiban/state_gc.py`）与两处调用点。 |
| `test_status_sets.py` | 25 | 状态集合的单一事实源：JSON 状态码全集、了结/未了结划分、「今日不必再签」三套词表。 |
| `test_no_position.py` | 19 | 无点位账号独立状态回归测试（2026-09-01，用户裁决）。 |
| `test_probe.py` | 15 | 探针模式 + 注册时账号验证测试（v0.23.x）。 |
| `test_saturday_sign.py` | 12 | 周六签到开关测试（2026-08-29；2026-09-07 v0.29.0 默认语义反转：默认关闭）。 |
| `test_state_purge_guard.py` | 11 | 清库/删除入口的状态侧防线：备份副本保护、state 清理 argv/dry-run、CLI --yes 门。 标签：D · 状态词汇与账号 |
| `test_state_file_writes.py` | 8 | 状态文件写盘的原子性（DAT-7）与"标记损坏"的失效方向。 |
| `test_yiban_status_single_source.py` | 6 | 状态词汇表单一事实源（M1）：`yiban.status` 定义，signin / web 只做别名引用。 |
| `test_global_pause_chain_e2e.py` | 5 | 急停端到端可复跑链路：`.env` 暂停 → run.sh 真链引擎 rc=2 → 页面显示与事实同源。 标签：D · 状态词汇与账号生命周 |
| `test_multi_task.py` | 4 | 多任务「随机选点、任一成功即停」签到语义测试（2026-08-29）。 |
| `test_global_pause.py` | 3 | 全局暂停（一键暂停签到）测试。 |

小计 **11** 文件 / **145** 用例

## E · Web：认证/权限/API

| 文件 | 用例 | 覆盖 |
|---|---:|---|
| `test_web_security_gates.py` | 80 | 对抗性审查修复验证（2026-09-01）。 |
| `test_web_auth_security.py` | 69 | 0.21.0 Task 2：Web 认证/授权/安全配置修复测试。 |
| `test_verify_jobs.py` | 66 | 在线校验任务：生命周期、失败冷却与并发闸。 |
| `test_pw_gate_tiers.py` | 35 | 口令复核门的三档（`YIBAN_PW_GATE`）与软性摩擦：档位矩阵 + 风控 + 倒计时确认。 标签：E · Web：认证/权限/API |
| `test_static_routes.py` | 25 | 静态资源与合规页路由：自定义优先、缺失 404、短缓存头。 |
| `test_account_abuse_gate.py` | 22 | 被盗号滥用面加固回归测试（2026-08-29）。 |
| `test_creds_write_accounting.py` | 21 | 凭据写入记账：`phone_code` 读侧计入 `creds_written`，`__clear__` 哨兵折算为清空语义。 背景一：写侧 |
| `test_registration_pause.py` | 13 | 暂停注册（v0.26.3）+ web 日志落盘（DailyFlockFileHandler）测试。 |
| `test_web_api_security.py` | 13 | 对抗性测试：用户体验不误杀 + 安全边界。 |
| `test_review_flow.py` | 12 | 账号审核流转 API 测试（锁定 2026-08-16 审查轮 ACCOUNT_STATUS_* 改名行为）。 |
| `test_switch_password_gate.py` | 12 | 系统开关口令门禁测试（global_pause / registration_pause 变更需 confirm_password）。 |
| `test_admin_privilege_web.py` | 11 | 管理员目标操作权限测试（安全审查 2026-08 修复验证）。 |
| `test_builtin_admin_sid.py` | 11 | 内置主管理员（.env 账号）的服务端会话吊销面。 |
| `test_webui_inline_context.py` | 9 | Web 前端注入面契约（2026-09-08）。 |
| `test_manual_paused_spawn_filter.py` | 7 | 手动签到在 spawn 前剔除已自暂停账号（批量计数不虚高）。 标签：E · Web：认证/权限/API 覆盖：`POST /api/sig |
| `test_manual_sign_reporting.py` | 6 | 手动签到与账号编辑守卫回归（2026-09-08）。 |
| `test_dupcheck_oracle.py` | 5 | 个人提交判重预检的"在册确认"收口回归：会话配额 + 每次命中审计留痕。 标签：E · Web：认证/权限/API 覆盖：`POST /ap |
| `test_rate_limit_tiers.py` | 5 | 全局限速分级（用户实拍：快速切页 /api/* 触发 429）。 |
| `test_review_bypass_and_fake_success.py` | 2 | 绕审直进主链与"假成功"回归（MF-59 ①②）。 标签：E · 存储：账号表 / Web：个人自助 覆盖：① `replace_accou |
| `test_gate_manifest_sync.py` | 1 | 高危门禁"清单与执行同源"回归（MF-58 ③）。 标签：E · Web：认证/权限/API 覆盖：`web.app.create_app` |
| `test_panel_false_signals.py` | 1 | 面板假信号回归（MF-55 ①）。 标签：E · Web：账号面板 覆盖：① `GET /api/accounts` 的用户自暂停合成——当 |
| `test_register_admin_email_indist.py` | 1 | 注册口不得成为"超管邮箱探针"回归（MF-58 ①）。 标签：E · Web：认证/权限/API 覆盖：`POST /api/registe |

小计 **22** 文件 / **427** 用例

## F · 前端与界面守卫

| 文件 | 用例 | 覆盖 |
|---|---:|---|
| `test_web_boundary.py` | 132 | web 层拆分边界：名字面完整 + 转发落到 app 模块级真状态。 |
| `test_delay_ack_frontend.py` | 31 | 受门禁操作提交的前端行为测试（node 真跑，非静态扫描）。 标签：F · 前端与界面守卫 覆盖：受门禁操作提交的前端真实行为——`YB.d |
| `test_logs_by_date.py` | 26 | 按天日志（sign-YYYY-MM-DD.log）读取与按日期查看功能测试。 |
| `test_calendar_state_visibility.py` | 24 | 签到日历状态可见性：状态表单一源（日期格渲染 + 图例）与急停/周末门真值显示。 标签：F · 前端与界面守卫 覆盖：状态显示表 `yiba |
| `test_smtp_identity_frontend.py` | 12 | SMTP 条目稳定 id 与额度/空态显示口径的前端行为测试（node 真跑）。 标签：F · 前端与界面守卫 覆盖：settings-ma |
| `test_web_js_modules.py` | 9 | 前端脚本装配守卫（多页 MPA + 组件化后的等价判据）。 |
| `test_dashboard_stats_caliber_js.py` | 8 | 数据总览页「账号数 / 事件数」双口径聚合的 JS 行为测试（MF-55 收口后版本）。 标签：F · 前端与界面守卫 覆盖：数据总览页对  |
| `test_settings_tiers_frontend_parity.py` | 7 | 档位表与设置页前端控件的双向对拍。 |
| `test_users_exit_surface_frontend.py` | 7 | MF-49 出口面·Task 2-9b：前端出口 node 真跑钉（禁纯文本断言，照 2-8/对拍先例）。 钉的出口（每条都是**真函数体在 |
| `test_env_line_break_frontend.py` | 6 | 前端 .env 行分隔符校验与下拉"未知枚举不静默换值"守卫。 **背景**（与后端 `.env` 行模型同族的另一半）： - 前端表单对行 |
| `test_web_render_golden.py` | 6 | Web 模板渲染「金标准」回归测试（2026-09-10 前端模块化护栏）。 |
| `test_schedule_edge_limit_js.py` | 4 | 设置页滑块量程与后端夹取口径的 JS 行为对拍（缓冲单边上限）。 标签：F · 前端与界面守卫 覆盖：设置页缓冲滑块上限 `edgeMaxM |
| `test_web_font_closure.py` | 3 | 回归守卫：中文字体栈收口（防止中文回退到宋体 SimSun）。 |
| `test_account_ops_reentry.py` | 2 | 账号写操作的防重入（在途时后续触发早退）。 标签：F · 前端与界面守卫 覆盖：`web/static/js/components/acco |
| `test_time_field_norm.py` | 2 | 时间字段 `norm` 的范围校验（形状 **与** 00:00–23:59 同一处）。 标签：F · 前端与界面守卫 覆盖：`web/st |
| `test_web_calendar_parity.py` | 2 | 回归守卫：签到日历只有**一份实现**（web/static/js/calendar.js）。 |
| `test_work_accounts_selection.py` | 2 | 账号页选中集的身份键：手机号，而非列表 index。 标签：F · 前端与界面守卫 覆盖：`pages/work_accounts.js`  |
| `test_web_page_consistency.py` | 1 | 页面级一致性守卫：整页唯一元素每页只出现一次 + 导航不靠客户端 tab 显隐。 |

小计 **18** 文件 / **284** 用例

## G · 安全：脱敏/审计/配置注入

| 文件 | 用例 | 覆盖 |
|---|---:|---|
| `test_rekey_key_source.py` | 92 | 回归测试（2026-08-29）：密钥来源去 cwd 依赖 + 告警通道门禁与留痕。 |
| `test_audit_chain.py` | 52 | Phase 3：审计日志 HMAC 哈希链测试。 |
| `test_masking_ssrf_gaps.py` | 37 | 对外脱敏与出站白名单的残余缺口回归（本轮对抗性审查活体复现的三条）。 |
| `test_audit_transaction.py` | 30 | 审计追责链的活体反例：同事务、判码、欠账归零、删尾。 被整改的缺陷是"写了但追不到人、丢了你不知道"：业务写与审计写永远两个事务（中间 被杀 |
| `test_env_line_break_injection.py` | 30 | .env 行分隔符注入提权回归（2026-09-07 安全审查，CRITICAL，活体复现）。 |
| `test_env_line_model_unified.py` | 21 | env 单一行模型 + 单一校验器 + 写入前后键集合 diff（web 与引擎两侧同源）。 **背景**：`.env` 过去有两套行模型— |
| `test_users_exit_surface.py` | 21 | MF-49 出口面·Task 2-9b：存储与展示面（后端侧）回归钉。 覆盖（与任务书 8 条逐项对应；前端出口由 `tests/test_ |
| `test_account_ciphertext_kid.py` | 17 | 密文带 kid（MF-50 验收不变量①）：形状、自证与 v1 兼容矩阵。 v1 密文不带密钥标识，"这把钥对不对"只能在解密撞 tag 后 |
| `test_locks.py` | 16 | 跨进程文件锁原语（`locks`，M0-5）与 D-4 回归守护。 |
| `test_config_input_caps.py` | 15 | 推送/邮件配置的写侧输入上限与"按落盘后实际类型校验"。 |
| `test_log_masking_formatter.py` | 15 | 日志输出面脱敏兜底：`MaskingFormatter` 与 `mask_phones_in_text` 的不变量。 脱敏此前靠各调用点手工 |
| `test_audit_anchor_selfcheck.py` | 14 | 审计锚点自检的活体反例：把输入改坏，工具必须响。 被整改的缺陷是"判据在自动层面等于没有"：锚点文件只判"行数变少/相等"两支， **仅追加 |
| `test_key_sources_startup_assert.py` | 12 | 启动断言"两侧读到同一把钥"（MF-50 验收不变量②）：fail-closed 与接线。 现网拓扑：web 的 YIBAN_ACCOUNT |
| `test_purge_guard.py` | 12 | 清库/删除入口的公共防线：目标指纹、确认回显、清库留痕 fail-closed。 标签：G · 安全：脱敏/审计/配置注入 覆盖：`yiba |
| `test_admin_creds_masked_ops.py` | 11 | 2026-08-20 对抗性审查修复回归测试。 |
| `test_exit_surface_repr_mail.py` | 11 | MF-49 出口面（三）：对象 repr 与邮件正文两个"formatter 够不着"的文本出口。 标签：G · 安全：脱敏/审计/配置注入 |
| `test_masking_tokens.py` | 11 | 对外脱敏（`yiban/masking.py::sanitize_text`）的凭据字面量覆盖测试（M1）。 |
| `test_audit_anchor.py` | 10 | 审计可追溯性与并发安全回归测试（2026-08-28 审查）。 |
| `test_web_mask_email_parity.py` | 10 | 前端 `YB.maskEmail` 与后端 `_mask_email` 的脱敏口径对拍（真实行为，非静态扫描）。 |
| `test_env_key_line_model.py` | 9 | `.env` 写键的行模型：折叠旧键行 + 拒绝潜伏分隔符。 |
| `test_env_writers_take_lock.py` | 9 | env` 写锁判据：锁在 `write_env_keys` 内部取得，且不存在不持锁的写入方。 背景：`write_env_keys` 曾* |
| `test_logs_export_masking.py` | 9 | 日志导出脱敏与留痕测试（2026-09-08 安全审查）。 |
| `test_cli_exit_surface.py` | 8 | MF-49 出口面（一）：`yiban.cli` 维护类子命令的 stdout/stderr 遮罩收口。 标签：G · 安全：脱敏/审计/配 |
| `test_exit_surface_generation.py` | 8 | MF-49 出口面（二）：文本直出出口的**生成点**遮罩（stderr 摘要 / --json.errors / 子进程 stdout 重 |
| `test_public_example_key_blocked.py` | 8 | 公开模板内置示例钥必须被**阻断**（精确比对 ⇒ ValueError），模板本体换占位。 实算登记：`.env.example` 曾把  |
| `test_audit_cleanup_visibility.py` | 7 | 清理量随体检/日报/取证 CLI 出箱。 |
| `test_gate_narrowing_e2e.py` | 7 | 口令门收窄（缩减批 6a）与管理员摩擦专项的管理员视角端到端钉。 功能：把"哪些操作免门免额度、哪些仍受门与额度"钉成一条管理员视角的端到端 |
| `test_account_plaintext_patch.py` | 6 | 2026-08-27 对抗性审查补丁测试：账号凭据明文驻留三缺口。 |
| `test_secret_key_strict_read.py` | 6 | ensure_secret_key` 的"全新部署/已有密钥"判定必须走严格读 + 同源一次读（MF-84）。 背景：同一形状的决策，`ac |
| `test_account_crypto_key_source.py` | 5 | 账号加密密钥的来源守卫（M3）：`load_key` 不得在"来源不确定"时自动建钥落盘。 |
| `test_log_masking_entry_mounts.py` | 5 | 三入口日志装配点的脱敏兜底挂载钉：入口初始化后，logger 链上必须在防线内。 兜底脱敏（`yiban.logging_ext.Maski |
| `test_env_write_transient_failure.py` | 4 | `.env` 原子写的瞬态失败重试（2026-09-17，Windows 上实测复现的 500）。 |
| `test_protocol_masking.py` | 4 | 协议层账号标识脱敏与登录页 key 破损的返回契约。 |
| `test_url_userinfo_masking.py` | 4 | URL userinfo 脱敏：唯一实现 + 两处调用点（回显与日志）的接线守卫。 |
| `test_env_fail_loud.py` | 3 | 2026-08-27：.env 解析快速失败（防静默重建密钥）回归测试。 |
| `test_audit_anchor_field_source.py` | 2 | 锚点行的字段同源与"链尾哈希不符→判篡改"诊断的可达性。 |
| `test_web_file_lock_slow_ops.py` | 2 | _file_lock` 锁内慢操作（SMTP / 口令哈希）的**静态判据**——登记存量，禁止新增。 标签：G · 安全：脱敏/审计/配置 |

小计 **37** 文件 / **543** 用例

## H · 通知：邮件与推送

| 文件 | 用例 | 覆盖 |
|---|---:|---|
| `test_notify_webhook.py` | 73 | `yiban/notify` Webhook 推送组件单元测试（2026-08-29）。 |
| `test_mailer.py` | 62 | mailer 邮箱通知模块单元测试（A 线：管理员告警邮件）。 |
| `test_mail_layout.py` | 29 | 邮件排版层 `yiban/mail/layout.py` 的专属测试。 |
| `test_mail_notify.py` | 27 | 邮箱通知 B 线（用户签到失败邮件）+ 用户开关测试。 |
| `test_notify_ledger.py` | 23 | 通知额度账本：磁盘持久化、跨天作废与并发不超支。 |
| `test_mail_smtp_target.py` | 22 | SMTP 目标的地址判据与发送侧日志粒度测试。 |
| `test_email_domain_review.py` | 21 | 邮箱域名黑白名单审查测试（2026-08-28 注册预拦截机制）。 |
| `test_alert_channel_dispatch.py` | 15 | 告警推送出口的判定与兜底（2026-09-08）。 |
| `test_env_mailer_tls.py` | 13 | 修复回归测试（2026-08-28 深夜）。 |
| `test_notify_throttle.py` | 10 | 回归测试：notify 同类型告警节流跨进程化（磁盘持久化）。 |
| `test_mail_send_never_raises.py` | 4 | transport._send` 的"静默失败"契约回归：坏字符不再把异常抛回锁内调用方。 标签：H · 通知：邮件与推送 覆盖：正文/主题 |

小计 **11** 文件 / **299** 用例

## I · 容量、熔断与账号有效性

| 文件 | 用例 | 覆盖 |
|---|---:|---|
| `test_capacity_limits.py` | 32 | 容量口径与容量上限设置（2026-09-08）。 |
| `test_breaker.py` | 25 | 账密熔断器（circuit breaker）测试：v0.18.4 核心行为防回归。 |
| `test_capacity_of.py` | 22 | 容量口径：`schedule.capacity_of` / `schedule.executor_count` 与四处调用点。 标签：I · |
| `test_account_liveness_gate.py` | 10 | 运行期账号有效性复核。 |
| `test_capacity_audit_scope.py` | 10 | 账号容量口径：**未通过审核的账号不占容量**（2026-09-15 修订）。 |

小计 **5** 文件 / **99** 用例

## J · 运维：部署/备份/发布

| 文件 | 用例 | 覆盖 |
|---|---:|---|
| `test_deploy_paths_and_restore_verdict.py` | 30 | 部署路径的解析口径与恢复件核验的结论分类。 |
| `test_cli_contract_mf60.py` | 23 | MF-60 契约用例（一）：`--json` 在任何退出路径上都必须留下可解析的一整行结构化对象。 标签：J · 运维：部署/备份/发布 覆 |
| `test_deploy_prod_artifacts.py` | 22 | 生产执行件入库与部署断言（M3 批次0：执行件入版本控制（高）/ 基线拉不到（仓内半条）/ 真名示例）。 标签：J · 运维：部署/备份/发 |
| `test_backup_sentinel.py` | 21 | scripts/backup_sentinel.py` 与 `scripts/yiban-backup-sentinel.sh` 的契约用例 |
| `test_backup_e2e.py` | 17 | backup.sh 的活体端到端契约（加密可解才删明文 / 明文模式护栏 / tar 护栏判码 / 轮转下界 / 行为断言取代源码文本断言） |
| `test_cli_contract.py` | 17 | `yiban.cli` 的契约用例（`docs/dev/cli.md` §2/§3 的可执行版本）。 |
| `test_run_sh_logging.py` | 16 | run.sh` 的日志与退出码契约：**任何**退出路径都留一行带退出码的日志。 标签：J · 运维：部署/备份/发布 覆盖：flock 弹 |
| `test_backup_flag_parsing.py` | 11 | backup.sh 旗标解析：全参数扫描、未知/移位即拒绝、`--require-encrypt` 位置无关。 标签：J · 运维：部署/备 |
| `test_env_export_line_divergence.py` | 9 | export KEY=value` 行在三个 .env 读取方的解析分叉——双向钉死收敛不变量。 标签：J · 运维：部署/备份/发布 ·  |
| `test_demo_data_guard.py` | 8 | demo 数据生成的清库防线：目标指纹 + 确认 + 清库留痕 + dry-run 真不删。 标签：J · 运维：部署/备份/发布 覆盖：` |
| `test_subpath_deploy.py` | 8 | 子路径 / 独立子域 前缀自适应部署契约回归测试（2026-08-23）。 |
| `test_release_version_source.py` | 6 | 版本号单一来源 + 轮次横幅带版本号（发布门槛的自证前提）。 |
| `test_window_alert_reset.py` | 6 | 窗口告警去重标记的业务日复位点（三个标记逐一）。 标签：J · 运维：部署/备份/发布 覆盖：`yiban/engine/schedule. |
| `test_engine_shell_forwarding.py` | 5 | 兼容壳的**打桩转发**有效性（引擎按"执行一轮"切分后的收口自证）。 |
| `test_maintenance_readonly.py` | 5 | 维护脚本的只读承诺：读账号/库的维护脚本不得经 `init_db` 建库/建表/切 WAL。 标签：J · 运维：部署/备份/发布 覆盖：` |
| `test_backup_require_encrypt.py` | 4 | backup.sh 契约（2026-09-08，静态源码核验）。 |
| `test_deploy_entry_imports.py` | 4 | 部署入口脚本的导入条件：只在 `scripts/` 入 sys.path 时也必须能导入。 |
| `test_runsh_env_parse.py` | 4 | shell 侧 .env 解析契约（2026-09-08 起；2026-09-15 扩到 run_probe.sh）。 |
| `test_docker_image_contents.py` | 1 | Docker 镜像内容门禁：源码 COPY 必须覆盖运行时真正导入的本地顶级模块。 |

小计 **19** 文件 / **217** 用例

## K · 登录协议与第三方隔离

| 文件 | 用例 | 覆盖 |
|---|---:|---|
| `test_login_protocol_shape.py` | 28 | 登录与签到**协议形状**的保护存在性断言（第三方层抽取前后都必须绿）。 |
| `test_fyiban_isolation.py` | 20 | 第三方隔离层（`yiban/fyiban/`）的**保护存在性断言**与边界守卫。 |
| `test_fyiban_concave_sampling.py` | 18 | 凹围栏采样：剪耳剖分路径的出界保证、绕序变化与退化输入边界。 |
| `test_login_e2e_mock.py` | 5 | 登录 / 签到链的**端到端演练**：真 HTTP 往返 + 假易班服务端（绝不碰真实易班）。 |

小计 **4** 文件 / **71** 用例

## L · 注销与软删

| 文件 | 用例 | 覆盖 |
|---|---:|---|
| `test_user_deregistration_web.py` | 31 | 用户自助注销 Web/API 层测试（数据库 v5 软删除对接）。 |
| `test_user_soft_delete.py` | 12 | 用户删除账号软删化（2026-08-28 用户裁决）回归测试。 |
| `test_user_deregistration_db.py` | 10 | 用户主动注销：数据库层测试（软删除 + 宽限期 + 邮箱复用）。 |
| `test_delete_timeline_consistency.py` | 9 | 注销（用户软删）与账号软删的**保留期与恢复口径**一致性。 |

小计 **4** 文件 / **62** 用例

## M · 架构分层与基础设施

| 文件 | 用例 | 覆盖 |
|---|---:|---|
| `test_infra_layer.py` | 7 | `yiban/infra/` 层的边界守卫（依赖单向、无重复实现）。 |

小计 **1** 文件 / **7** 用例

## 总计

**182** 文件 / **3414** 收集用例。

> 无 `标签：` 行的文件（按组名关键词兜底归组，待补标签）：`test_gate_narrowing_e2e.py`。
