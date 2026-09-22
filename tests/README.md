# 测试目录说明

本目录为 pytest 测试套件（`testpaths = ["tests"]`，由 `pyproject.toml` 配置）。测试文件按**功能域**命名（`test_<功能>.py`），便于按需运行与定位。带批次/日期后缀的一次性锁文件已并入对应主题测试（并入关系见各目标文件头部说明）。

## 运行方式

```bash
# 全量（串行，约 8 分钟）
python -m pytest tests/ -q

# 全量（并发，约 2-3 分钟，需 pytest-xdist）
python -m pytest tests/ -q -n auto

# 按功能域分类运行（文件前缀 = 功能域）
python -m pytest tests/test_notify_*.py   # 消息推送与告警
python -m pytest tests/test_web_*.py      # 网页管理后台
python -m pytest tests/test_scheduler*.py # 调度器
python -m pytest tests/test_db_*.py       # 数据库与迁移
python -m pytest tests/test_signin*.py    # 签到核心

# 单个文件
python -m pytest tests/test_db_integrity.py -v
```

> 说明：本仓库未在 `pyproject.toml` 注册自定义 markers（`-m` 过滤不可用），按文件名前缀分域运行。
> `scripts/` 下的一次性运维脚本不作为 pytest 收集目标（`testpaths = ["tests"]`）。

## 文件清单（按功能域分组）

### 冒烟 / 端到端
| 文件 | 说明 |
|---|---|
| `test_db_integrity.py` | 数据完整性、迁移修复与核心路径冒烟 |
| `test_state_gc.py` | 按日状态文件保留期清理与时钟守卫 |
| `test_verify_jobs.py` | 在线校验任务生命周期、失败冷却与并发闸 |
| `test_subpath_deploy.py` | 子路径 / 独立子域前缀自适应部署契约 |
| `test_static_routes.py` | 静态资源与合规页路由（favicon / 备案图标 / robots / 错误页 / 合规文档渲染） |
| `test_sign_events.py` | sign_events 表与可视化三表聚合 |

### 网页管理后台（web/）
| 文件 | 说明 |
|---|---|
| `test_admin_privilege_web.py` | 管理员目标操作权限 |
| `test_admin_creds_masked_ops.py` | 管理员空凭据拒绝、手机号脱敏操作（删除/批量/审核） |
| `test_web_api_security.py` | Web API 安全边界：越权（IDOR）、mass assignment、输入校验、限速 |
| `test_web_auth_security.py` | Web 认证 / 授权 / 安全配置（0.21.0 修复） |
| `test_web_security_gates.py` | web 修复验证：重置密码门禁、日志 handler、版本同步 |
| `test_account_abuse_gate.py` | 被盗号滥用面加固：告警节流、高危操作门禁 |
| `test_session.py` | 会话缓存表族与会话恢复（SID 吊销 / 恢复保持有效） |
| `test_registration_pause.py` | 暂停注册（v0.26.3）+ web 日志落盘 |
| `test_review_flow.py` | 账号审核流转 API |
| `test_email_domain_review.py` | 邮箱域名黑白名单审查（注册预拦截） |
| `test_user_deregistration_web.py` | 用户自助注销 Web/API 层 |
| `test_user_soft_delete.py` | 用户删除账号软删化 |
| `test_global_pause.py` | 全局暂停（一键暂停签到） |
| `test_logs_by_date.py` | 按天日志读取与按日期查看 |
| `test_logs_export_masking.py` | 日志导出脱敏副本（与视图同一过滤管线）+ 审计留痕 + 每 IP 限速；写入侧裸号/裸 IP 收口 |
| `test_env_line_break_injection.py` | .env 行分隔符注入提权（宽/窄行模型、旧键折叠、主凭据歧义 fail-closed、启动歧义检测） |
| `test_capacity_limits.py` | 容量口径单档化与容量上限设置：保存门单门化（活跃账号数）、裸账号占配额、max_users/max_accounts 主管理员专属/钳位/携带才写/热读、potential_load |
| `test_webui_inline_context.py` | 前端注入面契约：police_link scheme 白名单、`<script>` 内 script_root tojson、onclick/onchange 不拼用户可控值（data-* + 事件委托） |

### 签到核心（scripts/signin.py、run.sh）
| 文件 | 说明 |
|---|---|
| `test_saturday_sign.py` | 周六签到开关 |
| `test_multi_task.py` | 多任务「随机选点、任一成功即停」 |
| `test_no_position.py` | 无点位账号独立状态（用户裁决） |
| `test_host_exit_semantics.py` | 宿主 run.sh 补签闸门退出码语义 |
| `test_breaker.py` | 账密熔断器（circuit breaker）核心行为 |
| `test_account_plaintext_patch.py` | 账号凭据明文驻留三缺口补丁 |
| `test_sign_round_guards.py` | 签到轮守卫（补签定向重跑/探针暂停门/死号先判后睡/超时冲刷汇总/晚到首签告警） |
| `test_manual_sign_reporting.py` | 手动签到退出码透传留痕 + 账号编辑仅凭据变更清熔断 |

### 调度器（docker/scheduler.py、调度 v2）
| 文件 | 说明 |
|---|---|
| `test_container_scheduler.py` | 容器内签到调度器回归 |
| `test_schedule_v2.py` | 调度 v2 build_schedule 统一填充框架 |
| `test_time_prefs.py` | 自选时间片全链路（db + 调度 + API） |
| `test_scheduler_gate.py` | 调度闸门 + 零成功告警 + 调度落盘标记（--only 过滤、补签轮判定、槽位标记） |
| `test_effective_window.py` | 业务钟与签到窗口判定（北京时钟、非 5 分钟整数倍窗口的自选片边界） |
| `test_schedule_retry.py` | 重试重排与补签时刻（重试落点、重试槽位注入、最小执行间隔） |
| `test_scheduler_env_probe.py` | 调度 env 合并 + 探针状态锁 |
| `test_probe.py` | 探针模式 + 注册时账号验证 |
| `test_env_fail_loud.py` | .env 解析快速失败（防静默重建密钥） |

### 消息推送与告警（yiban/notify、yiban/mail）
| 文件 | 说明 |
|---|---|
| `test_notify_webhook.py` | yiban/notify Webhook 推送组件单元测试 |
| `test_notify_ledger.py` | 通知额度账本：磁盘持久化、跨进程共享额度与单次文件锁临界区（RMW 竞态） |
| `test_notify_throttle.py` | 同类型告警节流跨进程化 |
| `test_alert_channel_dispatch.py` | 推送通道出口判定（notify.is_configured）+ signin 即时告警门控 + 汇总无收件人推送兜底 |
| `test_mailer.py` | 邮箱通知模块单元（A 线：管理员告警）+ 通道三态判定 |
| `test_mail_notify.py` | 邮箱通知 B 线（用户签到失败邮件）+ 用户开关 |
| `test_mailer.py` | 邮箱通知模块单元（A 线：管理员告警）+ SMTP 条目列表化与 failover + 通道三态日报 |
| `test_web_mask_email_parity.py` | 告警收件人网页可编辑（写入/多地址/清空/口令门禁/旧收件人变更通知）+ 站点分享摘要 meta 与 og 标签 |

### 数据库与迁移（scripts/db.py）
| 文件 | 说明 |
|---|---|
| `test_db_migrations.py` | 通用幂等迁移框架（含 v13 畸形列声明修复与 _ensure_column 防复发） |
| `test_db_integrity.py` | 数据完整性与迁移修复（0.21.0 Task 3） |
| `test_db_owner_constraint.py` | 每人限 1 账号 DB 约束 |
| `test_audit_chain.py` | 审计日志 HMAC 哈希链 |
| `test_audit_anchor.py` | 审计可追溯性与并发安全 |
| `test_user_deregistration_db.py` | 用户注销数据库层（软删除 + 宽限期） |
| `test_locks.py` | 跨进程锁族：共享 .env 文件锁、密钥生成竞态、锁降级告警留痕 |

### 辅助脚本
| 文件 | 说明 |
|---|---|
| `test_env_mailer_tls.py` | 子进程 env + mailer TLS 上下文 |
| `test_rekey_key_source.py` | 密钥来源去 cwd 依赖 + rekey 迁移推送/邮件密文 + 通道自检 |
| `test_backup_require_encrypt.py` | backup.sh 契约（静态核验 + bash -n）：--require-encrypt 与异机副本解耦、加密失败 fail-closed 清场 |
| `test_runsh_env_parse.py` | run.sh 契约（静态核验 + bash -n）：.env 解析去 BOM、key/value 剥空白对齐 env_io |

### 用户操作相关
| 文件 | 说明 |
|---|---|
| `test_sign_round_guards.py` | 签到轮守卫 + 批量手动签到冷却（30 分钟默认，可配置） |

## 命名规范

- 文件按**功能域**命名 `test_<功能>.py`，不带审查批次/日期后缀；
- 带批次/日期后缀的一次性锁文件已把仍有效断言并入主题测试文件后删除
  （如 `test_notify_ledger_disk/race` → `test_notify_ledger`，`test_smoke` → `test_db_integrity`）；
- 拆分契约守卫按域合并：`test_web_boundary.py`（web 服务/渲染/安全域）、`test_store_boundary.py`（store 门面转发）、
  `test_env_key_line_model.py`（`.env` 写键行模型）；
- git 历史可经 `git log --follow tests/<文件>.py` 追溯旧名。
