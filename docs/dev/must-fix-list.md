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
> **编号预约已兑现（2026-09-25 汇总）**：审查流初稿原 MF-4..25 按本节预约整体 **+36** 重编号为 **MF-40..61**；跨流对账（FA-vs-ANNOTATE）带出的 5 条注释回归记 **MF-62..66**；注释流据那 5 条反查代码、新立 **MF-67**（领取层无当日跨轮上限）。
> **下一空号：MF-68**（审查流剩余 L2/L3/S/semgrep 的产出从 68 起排；`MF-67` 已被占用，勿双写）。

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

---

# M2 审查流检出（MF-40..66，2026-09-25 汇总入库）

> **来源与编号**：审查流初稿 `_scratch/m2rev/out/MF-DRAFT.md`（原编号 MF-4..25）按注释流在上一节写明的
> 预约协议整体 **+36** 重编号为 **MF-40..61**。基线 `9d3f491`（现网 `server-web@6d4eafa`，分叉）。
> 来源层：L1 84/84 单元 + 裁决 V1–V8 + S 黑箱 + L2 两链 + L3 + 假担保两遍 + 注释流对账。
> 每条给"验收不变量"= 一条可执行断言；现网三态标注沿用草案：`[已复现]` / `[未部署]` / `[需确认]`。
> **覆盖边界见文末覆盖面声明——别把这份当"查过了"。**

## A 簇 · 部署日阻断（P0，先于一切修复）

### MF-40 升线第一次启动会全员拒签且无失败信号
合并 L15、L16、L17、链5-C5-4。根因：**v18/v19/v20 标"可选"实为新路径硬前置**——任一失败只发 warning、`user_version` 永不提升，而 `try_claim` 把 `epoch` 当硬编列名；现网 `user_version=17`、`sign_claims` 缺 `epoch`。同簇连带：`_ensure_column`/`_ensure_index`/v5 内部的 `conn.commit()` **提前结束 `BEGIN IMMEDIATE`**（"整段迁移原子"三处注释 + 设计 §3.3 均不成立，实测第二连接可 BEGIN）；`migrate_v5` 缺 `DROP TABLE IF EXISTS users_new` ⇒ 半程崩溃后重跑必 `table already exists` = **启动永久阻断**；`migrate_v20` 状态目录读不到时静默返回 0 **却照样提升版本**；v18/v20 以 `vshard=-1`+`INSERT OR IGNORE` 占 `(phone,day)` ⇒ 被补成 failed/skipped 的账号**永久失去当日计划**，而 `pending_count` 只数 pending，闸门显示"已了结"；`_rename_backup` 先写 tmp（umask 0644，含明文凭据）**才** chmod ⇒ 失败即永久残留。
修法方向：可选/硬前置要分级并显式声明；迁移要有记录表与"未完成即拒绝启动"的 fail-closed；补 `DROP TABLE IF EXISTS`；v20 读不到目录**不得**提升版本；`INSERT OR IGNORE` 占位改成显式标记行不与计划同键；重加密走"临时文件权限先于内容"。
验收不变量：① 启动时若 `epoch` 列缺失 ⇒ 非零退出并点名缺哪条迁移；② 每条迁移跑完断言"行数守恒 + 版本已提升"；③ 一个"迁移中途 kill"的用例必须能重跑成功；④ tmp 文件权限断言。
`[需确认：现网 v18–v20 是否落地]`

### MF-41 基线拉不到 + 生产有两个基线没有的提交
合并 L61、L62。`gitee/develop` 实测停在 `b6457e6`，`9d3f491` 只在本地；现网部署命令是 `git pull gitee server-web` ⇒ **按现流程根本部署不到 M2 审的这份代码**。且 `rev-list --left-right --count ≈ 2/100`：生产有 2 个基线没有的提交（合计 1 行：备注 placeholder 示例改"电力123庄方宜"，`6d4eafa` 为空合并），merge 三方干净但 `checkout/reset` 到基线会**静默丢掉**且全仓无测试钉住。同提交链上另发现：GitHub 仓为 **PUBLIC**，该姓名样式示例已随 `origin/server-web` 外推。
修法方向：先统一发布线（哪个分支是部署线、远端是哪个），再把那 1 行回合或显式放弃，并决定公开仓是否要放真名样式示例。
验收不变量：部署前一条命令断言"目标提交在部署远端可达"。`[已复现]`

### MF-42 生产执行件不在版本控制里（部署漂移的根）
合并 L56、L57。`/usr/local/sbin/yiban-backup.sh` 是手工拷贝（09-23 已同步，但基线那份与其差 3 行）、`.bak-20260923` 旧件仍在、**`yiban-backup-wrapper.sh`（口令注入点）与 `/etc/cron.d/yiban-{cleanup,sign,probe}` 三张 cron 表全仓无原件**。⇒ "审仓库 ≠ 审生产"这条结构性缺口。连带：`backup.log` 实测 **0644 全局可读**（同目录 `cleanup.log` 是 0600），与 `backup.sh:643-645` 自述"哪天有备份=删链地图"冲突；wrapper 用 `export` 把口令带进整棵子进程树环境。
修法方向：这些件要么入库+安装脚本（`install` 目标），要么明确"生产专属"并纳入巡检清单；`backup.log` 收权限；口令改 `--passphrase-fd 0` 单跳传递不进环境。
验收不变量：一条 grep 断言"cron 引用的每个路径都能在仓库或安装清单里找到来源"。`[已复现]`

---

## B 簇 · "会漏签但没人知道"（P0/P1，四条独立失效指向同一结果）

### MF-43 补签链：设计是常驻兜底体，实现是"轮末问一次"，且问的输入本身是坏的
合并 V6、L2-A(1-1/1-2/2-1)、FALLBACK-DESIGN-VS-CODE、FALLBACK-PURPOSE。**这是你那个设计问题的完整答案**：
- **② 进程内第二轮**：`workers._await_workers` 交出 `Popen.poll()` 原始码，被信号杀 = **负数**，而 `run_worker_supervisor:172-179` 只认 10/1/3/2、**其余归 0** ⇒ `run.sh` 写 `SUCCESS` ⇒ 补签判定被同一份 rc 否决、`SECOND_DONE_MARKER` 无条件封存。同族另有两处："未领取账号在 `claims.stats` 里既不算 open 也不算 total"（⇒ 整批没领被判"无未了结"）、收尾标记由**子执行体各写一份**（`runner.py:609`）。
- **① 常驻兜底体确实存在**（`workers.py:184 run_fallback_worker`），但不是"失败即入队"：它**每 60 秒扫全量**、靠领取池去重，且 `:246` 让位分支在**全量轮持锁期间一个账号都不接**——失败集中的那 06:31–07:28 全程它休眠。
- **③ 07:12 固定轮既非冗余也非坏件**：它是唯一"主进程消失后才动"的恢复腿（`flock` 即存活判据），删掉即失去 06:31–07:12 崩溃/OOM 的当日恢复（08-05 有真跑实证）。它今天不触发，是**被 ② 的坏 rc 弹开的**。
- **B 类分支错误**：`--fallback` 在 `runner.py:185` 早于 `:462` 的 v2/v3 分流返回 ⇒ 兜底硬编 v2/`sign_claims`，**v3 的 `sign_tasks.failed` 当日无人回炉**（`claim_batch` 只取 pending）。且 `YIBAN_FALLBACK_ENABLE` 缺省关 + **设了键也不拉进程、须手工加 cron** + 清单里标 `type=fallback` 同样不拉进程。
修法顺序（不可颠倒）：② rc 契约修好 → ① 改成真事件驱动且让位只让"同一账号"不让"整段" → ⑤ 给 v3 `failed` 回炉口 → 才谈撤 ③。撤 ③ 前置 6 条：常驻真在跑（开关+cron 件）、rc 已修、让位不整段停摆、单进程吞吐判据、v3 有回炉口、四条契约迁移（含 `PROMPT.md §6.10`"有效轮次"定义需你点头）。
验收不变量：任何非零退出（含负数/信号）不得被写成 SUCCESS；marker 封存必须以"确实无未了结"为前置；开关置 1 后若无进程，启动即告警。`[需确认：现网 fallback 开关状态（.env 不可读）]`

### MF-44 告警链：四种"发了但没到"和"该发但静默"叠成完全无声
合并 L27、L28、R4c、R10a、R10b。`send_admin_alert` 返回 False 被 `notify_mail.py:184` 丢弃 ⇒ `degraded` 恒假、周报走**同一条 SMTP**、`channel_health.py:409` 在被吞失败后**照写去重标记**（docstring 与实现相反）；记账三面不一致：额度有 `BudgetTicket` 退还，**去重位在扣额度之前写盘且永不回滚**，`notified` 取走即置位无退还；Server酱 返回合法 JSON 但非对象 ⇒ `AttributeError` 逃逸、`_refund_daily_budget` 被跳过（**额度 5→4 且永不恢复**）；custom 出口按 `status_code<400` 判成功 ⇒ 3xx 与"200+错误 body"都算已送达并真实扣额；`URGENT_ONLY=false` **静默保持开启**、`DAILY_MAX=-1` 钳成不限额、`is_configured` 与 `send` 在 TYPE/白名单两处口径分叉。邮件正文侧：对 `yiban.masking` 零依赖、`sanitize_text` 不遮裸号、告警正文含被爆破账号明文邮箱、`_fold` 漏 9 个换行族字符可伪造正文行、`user` 字段零校验可致 SMTP 命令注入。
验收不变量："送达"与"已发送"必须是两个状态且都入库；任何额度占用必须有对应送达回执或退还；一条"SMTP 拒收 → 仍有声音"的端到端用例。
`[需确认]`

### MF-45 急停在面板上不可见（与 MF-46 成环）
合并 L30、L58、R14h。`sign-calendar-view.js:30-44` 漏 `global_paused`/`no_position` ⇒ 急停渲染成"排队待签"；日历**图例只覆盖 12 种状态里的 2 种**（双单元撞实）；三个窗口告警标记**全仓无生产复位点** ⇒ 常驻进程第 2 天起彻底无声；web 侧急停/周末门经 `day_off(env=None)` 落回 `os.environ` ⇒ **在 web 侧恒不生效**（引擎读 `.env` 真值，所以实际会暂停、界面却说没有）。
验收不变量：状态枚举与图例同源（一份表两侧消费）；任一告警标记有显式复位点并有用例。`[需确认]`

### MF-46 `.env` 的两套行模型 + 不校验本次值 + UI 静默换值 ⇒ 一次无关保存能改急停
合并 L01、L39、L60、L66。**V2 已活体复现**：web 侧 `write_env_batch:243`/`ensure_secret_key:280` 用宽 `splitlines()`（实测 10 个分隔符），引擎 `write_env_keys` 只认 `\n\r`，差集 8 个即潜伏面；注释里潜伏 U+0085 ⇒ 一次"只改签到模式"的保存把 `YIBAN_GLOBAL_PAUSE=1`、甚至 `ADMIN_PASSWORD_HASH=pwned` **实体化成真配置行**，不过 A 档口令门、不进审计（还反写"全局暂停=不变"）、写后 `find_env_key_collisions` 归零＝痕迹被抹。反向：`write_env_keys` **对传入 value/key 一字不校验**（可把 NEL 写进盘）；`export KEY=` 在 shell/引擎/web 三处解析不一致且只有一处出声；`paint()` 把未知枚举静默换成**首项**并污染 `snap` ⇒ 后续无关保存会把它写进 `.env`；前端对行分隔符**零校验**。
修法方向：一处行模型、一处校验器（含全部换行族），写入前后各做一次"键集合 diff"并强制入审计。
验收不变量：任意"本次未请求的键发生变化"必须让写入失败；一个含 8 个宽分隔符字符的入参在两侧都被同一句拒绝。`[需确认：现网 .env 内容不可读]`

---

## C 簇 · 互斥与重复真实登录（P0，"认领两次"族的总账）

### MF-47 `(phone,day)` 互斥有六条绕过路径，且 `touch` 从未被调用
合并 V6、L03、L04、L41、R3g、R5d。① `reclaim=bool(args.only)` 使 done 行**不校验 owner/租约**即改回 claimed；② `owner = excluded.owner` 命中即**短路整个租约判据**，缺省身份 `single@{host}` 不含 PID ⇒ cron 与 web 手动零互斥；③ 领取与登录跨事务、收尾在整轮末尾 ⇒ 崩溃后 900s 被接管再登一次，而 **`claims.touch` 生产端无调用者** ⇒ 心跳永不续、任何 >900s 的账号对所有人可接管；④ v3 `requeue_task` 的 WHERE 不含 state、epoch 自愿 ⇒ 迟到重排把 done 复活成 pending；⑤ flock 分名（`.w{i}`/`.fallback`）且超时"无锁继续"，运行锁三条 fail-open（超时放行 / `OSError→None` 零日志 / 无 fcntl→未加锁句柄）**四个调用点全不检查返回值**；⑥ `claims.purge` 未接时钟跳变守卫（同库另 5 处都接了）⇒ 前跳 >14 天整删当日互斥面。另有 `_claim` 未初始化即放行（链3）、容器 scheduler 与宿主 cron 互斥面为 0（现网未部署容器形态）。**V5 已用临时库实验证明 v2/v3 同日双放行、V6 用双进程证明同名 owner 可双领取。**
验收不变量：领取必须"同事务内校验+写入"，owner 必须含 PID/代次；心跳由执行侧周期性写入并有断言（"任何 >900s 仍 claimed"必须被 reaper 处理，而不是被接管）；purge 走跳变守卫。`[需确认]`

### MF-48 三处零守卫清库 + `--dry-run` 假演练
合并 L38、L63、L68、L20、L55。`generate_demo_data.py` 守卫是 `db_path == "demo-log/demo.db" or yes` 的**字面串比较**，`--yes` 即清空五表且**删的正是 `audit_logs`、链校验反显 ok**、无倒计时零留痕；`seed_accounts.py` 更硬——**有清五表能力且零守卫**（连 `--yes` 都没有），还会改写生产 `.env`；`load_env_file:274` 读不到就静默继承宿主环境 + `clear_session_cache:284` 无门 DELETE ⇒ 误指生产库即 **89 号真实重登风暴**。包装层两个脚本的 `main(argv)` 是死参数 ⇒ **`--dry-run` 被静默丢弃并真删，rc=0 像演练成功**；`backup.sh` 旗标只认 `${1}` ⇒ `--require-encrypt` 移位即门禁静默失效；`state --yes` 无口令无审计、**按文件名删掉 `db --backup` 写进状态目录的副本**。
验收不变量：任何清库/删除类入口必须显式声明目标指纹（非路径字符串比较）+ 需要确认 + 入审计；被丢弃的参数必须报错而不是静默；"dry-run"三入口语义统一。`[已复现：demo/seed/包装层参数丢弃]`

---

## D 簇 · 隐私与凭据（P1，一条总闸 + 若干出口）

### MF-49 脱敏要分三类处置（不是"生产没实现"）
合并 L09、L10、L24、L25、L40、L45、L48、L51。基线**有**兜底 `MaskingFormatter`（`2437bfb` 引入，全仓仅 2 处 `setFormatter`：`cli_support:197`、`web/app.py:1641`）⇒ 缺口三类：**① 现网未部署**（三处装配计数 0，版本号同为 0.4.7 判不出，裸号行 09-22/23/24 = 258/314/368，`probe.py:100` 60/60 明文）；**② Formatter 结构上管不到**（`cli config/capacity/state/db/version` 无装配、`docker/scheduler` 的 `print`、`run.sh` 的 `2>&1`、`signin_api` 的 `stdout=log_fh`、`report_fatal_error`/`--json.errors`、`Account.__repr__`、**子进程 argv**、**进程 environ**、邮件正文、`sign_events.message`、坐标明文）；**③ 消费侧反向**（`q=` 先遮后滤 ⇒ 升级后日志页从"能搜到"变"搜不到"，`my.py:114-117/656` 按磁盘原文匹配；现网 `_mask_log_phones` 窄口径另有 124 个/日外发）。**出口字段**：`owner_display` 外发邮箱 @ 前完整本地部分（现网 9/96 即手机号）、`account-table.js:ownerText` 首选它、`account-form.js` 里 `email.split("@")[0]` 是同规则的**第二份定义**；`/api/users` 整表明文邮箱被 `apiCached` 写进 **sessionStorage**（含 `csrf_token`，logout 失败不清）；users 单条操作把明文邮箱编进 **URL path** ⇒ nginx `combined` 记 `$request`（同源 Referrer 再外送约 10 次）。审计侧 `actor` 列 61% 明文邮箱、`purge` 明文其余掩码（118→106 形态、12 碰撞）。
修法方向：**单一脱敏原语 + 全部输出面（log/邮件/JSON/argv/environ/URL/DOM/storage）消费同一份**，加可执行不变量；展示层禁止第二套口径；现网部署 + 版本号能区分有无兜底。
验收不变量：构造一条含裸号/明文邮箱的记录 ⇒ 断言每个出口都是遮罩形态（现在只测日志）。`[已复现]`

### MF-50 凭据加密密钥已不可安全轮换
合并 L18。工具链被 S1 删除后，README 手工流程只重加密库内两列、**漏掉同钥加密的 `.env` 密文**（`YIBAN_MAIL_SMTPS_ENC`、SendKey）⇒ 换钥即告警通道静默死亡；且流程只让改 `.env`，而现网 web 的钥由 systemd `EnvironmentFile=/etc/yiban/accounts-key` 注入、**env 优先级高于 `.env`** ⇒ 照做必致"web 一把钥、engine 另一把钥"；无 `kid`，动手前后都无法自证。密码学本身经生产实测通过（GCM/AAD/tag、重复 nonce 0/96）；但**无 rehash 升级路径**（现网 1 行 `role=admin` 仍是 `scrypt:32768:8:1`，N 减半）、`_encrypt_field` 把空凭据静默写 `""`、一行坏密文拖停整轮。
验收不变量：密文带 `kid`；启动时断言"两侧读到同一把钥"，不一致即拒绝启动而非静默解密失败。`[已复现（拓扑侧）]`

### MF-51 SMTP 条目以数组位置为身份 ⇒ 删一行即凭据错配
合并 L59。`collectSmtps` 用位置当身份、后端 `notify.py:164,175` 按索引沿用旧 user/pass ⇒ 中间删一行就凭据错配；只改 host 时**旧授权码随新域名一起发出**，risk 档零口令零确认。同文件族：额度 UI 只显余额（看不出被"从未发出的信"吃掉，接 MF-44）、`configured = max(1,…)` 把 0 执行体包装成"并行 1"、空清单无提示。
验收不变量：配置条目必须有稳定 id，禁止以位置作身份；改中继/收件人属"改告警去向"，必须入审计并留可还原目标。`[已复现：configured 三条]`

---

## E 簇 · 审计完整性（P1，红线）

### MF-52 审计锚点自检在自动判据层面等于没有
**V7 独立裁决 CONFIRMED**：`_anchor_file_state` 只写了"行数变少/相等"两支，**"变多"无人管** ⇒ **仅追加 1 条垃圾行**就让两道判据同时返回"无异常"；14 形态实测其余方向均显式 FAIL（安全），但**非法 UTF-8 抛 `UnicodeDecodeError` 被 `web/app.py:2463` 吞成一条 WARNING ⇒ 当日校验整体不执行**。无密钥即可伪造被采信的 v2 锚点行（`prev_line_hash` 是无密钥 sha256，其余字段是库内明文副本；改内容仍被 HMAC 兜住）。**兜住锚点的指纹存在库里**、而现网锚点与库同为 `yiban:yiban 600` ⇒ 有双写权限者可抹掉最近 N 条审计且 `audit_health.healthy=True`；垃圾行不可自愈。规模（V7 亲数）：28 站点 / 25 吞异常 / 17 与清白同形 / 16 零日志 / 12 同形且零日志。
修法方向：解析失败必须判"**无法定论**"而不是"无异常"；判据补 `!=`；校验器改取 `max_id` 最大真行；指纹跨权限存放。`[已复现（权限实测）]`

### MF-53 审计"写了但追不到人、丢了你不知道"
合并 L05、L06、L22、L32、L64。链构造本身成立（`prev_hash` 与 INSERT 同事务、AUTOINCREMENT 保跨进程不分叉），但：**业务写与审计写永远两个事务** ⇒ 中间被杀则"做了无留痕、欠账仍为 0"（欠账检测结构性看不见这类丢法）；无会话/请求 id，来源列只有可伪造 IP 的哈希；`audit_head_hash` 读失败与空链同返回 `""`；欠账单调无归零口径 ⇒ urgent 邮件永久刷屏；`_rechain_audit_logs` 分批 commit 击穿原子承诺；`audit_verify.py` 无顶层异常兜底 ⇒ `database is locked` 以 exit 1 **冒充"检出篡改"**，而唯一下游"修复"是重跑 `migrate_v3`（其分批 commit 会把可恢复态变成永久断链），无锚点时把"没查"印成"通过"，删尾/整链重签两类都检不出；`state_cleanup.py` 的 `--dry-run` 见 MF-48。
验收不变量：审计与业务同事务（或写审计失败即回滚业务）；"未查"与"通过"必须是两个不同返回值；校验器对"锁住了"和"检出篡改"给不同退出码。`[需确认]`

---

## F 簇 · 数字口径（P1/P2）

### MF-54 "同一事实 N 份定义"总账（建议立成一条独立任务，不逐点修）
实测计数：**周末门 4 份解析器两套值域**（`=true` 时引擎照签、日历标休并短路不查日志）；**窗口几何 5 份默认常量 + 4 份宽度算术 + 4 种分钟栅格**；`role→首页` **7 处**；**容量三数并立 585/4680/29952**；字段权限清单 6 处；模板内硬编判断 8 类；`.env` 行模型 2 份；`_mask_email` 真源 1 份但 `email.split("@")[0]` 第二份在 `account-form.js`。已知后果：MF-45 的日历误导、MF-46 的注入、L34 的迟启动强杀、L50 的看板 10.1×。
修法方向：**定一份权威 + 其余处只读它**（常量集中到后端单一模块，前端经接口取），并把"不得有第二份"做成 grep 级门禁。`[需确认]`

### MF-55 统计口径与"只抬不降"的默认分支
合并 L50、链3、R14f。看板"签到账号总数"把按 `(day,status)` 去重的 `cnt` 跨天跨状态**直加**，现网实测 **953 vs 真值 94（10.1 倍）**，而后端注释恰警告过此式；**一份错法喂三个出口**（分布卡跨天、日历格跨状态、染色 fail 优先）；防探针混算的唯一防线是 URL 里 10 个字符（`test_dashboard_stats_caliber_js.py` 既没钉它也没钉 `rateOf` 分母），且该测试的 `statusKind` 是页面的**不等价重写**（`already`→skip）；**未知新状态码默认落 skip ⇒ 成功率只抬不降**；探针软失败落库记 `status=success`（生产 255 行全 success、0 failed）；6 个计数器把未验 ACTIVE 当正常账号。
验收不变量：状态枚举必须穷举处理、未知值显式报错；`cnt` 类跨维度聚合禁止（要么后端出终值、要么前端按维度分组）。`[已复现]`

---

## G 簇 · 吞吐与容量（P1）

### MF-56 限速不成立 + 执行数被夹成 1 + 输入常数未测 + 122 悬崖
合并 L12、L34、L67、L3、L31、L33、L14、L23、L21。① 每引擎进程各 new 一份桶、`Λ` 不落库 ⇒ **真实全局速率 N×Λ**；桶键是 `executor_id` 而非物理出口 ⇒ 共用一个 Squid 的 K 进程各持一桶；② `executor_count` 被 `egress_count` 夹死 ⇒ **现网 K 恒 1**，与文档"5000 需 20–22 执行体"差 20 倍；③ 容量输入量 `1.87~3s` **不是实测**（mock 注入 6×300ms / 配置缺省）⇒ 所有继承它的数字继承的是假设；④ 悬崖：**121 账号临界，122 起当天必签不完，而引擎要到 361 才告警 ⇒ 122–360 是静默死带**；现网 89 个账号，余量 23 分钟；500 档缺 4 小时、万档需 106.7 小时；⑤ MF-3 单桶口径（`capacity_accounts` 无峰值无储备，末落点距 `eff_hi` 仅 1 秒，n=1560 约 43 账号在死带内；v3 不吃 `gap`，g=60 时 v2=75 / v3=3744）、MF-2 vshard 集中（N=200/V=64/K=64 最重 5.44×、42% 执行体 0 片、清单缺失静默回退 K=1⇒100%）；⑥ `capacity_probe` 与引擎**不是同一个式子**且判据不读 success/failed、造号写死窗口 ⇒ 白天跑全落 `skipped_window` 仍报"够用"。
修法方向：桶与出口同键且跨进程共享（落库或单点服务）；解开 `executor_count` 的假约束；把 t 换成实测分位数并入库；容量告警阈值降到"窗口 − 重试储备"实算值。
验收不变量：两进程同时领取时全局速率 ≤ Λ（当前必超）；`122 ≤ 账号数 < 361` 必须触发告警。`[已复现：K≡1、现网 89 号余量 23 分钟]`

### MF-57 一封邮件能把整站冻 150 分钟
合并 L36、L29、L37、L31、L9 的持锁部分。465 路径单条约 15 个阻塞操作、15s 是**每操作**超时且不含 DNS ⇒ 单收件人上界 37.5 分钟、A 线 4 收件人 **150 分钟**，批量拒绝还要在锁内 ×K；改密/注销在进程级 `_file_lock` 内做 2 次 scrypt + 2 次 SMTP（违反 `locks.py:31/32` 自家"哈希留锁外"），现网 `gunicorn -w 1 --threads 8` ⇒ 锁一堵八条线程全等；`accounts_api` 在锁内做同步 SMTP；`_render`/MIME 构造在 `try` 外 ⇒ 孤立代理对引发**锁内 500 + 审计欠账**；无连接复用。锁层另有：`held` 标志泄漏后**该线程此后所有同名锁全跳过、零告警**（V 判高）、相对路径+不同 cwd 使同名锁 Δ0.01s 同时进入、超时后降级继续 rc=0、`lock_kind()` 自称"非 None 即互斥生效"是假担保。
修法方向：通知一律异步出锁（队列 + 后台发送进程），慢操作（scrypt/SMTP）永不在锁内。
验收不变量：`_file_lock` 持有时长上限断言（秒级）；一次 SMTP 挂起不影响任何 API 响应。`[需确认]`

---

## H 簇 · 门禁与鉴权（P1，多为"声明与执行已漂"）

### MF-58 门不在装饰器层：漏挂即无门，且声明与实现相反
合并 V3、L26、L47、L54、L58、L53（部分已降档）。口令门在**服务层按手工枚举清单 opt-in**（`app.py:2380-2390`）⇒ 新写路由默认不过门；`/api/signin*` 三档全不要口令（V3 判"属 S1 拍板范围内，但三处矛盾"：红线"易班锁号冷却"只覆盖验证路径、文档称"只判主管理员"为假、同类不可逆有倒计时而它没有）；`risk` 档判据基准在**签名不加密**的 cookie（`login_ip`），现网 nginx 是覆盖式 ⇒ 远程伪造不成立，残留=本机直连 `127.0.0.1:17892`（含 SSRF）可任意伪造、且**代码对拓扑无自检**（Proto 段有）；`login_ip`/审计来源因此可被本机进程污染；改密不踢会话（`me.py:173` 保留 + `GET /login` 302 弹回）+ 改密不清会话缓存；倒计时 presence-only 且凭据沿多段链前递（一次手势放行整链不可逆）；模板/文案/前端三份权限声明互斥（"任意管理员可改"实为 `MASTER_ONLY_KEYS` 必 403，前端还渲染成可用）；`reject_default_admin_password` 把 `.env` 读失败当"无明文"静默放行（fail-open 零日志）；档位本身零留痕（放行不落审计，事后无法区分"验过口令"与"同出口免检"）。
修法方向：门禁声明从"清单"改成"路由注册时强制声明类别"（未声明即拒绝注册）；拓扑自检；改密吊销会话。`[需确认]`

### MF-59 未验证凭据以 ACTIVE 落库并被引擎真外呼
合并 L08、L44。`VerifyGateBusy` 在 `accounts_api.py:316-321` 只 `logger.warning`（注释称"置 rejected"）；6 跳判定链已给全：异步 → 扣配额 → 无邮箱即 `status=ACTIVE` → 落库 → 队列满返回 None → raise；引擎 `engine/accounts.py:140-141` 只跳 pending/rejected ⇒ 从未验过的凭据进主链。同形第二处：`attempt/jobs.py` 席位耗尽 → 置 rejected 并拒账号、**不扣额度不审计**（现网 `verify_jobs` 0 行故多为潜伏）；`_reject_account` 在同一条不变量上一侧 fail-closed 一侧 fail-open。
验收不变量：状态只能由"验证结论"置为 ACTIVE；任何兜底分支不得把未知当通过。`[需确认]`

---

## I 簇 · CLI / S 黑箱（P1/P2）

### MF-60 "自称只读"的命令造出看着健康的空库；agent 无法预校验任何东西
合并 L62、S。`config` 自述只读不联网，实测当场建出 69632B / 6 表 / `uv=0` 的伪库，随后 `db --integrity` 对它判 **"ok"**；`migrate=False` 仍建库/建表/切 WAL，且**漏一个入口**（`list_duplicate_owners.py:43`；本组另 5 个维护脚本**全部**会顺手写库——`init_db` 关不掉）；`.env` 与库路径全按 cwd 相对解析、唯一改道变量 `YIBAN_DB_FILE` 不在任何 help 里 ⇒ `.env` 写了账号仍报"未配置任何账号"却同时回显 `env_file:.env`；`sign --bogus --json` ⇒ rc=2 且 **stdout 零字节**（`--json` 契约破产）；六种非法参数输出**逐字节相同**、rc=1 同时承载配置错/参数错/真失败；零账号闸门挡在参数校验前 ⇒ **无法做任何预校验**；`probe` 的跳过/撞锁/真跑**全是 rc=0**（外部监控永远看不见它坏）；`print_config_summary` 用 `print` 破坏 `--json` 单行契约；CLI 面**没有 restore**；`db --backup` **静默覆盖上一份**（与 R10e 的"幂等"定性互相矛盾，见待裁决）；入口 `python -m yiban.cli` 要猜 4 次、WSL 无可用解释器（新人上手成本）。
修法方向：把"只读"承诺变成代码断言（只读连接 + 不建表）；退出码分族；`--json` 永远输出结构化错误（含 rc=2 路径）；补 `restore` 或在 help 明示不存在及替代路径。
验收不变量：任一"只读"子命令跑完后 `git status`/库文件 mtime 不变；非法参数必须有可区分退出码。`[已复现（黑箱自测）]`

### MF-61 前端"假成功 / 假可用"一族（P2，逐点改）
合并 L48、L49、L52、L40、L65、L46。`useServerMsg` 零调用点传 true ⇒ batch/purge 的后端 msg 永不上屏；`state.busy` 只暂停轮询、非防抖 ⇒ `signin/restore/move/purge` 可双击重发；`__clear__` 空操作却回 200 + toast"已保存"（**现网 21/96 个号可见**）；选中键用数组 `index` 而 `move` 改持久化顺序 ⇒ 单人即可误删他人账号；`configured=max(1,…)`；`apiCached` 把 `/api/users` 整表邮箱与 CSRF 写进 sessionStorage；`time-field.norm` 只查形状不查范围 ⇒ 显示 `25:00`/落盘 `23:00`/生效 `06:30~23:00` 三段不等；`windowSec()` 窗口倒置返回 0 ⇒ 缓冲上限放到最大（fail-open）；"3 个不剥注释的门"在给一个**永不发布**的 `component_layer.html` 把关（`_stub_macro.html`、`tailwind_config.html` 同为死档）。
验收不变量：写操作后必须回读校验；`index` 禁止作身份键；形状与范围校验同一处；死档要么接线要么删。

---

## 修复顺序（建议给修复流照这个次序）
1. **MF-41 / MF-40 / MF-42** —— 先把"能不能安全升线"解决；升线前手工确认 `v18–v20` 与 `epoch` 已落地，否则**不要部署**（MF-49 的兜底也依赖这次部署）。
2. **MF-52 + MF-53 + MF-47 + MF-48** —— 红线与数据不可逆组（审计可被抹、互斥可被绕、零守卫清库、`--dry-run` 假演练）。
3. **MF-43**（补签，按内部顺序 ②→①→⑤→才谈 ③）+ **MF-45/MF-46** 成环那两条 —— 直接决定"漏签有没有声音、急停会不会被静默打开"。
4. **MF-49 / MF-50 / MF-51 / MF-58 / MF-59** —— 隐私、凭据、门禁声明与执行对齐。
5. **MF-56 / MF-57 / MF-55 / MF-54** —— 容量与口径（含把 `t` 换成实测分位数）。
6. **MF-60 / MF-61** —— CLI 契约与前端假成功。
7. 全程带 **MF-54** 的"单一事实源"任务，否则上面每一簇都会在下一轮重新长出第二份定义。

## v3 灰度批的两个硬前置（新增，来自 V5/V6/FALLBACK 三处独立证据）
① `claims` 与 `tasks` **两池互斥修好**（否则 `SCHEDULER_V3=1` + `FALLBACK_ENABLE=1` 当天全站双倍真实登录）；② v3 的 `sign_tasks.failed` **要有当日回炉口**（否则开了 v3 之后失败账号只能等第二天）。两条都满足前，灰度开关不进生产。

## 已否证清单（别再去修）
`apiCached 零调用者`（实测 5 个调用点）· `login.html` 无 method 的 form 会明文提交口令（JS 下是死按钮，非泄漏）· `is_configured` 与 `send` 口径分叉（同口径，分叉在 TYPE/白名单）· `urgent 额度被 4 个执行体刷光`（与个数无关）· `notified 会误导界面`（不进 API）· `明文凭据进备份包`（`backup.sh:482-489` 白名单挡得住）· `_file_lock` 跨进程失效（portalocker/flock 真互斥；只有 web 侧那把是 `threading.RLock`）· `07:12 走 yiban-fallback.sh`（现网走 `run.sh`，cron.d 无 fallback 条目）· `备份连断是当前状态`（耦合 09-23 已修，09-23/24/25 已出包）· `缺 epoch 时静默通过`（实为全量拒跑）· `_convert_integrity_error` 对部分索引不匹配（本地 sqlite 复现匹配成立）· 18 条 UPDATE/DELETE 缺 WHERE（无）· `F1/F2/F3 改过 config_check.py`（没改）。

## 跨流对账带出的注释回归（MF-62..66）

> 审查流把 116 条"假担保"清单与注释流合入的 195 个文件逐一对账：86 条禁动项里 **8 条锚点被改写**，
> 其中 **4 条属"洗白/强化"**（MF-62..65），另有一条注释流漏改到位（MF-66）。
> Python 侧 AST 等价由**两个独立实现各验一遍**（184/184，无越界）——以下是"注释变假了"，不是"代码变坏了"。

| MF | 位置（文件:符号） | 问题 | 判定 |
|---|---|---|---|
| MF-62 | `yiban/store/claims.py` 模块头状态表 + `summarize` | 仓内唯一自认「failed 本窗口内不再重试」的假担保句被删，改成与代码一致的「当日仍可再领」⇒ **缺陷从此隐形**（审查流 FA-06 / F8） | 洗白 |
| MF-63 | `web/services/accounts_data.py` 软删保留期注释 | 仓内唯一自认「三份互不相干的 7」的自白被抹平，陈述句换成条件句（FA-30 族） | 洗白 |
| MF-64 | `web/services/capacity.py:55` 模块 docstring | 注释流**新写**的更硬担保「改这里等于同时改三处」——同一谓词另有直接消费路径（`settings_api.py:1150` 调 `m.signin.capacity_of`），"三处同一源"全仓是否成立没人证过 ⇒ **本轮新引入的假担保**，唯一应当改准的一条（FA-50） | 新引入 |
| MF-65 | `scripts/backup_sentinel.py` 退出码行尾注释 | 新增「cron 自己的报错邮件是最后一道声音」——**事实错误**：仓库给的 crontab 行（`README.md:739`、`backup.sh:50`）带 `>> /var/log/yiban/backup.log 2>&1` 且全仓无 MAILTO，报错被吞（FA-45） | 新引入 |
| MF-66 | `web/app.py` 健康日报 docstring | 仍写「告警通道健康日报（每日线程调用）」，代码已收敛为 `_HEALTH_REPORT_WEEKDAY = 0` 的周报（FA-58） | 漏改 |

**方法论教训（与 MF-36 同源）**：AST 等价与"剥注释逐字比对"都把注释当噪声丢掉，而假担保恰恰长在
注释里 ⇒ **"纯注释改动"的证明不覆盖"注释是否仍为真"**。注释流验收需要第三条腿：对被改写的锚点句
逐条核对"改后是否仍与代码事实一致"。

### 处置（2026-09-25 注释流收尾轮；改动在 `docs/comments-truthfulness`，八文件均注释级）

五条一律按"**不 revert 语义、把话说回事实**"处理，Python 侧与 `12c54a6` 剥 docstring 后
`ast.dump` 逐文件等价：

- **MF-62** 保留"failed 当日仍可再领"的正确描述，另把被删掉的**代价**写回 `STATE_FAILED` 注释：
  重试预算住在单轮进程内、`attempts` 列无人当判据 ⇒ 领取层没有跨轮上限。反查出来的代码缺陷新立
  **MF-67**（就是原来那句假担保所遮盖的东西，不能只靠注释自认了事）。
- **MF-63** 把"各写一份'7'就会各自漂移"的假设句改回既成事实：常量之外全仓 48 处「7 天」字面量，
  其中 24 处在模板与前端 JS 里直接向用户承诺天数（复点命令已写进注释）。
- **MF-64** 撤掉注释流自己新写的"改这里等于同时改三处"，改为"同源成立在**判据**
  `account_signs_in`、不成立在**计数表达式**"，并点名设置页那份内联计数与走引擎公式的预估路。
- **MF-65** 删掉"cron 自己的报错邮件是最后一道声音"（含 `.sh` 排期注与其测试 docstring 两处同族句）：
  仓库给的三条排期行都带 `>> backup.log 2>&1`、全仓无 `MAILTO` ⇒ 退出码 1 只进日志。
- **MF-66** 健康报告的"日报"改准为例行周一播、通道降级或额度耗尽当天照播（旧称仍留在若干符号名里，
  已在注释里注明它是收敛为周报之前的历史称呼，不改代码符号）。

### MF-67 领取层对"当日重复领取"没有跨轮上限：弃权账号每天每轮都重来一次 ✅

- **现象**：`give_up` 把行置为 `failed` 并**主动把 `heartbeat_at` 写成已过期**，而 `try_claim` 的冲突
  分支把 `claimed` 与 `failed` 同列"可再接手"。于是一个"本轮重试预算耗尽"而弃权的账号，当日会被后面
  每一轮（补签轮、兜底常驻、别的执行体）重新领到并重走一遍登录+签到。设计意图是给补签链接得上失败
  账号，但没有按**弃权原因**分档：窗口外跳过（该重试）与预算耗尽/风控类失败（不该无上限重试）在
  领取层是同一个 `failed`。
- **证据**：`yiban/store/claims.py::give_up`（`expired = _utc_offset_str(LEASE_SECONDS)` 写进
  `heartbeat_at`）、同文件 `try_claim` 的 `state IN (?, ?)` 传 `(STATE_CLAIMED, STATE_FAILED)`、
  `OPEN_STATES`；预算实际住在单轮进程内（`yiban/engine/attempts.py::_retry_budget`/`MAX_ATTEMPTS`，
  口径见 `yiban/engine/round.py` 的"总尝试次数同受 `_retry_budget` 分级控制"），换一轮即重新计数；
  `sign_claims.attempts` 只被 `try_claim` 的 `attempts=attempts+1` 自增，
  `grep -rn "attempts" yiban/store/` 无任何读取判据（`queue_store.py:214` 同样只自增）。
  外呼代价：`yiban/engine/attempts.py::attempt_signin` 每次都会构造客户端并走 `login*()`；
  `yiban/fyiban/protocol.py:378-379` 有会话缓存时先探活复用，但同层 `:406` 与
  `executor_v3.py:479` 的 `clear_session_cache_quiet` 会在特定失败后清缓存 ⇒ 缓存被清过的那些轮
  是**又一次真实登录**。
- **验收不变量**：领取必须按弃权原因分档——"窗口外/无点位"可当日再接手，"预算耗尽/风控类"需显式
  路径（同 `allow_settled` 一档）或领取侧按 `attempts` 列设当日上限。断言：`give_up` 之后用默认参数
  再领同一 `(phone, day)` 必须 `ok=False`，补签轮/手动用显式参数才 `ok=True`，且 `attempts` 递增可查。
- **现网三态**：**未知**——取决于当日有多少轮会碰到同一失败账号。一句只读 SQL 可判：
  `SELECT COUNT(*) FROM sign_claims WHERE day=<当日> AND state='failed' AND attempts>1`；无现网读数。
- **来源**：注释流收尾轮改准 MF-62 那句时反查出来（上一轮把"本窗口内不再重试"这句假担保删掉之后，
  这个缺口在仓内不再有任何自认）。与 MF-47 的六条绕过路径**不同轴**：MF-47 讲"同一时刻被领两次"，
  本条讲"先后每一轮各领一次且无上限"。

## 待裁决（原草案 7 条：两条已由读码裁决，五条待你拍板）

| # | 事项 | 现状 | 建议 |
|---|------|------|------|
| 1 | ~~`db --backup` 是否幂等~~ | **已裁决（读码 2026-09-25）**：`_db_backup` 写 `args.backup or (db_file+".backup")`，目标已存在时**直接覆盖**，仅在返回字段里带 `backup_exists` ⇒ S 黑箱判「静默覆盖」成立，R10e 的「幂等」不成立 | 并入 MF-60 族：已存在时要求 `--force` 或自动时间戳 |
| 2 | `component_layer.html` 等三个死档是否升为"门禁静默失效"（P1） | 三个**永不发布**的文件仍被 3 个源文本门把关 | 建议 **P2**：拦的不是真代码，列入"要么接线要么删"（MF-61 已含），避免 P1 稀释 |
| 3 | 假担保清单计数口径 | 草案中途报过"90/123"，与终稿混轴 | 采纳终稿口径：**116 禁动（§2.C 86 项）/ 30 可改 / 文档面 7 = 123** |
| 4 | ~~`YIBAN_WORKERS` 校验与 K≡1 是否同一处~~ | **已裁决（读码 2026-09-25）**：不是同一处——`egress.py:369-374` 的 1~64 钳制管**旧口径 worker 数**；K≡1 是 `schedule.executor_count` 被**配置的出口数**夹死；两套会叠加（出口 1 ⇒ K=1，与 WORKERS 无关） | 并入 MF-56 一并修 |
| 5 | 注释流 4 条洗白（MF-62..65） | 不 revert、各立一条"从注释反查回代码"；**例外 MF-64** 应改准 | 待确认（本轮提问） |
| 6 | 许可与公开仓：`waf.py` 血缘三口径（README"已弃用" vs PROVENANCE"另议" vs `docs/dev/README` 第三口径）；GitHub 公开仓含真名样式示例；**桌面明文备份口令** | 需要你处理 | 口令轮换+移出明文；血缘统一成一处；示例改名或脱敏 |
| 7 | 旧服务器：22 端口 OPEN、Web 与每日备份仍在跑、签到 cron 是否真停未核实 ⇒ 跨机**零互斥面** | 需要你授权 | 派只读代理登机核一次 |

## 覆盖面声明（别把这份当"查过了"）
L1 84/84 单元已覆盖（`web/static/vendor/**` 31,331 行第三方与 `web/static/css/**` 4,508 行样式**未审**，`tests/**` 属注释流范围）；V1–V8 只覆盖被点名的 8 条高危；L2 覆盖任务书点名的 5 条链；**L3 只有 4/20 格有真 HTTP 全链路证据**（6 格因假上游缺故障注入能力而不可判）；S 只覆盖清单内测试项目。**未做**：跨用户并发压测、真实浏览器端到端、v3 开态实测、旧服务器、以及任何依赖读 `.env` 内容/`/etc/nginx` 全文的判定。
