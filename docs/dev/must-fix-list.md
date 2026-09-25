# 后续必修清单（持续登记）

> 每条含：现象 / 调查线索 / 处置时点。M2 逐码审查、M3 修复时逐条消费。

## MF-1 生产版本没有隐私遮罩（用户 2026-09-23 报告）

- **现象**：服务器现有版本（v0.4.7，`server-web` @ 6d4eafa）的日志与展示面存在未脱敏的手机号。
- **已定位根因（2026-09-23 生产只读取证，用户提供现场）**：
  1. **没有兜底脱敏机制**：全仓 `addFilter` / `logging.Filter` **零命中**（无任何日志级 filter）；
     `yiban.masking.mask_phone` 只被**少数调用点显式调用**（`cli` 账号列表、`config_check`、
     `round` 的汇总行），因此脱敏是**按调用点手写的纪律**，漏一处就漏一处。
  2. **实测泄露面**（生产原始日志 `/var/log/yiban/sign-2026-09-23.log`，未经任何导出）：
     - `yiban: [189****2278] ⏹️ 用户已取消签到，跳过执行` —— 来自 `yiban/engine/round.py` 的
       `logger.*(f"[{phone}] …")` 系列（该文件十余处同形）；
     - `yiban.store.session_cache: 会话缓存作废（…）: 178****5321，本次真实登录`
       —— `yiban/store/session_cache.py:174`（同文件 `:173` 的"清理过期会话缓存失败"同病）。
     - **量级**：`session_cache` 那条**每账号一行**（当日 78 行 = 78 个明文号）。
  3. **展示/导出层是第二处且口径不完整**：用户桌面那份导出件里 `[…]` 形态已被遮成 `189****2278`，
     但 `session_cache` 那种"中文逗号分隔的裸号"**漏过**了 ⇒ 展示层只覆盖方括号形态。
  4. 与交接文档 **P-1（日志脱敏不一致）同源且未修**：P-1 记录的 `round` / `runner` / `session_cache`
     打裸号在生产**仍复现**；而已手工脱敏的 `yiban.fyiban.protocol` 等模块是"做对了一半"的部分。
- **修法（单一来源 + 可执行不变量，推荐按序）**：
  1. **兜底**：在日志输出面挂一层脱敏（`logging.Formatter` 子类或 handler `Filter`，对最终消息做
     幂等的「11 位号 → 前3****后4」替换），使**任何调用点都不可能留下裸号**，包括将来新写的日志；
     **挂载点需先清点**（引擎 CLI 的 `cli_support`、web 的 handler 初始化、容器调度器各自的日志初始化处）。
  2. **点修**最直接的泄露口：`yiban/store/session_cache.py:173-174`。
  3. **展示/导出层复用同一实现**，禁止第二套口径（两套必然再分叉）。
  4. **补可执行不变量测试**：构造一条含裸号的日志记录 → 断言输出为遮罩形态。
     这样"又漏了一处"不再靠人眼查（本缺陷已因纪律失效复发过一次）。
- **处置时点**：M2 的根因调查**已完成**（见上）；修复归 **M3**——**必修，不得裁撤**。
  生产生效需下一次部署；若要求更快，属单独 hotfix 决策。
- **红线关联**：脱敏属 PROMPT.md §6.10 门禁口径（安全测试第一条），修复后需补/改对应测试。

## MF-2 vshard 集中错配

- **现象**：虚分片（vshard）分工可能出现「集中错配」——账号/任务堆到少数分片或执行体，具体表现面待 M2 定位。
- **调查线索**（M2 逐码审查时查原因）：
  1. 由 `docs/dev/ledger-dual-version-design-20260923.md` §5 记录，归 **§12 自适应体系批**，本设计不展开；
  2. 相关面：`yiban/engine/hrw.py` 的 vshard 计算与 `yiban/engine/planner.py` 的按 vshard 批量领取/交接——
     核查分工是否真均匀、K 变化或账号分布偏斜时是否退化。
- **处置时点**：§12 批——M2 逐码审查时调查并登记根因，M3 随 §12 一并修复。

## MF-3 density 单桶口径

- **现象**：密度（density）按单桶/均一口径估算，未按峰值约束与「窗口 − 重试储备」整形，具体表现面待 M2 定位。
- **调查线索**（M2 逐码审查时查原因）：
  1. 由 `docs/dev/ledger-dual-version-design-20260923.md` §5 记录，归 **§12 自适应体系批**，本设计不展开；
  2. 相关面：`yiban/engine/schedule.py` 的容量口径与 `yiban/engine/planner.py` 的密度/片预算——
     核查「单桶」假设在自选片与非自选片混合时的偏差。
- **处置时点**：§12 批——M2 逐码审查时调查并登记根因，M3 随 §12 一并修复。

---

## M2 注释整备流带出的缺陷（2026-09-25 登记，MF-4 起）

> **来源**：注释流 A1~A6 + B1~B4 共 10 组、195 个文件的整备过程。**本流一行代码都没改**
> （已用可复跑工具证明 195/195 为纯注释改动），以下全部是"看清了代码在干什么"之后撞见的缺陷，
> 一律**只登记未修**。证据标记：✅=协调者或汇总者真复跑过；📄=静态读码可核（命令已给）；
> ⚠=代理自报、数字或结论待独立取证。
> **下一空号：MF-40**（审查流若要登记，请从 MF-40 起，避免撞号）。

### 高

## MF-4 `sanitize_text` 的凭据值会被空格/逗号/分号截断，明文尾巴外泄 ✅

- **现象**：通用凭据键规则的值域是 `[^\s,;]+`（`yiban/masking.py:63`），遇到分隔符就停，
  只遮住值的前半，**后半原样留在文本里**。实测：
  `refresh_token="abc def"` → `refresh_token=*** def"`；`session_cookie="a b c"` → `*** b c"`；
  `csrf="x y"` → `*** y"`；`api_key=foo bar` → `*** bar`；`refresh_token=abc,def` → `***,def`。
- **关键对照（说明这是遗漏不是设计）**：同文件 `:41` 已有能跨空格配对引号的
  `_QUOTED_OR_BARE`，但**只挂在 `password` / `phone_code` 两条规则上**（`:55`/`:58`），
  凭据键那条（`:62-66`）没挂。而 `:39-40` 的注释写着裸值模式"会在值内含另一种引号时截断，
  残留首引号之后的明文"——作者为 `password` 踩过这个坑并修了，**同族规则没一起修**。
  另：裸值含 `,` / `;` 时 `password` 同样残留（`password=abc,def` → `password=***,def`），
  因为 `_QUOTED_OR_BARE` 的裸值分支也是 `[^\s,;]+`。
- **为什么算高**：`sanitize_text` 是异常消息、HTTP 错误文案、告警正文的**最后兜底面**，
  漏了没有任何下游会再拦；且完全静默。
- **复跑**：`python -c "import sys;sys.path.insert(0,'.');from yiban import masking as m;
  print(m.sanitize_text('refresh_token=\"abc def\"'))"`
- **修法方向**：把 `:63` 的 `[^\s,;]+` 换成 `_QUOTED_OR_BARE`，并重新决定裸值是否该以
  `,` / `;` 终止（cookie 串 `a=1; b=2` 需要逐对遮，这是该模式存在的理由，不能简单放宽）。
  **补可执行不变量测试**：含空格/逗号/引号的凭据值 → 断言无明文残留。
- **关联**：与 MF-1 同族（都是"脱敏是按调用点/按模式的纪律，漏一类就漏一类"）。
- **处置时点**：M3，必修。

## MF-5 `test_host_exit_semantics.py` 断言的是**抄进测试文件的生产逻辑副本**，且副本已与生产漂移 ✅

- **现象**：该类不 import `runner`（全文件只 `import signin`），自己写了 `_compute(statuses)`
  （`:42`）复刻 `main()` 尾部汇总判定——文件 docstring `:40` 自承"复制 main 尾部判定"。
  副本已与生产**不一致**：生产 `yiban/engine/runner.py:531-533` 把 `STATUS_NO_POSITION` 归入跳过集合，
  副本的 elif 集合没有它 ⇒ 同一输入副本判 1、生产判 0。
- **活体复现（在 `git archive` 出的临时副本里做，未动仓库）**：把生产
  `runner.py:620` 的 `if not has_executed or has_window_skip:` 整条改成 `if False:`
  （即摘掉"窗口外未了结 ⇒ exit 2 ⇒ run.sh 触发 07:10 补签"这条规则），
  该测试文件**仍 18/18 全绿**。
- **为什么算高**：这条正是"部分成功压制补签"的唯一闸门。闸门坏掉时**没有红灯**——
  测试给的是假的安全感，而这属于静默漏签方向。
- **处置时点**：M3。修法是让用例经 `signin.main` 真实出口驱动，而不是复刻判据；
  或至少加一条"副本与生产判据同源"的钉。注释流已把风险写进行内注释（`:44`），但**注释不能代替测试**。

### 中

## MF-6 容器侧签到门与宿主门分叉后无信号 📄
`docker/scheduler.py` 的 `_UNDONE_STATUSES` 只是 `signin.UNDONE_STATUSES` 的别名、
`_full_run_done_today` 转调 signin，但 `tests/test_container_scheduler.py` 的闸门用例**一律直接调容器侧自己的谓词**，
全文件 `assert_called` 仅 2 处且都是 `window.from_env`——没有任何一条打桩证明转调真的发生。
⇒ 若有人把转调改回"容器另写一套门"，无人拦得住。补：断容器侧**转调**宿主谓词。〔B1〕M3。

## MF-7 `session_cache` 解密失败分支把明文手机号写进日志 ✅
同函数其它路径用 `_mask_phone(phone)`，该路径（集成树实测 `:182`；A1 首批报时为 `:187`）直传 `phone`。
兜底 `MaskingFormatter` 全仓**只有两处装配**（`web/app.py:1616`、`yiban/engine/cli_support.py:198`），
不经该 formatter 的 sink（裸 StreamHandler、`caplog`、测试自建 handler）就是裸号。
〔A1 提出，A1-r2 / A3-r2 复核仍在并补覆盖面〕属 MF-1 点修清单的一部分，M3。

## MF-8 手机号兜底脱敏覆盖面比宣称窄 ✅
`masking.mask_phones_in_text` 只认**连续 11 位数字**：`+8613800138000`、`138-0013-8000`、
`138 0013 8000` 实测原样穿过；且保护与否取决于"该 handler 有没有挂 formatter"。
`test_log_masking_formatter.py` 原头部曾把它写成"任何一条日志记录经格式化后都不再含裸号"——
注释面已由 A3-r2/B2 改成如实表述，**代码面未动**。复跑：
`python -c "import sys;sys.path.insert(0,'.');from yiban import masking as m;
print(m.mask_phones_in_text('+8613800138000'), m.mask_phones_in_text('138-0013-8000'))"`。M3，与 MF-1/MF-4 同族。

## MF-9 `sanitize_url` 完全不解析 fragment ✅
只取 `parts.query`，故 `https://x/cb#access_token=ABCDEF` 原样返回。OAuth 隐式流把令牌放 fragment；
本项目链路上是否真会出现**未验证**（不夸大为"已确认泄露"）。〔A3〕M3 先确认是否可达，再决定是否补。

## MF-10 备份明文告警的用例是假绿 ✅
`tests/test_db_integrity.py::BackupPlaintextP3Test::test_plaintext_local_warning_present` 的实断只有
`assertIn("BACKUP_PLAINTEXT=1", src)` 与 `assertIn("明文", src)`，两者在 `backup.sh` 的**纯注释行**里就已满足。
临时副本内删掉 `scripts/backup.sh:552-555` 四行大字告警 ⇒ 该类 **4 条用例仍全绿**。
同类 `test_remote_still_attempts_encryption` 只断两个标识符存在，与"本地明文时异机仍加密"无因果。
⇒ "备份变成明文"这件事的"看得见"其实没被钉住。M3 改成行为断言。〔B2〕

## MF-11 `.bak` 凭据文件 0600 的断言在 Windows 上不存在 ✅
`tests/test_account_plaintext_patch.py:139`、`:167` 两处 `if os.name != "nt":` 把权限断言整块包住
⇒ 项目主开发平台（Windows）上这条凭据文件权限面**长期无人守**，只有 POSIX 形态才核。M3。〔B2〕

## MF-12 node/子进程 harness 缺 `encoding="utf-8"`，4 只前端测试在本机长期红 📄
`test_delay_ack_frontend.py:422,649`、`test_web_font_closure.py:212`、`test_logs_by_date.py:409`、
`test_schedule_edge_limit_js.py:59`、`test_dashboard_stats_caliber_js.py:71` 均写
`subprocess.run([...], capture_output=True, text=True, timeout=…)`，中文过 node 边界回来按 GBK 解码
⇒ 乱码或取不到 stdout。B3 用"回退成基线内容复跑、结果字节级相同"证明属 `9d3f491` **存量红**，非注释引入。
后果是这些文件长期占红、掩盖真实回归。M3 加 `encoding="utf-8"`。

## MF-13 `claims` 与 `queue_store` 双台账窗口期无代码级互斥 📄
展示/了结判据读旧表（`claims.stats/activity/owners_for_day`），调度 v3 的写只进新表（`queue_store`）；
两个写者靠"当日单一写者"的**运维规则**互斥，代码层无保障。窗口封口时点未定。
⇒ 规则被破即静默失真。见 `yiban/store/queue_store.py:26-42`、`executor_v3.py`、`round.py:193-224`。〔A1〕

## MF-14 源文本断言型用例占比过大 ⚠（方向成立，计数待取证）
断"某文件里有没有某段字"而非行为 ⇒ 实现改名/搬文件就误红，行为坏掉可能反而不误红。
旁证（本项目自记）：`test_executors_kpi_scope.py` 写明"把 payload 键改坏 + 注入一行注释，
旧用例仍绿"。⚠ B1 报"34 只中 21 只"，汇总者粗口径复核得 25 只，且 `test_egress_and_executors_api.py`
报 69 / 实测 43、`test_executor_manifest.py` 报 30 / 实测 12 ⇒ **精确数字不可信，先定义计数口径再立项**。
M3 前排产：需要先定"什么叫源文本断言"的判据。

### 低（批量登记，一行一条）

| MF | 位置（文件:符号） | 现象 | 据 |
|---|---|---|---|
| MF-15 | `web/app.py:_high_risk_gate` vs `settings_api.py:_executor_write_guard` | 同一道口令门的拒绝留痕强度不一致：执行体写路径 `db.audit("executors_pw_fail")`，高危门禁直接 `return pw_err` 不落审计 | ✅ |
| MF-16 | `yiban/notify/ledger.py::_loginfail_daily_limit` | `max(0, int(v))` 把负值收敛成 0，而 0 在本层＝"不限额" ⇒ 配置手滑的方向是**变宽**（fail-open） | ✅ |
| MF-17 | `web/services/capacity.py::_registration_paused` | 开关只认整数 `1`，写 `true/on/yes` 被读成缺省 0（静默不暂停注册）；同仓 `signstatus._env_flag` 认四个真值字面量、`run.sh` 用 `_is_truthy` | ✅ |
| MF-18 | `yiban/masking.py::sanitize_url` 另三面 | ①`;` 不作参数分隔，`;` 后段落进前一参数值原样回显；②高熵兜底是"值全为 `[A-Za-z0-9_-]`"的形式判定，含 `% + .` 的长令牌不命中；③返回**解码后**重拼的 query，只可作日志文本，回填去发请求会改变语义 | ✅ |
| MF-19 | `tests/test_admin_creds_masked_ops.py::test_my_accounts_logs_masked` | 只喂方括号形态 `[13800138000]` 却声称"与 /api/my-logs 口径统一"；裸号形态在该出口无覆盖 | 📄 |
| MF-20 | `yiban/engine/planner.py::has_plan` ↔ `executor_v3._max_vshard` | 两条"当日有无计划"判据不同源（`has_plan` 只看 `day`，把 `vshard=-1` 惰性行也算"有"）；且异常分支日志文案"按无计划处理，走动态领取"与实际行为不符 | ✅ |
| MF-21 | `tests/test_state_file_writes.py::test_signin_state_writers_use_replace` | 字符窗钉很薄：窗口起点是 `def` 而非函数体首行、docstring 也计入，区间内任何正常改动都会红。集成树实测 `_write_sign_state` 的 `os.replace` 在 2257/2500（余 243，注释轮把它从 2468 往回推了） | ✅ |
| MF-22 | `yiban/store/events.py::add_sign_event` | `conn` 在 `get_conn()` 抛错时尚未绑定，`except` 里 `conn.rollback()` 触发 `UnboundLocalError`，被外层 `contextlib.suppress` 吞掉 ⇒ 回滚未执行、原始异常只剩一条 `%s` | 📄 |
| MF-23 | `web/services/capacity.py::_notify_capacity_once` | 去重旗 `_capacity_alerts[kind]=True` 在 `send_notification` **之前**置位且失败不回滚 ⇒ 首次触顶赶上 SMTP 故障则该资源容量告警永久静默 | ✅ |
| MF-24 | `web/services/logs.py::_cred_paused_phones` | 读失败退化为空集合 ⇒ 页面"0 个暂停"与"确实没有暂停"不可区分（注释自述只服务展示，不参与判定） | ✅ |
| MF-25 | `yiban/notify/ledger.py::_throttle_path` | 磁盘节流表键是**告警标题原文**，标题里的账号标识等信息不脱敏落盘，仅按冷却期过期删除 | 📄 |
| MF-26 | `yiban/engine/alerts.py::_maybe_alert_zero_success` | `is_second_run` 是死形参、`state_io._sched_marker_exists()` 是死赋值（赋值后未被读），每次调用多读一次状态目录 | ✅ |
| MF-27 | `yiban/status.py` 池侧词表注释（第 113-114 行） | 注释称池内 `done` 涵盖 `no_position`/`skipped_window`，与 `migrations.py:842-847` 的 v18 平移表及 `executor_v3.py:466` 运行时事实**相反**。**跨组漏网**：A1-r2 在自己清单外发现，A3 是该文件归属组但未改 ⇒ 需派单 | ✅ |
| MF-28 | `web/static/js/components/settings-quota.js::paintButton` | 冷却剩余由前端按秒自减，后端另有 429 `next_allowed_in` ⇒ 两处口径并存 | 📄 |
| MF-29 | `web/static/js/core.js::dangerousSubmit` | `triedPw`/`triedAck` 是整串请求共用的一次性标记，凭据需求落在后段时直接上抛而非再问一次（"后段静默失败"，是否可接受需产品确认） | 📄 |
| MF-30 | `tests/test_schedule_retry.py` | bash 缺失时真起 bash 且**失败而非 skip**，同批 `test_run_sh_workers`/`test_scheduler_gate`/`test_yiban_fallback_sh` 都有 `skipIf`，口径不一致 | 📄 |
| MF-31 | `tests/test_capacity_audit_scope.py::SignsInSingleSourceTest::test_predicate_matches_runtime_gate` | "一致性"那半句按构造恒真（`is_signable` 末行就是 `return signs_in(...)`），两条断言只能同时红、不可能单独红 | ✅ |
| MF-32 | `tests/test_global_pause.py::test_manual_signin_not_blocked` | 只断 `assertNotEqual(code, 2)`，任何非 2 出口都通过，区分不了"手动放行进入签到"与"另一条跳过分支" | ✅ |
| MF-33 | `tests/test_docker_image_contents.py::test_imported_local_packages_are_copied` | 覆盖面单向：只认 `COPY <pkg>/` 前缀，Dockerfile 改 `COPY . /app` 会误红（不会漏报）；判据行尾已写明"别指望它兜底" | ✅ |
| MF-34 | `scripts/backup_sentinel.py` + `tests/test_backup_sentinel.py::SentryVerdictTest` | 缺包/缺清单/漂移三类**告警发出后 `main()` 仍返回 0**，返回 1 只发生在"发不出去" ⇒ cron 侧看到的是正常退出（"哨兵响了没人知道"方向是静默）。原头注释与事实不符，注释已由 B4 改述 | 📄 |
| MF-35 | `tests/**` 夹具与常量里的明文假手机号 | **71 只** `tests/test_*.py` 含明文形态手机号、共 **714 处**（其中 48 只匹配 `138001…` 形态）。⚠ B4 自报"279 只文件"已被协调者否证（`tests/` 总共才 137 只，不可能 279）。假号非真实 PII，是否统一属卫生裁定；B 批禁改夹具故未动 | ✅（协调者复算） |

**本流不重复登记的既有项**：SL-1 / SL-3 / SL-5 / SL-13 / SL-16（见 `security-slim-plan-20260923.md` §9）各组均确认无新证据。

**本流已否证、不要再去查的**：A2 首批报"round.py 余量不足无法整备"——回炉批实测反而净减 43 行
（590→547），说明"余量不够"是旧落点风格（头部散文）的产物，不是真实约束；
`store/accounts.py` 与 `store/db.py` 的双份实现在 A1-r2 复核中**已不成立**（首批结论被自己第二遍推翻）。

## MF-36 措辞门禁按裸字符串扫描、分不清语境，且 JS 里已有 2 处潜伏命中 ✅

- **背景**：注释流一度把 `test_ambiguous_wording_is_gone` 跑红（两处是我们自己写进去的注释，
  已在 `5bc77cc` 改写修掉：一处为解释规则而**复述了被禁写法**，一处是无关语境用了"两类调用点"）。
- **仍未修的是门禁本身的脆性**：它的判据是 `(?<!至少)两类` 这种**裸字符串扫描**，
  取证面又是 `_frontend()` 的**聚合源码**（模板 + 递归 include + 外链 JS）且**含注释**，
  ⇒ 任何无关语境下正常使用的中文数量词都能把它顶红，而报错信息会误导人以为是口令文案出了歧义。
- **潜伏命中 2 处**（当前不红，只因为这两个组件不在 `PW_TEMPLATES` 那两页的聚合路径上）：
  `web/static/js/components/settings-switches.js:35`、`web/static/js/pages/data_dashboard.js:313`。
  一旦有页面引用它们，门禁立即变红且原因看似无关。
- **建议处置**：把判据收窄到"只在承载口令策略文案的常量/模板节点里查"，
  或要求被禁写法只在字面量参数区扫描；同时把上面两处 JS 用词改掉以消除潜伏命中。
- **复跑**：`grep -rnP '(?<!至少)两类' --include=*.py --include=*.js --include=*.html web/ yiban/ scripts/`
- **处置时点**：M3。**另记一条方法论**：AST 等价与"剥注释后逐字比较"这两种证明都把注释当噪声丢掉，
  而本门禁恰恰只看注释 ⇒ **"纯注释改动"的证明不等于"不会改红测试"**，按源码文本判定的门必须靠跑测试兜。

## MF-37 派发计划声明的全量门禁基线在本机不可复现 ✅

- **现象**：`m2-dispatch-plan-20260924.md` 与两份任务书都写死
  `pytest -q -n auto --dist loadfile` → **2928 passed / 4 skipped / 0 failed / 57.43s**。
  本机（Windows `.venv`，非 WSL）实测：**基线树 `b26736f` 跑出 6 failed / 2900 passed / 7 skipped / 19 errors / 86.62s**。
- **差异来源（已定位，非本流引入）**：4 只是 `test_scheduler_gate.py::BackupDockerScriptTest`（本机无 docker）、
  1 只是 `test_delay_ack_frontend.py` 的 `MailClearAdminToTipTest` + 该文件 19 只 error（即 **MF-12** 的 node harness
  缺 `encoding="utf-8"`），另有 `test_login_e2e_mock.py` / `test_manual_sign_reporting.py` 在 `-n auto` 下**时红时绿**
  （串行复跑即绿，属计划§5 第 7 条已登记的间歇用例）。
  WSL 侧 `~/.venv-yiban-wsl` 存在但**无 pytest**、且该 WSL **无 docker**，说明 57.43s / 0 failed 那组数字
  是在第三套环境下取得的，仓库里没有记录是哪套。
- **后果**：以"0 failed"为放行条件会在本机永远无法满足，容易被误读成本流改坏了代码；
  反之若有人为了凑绿而放宽门禁同样危险。
- **建议处置**：把门禁基线改写成**分环境的已知失败集合**（哪些用例在缺 docker / 缺 node / Windows
  下必然红），并记明测于哪台环境；间歇用例单列一份清单与判定规则。属 M3 的排产项，但
  **在下一轮派发之前就该修正计划文本**，否则每轮都要重新吵一遍"到底谁红的"。

## MF-38 `ManualSignExitTest` 只在全量套件下失败，单独跑恒绿 ⇒ 跨文件干扰，门禁不可判红 ✅

- **测得事实**（同一台机、同一 `.venv`、`-n auto --dist loadfile`，两棵树各跑一遍做集合级对照）：
  注释流分支比基线多红 2 条 `tests/test_manual_sign_reporting.py::ManualSignExitTest::{test_single_manual_sign_exit3_logged_as_failure, test_batch_manual_sign_exit3_logged_as_failure}`；
  基线反过来多红 1 条 `tests/test_login_e2e_mock.py::LegacyLoginE2ETest::test_legacy_chain_rehearsal`。
  **两边红的不是同一只**，且都是退出码类用例。
- **单独跑该文件 6/6 全绿**：串行 3 次（3.19 / 2.87 / 2.90s）、`-n 8` 并行 3 次（2.42 / 2.46 / 2.39s）**全绿**。
  ⇒ 不是该文件自身的随机性，而是**全量套件里的跨文件干扰**（进程/环境/时序），只在 `-n auto` 满负载下现形。
- **与本流无关**：注释流已证明可执行语义逐字不变（`5bc77cc` 之后重测），且基线树同形别地飘。
  这条**不应记在注释流账上**，但必须登记，因为它使"全量绿"无法作为放行判据。
- **后果**：任何以"全量套件必须 0 红"为门槛的流程，都会在这两条上来回误判——
  本轮我最初就把它当成注释流的回归查过，白耗一轮。
- **建议处置**：
  1. 定位干扰源（`ManualSignExitTest` 起子进程跑 `signin`，怀疑与并发用例共享
     `YIBAN_STATE_DIR` / `YIBAN_DB_FILE` 或退出码读取受 CPU 抢占影响）；
     `tests/conftest.py::_restore_environ_around_class` 的说明里已记过同类环境泄漏史（`test_mailer` + `test_scheduler_gate` 组合必挂），可顺同一线索查；
  2. 短期先建立**已知间歇清单 + 串行复跑即判绿**的规则，把本条两只与
     计划§5 第 7 条那批一并纳入，避免每轮重新吵。
- **处置时点**：M3 测试质量批。

## MF-39 `ruff` 门禁命令不扫 `web/`，那里有 2 处 RUF100 长期看不见 ✅

- **现象**：项目所有文档与派发纪律里的门禁命令都是
  `ruff check yiban/ tests/ scripts/ --quiet` —— **不含 `web/`**。实测 `ruff check web/` 报 2 处 RUF100
  （`web/app.py:218-222` 一带与 `:364` 的 `# noqa: F401` 已无必要，因为对应导入实际被用到）。
- **归因**：**非注释流引入**。同一命令在 `9d3f491`、develop 合并后、以及当前工作树上**都是 2 处**，
  即基线就有；只是没人跑过 `web/` 那一档。
- **为什么值得记**：这与 `pyproject.toml` 设 `line-length=100` 却 ignore `E501`、CI 只跑 `ruff check`
  不跑 formatter 是同一族问题——**流水线看不见的区域会稳定积累腐坏**。
  `web/` 是 29 个 py / 12,784 行的最大面，恰好整个在门禁命令之外。
- **建议处置**：把 `web/` 纳入 `ruff check` 范围（命令与 CI 一并改），先清掉这 2 处 RUF100；
  同时复核还有没有别的目录在命令范围外。属 M3 小批。
- **复跑**：`D:/code/yiban-auto-sign/.venv/Scripts/python.exe -m ruff check web/ --quiet`

**关于 MF-36 的补充（2026-09-25）**：`web/` 内两处潜伏命中已由 `574c7ee` 清掉
（`settings-switches.js:35`、`data_dashboard.js:313`，改为"这两种/这两组"）。
`yiban/` 下还剩 3 处裸「两类」——`engine/executor_v3.py:645`、`engine/state_io.py:6`、
`infra/env_io.py:182`，均在措辞门禁的扫描面**之外**且属正常中文表述，本轮不动，
留作"若将来扩大扫描面会一并变红"的备忘。
