# 审查修复批 · 修复计划（feature/review-fix-20260921，基线 develop=37d7334）· 6c3-D 对账版

> 原文（21 条复选框的完整正文）在主工作区 `D:/code/yiban-auto-sign/docs/dev/review-fix-plan-20260921.md`（未跟踪）。
> 本文件为 2026-09-29 批 6c-3-D 的对账版：条目正文**压缩为一行**，逐条给处置与证据；
> 复选框 `[x]` = 已修且本次核过证据，`[ ]` = 仍开放（标「立任务」）。
> 对账环境：工作树 `D:/code/yiban-wt-s6a` @ 批 6c-3 系列提交（HEAD 见提交）；证据均为本节内实际 grep/读源码所得。

## 批次 1：高严重度行为修复（优先）

- [x] **E2**（高）round.py 窗口收尾跳过"已有结论"账号 → 不写 results / runner 按 PENDING 归失败。
  - 对账：`yiban/engine/round.py:351-375` `_mark_window_skip` 对已有结论账号写显式跳过 results
    （`STATUS_SKIPPED_WINDOW`）；**但**原计划落点 `tests/test_effective_window.py` 已无 `returncode`
    断言，退出码契约现落在 `tests/test_host_exit_semantics.py`——该文件自述（:14）断的是文件内
    `_compute` 副本、不经 `runner.main`，且副本与生产对 `no_position` 归类已不一致 → **销账，附立任务**。
- [x] **E5**（高）workers.py 清单模式 `--workers` 值与槽位数不等时漏进子进程 argv。
  - 对账：`yiban/engine/workers.py:96/170-171` 按 argv 序位剔除 `--workers` 及其后随值；
    `tests/test_multi_executor_engine.py:346` 有"值≠槽位数"用例 → 销账。

## 批次 2：中严重度行为修复

- [x] **N-1**（中高）tracking.py / audit_chain.py 写 .env 缺 has_line_break 守卫与折叠口径。
  - 对账：两处已改为共用 `env_io.write_env_key`（`yiban/store/tracking.py:65` /
    `yiban/store/audit_chain.py:147` 的 docstring 明示）；`test_env_key_line_model.py` 三站点参数化
    逐条钉住潜伏分隔符拒绝与 `KEY = v` 折叠 → 销账。
- [x] **N-2**（中）audit_chain.py `audit_anchor_path` 只读 os.environ。
  - 对账：`yiban/store/audit_chain.py:764-775` docstring 明示 `YIBAN_STATE_DIR` 按
    `env_io.resolve_path` 解析（进程环境 → .env → 默认值），并记录了"此前只读 os.environ"的缺陷 →
    销账。
- [ ] **E1+E7**（中）内联 os.environ 兜底绕过 resolve_path（runner/alerts/probe/cli_support/
      cred_state/state_gc 六文件）。
  - 对账：六文件里**仅剩** `yiban/engine/probe.py:189` 一处
    `os.environ.get("YIBAN_ENV_FILE", "").strip() or ".env"`（once 自动关闭写 .env 的路径），
    仍未走 `env_io.resolve_path`；其余点已无该形态 → **立任务（单点收口）**。
- [x] **E6**（中）workers.py 兜底每轮重读熔断计数、从不 `_save_cred_state`。
  - 对账：`yiban/engine/workers.py:404/422` 兜底轮末 `_load_cred_state` + `_save_cred_state(...,
    touched=…)` → 销账。
- [x] **N1**（中）protocol.py 默认流认证跳 `allow_redirects=True` 无白名单校验。
  - 对账：`yiban/fyiban/protocol.py:270-275` 该跳后逐跳 `policy.require_trusted(entry)` +
    `require_not_blocked` + `require_redir_chain_trusted(resp, "login_entry")` → 销账。
- [x] **B6**（中）rekey_accounts.py env_path 回落相对路径且 key_source=None 时不校验旧钥。
  - 对账：`scripts/rekey_accounts.py` **已不在仓**（工具族出仓/改名，全仓 `find -name "*rekey*"`
    只剩 `tests/test_rekey_key_source.py`）→ 目标被移除，**无需在测试仓修**；密钥来源守卫仍在
    `test_rekey_key_source.py`（92 条）。
- [x] **B12**（中高）loadtest mock_env.py 自检返回值丢弃 / capacity_probe._run 吞输出。
  - 对账：`tools/loadtest/mock_env.py`、`capacity_probe.py` **均不在仓**（同上，工具族出仓）→
    目标被移除，**无需在测试仓修**。
- [x] **B3**（中）accounts_data.py BLOCK_CAP=0 时 idx//k 除零。
  - 对账：`web/services/accounts_data.py:416` 注释与实现"`k<=0` = 不限容量，与 my.py 拥挤度口径
    一致" → 销账。
- [x] **B10**（中）build_cjk_font_slices.py outdir 无校验直接 rmtree。
  - 对账：`scripts/build_cjk_font_slices.py:651` 白名单解析+拒绝；`test_web_font_closure.py`
    的 `FontSliceOutdirGuardTest`（进程级行为，批 6c3-A 保留）逐点钉住 → 销账。
- [x] **B1**（中）settings_api.py 部分更新时 gap 取默认 0 绕过容量硬门。
  - 对账：`web/routes/settings_api.py:208-211` 缺省取 `DEFAULT_ACCOUNT_GAP_MAX`（10）再算
    `_capacity_estimate`，不再是 0 → 销账。
- [x] **C1**（中）notify_mail.py `_review_reject_mail` 未净化正文插值。
  - 对账：`web/services/notify_mail.py` 有 `_nl_safe`（:46）且审核拒绝通知走它；
    `test_web_boundary.py::ReviewRejectMailSanitizeTest::test_review_reject_mail_sanitizes_fields`
    在真实出口钉脱敏 → 销账。

## 批次 3：低严重度择要 + 注释 P0

- [ ] 注释 P0：algo.py 轴注释抄错 / protocol.py:30 断言过期 / round.py:149-150 None/空串 / 乱码句 4 处。
  - 对账：抽核 `yiban/fyiban/algo.py:378+`（凹围栏剖分说明已具语义）、`yiban/engine/round.py:147-152`
    （分片节流实现）未见原缺陷形态；**但"4 处乱码句 / 注释抄错"无清单可逐点复核** →
    **立任务（逐点人工复核）**。
- [x] **T-ATTR-1**：waf.py 头部 + PROVENANCE.md 血缘改对（真实上游 sdk250/Auto-Test，license=None）。
  - 对账：`yiban/fyiban/waf.py:3-4` 明示沿用 `sdk250/Auto-Test`、"该仓库无 LICENSE，默认全权保留；
    逐块对照见 `PROVENANCE.md`"；`yiban/fyiban/PROVENANCE.md` 在仓 → 销账。
- [ ] 择要低危：B2/B4/B5/B7/C6/C3/C4/N3/N5-N7/E3/E4/E8/E9/E10 视修复成本取舍。
  - 对账：原文即"择要取舍"，无逐条清单与判据 → **立任务（低危择要，逐条复核后取舍）**。

## 批次 4：注释达标（新 worktree 上对 develop 版本做）

- [ ] 任务痕迹清除：82+ 处（工单号/日期/轮次/"修掉了什么"叙事）。
  - 对账：本批**无法以测试仓证据判定**——仓内仍有大量 MF-xx/审查轮次标识（多作为契约标识符，
    如 MF-49/MF-58/MF-60）；"叙事"是否已清需人工抽查 → **立任务**。
- [ ] 体量裁到 +20%：17 个超标文件（逐条回答"删了会丢什么"）。
  - 对账：门槛依赖原 17 文件清单与预算表（本仓不可考）→ **立任务**。
- [ ] 头部四问补齐：7 文件（补"复用"）。
  - 对账：现状抽核：`yiban/` 下 38 个生产文件带 `**通信**` 一问、34 个带 `**功能**`（`round.py:22/30`
    四问块完整）；原"7 文件清单"不可考 → 视为**大部分已落地**，余项**立任务（逐文件核）**。
- [ ] **头部四问规范（用户强调）**：通信一问必须暴露前端调用点。
  - 对账：同上，抽核文件已含通信一问（前端调用点是否逐条列全需人工抽查）→ **立任务**。
- [x] TICKETS.md 收口（主工作区同步并提交 docs/dev/reviews/）。
  - 对账：`docs/dev/reviews/TICKETS.md` 在仓且被跟踪 → 销账。

## 对账结论（2026-09-29，批 6c-3-D）

21 条逐条对账结果：**13 条销账（[x]）**、**8 条立任务（[ ]）**。

销账（证据见上逐条）：E5、N-1、N-2、E6、N1、B6、B12、B3、B10、B1、C1、T-ATTR-1、TICKETS.md；
其中 E2 亦销账但**附一条立任务**（退出码契约的测试落点 `test_host_exit_semantics.py` 断的是
`_compute` 副本、不经 `runner.main`，且副本与生产对 `no_position` 归类不一致——该文件自述，批 6c-3
范围外，只报告不动手）。

立任务 8 条：`E1+E7 剩 probe.py:189 单点收口`、`注释 P0 逐点复核`、`择要低危逐条复核`、
`任务痕迹清除人工抽查`、`体量裁到 +20%（需原清单）`、`头部四问逐文件核`、`通信一问前端调用点抽查`、
`host 退出码副本与生产不一致（E2 附）`。

对账范围与限制（如实登记）：
- 只核"以当前测试仓源码/测试可判定"的项；批次 3/4 中依赖原清单（乱码句位置、17 文件体量清单、
  7 文件四问清单、82 处任务痕迹清单）的项，原清单不在本仓，无法逐点证实 → 一律标立任务，不推定完成。
- 本批对账只读与文档写入，未改动任何生产码；销账依据的最小证据均在本文件内注明（文件:行）。
