# 测试目录说明

本目录为 pytest 测试套件（`testpaths = ["tests"]`，由 `pyproject.toml` 配置）。测试文件按**功能域**命名（`test_<功能>.py`），便于按需运行与定位；部分文件保留了批次/审查代号作为前缀或后缀的语义继承（见各文件 docstring 首行）。

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
python -m pytest tests/test_smoke.py -v
```

> 说明：本仓库未在 `pyproject.toml` 注册自定义 markers（`-m` 过滤不可用），按文件名前缀分域运行。
> `scripts/` 下的一次性运维脚本不作为 pytest 收集目标（`testpaths = ["tests"]`）。

## 文件清单（按功能域分组）

### 冒烟 / 端到端
| 文件 | 说明 |
|---|---|
| `test_smoke.py` | 核心路径冒烟（单人维护用）：改完代码跑一遍防回归 |
| `test_subpath_deploy.py` | 子路径 / 独立子域前缀自适应部署契约 |
| `test_legal_doc_render.py` | 合规文档渲染（_render_md / _doc_page） |
| `test_visual_tables.py` | 可视化三表（sign_events / page_visits / server_metrics） |

### 网页管理后台（web/）
| 文件 | 说明 |
|---|---|
| `test_admin_privilege_web.py` | 管理员目标操作权限 |
| `test_admin_creds_masked_ops.py` | 管理员空凭据拒绝、手机号脱敏操作（删除/批量/审核） |
| `test_web_api_security.py` | Web API 安全边界：越权（IDOR）、mass assignment、输入校验、限速 |
| `test_web_auth_security.py` | Web 认证 / 授权 / 安全配置（0.21.0 修复） |
| `test_web_security_gates.py` | web 修复验证：重置密码门禁、日志 handler、版本同步 |
| `test_account_abuse_gate.py` | 被盗号滥用面加固：告警节流、高危操作门禁 |
| `test_session_restore.py` | 会话恢复（SID 吊销 / 恢复保持有效） |
| `test_registration_pause.py` | 暂停注册（v0.26.3）+ web 日志落盘 |
| `test_review_flow.py` | 账号审核流转 API |
| `test_email_domain_review.py` | 邮箱域名黑白名单审查（注册预拦截） |
| `test_user_deregistration_web.py` | 用户自助注销 Web/API 层 |
| `test_user_soft_delete.py` | 用户删除账号软删化 |
| `test_webui_stats_db.py` | WebUI 统计/监控 DB 补齐 |
| `test_global_pause.py` | 全局暂停（一键暂停签到） |
| `test_logs_by_date.py` | 按天日志读取与按日期查看 |
| `test_logs_export_masking.py` | 日志导出脱敏副本（与视图同一过滤管线）+ 审计留痕 + 每 IP 限速；写入侧裸号/裸 IP 收口 |
| `test_env_line_break_injection.py` | .env 行分隔符注入提权（宽/窄行模型、旧键折叠、主凭据歧义 fail-closed、启动歧义检测） |
| `test_capacity_limits.py` | 容量口径单档化与容量上限设置：保存门单门化（活跃账号数）、裸账号占配额、max_users/max_accounts 主管理员专属/钳位/携带才写/热读、potential_load |
| `test_webui_inline_context.py` | 前端注入面契约：police_link scheme 白名单、`<script>` 内 script_root tojson、onclick/onchange 不拼用户可控值（data-* + 事件委托） |

### 签到核心（scripts/signin.py、run.sh）
| 文件 | 说明 |
|---|---|
| `test_signin_fixes.py` | 签到核心修复（0.21.0 Task 4） |
| `test_saturday_sign.py` | 周六签到开关 |
| `test_multi_task.py` | 多任务「随机选点、任一成功即停」 |
| `test_no_position.py` | 无点位账号独立状态（用户裁决） |
| `test_host_exit_semantics.py` | 宿主 run.sh 补签闸门退出码语义 |
| `test_breaker.py` | 账密熔断器（circuit breaker）核心行为 |
| `test_account_plaintext_patch.py` | 账号凭据明文驻留三缺口补丁 |
| `test_batch19_knife6b_0908.py` | 业务逻辑缺陷修复回归（补签定向重跑/探针暂停门/熔断防误清/死号先判后睡/手动签到退出码透传/超时冲刷汇总/晚到首签告警） |

### 调度器（docker/scheduler.py、调度 v2）
| 文件 | 说明 |
|---|---|
| `test_container_scheduler.py` | 容器内签到调度器回归 |
| `test_batch19_knife6b_0908.py` | 含容器调度槽位落盘标记（重启不二次触发）与 `_child_timeout` 下限钉版用例 |
| `test_schedule_v2.py` | 调度 v2 build_schedule 统一填充框架 |
| `test_time_prefs.py` | 自选时间片全链路（db + 调度 + API） |
| `test_slot_boundary.py` | 自选时间片边界 |
| `test_edge_opt.py` | 掐头去尾前后独立调度 |
| `test_retry_reschedule.py` | 重试重新尊重计划 |
| `test_min_exec_gap.py` | 相邻请求最小间隔兜底 |
| `test_sched_marker.py` | 调度修复（--only 过滤、补签轮判定） |
| `test_scheduler_gate.py` | 调度闸门 + 零成功告警 |
| `test_scheduler_env_probe.py` | 调度 env 合并 + 探针状态锁 |
| `test_web_scheduler_timeout.py` | Web 限速 + 调度超时 |
| `test_probe.py` | 探针模式 + 注册时账号验证 |
| `test_env_fail_loud.py` | .env 解析快速失败（防静默重建密钥） |

### 消息推送与告警（scripts/notify.py、mailer）
| 文件 | 说明 |
|---|---|
| `test_notify_webhook.py` | notify Webhook 推送组件单元测试 |
| `test_notify_ledger_disk.py` | 每日预算磁盘持久化（跨进程共享额度） |
| `test_notify_ledger_race.py` | 账本单次文件锁临界区（RMW 竞态修复） |
| `test_notify_throttle.py` | 同类型告警节流跨进程化 |
| `test_alert_channel_dispatch.py` | 推送通道出口判定（notify.is_configured）+ signin 即时告警门控 + 汇总无收件人推送兜底 |
| `test_mailer.py` | 邮箱通知模块单元（A 线：管理员告警）+ 通道三态判定 |
| `test_mail_notify.py` | 邮箱通知 B 线（用户签到失败邮件）+ 用户开关 |
| `test_mail_failover_0907.py` | SMTP 条目列表化：smtp_list 回落/ENC 解密、mail-config 保存原子性与告警时机、发送 failover、GET 脱敏、通道三态日报渲染 |
| `test_public_beta.py` | 公测反馈修复：会话陈旧预算、登录告警分级 |

### 数据库与迁移（scripts/db.py）
| 文件 | 说明 |
|---|---|
| `test_db_migrations.py` | 通用幂等迁移框架 |
| `test_db_integrity.py` | 数据完整性与迁移修复（0.21.0 Task 3） |
| `test_db_owner_constraint.py` | 每人限 1 账号 DB 约束 |
| `test_db_residue.py` | 自愈收口 + 文件残留三类 |
| `test_audit_chain.py` | 审计日志 HMAC 哈希链 |
| `test_audit_anchor.py` | 审计可追溯性与并发安全 |
| `test_cleanup_clock_guard.py` | 清理残留 + 时钟跳变保护 |
| `test_session_cache_db.py` | 会话 Cookie 缓存（v8 session_cache 表） |
| `test_batch_transaction.py` | 批量操作事务化 |
| `test_user_deregistration_db.py` | 用户注销数据库层（软删除 + 宽限期） |
| `test_env_lock.py` | 共享 .env 文件锁 + 密钥生成竞态 + 文件锁降级进程内锁的告警留痕 |

### 辅助脚本
| 文件 | 说明 |
|---|---|
| `test_env_mailer_tls.py` | 子进程 env + mailer TLS 上下文 |
| `test_rekey_key_source.py` | 密钥来源去 cwd 依赖 + rekey 迁移推送/邮件密文 + 通道自检 |
| `test_p3_fixes.py` | 迁移原子性、rekey argv 泄露、backup 明文警告 |
| `test_backup_require_encrypt.py` | backup.sh 契约（静态核验 + bash -n）：--require-encrypt 与异机副本解耦、加密失败 fail-closed 清场 |
| `test_runsh_env_parse.py` | run.sh 契约（静态核验 + bash -n）：.env 解析去 BOM、key/value 剥空白对齐 env_io |

### 用户操作相关
| 文件 | 说明 |
|---|---|
| `test_batch_sign_cooldown.py` | 批量手动签到冷却（30 分钟默认，可配置） |

## 命名规范（2026-09-01 起）

- 文件按**功能域**命名 `test_<功能>.py`，不再带审查批次日期后缀（如 `test_batch14_fixes_0829.py` → `test_rekey_key_source.py`）；
- 修复日期保留在文件 docstring 首行，便于追溯审查历史；
- git 历史可经 `git log --follow tests/<文件>.py` 追溯旧名。
