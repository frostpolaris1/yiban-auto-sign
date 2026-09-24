# 测试项目清单（按功能分组，2026-09-23）

> 自动生成：扫 `tests/test_*.py` 的模块 docstring 首句 + 用例方法数（`ast` 计数，非 pytest 收集数）。
> 当前：**126 个文件 / 2667 个用例方法**。

## 怎么只跑一组

```bash
cd /mnt/d/code/yiban-wt-schedv3 && source ~/.venv-yiban-wsl/bin/activate
python -m pytest tests/test_planner.py tests/test_hrw.py -q          # 指定文件
python -m pytest tests/ -q -k mine_or_planner_or_hrw               # 按关键字
```

## A · 调度：计划与分片

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_effective_window.py` | 27 | 有效窗口单一口径（排计划 / 判关闭 / 算容量同源）。 |
| `test_hrw.py` | 11 | `yiban.engine.hrw` 的契约用例：纯函数 HRW 分工（虚分片 + argmax 归属）。 |
| `test_planner.py` | 52 | `yiban/engine/planner.py`（双粒度分片 Planner）与 `schedule.capacity_accounts_v3` 的契约用例。 |
| `test_schedule_v2.py` | 23 | 调度 v2（S1 demo）build_schedule 统一填充框架测试。 |
| `test_slot_geometry.py` | 11 | 自选片几何的准绳一致性：`schedule._slot_to_bi` / web `_pref_slots` 与 `_schedule_blocks` 同源。 |
| `test_time_prefs.py` | 69 | 调度 v2（S2/S3）自选时间片全链路测试：db 层 + 调度层 + API 层。 |
| `test_window_edge_clamp.py` | 11 | 缓冲过大时收缩缓冲、保留窗口（`yiban/window.bounds` 的退化处置）。 |

小计 **7** 文件 / **204** 用例

## B · 调度：领取/队列/执行体

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_claims.py` | 25 | 签到领取池与账号级租约（`yiban/store/claims.py`，v17）的行为与并发断言。 |
| `test_claims_fencing.py` | 17 | fencing epoch：领取自增、**所有终态写**都带 `epoch=?`，以及 `try_claim` 的 fail-closed。 |
| `test_container_scheduler.py` | 43 | 容器内签到调度器（docker/scheduler.py）回归测试。 |
| `test_egress_and_executors_api.py` | 70 | 出口分配（`yiban/egress.py`）与执行体接口（`/api/scheduler/executors`）的断言。 |
| `test_executor_manifest.py` | 45 | **执行体清单**（`YIBAN_EXECUTORS`）的模型、迁移等价与行接口断言。 |
| `test_executor_write_guard.py` | 14 | 执行体写操作的**口令门**与行的**自定义名**（2026-09-17）。 |
| `test_executors_kpi_scope.py` | 19 | 执行体分区的口径说明，用源级断言钉住。 |
| `test_fallback_gates.py` | 21 | 兜底常驻执行体的三道门与窗口边界（2026-09-17 实测缺陷的钉版回归）。 |
| `test_host_exit_semantics.py` | 18 | 回归测试：宿主 run.sh 补签闸门不被「部分成功」吞掉。 |
| `test_multi_executor_engine.py` | 17 | 多执行体的**引擎侧**行为断言：领取池进入执行循环后的分派与了结语义。 |
| `test_run_sh_workers.py` | 16 | `run.sh` 的多执行体开关（`YIBAN_WORKERS`）外壳行为。 |
| `test_schedule_retry.py` | 37 | 重试重排与补签时刻：重试落点、槽位时刻与宿主脚本契约。 |
| `test_scheduler_env_probe.py` | 19 | 对抗性审查修复回归测试（v0.24.3，2026-08-27）。 |
| `test_scheduler_gate.py` | 57 | 修复回归测试（对抗性审查 2026-08-29）。 |
| `test_sign_round_guards.py` | 21 | 签到轮守卫回归（2026-09-08）。 |
| `test_store_queue.py` | 16 | `yiban/store/queue_store.py`：sign_tasks 的批量领取 / 批量收尾 / 重排 / 当日计数。 |
| `test_supervisor_recursion_guard.py` | 4 | 多执行体的**子进程不得再当监督进程**（2026-09-17 对抗性审查 H1 的钉版回归）。 |
| `test_token_bucket.py` | 31 | `yiban/engine/token_bucket.py` 的契约用例：GCRA/TAT 令牌桶、AIMD、全局 Λ、gap 门、EWMA。 |
| `test_yiban_fallback_sh.py` | 5 | 兜底常驻执行体外壳（`scripts/yiban-fallback.sh`）的行为断言。 |

小计 **19** 文件 / **495** 用例

## C · 存储：迁移与库完整性

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_db_integrity.py` | 46 | Task 3：DB 层数据完整性与迁移修复测试。 |
| `test_db_migrations.py` | 12 | Phase 0：通用幂等迁移框架测试。 |
| `test_db_owner_constraint.py` | 7 | Phase 2：每人限 1 账号 DB 约束 + 可区分错误码测试。 |
| `test_ledger_check.py` | 12 | `scripts/ledger_check.py` 的契约用例：三项检查、退出码 `0/1/2`、白名单补项。 |
| `test_migration_compat.py` | 3 | 升级兼容硬门：旧代码打开新库（零写入）与「既有表零变更」。 |
| `test_migrations_v18.py` | 8 | v18 迁移：持久化任务队列 `sign_tasks` + 出口令牌桶状态 `egress_state`。 |
| `test_migrations_v19.py` | 7 | v19 迁移：`sign_claims` 补 fencing token 列 `epoch`（`sign_tasks.epoch` 兜底补齐）。 |
| `test_migrations_v20.py` | 12 | v20 backfill：把 `sign-state-*.json` 里的**终态**补进 `sign_tasks`。 |
| `test_session.py` | 37 | 会话缓存表族与会话恢复：有效期判定、凭据加密、重启后恢复。 |
| `test_sign_events.py` | 12 | 签到事件表 `sign_events`：写入、按天聚合与前端统计口径。 |
| `test_store_boundary.py` | 140 | store 层拆分边界：门面读写转发 + 表级 CRUD 真实可用。 |

小计 **11** 文件 / **296** 用例

## D · 状态词汇与账号生命周期

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_global_pause.py` | 3 | 全局暂停（一键暂停签到）测试。 |
| `test_multi_task.py` | 4 | 多任务「随机选点、任一成功即停」签到语义测试（2026-08-29）。 |
| `test_no_position.py` | 19 | 无点位账号独立状态回归测试（2026-09-01，用户裁决）。 |
| `test_probe.py` | 17 | 探针模式 + 注册时账号验证测试（v0.23.x）。 |
| `test_saturday_sign.py` | 11 | 周六签到开关测试（2026-08-29；2026-09-07 v0.29.0 默认语义反转：默认关闭）。 |
| `test_state_file_writes.py` | 8 | 状态文件写盘的原子性（DAT-7）与"标记损坏"的失效方向。 |
| `test_state_gc.py` | 37 | 按天状态文件的清理策略（`yiban/state_gc.py`）与两处调用点。 |
| `test_status_sets.py` | 25 | 状态集合的单一事实源：JSON 状态码全集、了结/未了结划分、「今日不必再签」三套词表。 |
| `test_yiban_status_single_source.py` | 4 | 状态词汇表单一事实源（M1）：`yiban.status` 定义，signin / web 只做别名引用。 |

小计 **9** 文件 / **128** 用例

## E · Web：认证/权限/API

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_account_abuse_gate.py` | 20 | 被盗号滥用面加固回归测试（2026-08-29）。 |
| `test_admin_privilege_web.py` | 11 | 管理员目标操作权限测试（安全审查 2026-08 修复验证）。 |
| `test_builtin_admin_sid.py` | 11 | 内置主管理员（.env 账号）的服务端会话吊销面。 |
| `test_manual_sign_reporting.py` | 6 | 手动签到与账号编辑守卫回归（2026-09-08）。 |
| `test_rate_limit_tiers.py` | 5 | 全局限速分级（用户实拍：快速切页 /api/* 触发 429）。 |
| `test_registration_pause.py` | 13 | 暂停注册（v0.26.3）+ web 日志落盘（DailyFlockFileHandler）测试。 |
| `test_review_flow.py` | 12 | 账号审核流转 API 测试（锁定 2026-08-16 审查轮 ACCOUNT_STATUS_* 改名行为）。 |
| `test_static_routes.py` | 25 | 静态资源与合规页路由：自定义优先、缺失 404、短缓存头。 |
| `test_switch_password_gate.py` | 12 | 系统开关口令门禁测试（global_pause / registration_pause 变更需 confirm_password）。 |
| `test_verify_jobs.py` | 66 | 在线校验任务：生命周期、失败冷却与并发闸。 |
| `test_web_api_security.py` | 13 | 对抗性测试：用户体验不误杀 + 安全边界。 |
| `test_web_auth_security.py` | 68 | 0.21.0 Task 2：Web 认证/授权/安全配置修复测试。 |
| `test_web_security_gates.py` | 81 | 对抗性审查修复验证（2026-09-01）。 |
| `test_webui_inline_context.py` | 9 | Web 前端注入面契约（2026-09-08）。 |

小计 **14** 文件 / **352** 用例

## F · 前端与界面守卫

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_logs_by_date.py` | 23 | 按天日志（sign-YYYY-MM-DD.log）读取与按日期查看功能测试。 |
| `test_settings_tiers_frontend_parity.py` | 6 | 档位表与设置页前端控件的双向对拍。 |
| `test_web_boundary.py` | 162 | web 层拆分边界：名字面完整 + 转发落到 app 模块级真状态。 |
| `test_web_calendar_parity.py` | 8 | 回归守卫：签到日历只有**一份实现**（web/static/js/calendar.js）。 |
| `test_web_class_hygiene.py` | 1 | 回归守卫（2026-09-10，V3-5b）：class 属性里 `dark:` 变体的**作用域**与**重复**。 |
| `test_web_component_adoption.py` | 8 | 回归守卫（2026-09-10，V3-5c）：**组件层是唯一事实源** —— 不得再绕过它裸写。 |
| `test_web_design_tokens.py` | 6 | 回归守卫（2026-09-10，V3-5）：设计令牌的两类"静默失败"。 |
| `test_web_font_closure.py` | 6 | 回归守卫：中文字体栈收口（防止中文回退到宋体 SimSun）。 |
| `test_web_js_modules.py` | 18 | 前端脚本装配守卫（多页 MPA + 组件化后的等价判据）。 |
| `test_web_page_consistency.py` | 3 | 页面级一致性守卫：整页唯一元素每页只出现一次 + 导航不靠客户端 tab 显隐。 |
| `test_web_render_golden.py` | 6 | Web 模板渲染「金标准」回归测试（2026-09-10 前端模块化护栏）。 |
| `test_web_text_contrast.py` | 7 | 回归守卫（2026-09-10）：Web「次要文字」的深浅档位不得写反。 |

小计 **12** 文件 / **254** 用例

## G · 安全：脱敏/审计/配置注入

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_account_crypto_key_source.py` | 5 | 账号加密密钥的来源守卫（M3）：`load_key` 不得在"来源不确定"时自动建钥落盘。 |
| `test_account_plaintext_patch.py` | 6 | 2026-08-27 对抗性审查补丁测试：账号凭据明文驻留三缺口。 |
| `test_admin_creds_masked_ops.py` | 11 | 2026-08-20 对抗性审查修复回归测试。 |
| `test_audit_anchor.py` | 15 | 审计可追溯性与并发安全回归测试（2026-08-28 审查）。 |
| `test_audit_anchor_field_source.py` | 3 | 锚点行的字段同源与"篡改/重链"诊断的可达性。 |
| `test_audit_chain.py` | 54 | Phase 3：审计日志 HMAC 哈希链测试。 |
| `test_audit_cleanup_visibility.py` | 7 | 清理量随体检/日报/取证 CLI 出箱。 |
| `test_config_input_caps.py` | 15 | 推送/邮件配置的写侧输入上限与"按落盘后实际类型校验"。 |
| `test_env_fail_loud.py` | 3 | 2026-08-27：.env 解析快速失败（防静默重建密钥）回归测试。 |
| `test_env_key_line_model.py` | 13 | `.env` 写键的行模型：折叠旧键行 + 拒绝潜伏分隔符。 |
| `test_env_line_break_injection.py` | 30 | .env 行分隔符注入提权回归（2026-09-07 安全审查，CRITICAL，活体复现）。 |
| `test_env_write_transient_failure.py` | 4 | `.env` 原子写的瞬态失败重试（2026-09-17，Windows 上实测复现的 500）。 |
| `test_locks.py` | 16 | 跨进程文件锁原语（`locks`，M0-5）与 D-4 回归守护。 |
| `test_logs_export_masking.py` | 9 | 日志导出脱敏与留痕测试（2026-09-08 安全审查）。 |
| `test_masking_ssrf_gaps.py` | 33 | 对外脱敏与出站白名单的残余缺口回归（本轮对抗性审查活体复现的三条）。 |
| `test_masking_tokens.py` | 5 | 对外脱敏（`yiban/masking.py::sanitize_text`）的凭据字面量覆盖测试（M1）。 |
| `test_protocol_masking.py` | 4 | 协议层账号标识脱敏与登录页 key 破损的返回契约。 |
| `test_rekey_key_source.py` | 92 | 回归测试（2026-08-29）：密钥来源去 cwd 依赖 + 告警通道门禁与留痕。 |
| `test_url_userinfo_masking.py` | 6 | URL userinfo 脱敏：唯一实现 + 两处调用点（回显与日志）的接线守卫。 |
| `test_web_mask_email_parity.py` | 9 | 前端 `YB.maskEmail` 与后端 `_mask_email` 的脱敏口径对拍（真实行为，非静态扫描）。 |

小计 **20** 文件 / **340** 用例

## H · 通知：邮件与推送

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_alert_channel_dispatch.py` | 14 | 告警推送出口的判定与兜底（2026-09-08）。 |
| `test_email_domain_review.py` | 19 | 邮箱域名黑白名单审查测试（2026-08-28 注册预拦截机制）。 |
| `test_env_mailer_tls.py` | 13 | 修复回归测试（2026-08-28 深夜）。 |
| `test_mail_layout.py` | 27 | 邮件排版层 `yiban/mail/layout.py` 的专属测试。 |
| `test_mail_notify.py` | 33 | 邮箱通知 B 线（用户签到失败邮件）+ 用户开关测试。 |
| `test_mail_smtp_target.py` | 22 | SMTP 目标的地址判据与发送侧日志粒度测试。 |
| `test_mailer.py` | 47 | mailer 邮箱通知模块单元测试（A 线：管理员告警邮件）。 |
| `test_notify_ledger.py` | 22 | 通知额度账本：磁盘持久化、跨天作废与并发不超支。 |
| `test_notify_throttle.py` | 10 | 回归测试：notify 同类型告警节流跨进程化（磁盘持久化）。 |
| `test_notify_webhook.py` | 69 | `yiban/notify` Webhook 推送组件单元测试（2026-08-29）。 |

小计 **10** 文件 / **276** 用例

## I · 容量、熔断与账号有效性

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_account_liveness_gate.py` | 13 | 运行期账号有效性复核。 |
| `test_breaker.py` | 25 | 账密熔断器（circuit breaker）测试：v0.18.4 核心行为防回归。 |
| `test_capacity_audit_scope.py` | 10 | 账号容量口径：**未通过审核的账号不占容量**（2026-09-15 修订）。 |
| `test_capacity_limits.py` | 31 | 容量口径与容量上限设置（2026-09-08）。 |

小计 **4** 文件 / **79** 用例

## J · 运维：部署/备份/发布

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_backup_require_encrypt.py` | 4 | backup.sh 契约（2026-09-08，静态源码核验）。 |
| `test_cli_contract.py` | 17 | `yiban.cli` 的契约用例（`docs/dev/cli.md` §2/§3 的可执行版本）。 |
| `test_deploy_entry_imports.py` | 4 | 部署入口脚本的导入条件：只在 `scripts/` 入 sys.path 时也必须能导入。 |
| `test_deploy_paths_and_restore_verdict.py` | 22 | 部署路径的解析口径与恢复件核验的结论分类。 |
| `test_docker_image_contents.py` | 2 | Docker 镜像内容门禁：源码 COPY 必须覆盖运行时真正导入的本地顶级模块。 |
| `test_engine_shell_forwarding.py` | 5 | 兼容壳的**打桩转发**有效性（引擎按"执行一轮"切分后的收口自证）。 |
| `test_loadtest_tools.py` | 29 | loadtest 工具链轻量冒烟测试（秒级，不进常规重负载）。 |
| `test_module_size_gate.py` | 7 | 模块化门禁：按类型设目标行数 + 超限必须写明工程理由。 |
| `test_release_version_source.py` | 6 | 版本号单一来源 + 轮次横幅带版本号（发布门槛的自证前提）。 |
| `test_runsh_env_parse.py` | 4 | shell 侧 .env 解析契约（2026-09-08 起；2026-09-15 扩到 run_probe.sh）。 |
| `test_subpath_deploy.py` | 8 | 子路径 / 独立子域 前缀自适应部署契约回归测试（2026-08-23）。 |

小计 **11** 文件 / **108** 用例

## K · 登录协议与第三方隔离

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_fyiban_concave_sampling.py` | 18 | 凹围栏采样：剪耳剖分路径的出界保证、绕序变化与退化输入边界。 |
| `test_fyiban_isolation.py` | 20 | 第三方隔离层（`yiban/fyiban/`）的**保护存在性断言**与边界守卫。 |
| `test_login_e2e_mock.py` | 5 | 登录 / 签到链的**端到端演练**：真 HTTP 往返 + 假易班服务端（绝不碰真实易班）。 |
| `test_login_protocol_shape.py` | 24 | 登录与签到**协议形状**的保护存在性断言（第三方层抽取前后都必须绿）。 |

小计 **4** 文件 / **67** 用例

## L · 注销与软删

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_delete_timeline_consistency.py` | 9 | 注销（用户软删）与账号软删的**保留期与恢复口径**一致性。 |
| `test_user_deregistration_db.py` | 10 | 用户主动注销：数据库层测试（软删除 + 宽限期 + 邮箱复用）。 |
| `test_user_deregistration_web.py` | 30 | 用户自助注销 Web/API 层测试（数据库 v5 软删除对接）。 |
| `test_user_soft_delete.py` | 12 | 用户删除账号软删化（2026-08-28 用户裁决）回归测试。 |

小计 **4** 文件 / **61** 用例

## M · 架构分层与基础设施

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_infra_layer.py` | 7 | `yiban/infra/` 层的边界守卫（依赖单向、无重复实现）。 |

小计 **1** 文件 / **7** 用例
