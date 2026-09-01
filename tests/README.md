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
python -m pytest tests/test_tui*.py       # 终端面板

# 单个文件
python -m pytest tests/test_smoke.py -v
```

功能域 markers 定义见 `pyproject.toml` 的 `[tool.pytest.ini_options].markers`：
`web` / `signin` / `scheduler` / `notify` / `db` / `tui`。

> 说明：`scripts/stress_security_test.py`（安全压力测试）需真实服务器地址、按脚本方式运行，**不参与** pytest 默认收集。

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
| `test_web_security_gates.py` | 批次16 web 修复验证：重置密码门禁、日志 handler、版本同步 |
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

### 调度器（docker/scheduler.py、调度 v2）
| 文件 | 说明 |
|---|---|
| `test_container_scheduler.py` | 容器内签到调度器回归 |
| `test_schedule_v2.py` | 调度 v2 build_schedule 统一填充框架 |
| `test_time_prefs.py` | 自选时间片全链路（db + 调度 + API） |
| `test_slot_boundary.py` | 自选时间片边界 |
| `test_edge_opt.py` | 掐头去尾前后独立调度 |
| `test_retry_reschedule.py` | 重试重新尊重计划 |
| `test_min_exec_gap.py` | 相邻请求最小间隔兜底 |
| `test_sched_marker.py` | 批次16 调度修复（--only 过滤、补签轮判定） |
| `test_scheduler_gate.py` | 批次12 调度闸门 + 零成功告警 |
| `test_scheduler_env_probe.py` | 调度 env 合并 + 探针状态锁 |
| `test_web_scheduler_timeout.py` | Web 限速 + 调度超时（批次7 P3 系列） |
| `test_probe.py` | 探针模式 + 注册时账号验证 |
| `test_env_fail_loud.py` | .env 解析快速失败（防静默重建密钥） |

### 消息推送与告警（scripts/notify.py、mailer）
| 文件 | 说明 |
|---|---|
| `test_notify_webhook.py` | notify Webhook 推送组件单元测试 |
| `test_notify_ledger_disk.py` | 每日预算磁盘持久化（跨进程共享额度） |
| `test_notify_ledger_race.py` | 账本单次文件锁临界区（RMW 竞态修复） |
| `test_notify_throttle.py` | 同类型告警节流跨进程化 |
| `test_mailer.py` | 邮箱通知模块单元（A 线：管理员告警） |
| `test_mail_notify.py` | 邮箱通知 B 线（用户签到失败邮件）+ 用户开关 |
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
| `test_env_lock.py` | 共享 .env 文件锁 + 密钥生成竞态 |

### 终端面板（tui/）与辅助脚本
| 文件 | 说明 |
|---|---|
| `test_tui_fixes.py` | TUI 与辅助脚本修复 |
| `test_env_mailer_tls.py` | 子进程 env + mailer TLS 上下文 |
| `test_rekey_key_source.py` | 密钥来源去 cwd 依赖 + rekey 迁移推送密文 |
| `test_p3_fixes.py` | 批次17 P3：迁移原子性、rekey argv 泄露、backup 明文警告 |

### 用户操作相关
| 文件 | 说明 |
|---|---|
| `test_batch_sign_cooldown.py` | 批量手动签到冷却（30 分钟默认，可配置） |

## 命名规范（2026-09-01 起）

- 文件按**功能域**命名 `test_<功能>.py`，不再带审查批次日期后缀（如 `test_batch14_fixes_0829.py` → `test_rekey_key_source.py`）；
- 遗留的批次/日期语义保留在文件 docstring 首行（如"批次14 第一档回归测试"），便于追溯审查历史；
- git 历史可经 `git log --follow tests/<文件>.py` 追溯旧名。
