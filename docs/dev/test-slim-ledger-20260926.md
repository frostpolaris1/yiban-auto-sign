# 测试瘦身取证台账（批 1 · Task 2a）

> 分支 `repair/m3-batch1` · 2026-09-26 · 只产出文档，**未删任何测试**（2b 另派执行）。
> 口径来源：`task-2-brief.md`「2a — 取证与清单」；判据来源 `docs/dev/must-fix-list.md`（六类剔除 + 已否证清单）。
> 手机号一律遮罩为 `138****0000` 形态。
> 机器产物（gitignored，不入库）：`.superpowers/sdd/m3-batch1-plan-20260925/artifacts/`
> （`collect-only.txt`、`ast-tests2.json`、`per-file-final.tsv`、`analyze_slim2.py`、`aggregate2.py`）。

## 1. 总盘（WSL 实测，Task 0 后基线）

| 指标 | 数值 | 命令 / 来源 |
|---|---|---|
| 测试文件 | **141** | `ls tests/test_*.py \| wc -l` |
| 用例（pytest collect-only） | **3049** | `~/.venv-yiban-wsl/bin/python -m pytest tests/ --collect-only -q`（4.00s） |
| 用例（AST 计数） | 3042 | `analyze_slim2.py`（差 7：collect 含少量重复/参数化节点，下文百分比以 3042 为分母） |
| 子测试（subTest） | 静态 `subTest` 调用 176 处；Task 0 报告全量运行时 **1372** | subTest 数只在运行时展开，按 Task 0 基线引用，未复跑全量 |
| 保护禁区（文件族 + 关键词命中） | **708 / 3042 ≈ 23.3%** | 见 §3 口径 |

## 2. 判据口径（先定义再计数——MF-14 明确要求）

- **C1 源文本断言型**：测试（或其类的 `setUp/setUpClass` 链）读取**生产源码文件**（`.py/.js/.html/.css/.sh`，路径为仓库内常量），
  断言形如「源文本含/不含某字符串」（`assertIn/assertNotIn/assertEqual/assertRegex` 的字面量参数）。
  **不含**对 HTTP 响应体、配置/`.env`、日志文本的字符串断言（那些是行为断言）。
- **C2 mock 掉被测物本身**：patch 目标名 ∈ `{set_session_cache, get_session_cache, clear_session_cache, touch, give_up, try_claim}`。
- **C3 多层重复**：同一分支在单元层与集成/e2e 层重复——**only 人工抽读认定**，本文不机械量化（宁缺勿滥）。
- **C4 已否证守灵**：对照 `must-fix-list.md` §「已否证清单」（13 条）与 §「本流否证追加」（14 条）逐条 grep 命中。
- **C5 钉旧行为且台账登记「无视」**：本次未发现成规模命中（见 §7 顾虑）。
- **C6 纯计数/调用断言**：测试的全部断言都只查 mock 调用状态（`assert_not_called/call_count/...`），无结果断言。

## 3. 保护禁区口径与计数

`security|mask|audit|login|private|csrf|ratelimit` 命中**文件名或用例名**即留（发布门 §2① 同口径），另加文件族：

| 保护族 | 文件数 | 用例数 | 文件 |
|---|---|---|---|
| 关键词命中（文件名） | 24 | 约 480 | test_web_security_gates / test_web_auth_security / test_audit_chain / test_audit_anchor / test_masking_ssrf_gaps / test_login_protocol_shape / test_admin_creds_masked_ops / test_log_masking_formatter / test_logs_export_masking / test_protocol_masking / test_masking_tokens / test_url_userinfo_masking / test_web_mask_email_parity / test_web_api_security / test_capacity_audit_scope 等 |
| 迁移链 `test_migrations*` | 5 | 70 | fail_closed/v18/v19/v20 + db_migrations |
| 备份可恢复 `test_backup_e2e` | 1 | 17 | — |
| 部署/恢复 `test_deploy_*` | 3 | 53 | prod_artifacts / paths_and_restore_verdict / entry_imports |
| 隔离护栏 `test_loadtest_isolation` | 1 | 18 | — |
| e2e 全链 | 4 | 54 | login_e2e_mock / run_sh_logging / run_sh_workers / manual_sign_reporting |
| **小计** | | **708**（含关键词命中的其它文件） | |

**保守追加（未计入 708，2b 亦 0 删）**：`test_host_exit_semantics.py`（18）——退出码语义即 run.sh rc 契约族；
`test_runsh_env_parse.py`、`test_cli_contract.py` 同属 rc/CLI 契约，建议按禁区对待。→ 实际禁区约 **744 / 3042 ≈ 24.5%**。

## 4. 分类计数表（每文件一行，全 141 文件）

列＝用例数 / 保护数 / 保护族 / C1 非保护 / C6 非保护。`C1/C6` 为**机器判定**（含假阳，见 §7）；
SUM 行：3042 / 708 / — / 471 / 25。

| file | total | prot | family | C1_np | C6_np |
|---|---|---|---|---|---|
| test_account_abuse_gate.py | 21 | 0 | - | 0 | 0 |
| test_account_crypto_key_source.py | 5 | 0 | - | 0 | 0 |
| test_account_liveness_gate.py | 13 | 0 | - | 0 | 3 |
| test_account_plaintext_patch.py | 6 | 0 | - | 0 | 0 |
| test_admin_creds_masked_ops.py | 11 | 11 | kw-file | 0 | 0 |
| test_admin_privilege_web.py | 11 | 0 | - | 8 | 0 |
| test_alert_channel_dispatch.py | 14 | 4 | - | 1 | 0 |
| test_audit_anchor.py | 15 | 15 | kw-file | 0 | 0 |
| test_audit_anchor_field_source.py | 3 | 3 | kw-file | 0 | 0 |
| test_audit_chain.py | 54 | 54 | kw-file | 0 | 0 |
| test_audit_cleanup_visibility.py | 7 | 7 | kw-file | 0 | 0 |
| test_backup_e2e.py | 17 | 17 | 备份可恢复 | 0 | 0 |
| test_backup_require_encrypt.py | 4 | 0 | - | 2 | 0 |
| test_backup_sentinel.py | 21 | 0 | - | 2 | 0 |
| test_breaker.py | 25 | 0 | - | 1 | 0 |
| test_builtin_admin_sid.py | 11 | 5 | - | 0 | 0 |
| test_capacity_audit_scope.py | 10 | 10 | kw-file | 0 | 0 |
| test_capacity_limits.py | 31 | 0 | - | 0 | 0 |
| test_capacity_of.py | 19 | 0 | - | 0 | 0 |
| test_claims.py | 25 | 0 | - | 0 | 0 |
| test_claims_fencing.py | 17 | 0 | - | 0 | 0 |
| test_cli_contract.py | 17 | 0 | - | 0 | 0 |
| test_config_input_caps.py | 15 | 1 | - | 0 | 0 |
| test_container_scheduler.py | 43 | 0 | - | 0 | 0 |
| test_dashboard_stats_caliber_js.py | 6 | 0 | - | 0 | 0 |
| test_db_integrity.py | 46 | 3 | - | 4 | 0 |
| test_db_migrations.py | 12 | 0 | - | 0 | 0 |
| test_db_owner_constraint.py | 7 | 0 | - | 2 | 0 |
| test_delay_ack_frontend.py | 31 | 0 | - | 23 | 0 |
| test_delete_timeline_consistency.py | 9 | 0 | - | 0 | 0 |
| test_deploy_entry_imports.py | 4 | 4 | 部署/恢复 | 0 | 0 |
| test_deploy_paths_and_restore_verdict.py | 30 | 30 | 部署/恢复 | 0 | 0 |
| test_deploy_prod_artifacts.py | 19 | 19 | 部署/恢复 | 0 | 0 |
| test_docker_image_contents.py | 2 | 0 | - | 0 | 0 |
| test_effective_window.py | 27 | 0 | - | 0 | 2 |
| test_egress_and_executors_api.py | 70 | 8 | - | 0 | 0 |
| test_email_domain_review.py | 19 | 0 | - | 5 | 0 |
| test_engine_shell_forwarding.py | 5 | 0 | - | 0 | 0 |
| test_env_fail_loud.py | 3 | 0 | - | 0 | 0 |
| test_env_key_line_model.py | 13 | 3 | - | 0 | 0 |
| test_env_line_break_injection.py | 30 | 0 | - | 0 | 0 |
| test_env_mailer_tls.py | 13 | 3 | - | 7 | 0 |
| test_env_write_transient_failure.py | 4 | 0 | - | 0 | 0 |
| test_executor_manifest.py | 45 | 0 | - | 0 | 0 |
| test_executor_v3.py | 86 | 0 | - | 0 | 0 |
| test_executor_write_guard.py | 14 | 2 | - | 0 | 0 |
| test_executors_kpi_scope.py | 19 | 0 | - | 5 | 0 |
| test_fallback_gates.py | 21 | 0 | - | 0 | 0 |
| test_fyiban_concave_sampling.py | 18 | 0 | - | 0 | 0 |
| test_fyiban_isolation.py | 20 | 2 | - | 5 | 0 |
| test_global_pause.py | 3 | 0 | - | 0 | 0 |
| test_host_exit_semantics.py | 18 | 0 | - | 0 | 2 |
| test_hrw.py | 11 | 0 | - | 0 | 0 |
| test_infra_layer.py | 7 | 0 | - | 0 | 0 |
| test_ledger_check.py | 12 | 0 | - | 0 | 0 |
| test_loadtest_isolation.py | 18 | 18 | 隔离护栏 | 0 | 0 |
| test_loadtest_tools.py | 29 | 0 | - | 28 | 0 |
| test_locks.py | 16 | 0 | - | 0 | 2 |
| test_log_masking_formatter.py | 15 | 15 | kw-file | 0 | 0 |
| test_login_e2e_mock.py | 5 | 5 | e2e全链 | 0 | 0 |
| test_login_protocol_shape.py | 24 | 24 | kw-file | 0 | 0 |
| test_logs_by_date.py | 23 | 0 | - | 7 | 0 |
| test_logs_export_masking.py | 9 | 9 | kw-file | 0 | 0 |
| test_mail_layout.py | 27 | 0 | - | 0 | 0 |
| test_mail_notify.py | 33 | 6 | - | 6 | 6 |
| test_mail_smtp_target.py | 22 | 5 | - | 0 | 0 |
| test_mailer.py | 47 | 7 | - | 0 | 0 |
| test_manual_sign_reporting.py | 6 | 6 | e2e全链 | 0 | 0 |
| test_masking_ssrf_gaps.py | 33 | 33 | kw-file | 0 | 0 |
| test_masking_tokens.py | 5 | 5 | kw-file | 0 | 0 |
| test_migration_compat.py | 3 | 0 | - | 0 | 0 |
| test_migrations_fail_closed.py | 35 | 35 | 迁移链 | 0 | 0 |
| test_migrations_v18.py | 8 | 8 | 迁移链 | 0 | 0 |
| test_migrations_v19.py | 8 | 8 | 迁移链 | 0 | 0 |
| test_migrations_v20.py | 12 | 12 | 迁移链 | 0 | 0 |
| test_module_size_gate.py | 7 | 0 | - | 0 | 0 |
| test_multi_executor_engine.py | 17 | 0 | - | 1 | 0 |
| test_multi_task.py | 4 | 0 | - | 0 | 0 |
| test_no_position.py | 19 | 0 | - | 0 | 0 |
| test_notify_ledger.py | 22 | 0 | - | 0 | 2 |
| test_notify_throttle.py | 10 | 0 | - | 0 | 0 |
| test_notify_webhook.py | 70 | 8 | - | 53 | 0 |
| test_planner.py | 52 | 0 | - | 0 | 0 |
| test_probe.py | 17 | 0 | - | 0 | 4 |
| test_protocol_masking.py | 4 | 4 | kw-file | 0 | 0 |
| test_pw_gate_tiers.py | 35 | 0 | - | 0 | 0 |
| test_queue_recovery.py | 20 | 0 | - | 0 | 0 |
| test_rate_limit_tiers.py | 5 | 5 | - | 0 | 0 |
| test_registration_pause.py | 13 | 1 | - | 9 | 0 |
| test_rekey_key_source.py | 94 | 22 | - | 65 | 0 |
| test_release_version_source.py | 6 | 0 | - | 4 | 0 |
| test_review_flow.py | 12 | 1 | - | 8 | 0 |
| test_run_sh_logging.py | 16 | 16 | e2e全链 | 0 | 0 |
| test_run_sh_workers.py | 27 | 27 | e2e全链 | 0 | 0 |
| test_runsh_env_parse.py | 4 | 0 | - | 0 | 0 |
| test_saturday_sign.py | 11 | 0 | - | 5 | 0 |
| test_schedule_edge_limit_js.py | 4 | 0 | - | 0 | 0 |
| test_schedule_retry.py | 38 | 0 | - | 7 | 0 |
| test_schedule_v2.py | 23 | 0 | - | 0 | 1 |
| test_scheduler_env_probe.py | 19 | 0 | - | 7 | 0 |
| test_scheduler_gate.py | 56 | 2 | - | 0 | 0 |
| test_scheduler_v3_doc.py | 5 | 0 | - | 0 | 0 |
| test_session.py | 37 | 2 | - | 0 | 0 |
| test_settings_tiers_frontend_parity.py | 7 | 0 | - | 2 | 0 |
| test_sign_events.py | 15 | 0 | - | 0 | 0 |
| test_sign_round_guards.py | 25 | 0 | - | 7 | 0 |
| test_slot_geometry.py | 11 | 0 | - | 0 | 0 |
| test_state_file_writes.py | 8 | 0 | - | 2 | 0 |
| test_state_gc.py | 37 | 1 | - | 2 | 0 |
| test_static_routes.py | 25 | 2 | - | 5 | 0 |
| test_status_sets.py | 25 | 0 | - | 0 | 0 |
| test_store_boundary.py | 136 | 1 | - | 1 | 0 |
| test_store_queue.py | 16 | 0 | - | 0 | 0 |
| test_subpath_deploy.py | 8 | 1 | - | 4 | 0 |
| test_supervisor_recursion_guard.py | 4 | 0 | - | 0 | 0 |
| test_switch_password_gate.py | 12 | 1 | - | 4 | 0 |
| test_time_prefs.py | 69 | 1 | - | 46 | 0 |
| test_token_bucket.py | 31 | 0 | - | 0 | 0 |
| test_url_userinfo_masking.py | 6 | 6 | kw-file | 0 | 0 |
| test_user_deregistration_db.py | 10 | 0 | - | 0 | 0 |
| test_user_deregistration_web.py | 30 | 4 | - | 22 | 0 |
| test_user_soft_delete.py | 12 | 0 | - | 0 | 0 |
| test_verify_jobs.py | 66 | 2 | - | 6 | 0 |
| test_web_api_security.py | 13 | 13 | kw-file | 0 | 0 |
| test_web_auth_security.py | 69 | 69 | kw-file | 0 | 0 |
| test_web_boundary.py | 163 | 32 | - | 81 | 3 |
| test_web_calendar_parity.py | 8 | 0 | - | 3 | 0 |
| test_web_class_hygiene.py | 1 | 0 | - | 0 | 0 |
| test_web_component_adoption.py | 8 | 0 | - | 0 | 0 |
| test_web_design_tokens.py | 6 | 0 | - | 0 | 0 |
| test_web_font_closure.py | 6 | 0 | - | 0 | 0 |
| test_web_js_modules.py | 18 | 1 | - | 5 | 0 |
| test_web_mask_email_parity.py | 9 | 9 | kw-file | 0 | 0 |
| test_web_page_consistency.py | 3 | 0 | - | 1 | 0 |
| test_web_render_golden.py | 6 | 1 | - | 1 | 0 |
| test_web_security_gates.py | 79 | 79 | kw-file | 0 | 0 |
| test_web_text_contrast.py | 7 | 0 | - | 0 | 0 |
| test_webui_inline_context.py | 9 | 0 | - | 9 | 0 |
| test_window_edge_clamp.py | 11 | 0 | - | 0 | 0 |
| test_yiban_fallback_sh.py | 5 | 0 | - | 5 | 0 |
| test_yiban_status_single_source.py | 4 | 0 | - | 0 | 0 |
| **SUM** | **3042** | **708** | | **471** | **25** |

## 5. 剔除清单（三档）

### 5.1 T1 确定可删 — 8 条（机械特征明确，且不损失安全/e2e 覆盖）

| # | 用例 | 类 | 理由（已抽读正文） |
|---|---|---|---|
| 1 | `test_executors_kpi_scope.py::KpiScopeTest::test_capacity_card_label_names_accounts` | C1 | 只对模板文案 `assertIn("设定的账号容量上限")/assertNotIn(...)`；文件自述「纯文案，无运行时断言可依赖」 |
| 2 | `test_executors_kpi_scope.py::KpiScopeTest::test_info_text_states_the_new_scope` | C1 | 同上，模板口径句两处 assertIn |
| 3 | `test_executors_kpi_scope.py::FallbackStatusCopyTest::test_off_state_names_the_switch_not_a_process` | C1 | 对 JS 字符串字面量 `off: "未启用"` 的 assertIn/assertNotRegex |
| 4 | `test_executors_kpi_scope.py::FallbackStatusCellIsStatusOnlyTest::test_pointer_copy_never_comes_back` | C1 | `assertNotIn` 对源注释文本；assertNotIn 型对已删文本**恒真**（静默失覆盖，`_frontend_src.py` docstring 自述同一风险） |
| 5 | `test_executors_kpi_scope.py::ExecutorListWordingTest::test_fallback_row_occupies_a_number_in_doc_and_banner` | C1 | 模板口径句 assertIn/assertNotIn，措辞锚点 |
| 6 | `test_executors_kpi_scope.py::ExecutorListWordingTest::test_doc_links_single_executor_and_guards_single_row_hint` | C1 | 同上（模板 3 句 + JS 函数体 3 句） |
| 7 | `test_executors_kpi_scope.py::RowMenuDividerSpacingTest::test_floating_divider_has_no_margin` | C1 | CSS 文本正则 `margin:\s*0`，非渲染行为 |
| 8 | `test_docker_image_contents.py::DockerImageContentsTest::test_yiban_package_is_copied` | C1 | Dockerfile 文本 `assertRegex`；同文件 AST 扫描用例（见 T2#3）已覆盖「被导入的本地包必须 COPY」 |

### 5.2 T2 建议可删 — 26 条（启发式命中 + 已抽读正文核实）

| # | 用例 | 类 | 理由 |
|---|---|---|---|
| 1 | `test_state_file_writes.py::SigninWritesAreAtomicTest::test_signin_state_writers_use_replace` | C1 (MF-21) | 字符窗 `split("def f(")[:2500]` 内 `assertIn("os.replace", body)`：窗口起点与长度皆脆（MF-21 已登记），且断的是源码文本不是落盘行为 |
| 2 | `test_state_file_writes.py::SigninWritesAreAtomicTest::test_writers_have_single_implementation` | C1 | `assertNotIn("def f(" , shell 文本)`：源文本断言，assertNotIn 恒真风险 |
| 3 | `test_docker_image_contents.py::DockerImageContentsTest::test_imported_local_packages_are_copied` | C1 (MF-33) | 单向覆盖（只认 `COPY <pkg>/`），判据行尾自述「别指望它兜底」 |
| 4-6 | `test_mail_notify.py::SignUserFailMailTest::{test_owner_empty_skips, test_unknown_user_skips, test_notify_off_skips}` | C6 | 仅 `m.assert_not_called()`，无结果断言；同一跳过语义由 `test_notify_on_sends_masked` 的分支覆盖 |
| 7-8 | `test_mail_notify.py::SignAdminMailSummaryTest::{test_flush_empty_skips, test_flush_skips_admin_to_when_admin_notify_off}` | C6 | 仅调用/计数断言 |
| 9 | `test_mail_notify.py::SignAdminMailSummaryTest::test_flush_uses_filtered_recipients` | C6 | 3 条断言全为调用/参数断言 |
| 10-11 | `test_locks.py::{LockPrimitiveTest::test_wrapper_passes_a_retry_timeout, D4DivergentLocksRemovedTest::test_env_write_lock_delegates_to_primitive}` | C6 | 只查 mock 调用，无锁行为结果断言（`_file_lock` 已否证项另见 §7） |
| 12-14 | `test_account_liveness_gate.py::AttemptSkipsRemovedAccountTest::{test_live_account_is_attempted, test_deactivated_before_turn_is_skipped, test_account_without_id_is_not_gated}` | C6 | 仅调用断言 |
| 15-16 | `test_effective_window.py::WindowDecisionUsesBeijingClockTest::{test_window_decision_follows_beijing_clock, test_window_still_closes_after_deadline}` | C6 | 仅调用断言（注：Task 0 日期锚定文件，删除须另附理由，故本可降为 T3） |
| 17-20 | `test_probe.py::ProbeSigninTest::{test_run_probe_collects_and_flushes, test_run_probe_once_auto_disable, test_run_probe_disabled_is_silent, test_run_probe_skipped_when_enabled_but_not_due}` | C6 | 断言全为 mock 调用计数 |
| 21-22 | `test_notify_ledger.py::UserFailMailQuotaTest::{test_send_failure_releases_quota, test_send_success_consumes_quota}` | C6 | 仅调用断言（额度归还的**状态**未直接断言） |
| 23 | `test_schedule_v2.py::WindowRecheckAfterSleepTest::test_normal_wait_still_executes` | C6 | 对照组仅调用断言 |
| 24-26 | `test_web_boundary.py::{WebServicesAccountsSplitContractTest::test_reject_account_cas_semantics, WebServicesNotifySplitContractTest::test_mail_alert_due_stub_reaches_send_notification, WebServicesNotifySplitContractTest::test_notify_capacity_once_uses_app_send_notification}` | C6 | 纯打桩穿透/调用断言（「面拆分」契约，删前需指挥者确认非发布门判据） |

> T1+T2 = **34 条 ≈ 1.1%** 用例（含 subTest 折算按 2b 实删计数）。

### 5.3 T3 需人工裁决（只列不删）— 52 条已列 + 机械可疑池

**T3-a 台账已点名 / 保护族内假绿（只列不改，2b 不动）**

| 用例 | 依据 | 说明 |
|---|---|---|
| `test_host_exit_semantics.py::ExitCodeSemanticsTest` 7 条 | **MF-5** | 断言的是抄进测试文件的 `_compute` 生产逻辑副本，副本已与生产漂移（`no_position` 归类不一致）⇒ 假绿。**属 rc 契约族（保守纳入禁区）→ 保留**；建议随「测试质量批」重写为经 `runner.main` 的真路径，而非本批删除 |
| `test_capacity_audit_scope.py::SignsInSingleSourceTest::test_predicate_matches_runtime_gate` | **MF-31** | 「一致性」半句按构造恒真（`is_signable` 末行即 `return signs_in(...)`），两条断言只能同时红。**文件命中 `audit` 关键词 → 保留**；建议只删第二断言（非本批「只删不改」范围） |
| `test_global_pause.py::test_manual_signin_not_blocked` | **MF-32** | 只断 `assertNotEqual(code, 2)`，任何非 2 出口都过；但待裁决 #8 已拍板「手动腿豁免是设计」→ 删除会丢失设计锁，建议**加严**而非删 |
| `test_state_file_writes.py::SigninWritesAreAtomicTest::test_cred_state_write_is_delegated` | C1 | 对 `state_io` 函数体 assertIn/assertNotIn 源码片段 |
| `test_store_boundary.py::WriteForwardingTest_SESSION::test_patch_string_target_round_trips` | C2 | patch 目标即被测存储层 `yiban.store.db.get_session_cache`；但本案的是「打桩面契约」，删前需确认 |
| `test_docker_image_contents.py::test_imported_local_packages_are_copied` | MF-33 | 见 T2#3（若指挥者认为构建门不可动，则升回保留） |

**T3-b 前端源文本/口径契约块（MF-14 核心，建议成块裁决）** — 约 **190 条**：

`test_delay_ack_frontend.py`（静态钉点类 ExecutorSaveCancelTest 3 + GatedCallSitesTest 6）、
`test_static_routes.py`(25)、`test_web_js_modules.py`(18)、
`test_executors_kpi_scope.py` 剩余(12)、`test_webui_inline_context.py`(9)、
`test_web_calendar_parity.py`(8)、`test_web_component_adoption.py`(8)、`test_subpath_deploy.py`(8)、
`test_settings_tiers_frontend_parity.py`(7)、`test_web_font_closure.py`(6)、`test_web_text_contrast.py`(6)、
`test_web_design_tokens.py`(6)、`test_dashboard_stats_caliber_js.py`(6)、`test_release_version_source.py`(6)、
`test_web_render_golden.py`(6)、`test_schedule_edge_limit_js.py`(4)、`test_yiban_fallback_sh.py`(5)、
`test_web_page_consistency.py`(3)、`test_web_class_hygiene.py`(1)、`test_module_size_gate.py`(7)。
判据：全部「读前端源码 → 断言字符串存在/计数」；其中多数是真实 parity/结构守卫（如 helper 收口、路由注册对齐），
**不能整块删**——建议 2b 抽读后按「纯文案 vs 结构守卫」二选一，或改为 CI 侧跑。

**T3-c 机械可疑池（未逐条抽读，只列不删）**：
- C1 非保护 **471 条**（全清单见 `artifacts/ast-tests2.json` 中 `src_assert=true & 非保护`）。含假阳：对响应体/`.env`/日志文本的字符串断言会被误收（如 `test_web_boundary` 81、`test_time_prefs` 46、`test_rekey_key_source` 65、`test_notify_webhook` 53）。
- C4 已否证关键词命中 **111 条**（`must-fix-list` §已有否证清单 + §本流否证追加 逐条 grep）。**关键词口径噪声大**（如 `epoch` 命中合法 fencing 用例、`notified` 命中合法列名），需逐条读正文确认「是否真为否证结论守灵」，本文不列入可删数。
- C2 **1 条**（见 T3-a）。

## 6. 零候选文件（一行一文件，无任何 T1/T2/T3 机械命中、且非保护族）

`test_account_abuse_gate` · `test_account_crypto_key_source` · `test_account_plaintext_patch` ·
`test_capacity_limits` · `test_capacity_of` · `test_claims` · `test_claims_fencing` · `test_cli_contract` ·
`test_container_scheduler` · `test_db_migrations` · `test_delete_timeline_consistency` ·
`test_engine_shell_forwarding` · `test_env_fail_loud` · `test_env_line_break_injection` ·
`test_env_write_transient_failure` · `test_executor_manifest` · `test_executor_v3` · `test_fallback_gates` ·
`test_fyiban_concave_sampling` · `test_global_pause` · `test_hrw` · `test_infra_layer` · `test_ledger_check` ·
`test_mail_layout` · `test_mail_smtp_target` · `test_migration_compat` · `test_module_size_gate` ·
`test_multi_task` · `test_no_position` · `test_notify_throttle` · `test_planner` · `test_pw_gate_tiers` ·
`test_queue_recovery` · `test_rate_limit_tiers` · `test_runsh_env_parse` · `test_scheduler_v3_doc` ·
`test_sign_events` · `test_slot_geometry` · `test_status_sets` · `test_store_queue` ·
`test_supervisor_recursion_guard` · `test_token_bucket` · `test_user_deregistration_db` ·
`test_user_soft_delete` · `test_web_component_adoption` · `test_web_design_tokens` ·
`test_web_font_closure` · `test_web_text_contrast` · `test_window_edge_clamp` ·
`test_yiban_status_single_source`（共 50 文件）

## 7. 遗留（保护族内假绿，2b 不动手，仅登记）

1. **MF-5**：`test_host_exit_semantics.py::ExitCodeSemanticsTest` 7 条断言生产逻辑副本（详见 §5.3 T3-a）。
2. **MF-21**：`test_state_file_writes.py::test_signin_state_writers_use_replace` 字符窗很薄（集成树 `os.replace` 已在 2257/2500）。
3. **MF-31 / MF-32 / MF-33**：见 T3-a。
4. **MF-72 家族**：`test_login_protocol_shape.py` 整体 mock `set_session_cache/get_session_cache`（4 处 `mock.patch.object`），
   断言只及 mock 参数 ⇒ 对「假成功写缓存」缺陷盲区。**文件属 e2e/鉴权链，保护禁区 → 0 删**，登记交测试质量批。
5. **C5 空白**：未发现成规模的「钉旧行为且台账登记『无视』」用例——`must-fix-list` 的「已裁决/无视」多为注释与配置面，
   未沉淀为独立防回归用例，故本类计数为 0（不是没找，是没有）。

## 8. 方法笔记

- 先建机械候选池：`grep -rnE "getsource|read_text\(|\.read\(\)"`（79 文件命中）、
  `grep -rn "mock.patch"`（1164 行）、`assert_called|call_count`、以及「码文件路径字面量 ∩ 读文件」交叉（70 文件 / 1850 用例上限），
  再用 AST（`analyze_slim2.py`，跟踪 `setUp/setUpClass` 的源码缓冲 + `_read()` 本地读取器 + 模块常量路径解析）逐用例判定，
  最后抽读正文核实（本批抽读：host_exit_semantics、global_pause、capacity_audit_scope、docker_image_contents、
  state_file_writes、delay_ack_frontend、executors_kpi_scope、multi_task、mail_notify、executor_manifest 等）。
- **MF-14 结论**：方向成立但**计数口径敏感**。「读生产源码的文件」占 79/141（56%）、
  「含码路径字面量 ∩ 读文件」70/141（50%）——**文件层面确实过度represent**；
  但严格定义为「断言目标是源码文本」的非保护用例，机器口径 471（15.5%），抽读后**确认为纯源文本可删的仅 T1/T2 的 11 条**，
  其余多为结构/parity 守卫。即 MF-14 原话「精确数字不可信，先定义口径再立项」得到复核支持。
- 不跑全量套件（遵守约束）；`collect-only` 一次（4.0s），subTest 数引用 Task 0 基线。

## 9. 顾虑与 2b 建议

1. **深度不达标（诚实报数）**：手核 T1+T2 = **34 条 ≈ 1.1%**；即使 T3-b 前端块整块批准（约 190）+T3-c 抽读后部分成立，
   乐观上限约 **500 / 3042 ≈ 16%**，仍 < 20%。**按任务单「若 T1+T2 不足 20%，如实报数并给 T3 放量建议，不强凑」执行。**
2. 达 20–40% 需动的只能是**行为类测试**（六类之外的重复/低价值），超出本任务授权范围；建议指挥者在 2b 前明确是否放宽。
3. C1 机器判定含假阳（响应/配置文本断言），2b 抽验 T2 时请按 30% 抽样；T3-c 全清单在 `ast-tests2.json` 可复跑。
4. 「rc 契约族」是否含 `test_host_exit_semantics.py` / `test_cli_contract.py` / `test_runsh_env_parse.py` 需指挥者裁定；
   本台账按**保守纳入禁区**处理（0 删）。

## 10. 待指挥者裁定项

1. T3-b 前端源文本块是否整块放行（约 190 条）？还是只放行「纯文案」子集？
2. 「rc 契约族」边界（是否含 host_exit_semantics / cli_contract / runsh_env_parse）。
3. T2 中 `test_web_boundary` 3 条（WebServices*SplitContract）是否属发布门判据（若是 → 转保留）。
4. 是否同意「深度不足 20% 如实报数」，或授权放宽到行为类测试。