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
> **收尾轮已兑现（2026-09-25 下午）**：派发 2/3 的产出登记为 **MF-68..103**（36 条，见"补充检出"节）。其简报写"下一空号 MF-67"时先于注释流 MF-67 入库，且 8 个分片代理各自自报编号造成撞车；合并入库时整体 **+1** 重排，**最终号-内容映射以该节为准**。**下一空号：MF-104**（MF-67 已被注释流占用，勿双写）。

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
合并 L61、L62。`gitee/develop` 实测停在 `b6457e6`，`9d3f491` 只在本地；现网部署命令是 `git pull gitee server-web` ⇒ **按现流程根本部署不到 M2 审的这份代码**。且 `rev-list --left-right --count ≈ 2/100`：生产有 2 个基线没有的提交（合计 1 行：备注 placeholder 示例改"电力123庄**"，`6d4eafa` 为空合并），merge 三方干净但 `checkout/reset` 到基线会**静默丢掉**且全仓无测试钉住。同提交链上另发现：GitHub 仓为 **PUBLIC**，该姓名样式示例已随 `origin/server-web` 外推。
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
- **MF-63** 把"各写一份'7'就会各自漂移"的假设句改回既成事实：常量之外全仓 48 处「7 天」字面量
  （含注释，24 处落在模板与前端 JS；复点命令已写进注释）。
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

## 补充检出（MF-68..103，2026-09-25 收尾轮 · 全部经独立裁决）

> 来源：`DISPATCH-REMAINING.md` 派发 2/3。semgrep 265 条归类（`_scratch/m2rev/out/SEMGREP-TRIAGE.md`）
> 与全部 `out/*.md` 发现节的补漏对账（`out/MF-CANDIDATES.md`：去重前 61 条自报候选 ⇒ 50 簇）。
> **8 个分片代理各自从 67 起编号造成全面撞车，最终号由指挥者统一分配**；并入 develop 时又因 MF-67 已被注释流占用而整体 **+1**（收尾轮原稿号 = 现号 − 1，故 `out/` 各报告里的 C-nn 按本节映射查号）；本节的号-内容映射是唯一有效的。
> 每条末注 `〔C-xx · 裁决 out/ADJ-n〕`，取证细节在对应 ADJ 报告里；本节只给四件。
> **严重度以裁决后为准**——12 条原报"高"里有 8 条被降级或改判，5 条被驳回（见"本流否证追加"）。

### 覆盖性订正（与原条目冲突时以本节为准，不必回改原条目）
- **MF-49 ③ 那句方向写反**：原文"升级后从能搜到变搜不到"不成立。基线 `_mask_log_phones` 即宽口径，
  `data.py:91 masked_all` 在 `:93 if q:` 之前 ⇒ **现在**粘 11 位完整号进 `q` 就恒 0 命中（`[号码]` 轮次行
  09-24 219 行 / 09-23 226 行从来搜不到）；"上线后新增搜不到"只对现网窄口径漏出的那 124 个/日非方括号裸号成立。两半各需各的断言。
- **MF-56 引用的 `L3` 是来源层名（`out/L3-e2e-perf.md`）不是 L03 行**；**MF-57 的"L9 持锁部分"** L09 实为外发邮箱、疑为 R9e/L37 错标；**MF-58 的 `L54`** 正文无 git 分叉内容、L54 实落 MF-41/42；**MF-60 的 `L62`** 已由 MF-41 独占、其正文真身是 L21。
- **MF-52、MF-54 缺"合并 Lxx"头**；**MF-62..66 被压成 3 列表格**，缺"现网三态"6 格、"验收不变量"5 格（与 MF-1..39 的四件式不一致）。
- **台账 L43 的两条主张在基线上为假**（`check_smtp_host:91-92` **显式拒** `localhost`；设置页与发送侧同用 `smtp_list()`，全仓 `smtp_host` 在前端 0 命中 ⇒ 不存在"界面说 A 真走 B"）⇒ 以本节 **MF-94** 的收窄形态为准。
- **MF-49 的数字口径需标出处**：表里"裸号行 09-22/23/24 = 258/314/368"与 `out/V4` §2.3 实测"09-24=313 / 09-23=359（按行计，占 517/547 行）"逐字不一致且日期序列方向相反 ⇒ 必须标"哪份取证 + grep 口径（行数 or 命中数）+ 覆盖哪几日"，否则修复流无法复算。谁对不由本流裁。

### J 簇 · 会把真实请求打到易班 / 拿用户凭据冒险
#### MF-68 压测隔离链的前提不成立：经代理时 `/etc/hosts` 改写完全无效（高）
`scripts/loadtest/scale_driver.py:221`、`concurrency_probe.py:306` 用 `dict(os.environ)` 继承全部代理键且从不摘除；引擎 `yiban/client.py:135` 裸 `requests.Session()`、`trust_env` 在生产代码 0 处设置（默认 True）⇒ 解析发生在代理端，本机 hosts 与 `--dport 443` REJECT（`mock_env.py:64`，且 REJECT 用 `-A` 追加链尾 vs ACCEPT `-I 1`）双双旁落；现网确有 Squid `127.0.0.1:3128`。"测试机"红线只有 Linux+root 两条（`mock_env.py:393-399`），生产机全中。唯一主动探测在缺省 `--egress-probe-ip=""` 下**恒走 `[SKIP]` ⇒ 代理路径下必报绿**（这才是假担保本体，判高）。全树 `atexit` 0 命中、搭建段无 try/finally。隔离件在部署线 `6d4eafa` 与基线**逐字节相同** ⇒ 代码已在现网树。
**收窄**：外呼打出去的是**对真实号段的假凭据登录风暴**（`capacity_probe.py:426-427` 用 `test.env`/`data/yiban.db`，`seed_accounts.py:75-83` 造假密码 + `131…` 真号段）；真凭据仅在 `--db/--env` 被显式写成生产路径时成立（`required=True` 无指纹校验）。
验收不变量：loadtest 启动即断言"环境里无任何 `*PROXY*` 键 + 探测 IP 非空 + mock 侧记账条数 == 发出条数"，任一不满足拒绝启动；`grep -rn trust_env yiban/ scripts/` 必须有正向命中。开发机本机已两次独立实测干净（hosts 0 命中、无代理变量）。〔C-09 · ADJ-5〕

#### MF-69 会话 cookie 往返丢掉 `domain`，空域即对任意主机带出（中）
`yiban/client.py:74` 把整份会话装回活动 session，`dict_from_cookiejar`/`cookiejar_from_dict` 往返折叠掉 `domain`（项目自己在 `protocol.py:456-457` 写明"域名为空即对任意主机带出"）；同族更早的一处是 `protocol.py:320-322`（legacy 命中挑战即在登录过程中做一次整 jar 往返）。三档降级里只有 `domain` 成立（`HttpOnly` 不适用、`Secure` 需 http 跳而默认流无、`expires` 由 DB 侧同业务日 + TTL 6h 双判据接管）。〔C-11⑤ · ADJ-11〕
验收不变量：往返后的 cookie 必须保留 `domain`，或断言"活动 session 内不存在空域 cookie"。

#### MF-70 重定向链信任校验晚于发出请求（低-中）
全仓唯一 `allow_redirects=True` 在 `protocol.py:271`，链信任校验在 `:275` ⇒ 先跳后判。危害有限：校验先于 `parse_login_page(:277)`（攻击者公钥不会被装载），那一跳 jar 里只有客户端自造的随机 `csrf_token`。同文件 `:469-479` 已有"先验后发 + 5 跳封顶"的正确模板可照搬。〔C-11③ · ADJ-11〕

#### MF-71 风控/WAF 失败分类整体错位，且分类口径实有**四**份（高，腿①需登机）
`ast` 遍历 `waf.py` 全部 raise 结点得 **15 处**（14 处带"ydclearance 挑战解析失败:"前缀 + `:146` 白名单；候选稿写的"9 处"须订正），逐条过表 ⇒ **全部**落 `classify_failure → MAX_ATTEMPTS=3` 的普通重试档（`_retry_budget` 还 `clear_cache=False`）：既不清会话也不减速。最可能的触发是 `waf.py:60「未找到挑战函数」`——且不需要易盾改版，`looks_like_challenge:40` 自身就能把正常响应判成挑战页。腿②的真实文案是 requests 的 `Expecting value:`（`>2000` 拦截页过 `require_not_blocked` 后 `.json()` 抛），四份口径零命中。**第四份口径是本轮新发现**：`probe.py:68-75 PROBE_HARD_FAIL_RE`（消费点 `:253`）同样不含这些文案 ⇒ 探针与"提交即验证"路径也不预警。
归置：登记表逐词元（`WAF`/`风控`/`ydclearance`/`挑战`/`分类`/`MAX_ATTEMPTS`/`关键词`）**命中数全为 0**（对照组 `签到`=6、`登录`=4 证明检索通道没坏）⇒ **MF-56 未覆盖这件事，L23 只是被它挂了号**：请把 L23 从 MF-56 合并清单删掉、改挂本条。修 `is_waf_blocked` 的 `len>2000` 短路易撞测试：`tests/test_login_protocol_shape.py:617-622` 把它**当规格钉住**，须同批改。腿①默认流不可达（`solve_ydclearance` 唯一调用点在 `login_legacy` 内，`client.py:125` 默认 killyiban），开关状态需登机。〔C-13 · ADJ-12〕
验收不变量：任意一条解析失败路径必须落进一个显式的"不可重试"档，且每档各带一条"把输入改坏 ⇒ 判据必须红"的活体反例（见 MF-91）。

#### MF-72 `login_killyiban` 只判 `code==0` 即宣称成功并写缓存 ⇒ 假成功同时进日志与密文库（中）
`protocol.py:482-489` 唯一判据是 `code==0`，随后 `logger.info("登录成功")` + 无条件 `session_store.save()`；`:382/:407` 形参 `csrf` 被缓存值覆盖。主后果是**审计不可信**（假成功写进 `session_cache` 密文库）与重复真实登录；漏签不成立（`:404-407` 探针判死会自愈）。
**修法锚点须订正**：被当作"正确参照"的 `login_legacy:359-360` 那句 cookie 存在性复核本身近乎恒真（`:256`/`:385` 已本地预置 `csrf_token`，`client.py:88-90` 只挡空 jar）⇒ **照抄不等于修好**，要新增的是"签发方回执"级判据。行为后果需假上游故障注入（派发 1）才能测，本流未测；现有测试把 `set_session_cache` 整体 mock（`test_login_protocol_shape.py:406/433/453`），对该缺口是瞎的。现网旁证"未登录或登录已经超时 18 次"被 `attempts.py:79-81` 的自然过期解释完全覆盖，**不可归因**。〔C-12 · ADJ-12〕

#### MF-73 执行体"实测"恒取账号表第一个，且当事人零知情（中）
`web/services/measure.py:110-116 _pick_measure_account` 恒取列表第一个（无排序/轮转/钉号），前端 `settings-quota.js:227` 传 `{}` ⇒ 每次点"实测"都用同一个不知情用户的真号发一次真实登录；一次 = **5 个请求**（`settings_api.py:1098`/`:1171` 自述 + `client.verify()` 拉任务）；全仓仅 `YIBAN_MEASURE_COOLDOWN`/`YIBAN_CAPACITY_MEASURED` 两键、**无隔离账号配置**；函数体内无任何通知调用。冷却的两处 fail-open 属实，但它是节流器不是鉴权（鉴权 fail-closed 于 `settings_api.py:1106`，且只有 `.env` 内置管理员可点，另 2 个 `role=admin` 必 403）⇒ 失控有界（600s ⇒ ≤144 次/日）。与 MF-59 根因不同（那条是"未验凭据以 ACTIVE 落库"，本条账号本就 ACTIVE、外呼由人手动触发），**不被其覆盖**。
验收不变量：实测必须走一个显式配置的专用账号，且落选账号不得是 `accounts` 首行；每次实测写一条含操作者的审计。〔C-04 · ADJ-9〕

#### MF-74 `--only` 的豁免只覆盖门，派发照旧 ⇒ 三处确定性危害（中）
链路：`signin_api.py:225→139→150` spawn ⇒ `scripts/signin.py:124` ⇒ `runner.py:222-243`（`:227-231` 派发只看 worker 行数，豁免只挡 `_day_off_skip()`）⇒ `:242 return run_worker_supervisor(argv)`；子进程 `workers.py:142-153` 原样带 `--only`、`:132-133` 各自独立 owner 与锁名。实测 2 行 worker ⇒ 2 个子进程都带同一号。
**"N 次真实登录"不成立**：`claims.py:150-152` 的 `state='claimed'` + fresh heartbeat 挡住了 done 分支（临时库探针：worker-0 `(True,2)`、worker-1/2 `(False,0)`）⇒ 是 **race ≤N**，归 MF-47① 族（补句见表六）。真正成立的是三条确定性后果：① 抢输的一路经 `runner.py:522-527→:620-622` rc=2 ⇒ `manual_sign.py:77,102-104` 把**其实成功了的那次点击**写成"本轮未实际签到"；② `--only` 的锁从"非阻塞即退 3"（`runner.py:352`）翻成阻塞 600s 后**无锁继续**（`workers.py:105` + `cli_support.py:112-134`）；③ web 的 `terminate()`（`signin_api.py:220-222`）只杀监督进程 ⇒ N 个子进程孤儿化。
**"与 MF-56 有顺序耦合"两头都是错的**：`executor_count`（`schedule.py:195-219`）从不参与派发（`runner.py:227-231`）；实测 `executor_count(89|122|361, 4200s, egress∈{1,2,4}) = 1` ⇒ 现网 K≡1 由"量 ÷ 窗口"造成、不是被出口数夹死。现网部署线 `6d4eafa` 同结构；唯一未知是启用 worker 行数（`PROD-FACTS.md` 未记 ⇒ 需 4 条只读命令）。
另记：这不是疏忽而是**记录在案的取舍**——`runner.py:232-234` 注释自述"三者照旧派发"，且 `tests/test_multi_executor_engine.py:289-331` 正向断言每个子进程都带 `--only`；而 `docs/dev/reviewfix-intended-design-20260922.md` L4-2 写的是相反意图 ⇒ **待裁决 #8 已裁决（2026-09-25）**：门豁免确认现状；L4-2 的冲突实为派发拓扑（`--only` 单进程）⇒ 修法收敛为上述三条危害 + 扇出收敛，M3 同批改实现、注释与测试。〔C-02 · ADJ-2〕
验收不变量：一条 `--only <单号>` 进程树整轮 `attempts.attempt_signin` 调用数 == 1、`Popen` 记录 ≤ 1、成功时 rc ∈ {0}；且 `--only` 必须仍能返回 3。

#### MF-75 容器调度器一次异常即让当天五项全废且不再重启（高，现网不触发）
`docker/scheduler.py:341-393 main_loop` 循环体无兜底，其中 `_run_signin_child:228` 的 `Popen` 没有 `except OSError`——对照同文件 `_start_fallback_child:299` 的调用点 `:319-323` 接住并 print，即同一文件两种判法。外层无人救：`supervisord.conf` 未写 `startsecs/startretries`（默认 1s/3 次），崩溃发生在重启后第一 tick（`hm>=FIRST` 立判、`_mark_slot` 排在子进程之后）⇒ 秒级三连进 **FATAL 不再重启**；`docker-compose.yml:56-62` 的 healthcheck 只 curl web 端口 ⇒ `restart: unless-stopped` 永不救。后果是首签/补签/探针/兜底/清理同进程全废全天。
**双跑改判**：`docker stop` **不产孤儿**（supervisord 是 PID 1，namespace 一起死）；真孤儿源是 `supervisorctl restart sched`（`stopasgroup/killasgroup` 未设）与崩溃重启，且容器子进程**其实拿引擎全局锁**（`runner.py:352`），只是全量模式 600s 后 fail-open（`cli_support.py:117-135`）＋同机 owner 同为 `single@{host}`（`round.py:169`）令 `claims.py:157` 恒放行 ⇒ 归 MF-47 补句，本条只登异常面。启用容器（`docker-compose up -d` 且不停宿主 cron）即变"已在现网"。〔C-08 · ADJ-15〕

### K 簇 · 备份与运维脚本（可恢复性不可证明 + 命令注入）
#### MF-76 备份"完成"不证明可解：明文唯一副本在验证前就被删，哨兵件从未安装（中）
`scripts/backup.sh:559-561` 在 `try_encrypt` 返回 0（判据只有 gpg 退出码 `:146-153`）后**立刻 `rm -f "${ARCHIVE}"`**；全仓唯一能证明密文可读的 `--restore`（`:169-214` 解密 + 三重包校验 → `:260-303` integrity/audit 双验）**没有任何 cron 或代码调用点**（只在 `:653` 当提示、README:287/742 手写命令）；`:585` 的 `sha256sum "${FINAL_LOCAL}"` 是对密文自指纹，证不了可解。**同仓反证**：`docker/backup-docker.sh:103-118` 已经做了"尺寸下限 + 流式解密解包自检 + 失败删件非 0 退出"⇒ 一个仓库两套契约，**弱的那套正是 cron 装的**。口径修正：删明文那一刻 `TMPDIR_BAK` 里还有明文组件（`:108-109` EXIT trap 收尾才清），且删除发生在异机同步之前。另一半是 **`backup_sentinel` 在现网从未安装**（V8 三源同判：L19 内容在登记表零命中；MF-34/MF-65 只覆盖相邻的退出码与注释）。
验收不变量：当日归档必须能经 `--restore` 解到临时目录且 `integrity_check=ok`，否则**不得删明文、不得出清单、非 0 退出**。〔C-23/L19 · ADJ-6〕

#### MF-77 备份轮转按 mtime 删、无最少保留、删后不数不验（中）
`backup.sh:646-649` 四条 `find -mtime +N -delete`；`RETENTION_DAYS`（`:66`）零校验 ⇒ `RETENTION_DAYS=0` 是合法值（`-mtime +0` = 删掉除当天外全部）；哨兵只查**当日**包存在（`backup_sentinel.py:12-13,94-103`）⇒ 历史被清而当天件在 ⇒ 完全静默。真实触发是时钟前跳、`cp -p` 保时间戳拷入、或 RETENTION 被改小（**不是**"mtime 变新"）。反证：同仓 `pull-prod-backup.sh:250-258` 已是"按文件名日期保留最近 N 份"并有 `:127` freshness_check。加重项：备份目录与主库同机 + `REMOTE_BACKUP` 默认空 ⇒ 退化成同盘单份。〔C-24② · ADJ-6〕

#### MF-78 `pull-prod-backup.sh`：一个环境变量就能在**生产机**上执行任意命令（高）
`:50 REMOTE_DIR="${REMOTE_BACKUP_DIR:-…}"` 与 `:52 MAX_FETCH` 被**未加引号地插进** `:85`/`:112` 的远端命令串 ⇒ `REMOTE_BACKUP_DIR='x; id #'` 一步成立，零文件名配合，且 `MAX_FETCH` 全脚本无整数校验。提报的"远端 `ls` 文件名回流"支（`:93-96` 单引号拼接）也成立但更窄：实测必须含 `'` 才能闭合（`$()`/反引号在单引号内不执行）；`:203 IFS= read -r` + `:209 basename` 不滤元字符。增量：跳过 `:213-215` 后"远端哈希自证"（`:233`）在远端失陷下结构性无效 ⇒ 执行/投放/反取证一条完成。`:49 SSH_HOST` 另给**本机** RCE（`-oProxyCommand` 作单 argv，中）。
**"现网未启用"不算豁免**：`:85` 的 glob 正命中生产同机的 `.gpg` 产物，真豁免只有"仓内 0 调度 + 跑在工作站"，而 `:28-32` 正在劝运维排调度 ⇒ 明天配了就爆。契约测试 10 条全是 `assertIn` 读源码文本 ⇒ 与 MF-10 同族假绿。生产实件与仓内是否同源=未知（MF-42）。
验收不变量：所有进远端串的变量必须经白名单校验（目录必须匹配 `^/[\w/.-]+$`、`MAX_FETCH` 必须是整数），并有一条"注入值 ⇒ 拒绝执行且退非 0"的活体反例。〔C-25 · ADJ-14〕

#### MF-79 `BACKUP_PLAINTEXT=1` 一个开关即产出含 `.env`+整库+`accounts-key` 的明文包，且哨兵认它"健康"（中-高）
明文包落 `${BACKUP_DIR:-/var/backups}/yiban-<date>.tar.gz`（`:65/:107/:533`，文件 0600/umask 077，但 `:355` **从不 chmod 目录**）；构成件已核全：`.env`(`:76`,`:361-368`) + 整库(`:390-427`) + `keys/accounts-key`(`:440-443`) + state 白名单 + 30 天 `sign-*.log`(`:509-517`)；零确认、零落盘护栏，且明文包同样享 30 天保留。**最硬的一条不在原指控里**：`backup_sentinel.py:70 ARCHIVE_SUFFIXES` 含 `.tar.gz` ⇒ **明文归档直接满足"当日包存在 = 健康"**，任何告警只进 MF-42 实测 0644 的 `backup.log`。
提醒：登记表 424 行把"明文凭据进备份包"记为误报，那依据（`:482-489`）挡的是 **state 白名单**，不可用来否掉本条。〔C-24③ · ADJ-14〕

#### MF-80 备份的三道路径护栏在 `tar` 自身非 0 退出时**整体 fail-open**（中）
`:58 set -euo pipefail` 使 `:201/:205/:211` 三护栏在 `tar` 非 0 时**全部不执行**（活体复现"GUARD_SKIPPED"，去掉 pipefail 才会拒绝），叠加两处 `2>/dev/null` 吞诊断。
**原指控的两条口径不成立、行号错了**：唯一 `--anchored` 站点是 `:216-217`（`:551-558` 是明文告警块、`:509-517` 是日志入包）；`--anchored` 在无 pattern 的抽取上是死选项（非 GNU 方言下首选恒失败 ⇒ 加固从未存在），去掉它不多打任何目录；换行拆行**绕不过**穿越判据，反而 `data/x\nlog.txt` 会让 `:205` 的 `^l` **误拒一份好备份**。路径穿越在 GNU tar 默认消毒（全脚本无 `-P`）下现网不成立、非 GNU 方言下条件成立。〔C-24④ · ADJ-14〕

#### MF-81 `run.sh` 把"写失败"当成状态翻转：一面漏签、一面再跑整轮真实登录（高-条件可达）
① `run.sh:331` `: > "$SECOND_DONE_MARKER" 2>/dev/null || true` 零留痕 ⇒ 标记缺失时 `:286` 不跳、`:292-297` 状态非 SUCCESS、`:306` **再跑一整轮真实登录**（重复真实登录；现网被 `PROD-FACTS.md:34`"07:12 从未拿到锁"遮蔽）。② 镜像面 `:110-114` 把 **noclobber 写失败**当成"今日已触发"⇒ 导出 `YIBAN_SECOND_RUN=1` ⇒ `:201` **静默关闭进程内补签轮**（漏签；脚本自己在 `:50-52` 承认这条混淆），而现网补签只剩这一条通道。③ `:128-131` 对锁目录做属主硬校验，对承载判定的 `STATE_DIR` **零校验**。④ `:17` `.env` 不可读时无 else 分支、静默回落默认值（配合 PROD-FACTS"仅 root 可读"与 README:824 的用户位 `yiban`）。⑤ `:204` 用 `_is_truthy`、`:276` 用 `= "1"` ⇒ 同一键两套取值域。MF-43② 登记的是同段另外三面，"写失败 ⇒ 状态翻转"未登记。〔C-06 · ADJ-6〕
验收不变量：任何决定"跑/不跑"的写入必须判码，写失败一律走 fail-closed（不跑并告警），且不得存在"写失败==已完成"的等价类。

#### MF-82 `run.sh` 无条件采信 `sign-status` 文本 ⇒ 与 MF-46 的注入面成环（中）
`:292-297` 与 `:316` 精确等串采信，无属主/来源/与库内事实的交叉核对 ⇒ 伪造或搬走 `STATE_DIR` 即可让当天 89 个账号一次不签，日志与真实成功一字不差。
**注入通道要降格**：快照上枚举写入方（`sign-status|STATUS_FILE|yiban-settled|yiban-run-today` 在 `*.py *.sh *.js`）**只命中 run.sh + 3 个测试** ⇒ 不是低权远程注入；环的另一半只能走 MF-46 的 `.env` 臂，而该臂在 `9d3f491` **仍活**（`web/services/env_io.py:238-239` 只验本次传入值、`:243/251` 仍 `splitlines()+join` ⇒ 存量潜伏分隔符被实体化；`:280/294 ensure_secret_key()` 是不需人为动作的第二条触发器，见 MF-84）。〔C-07 · ADJ-6〕

### L 簇 · `.env` 写入面（安全开关可被静默改）
#### MF-83 `.env` 的整文件读-改-写不保证持锁，锁责任写在 docstring 里（中）
`yiban/infra/env_io.py:write_env_keys` 函数体不持锁（`:234` docstring 推给调用方），运行期无痕迹。逐个核完 10 个写入点：**9 个真持锁**（`account_crypto.py:318-323`、`audit_chain.py:129-133`、`tracking.py:69-73`、`web/services/env_io.py:232/260`、`me.py:118`、`settings_api.py:861`、`executor_env.py:193/220/240`、`probe.py:189`），**1 个不持锁**：`scripts/loadtest/seed_accounts.py:32-57`（无锁 + `open(path,"w")` 就地截断 + `:149` 把 `YIBAN_GLOBAL_PAUSE` 写成 `"0"`）。"拿不到锁 30s 后告警并继续写"（`locks.py:128-131`）今天不可达（`.env` 那把锁内查不到慢 I/O；MF-57 的 150 分钟是**另一把** `_file_lock`），但"锁文件建不出来""相对路径 + 不同 cwd 使 `abspath` 分叉"两支不需要攻击者（后者已在 MF-57，不重复）。后果是**整行消失**而非值覆盖（后落盘者的 `out` 里根本没有对方刚 append 的那行；delete 语义还会把对方新行删成不存在）。〔C-16 · ADJ-13〕
验收不变量：锁在 `write_env_keys` 内部取得（外层拿不到即失败），并有一条 `grep` 级断言"不存在不持锁的 `.env` 写入方"。

#### MF-84 `ensure_secret_key` 用宽松读判"全新部署"⇒ 读不到就生成新钥并折叠旧键（中）
同文件口径自相矛盾是最硬证据：`yiban/infra/env_io.py:31-32` 明写"读失败误判未配置会静默生成新钥覆盖旧钥，宁可启动失败"，该 strict 支在 `account_crypto.py:282-290`、`audit_chain.py:79` 被采纳，**唯独 `web/services/env_io.py:267` 用宽松读做同一形状的决策**。机理关键是 `:267`（宽松）与 `:279-280`（裸读）**两次不同源读取**。删除真发生：`:288-290` 无条件滤掉旧键行，`:291-293` 追加 `YIBAN_REGISTRATION_PAUSE=1` 却**不折叠旧的 `=0` 行** ⇒ 两行并存、后写覆盖先写、注册被静默关闭。权限正常时最可能的触发是"空/残缺文件窗口"（`upsert_env` 截断、`vim` 默认 unlink+新建 ⇒ 走 `FileNotFoundError` 支）；`UnicodeDecodeError` 不吞、会炸启动（排除项）。
现网取证一条（只读）：日志里在非首启机器上重复出现"已自动生成 YIBAN_SECRET_KEY"即命中。不并入 MF-46，但须引用其影子行机制，并与 MF-58（同支宽松读的读侧 fail-open）、MF-48 交叉引用。〔C-17 · ADJ-13〕

### M 簇 · 凭据与隐私出口
#### MF-85 弱密钥只 `WARNING` 不阻断，而模板自带一把**逃过全部三条判据**的示例钥（中）
实算（跑 `_decode_key` 同款判据）：`.env.example:18` 的 `0123456789abcdef`×4 解出 `01 23 45 67 89 ab cd ef`×4 ⇒ 全零 False、`len(set)=8` False、`bytes(range(32))`/逆序均 False ⇒ **三条全逃**。根因是判据 3 比的是**字节值**连续，而模板串连续的是**十六进制字符**（`account_crypto.py:292/300/302/304/306`）。
打折项：`:18` 是**注释态**、`.env.docker.example` 与 compose 都不注入该键、默认自动建钥走 `:108 secrets.token_bytes(32)` ⇒ 触发需人工取消注释；"仓库挂公开 GitHub"这一支撑"高"的前提在基线与 PROD-FACTS 里查不到（实证据是 `git pull gitee server-web`）⇒ 不据此升档（公开仓一事仍留在待裁决 #6）。修法裁为**阻断 + 模板换占位**（精确比对公开串零误杀）；否决 KDF/通用熵检测——Web 侧不管理这把钥（无写侧校验点可挂），改成读侧启动即崩会把外泄风险换成全站不可用并撞 MF-50 的不可轮换。〔C-30 · ADJ-16〕

#### MF-86 `phone_code` 不计入 `creds_written` ⇒ 只改设备识别码不要口令、不标凭据改写、不发变更信（中）
读写口径互斥：读侧只认 password+phone 共 3 处（`accounts_api.py:376-377`，同式在 `logs.py:297-300`）；写侧与 password 同档 4 处（`store/accounts.py:161/443-446/454-457/467-473`）。门与信号原文行：`accounts_api.py:378-381`（门调用点）+ `app.py:2269-2275`（full 必输 / risk 未命中换环境即放行 / off 永不）、`:426-432`（"改写凭据"位）、`:437-458`（当事人信）、`:464-475`（管理员 urgent）。
**前提订正**：`protocol.py:250/365` 两条登录函数参数里没有 `phone_code`，它只进 `sign_in_form:237-243` 的 `"Code"` ⇒ **不是账号接管**，且无下发通道（`accounts_data.py:152`）；成立的是完整性/可用性轴的静默改写。现网三态：`6d4eafa` 的 `accounts_api.py:374` 同形 ⇒ **已在现网**，且现网 `PW_GATE` 缺省 `risk`。〔C-41 · ADJ-9〕

#### MF-87 `__clear__` 哨兵在进 SET 前被 pop ⇒ 清空凭据是静默空操作而接口回 200（中）
`accounts_api.py:386-387` 与 `my.py:702-704` 把哨兵 `pop` 掉 ⇒ 不进 SET；全仓 4 个消费点无一折算成 `""` ⇒ 用户点"清除设备识别码"看到"已保存"，库里值原封不动。MF-61 只从前端侧记过"`__clear__` 空操作却回 200 + toast"，**后端为何空操作的机制未登记**（本条即机制），二者勿分两处修。〔ADJ-9 新挖 · 关联 MF-61〕

#### MF-88 个人提交口把"号码是否在册"变成可定向确认的预言机（中）
`my.py:443` 的判重打在**全站账号表**上（`accounts_data.py:94-106/179-184`），且早于任何真实外呼（`my.py:458`）⇒ 零配额、**零留痕**（`:454-458` 区间无 `db.audit`）。可达者是"已登录且名下无未删账号"的会话（现网 110−89 ≥ 21 个天然可达）。
**两条原报数字被推翻**：限速是 `app.py:541-542` 的 60 次/10 秒 ⇒ **≈21 600 次/小时/IP**（不是 360）；但 89 个在册目标 ÷ `^1\d{10}$`(=10¹⁰) ⇒ 满速期望约 5 100 小时一次命中 ⇒ **"批量枚举"不成立**，只剩"定向确认某个号在不在册"。管理口 7 处 400 回显（`accounts_api.py:175/233/284/288/371/410/414`）行号复核为真，但回显的是**调用方自输**的 `clean['phone']`、审计与日志侧均已 `_mask_phone`（`:429/:477`）⇒ 不构成外泄；`_duplicate_phone_error:187-202` 自订的是"不泄露**归属**"而非"遮号码"⇒ 原报的"正面冲突"不成立。〔C-38 · ADJ-9〕

#### MF-89 状态文件用内置 `open()` 建 tmp ⇒ 终文件继承 umask，"创建即 0600"的既有契约只覆盖部分通道（低，两条待取证）
13 站点逐个复核（tmp 命名行 / `open()` 行 / `os.replace()` 行三行号全对上 ST-3 清单）；仓内已有合规助手 `state_io._write_private_json`（`os.open(...,0o600)`）并被 `test_probe.py:204`、`test_notify_webhook.py:1361` 钉死。内容分档：**明手机号 6 处**（`state_io.py:229/:246`、`alerts.py:321/:353`、`runner.py:591`、`cred_state.py:129`）、仅计数/时刻/pid 7 处；**凭据字段 0 处、审计明文 0 处**。两条升级口已堵：`sign-state.message` 上游已脱敏（`attempts.py:208-212`）、`cred-state.json` 只有 `fail_days/last_fail/paused_since/probe_date`。
不成立的是"多台机全局可读"：`runner.py:142` 与 `cli.py:754` 都在 `main()` 首行 `os.umask(0o077)`，`web/deploy/yiban-web.service` 模板自带 `UMask=0077`（仓内共 12 条 umask 077 代码锚点 + 1 条 systemd 声明）；按进程切比按站点切有用——web 只写其中 3 处（cred-state / notify-ledger / notify-throttle），其模式**唯一押在实装 unit 的 `UMask=` 行上**。tmp 的真窗口不是半写而是"整表内容 + 宽模式 + 崩溃残留 ≥1 天"（`state_gc.py:83-84 _TMP_MAX_AGE_SEC=86400`）。缺的两条取证：状态目录模式（README:171 裸 `mkdir` 会是 0755）与是否存在第三用户 ⇒ 任一为"宽"则升中。
与 `migrations.py:1195-1199`（已判入 MF-40）**同族同判**：`os.replace` 不改 mode、chmod 追不上残留 tmp，"补 chmod"劣于"只走一个通道"。〔C-28 · ADJ-8〕
验收不变量：`tests/test_state_file_writes.py` 里**外层套 `os.umask(0o000)`** 再断言终文件 0600 + 无 tmp 残留——不加这一句，测试会继承 077 而恒绿，这正是这 13 处今天漏网的原因。

#### MF-90 告警无幂等：服务端已接收之后才超时会换条目**重发同一封**，上界 10 份（中）
机制源码级闭合：`smtplib.SMTP.__exit__` 的 QUIT 会抛未被放行的 `SMTPResponseException` ⇒ `transport.py:114` 捕获 ⇒ 换下一条目再投同一封；全仓无 `Message-ID`/幂等键 ⇒ 上界 = `web/app.py:592 MAIL_SMTPS_MAX = 10`。
**行号与措辞订正**：`sent = sent or _send(...)` 全仓 grep 0 命中（`:111` 实为 `sendmail`，聚合真身在 `:143-147`）；`web/security.py:hash_ip` **不存在**（唯一定义在 `tracking.py:109`）。分轴后另两支不占号：OR 聚合 + `SMTPRecipientsRefused.recipients` 零消费者 ⇒ **补句 MF-44**（并写明"零记录"过头——`:112/:118-122` 逐地址记了日志，缺的是结构化投递账）；逐地址串行/每地址重建连接 ⇒ **同 MF-57**；`_classify_send_error`（`:40-42`）把证书失败与断网同文案 ⇒ 低、不占号（`:103 create_default_context()` 已挡住外泄，`:28-34` 是写明的反端口扫描取舍）。收件人 4 与 SMTP 条数是**假定值**（现网未取证）。〔C-21轴2 · ADJ-16〕

### N 簇 · 自检面与数字口径（绿了但什么都没证明）
#### MF-91 伞形条目：判据的输入由被检对象的写入者供给 ⇒ "缺陷 ⇒ 判据红"这条边被构造性切断（高，元条目）
本簇四条（MF-92/93/94 + 已登记的 MF-52）共享一个可用一句写完的元命题：**期望集或输入来自被验证的那一方，且测试用夹具绕开同源点 ⇒ 没有任何一层会喊**。分工必须写死，否则会与 MF-54 混修：MF-54 治"**源**的分叉"（不变量＝第二份定义不存在，grep 可门禁）；本条治"**证据的独立性**"（不变量＝每道自检必须自带一条"把输入改坏 ⇒ 工具必须红"的活体反例）。**MF-54 的修法在 MF-93 这类条目上反而有害**（把两份定义收成一份自证的定义，一致得更彻底）。
条目只管元命题 + 硬门禁 + 一张"每道自检的独立证据来源"登记表；点条目不并进来，只打标记。〔ADJ-10 总问题〕

#### MF-92 `ledger_check` 拿补账的输入去对账补出来的表 ⇒ 且窗口外反向**恒红**（高）
`ledger_check.py:128-133` 用 `sign-state-<day>.json` 验 `sign_tasks`，而 `migrations.py:861/934` 的补账来源正是同一份 `terminal_task_state` ⇒ 对被 v20 补出的那批行 check 1 构造上恒真。**措辞要改**：不是"恒通过"——check 1 只 `SELECT phone` **不比状态**，而 v20 是 `INSERT OR IGNORE` ⇒ planner 先写的行赢，"JSON 说 success / 台账说 failed"**永久失明**；而窗口之外（v20 一次性、只回看 14 天，删账号还会连带删台账）它**恒红**。骗过的门有两处：设计文档 `ledger-dual-version-design-20260923.md:258` 把它写成"迁移的唯一验收门"并承诺**三方对账**，交付时被瘦成单向；`tests/test_ledger_check.py:80-86` 的夹具刻意绕开 backfill ⇒ 13 例全绿证的不是生产数据流。另 `:144-155` check3 的 `other = total − translated − backfilled` 是残差定义（不构成独立证据）。〔C-26 · ADJ-10〕

#### MF-93 容量估算是"一份算式跨两个配置层"⇒ 保存闸门按偏大值放行（中）
`capacity.py:93` 只传 `gap`，`avg/enabled` 落 `schedule.py:69` 的 `os.environ`；**web 进程从不把 `.env` 装进环境**（`web/*.py` 内 `load_dotenv`/`os.environ[` 零命中），而 `gap` 偏偏读 `.env`（`settings_api.py:310-312`）⇒ 同一次估算跨两层。复算：W=4200/gap=10 时 avg=3⇒**323**、avg=12⇒**191**（台账写 190，差一），高估 **69%**（≈台账的"约 70%"）。骗过的门是闸门自己：展示面 `:202/:213` 与判定面 `:312-314` 共用同一个偏大值 ⇒ 两出口永远互相对齐；`test_capacity_*.py` 18 处全用 `mock.patch.dict(os.environ,…)` 造输入，测的是另一个前提。现网 89 < 191 ⇒ **当前不越线**，但界面白送约 130 个名额的错觉。〔C-27/L11 · ADJ-10〕

#### MF-94 邮件配置"写 `.env`、读进程环境"+ README 教的部署方式会把新值冻住（中，含 L43 订正）
真不同源在**读序**：`mail/config.py:_get` 是 env 优先、`.env` 兜底，而 `README.md:634` 指示的部署正是把整份 `.env` 拷成 `EnvironmentFile` ⇒ 保存的新值被启动期冻结拷贝压住，**restart 也修不好**。与 MF-51（按数组位置沿用凭据 ⇒ 才是"界面 A 凭据 B"的真路径）**不是同一条**；与 MF-50 同拓扑、不同后果面。另有 1 个新增低危点：`host` 写成 `127.0.0.1:465` 会因 `_is_ipv4_literal_like` 的"4 段全数字"判据被放成"真域名"（构造性绕过，但 `getaddrinfo` 必失败 ⇒ 后果低）。`SMTP_PORT` 非法→回退 465 有**两份实现**且发送侧零日志。
需订正：**台账 L43 的两条主张在基线上为假**（`check_smtp_host:91-92` 显式拒 `localhost`；设置页渲染与发送侧同用 `smtp_list()` ⇒ 不存在"界面说 A 真走 B"）⇒ 本条即其收窄后的幸存形态。三态：②③④⑤代码级即可判；①需现网取证 `/proc/<gunicorn pid>/environ` 是否含 `YIBAN_MAIL_*`（按红线未登机）。〔C-22/L43 · ADJ-10〕

#### MF-95 注销冷静期把 `/api/login` 变成零留痕的凭据验证器（中，代码已在现网、当前可达集合 0）
`auth.py:92-99` 判据与 `:157-159 recoverable` 出口 ⇒ 口令输对但账号处于冷静期时既不写 `login_ok` 审计（`db.audit` 在 `if role:` 块 `:104-126` 内）也不计失败（`:160`）。应用侧确实零留痕：`audit_chain.py:359/404` 是唯一写入口且未被走到，`page_visits`/`server_metrics` 已由 v14 删除，两个限速表是进程内 dict ⇒ 只剩 nginx 一条同为 200 的 access log（不可归因）。
**两处原报口径必须改**：①限速绑的是 `auth.py:71` 的 **10 次/60 秒/IP（600/h）**，`app.py:541-542` 的 60/10s（21600/h）是更宽的全局桶 ⇒ "无限次"与"21600/h"都不成立（XFF 由 nginx 覆盖式写、不可伪造；`-w 1` 计数不分叉）；②时延放大器**方向反了**：冷静期账号对错都跑 2 次 scrypt，不存在/活跃只 1 次 ⇒ 2× 标出的是"7 天内注销过的邮箱"（**免口令枚举**），而口令对错由响应体直告。
三态：`6d4eafa` 的 `auth.py:92-99/153-155` 同形 ⇒ 代码已在现网；`deleted=1` 是冷静期的**超集**，"软删 0 ⇒ 此刻可达集合 0"成立，但窗口由任一用户自助注销打开 7 天 ⇒ 写"已在现网、当前可达集合 0"，**不采纳**"现网不触发"。〔C-42 · ADJ-15〕

### O 簇 · 面板与接口给假信号
#### MF-96 `POST /api/notify-test`：`force=True` 跳过冷却与两本每日额度、零审计（中）
`web/routes/notify.py:406-414` 确实只判 `_is_builtin_admin_session()`、无 `_high_risk_gate`、零 `db.audit`（notify.py 的审计只有 `:238/:398`；全站唯一 `after_request`（`app.py:2088`）只设响应头）。外呼为真但**不是 SMTP**：`transport.py:182-188 → send(force=True)`，`:153` 把 cooldown（`:158`）与两本每日额度（`:161`）一起跳过，出口是 `:71-100` 的 ServerChan HTTPS POST 或经白名单的 custom webhook；**不能群发到任意地址**（view 不读任何请求参数，收件方 100% 来自服务端配置）。"无限额"订正为：仍吃全站 `RATE_MAX=60/10s`（`app.py:541-542,1870-1890`）+ CSRF（`:1979-2011`）+ nginx 50r/s。"不在 `app.py:2380-2390` 清单"= **同 MF-58** 的根（那份 docstring 是"必须当次输口令"的落点表，不是审计清单，提报定性偏了）。现网：该路由在部署线 `6d4eafa` 存在（`:459/:476`），触发需主管理员会话被盗。〔C-44 · ADJ-7〕
验收不变量：第 4 次调用必须 429 且**零外呼**；每次外呼必须落一条含操作者的审计。

#### MF-97 `/api/logs` 的三元组各说一件事 ⇒ 前端"是否还有更多"必然判错（中，已在现网）
`web/routes/data.py:api_logs:91-102`：`total_lines = len(masked_all)` 在 `if q:` **之前**；`_LOG_VIEW_CAP = 5000` 在 `if show_all:` 分支**体内**赋值；`truncated = len(masked_all) > _LOG_VIEW_CAP` 在过滤**之后** ⇒ 三个数不同轴。同族两份封顶口径（`-80:` 裸字面量 vs `_LOG_VIEW_CAP`）属 MF-54 补句。复核状态：**单源未独立裁决**（纯计数逻辑，可由读码直接判）。〔C-48 · GAP-4〕

#### MF-98 "我的日历"把"状态目录读不到"渲染成"这个月没签"，还把伴生文件当成额外的天（中，幻影键已在现网）
`web/routes/my.py:615-627`：`except OSError: pass` ⇒ `ok:true` + 全月空白 + 零日志；`date = entry.name[11:-5]` 不过滤伴生文件 ⇒ `X.json.lock` 被切成 `"X.json"`、`X.json.tmp1234` 切成 `"X.json.tm"`。造键源 `runner.py:572`（每天必造 `.lock`）+ `locks.py:62` + `state_gc.py:7/81-85`（`_TMP_MARK=".tmp"`、`_TMP_MAX_AGE_SEC=86400`）。幻影键部分**已在现网**（只要写过 `sign-daily-<date>.json` 就必有 `.lock`，`python -c` 切片实跑验证），空白日历部分未知（需权限异常/目录分叉）。属 MF-45/MF-61"面板给假安心"的反面同族 ⇒ 交叉引用不合并。〔C-34 · ST-3，实跑取证〕

#### MF-99 日志页用宽行模型切日志、不匹配就 `continue` ⇒ 被撑开的后半行整块静默消失（中，取证未知）
`web/services/logs.py:91 raw.decode(…).splitlines()`，`parse_sign_log:94-116` 内 `:107` 不中即 `:110-111 continue`；`_log_lines_for:139-152` 还要 `startswith(f"[{date_str} ")` ⇒ 半行永久丢失且无计数。对照窄侧 `:85 f.readline()`。可达性依赖与 MF-86 同一个"无字符集校验"字段。与 MF-46 的 `.env` 行模型同族但对象是日志文件 ⇒ **勿并**。复核状态：单源（ST-4），机制读码可判、触发需生产日志里的裸 U+0085。〔C-36 · ST-4〕

#### MF-100 脱敏上线后运维按完整手机号 `grep`/`journalctl` 恒返回空，且没有替代口径与 runbook 禁则（中，代码外面）
盘上不再有 11 位连续数字 ⇒ 按完整号码的 shell/journalctl 检索恒空，会把人引向"这台机器没签过这个号"的错误结论。修法缺口：MF-49 的方向只覆盖"单一原语 + 展示层禁止第二套口径 + 版本号可判别"，**没有** a) 后 4 位/遮罩形态的反查工具 b) runbook 禁则。本流独立复核 `journalctl|runbook|后 4 位|反查` 在登记表命中 0。与 MF-49③（UI 侧 `q=`）分两半：代码内 vs 代码外 ⇒ 独立成条、双向交叉引用。〔C-49 · GAP-4〕

#### MF-101 手动签到不在 spawn 前过滤 `user_paused` ⇒ 批量"N 个账号"计数虚高（低）
`signin_api.py:200/:294` 不在派发前剔除已自暂停的号（全文件 `user_paused` 0 命中），单条提示与批量计数都会虚高；真正拦住的是引擎侧 `round.py:464`（rc=2、`attempt_signin=0`、日志"⏹️ 用户已取消签到"）。
**必须同时记录这次改判**：原指控"一键暂停/周末/熔断/自暂停四类门对手动腿完全无效，急停后照样真实登录"经实跑**不成立**——①急停、②周末确实放行（但这是写进 UI 契约的**设计**：`work_settings.html:632/636`「手动签到不受影响」，由 `tests/test_global_pause.py:59` 锁定，自 `c700ae2` 即如此）；③用户自暂停**实际拦住**；④熔断放行 1 次且成功后清除记录、失败顺延 `probe_date`（半开试探语义）。三份取证共同断言的"子进程 exit 0、全链路无信号"实为 **exit 2**，并经 `signin_api.py:127 → manual_sign.py:74-76,98-105` 往当天日志写"⚠️ 手动签到未完成…本轮未实际签到"，另有 ⏹️ 状态与 `:259` 审计行。设计意图冲突已裁决（2026-09-25，待裁决 #8）：手动腿保持豁免，本条只剩 spawn 前过滤 `user_paused` 一半照修。〔C-01 · ADJ-1〕

#### MF-102 窗口谓词两式不一致 ⇒ 每天恰有 1 个墙钟秒"文案已打、门未关"（低）
`window.py:198-201` 预检用 `remaining_sec <= 0`，`:203-205 is_closed` 用 `>`，而 `_minute_of_day`（`:245-246`）只到整秒 ⇒ 在 hi（缺省 07:49:00.000~0.999）有 1 秒窗口文案已打、门未关；该请求仍在 `YIBAN_SIGN_END` 之内，只越过自设掐尾缓冲。修法：**预检改用 `_win.is_closed(now)` 复用同一谓词**。
**严禁照原指控"补 `return`"**：`runner.py:399-412` 之后有第二道同源门必然挡住（v2 `round.py:323 _window_closed` 是 `while pending:` 弹出第一条判定、排在唯一请求出口 `:374` 之前；v3 `executor_v3.py:581` 同理排在 `claim_batch:589` 之前；`tests/test_effective_window.py:188` 断言 `attempt.call_count == 0`）。补 return 会让 `results` 留空、账号落进 `runner.py:528` 的"未执行"默认桶 ⇒ 退出码 1 + 失败邮件 + `run.sh` 写不出 SKIPPED，正是 `:267-270` 记过并修掉的坑。另清点 21 条"宣告整轮不干"的文案，除 `runner.py:401/409` 外**全部自带 break/return/continue** ⇒ 单点非家族。〔ADJ-3 残留〕

#### MF-103 `core.js` 的 4 处 string-HTML 出口不在 JS 不变量的文件集内（低，潜伏 footgun 而非漏洞）
`core.js:310/:340/:396/:57` 是仅存的 string-HTML 出口，而 `tests/test_web_js_modules.py:251-268/:270-289` 两条规则只扫 `pages/*.js` 与 `components/*.js` ⇒ **`core.js` 与 `calendar.js` 都不在文件集里**，缺口正好是被指控的那个文件。补强断言：把 `appendBody` 收成只接受节点（`:310` 那句"调用方保证可信"的注释承诺变成类型上不可表达），并把 `core.js`/`calendar.js` 纳入扫描集。
**必须同时记录这次改判（原指控"存储型 XSS 打到管理员"不成立）**：提报把 `YB.confirmDialog({body:"…"+name})` 当成了 `openModal`——`core.js:445` 是 `body: el("div",{class:"pm-confirm-text", text: opts.body})`，`el()` 的 `text` 分支即 `core.js:56 node.textContent` ⇒ `appendBody` 在 `:309` 走 `nodeType` 分支返回，**被指控的 `:310` 分支根本不执行**。5 个被点名的调用点（`account-ops.js:83/97`、`my-accounts.js:310/337/367`）全是这个形态；全仓 11 个 `display_name` 出口逐条读完 **0 处进 innerHTML**；`setBody` 全仓 0 个调用者（死分支）；`insertAdjacentHTML/outerHTML/document.write/eval` 全 0。写侧确为真（任何登录用户可写 `name`，`accounts_data.py:217-218` 只查 `len>50` 不查字符集，而同函数 `:224` 的 `PHONE_RE` 把 phone 钉死），但只作**纵深口径附注、不挂 XSS**。"sessionStorage 里有 csrf 会放大"也不成立：同源脚本本就能 `GET /api/me` 自取 token（页面自己在 `core.js:975` 就这么干）⇒ 该事实属 L40/MF-49 的独立面，不得在 XSS 下重复计价。库里是否已有人塞 `<` = 未知（取证 SQL 在 `out/ADJ-4-xss.md` §D）。〔ADJ-4 · 关联 ST-4〕

### 表六 · 补句清单（判为"同一条/应回填"，**不占号**；逐字表述见 `out/MF-CANDIDATES.md` 表二）

| 目标条目 | 要补进去的要点（一句） | 出处 |
|---|---|---|
| MF-47 ① | `--only` 多子进程是 **race ≤N**（被 `claims.py:150-152` 挡住），并补 CLI 侧第二处入口证据与 v3 `pending_count` 库异常时 `warning + return 0` 的 fail-open（与同表 `try_claim` 的 fail-closed 相反） | ADJ-2 / GAP-2 ②1 / GAP-3 ② |
| MF-47 ⑤ | web 侧第五支：`signin_api.py:74 except OSError: return True  # 被其他签到进程持有`——把任何 flock 错误报成"有人在跑"；并补"只持 `signin-run.lock.fallback`、一辈子不碰全局锁"的兜底常驻（接上 `scripts/yiban-fallback.sh:94` 即变已在现网），锁名裸字面量另归 MF-54 | ST-3 §4-B / GAP-4 / ADJ-7 |
| MF-47 ⑥ | 时钟跳变守卫的站点清单不能只有 `claims.purge`，须补 8 个（`state_io`/`alerts`/`notify/ledger`/`mail/transport`） | GAP-2 + GAP-3 互证 |
| MF-43 ①② | 兜底轮 `workers.py:271-277` 不传 `event_sink` ⇒ 补签成功也不写台账；`L2-A 2-4`（同一分钟 shell 与 python 对"要不要补跑"给出相反判定，且这次"只读判定"顺手拉起 2 个执行体并各 purge 一次）与 `1-6/1-7`（`--second-run-check` 除 10 外一切码当"无需补跑"） | GAP-2 ②2 / GAP-3 ② |
| MF-44 | 额度独立腿：`settings_api.py:_executor_change_alert:83-91` 用 `urgent=True` 但**不带 `force=True`**，与 6 处管理侧告警同账本（`DEFAULT_URGENT_DAILY_MAX=3`）⇒ 改执行体清单即吃光当日紧急额度，耗尽后只 `_log_skip`、客户端零信号；另加 MF-90 的"OR 聚合 + `recipients` 零消费者" | GAP-2 ②5 / ADJ-16 |
| MF-49 | 见本节开头"覆盖性订正"第 1、5 条；再补：②类点修清单缺 `time_prefs.py:118`、`claims._notify_pool_added`、`alerts.py:117`、`logs.py` 服务层直返未脱敏行四个出口；②类另有一处出口 = **HTTP 响应体**（`signin_api.py:219/229/237` 三条文案原样回显裸号，同函数入日志与审计处均已遮）；argv 落点是 `ps`/`/proc/cmdline` 而非日志（`_launch_signin_proc:151` 无 shell ⇒ 不落 history；`combined` 只记请求行 ⇒ 不落 nginx）；`round.py:226/288` 两条证明 **66 命中是下界**；加 C-37 的"点修原语自身失效"（`mask_phone` 的 `len!=11` 直返 + `_mask_email` 无 `@` 直返 + 第二份定义 `store/accounts.py:141` 丢了幂等保护 ⇒ 只留一份原语）；加 C-39 的"`target` 列三套口径"（`jobs.py:117`、`signin_api.py:259` 用 `mask()`/`_mask_phone()` 而 `time_prefs.py:60-64` 用加盐哈希） | GAP-4 / ST-1 / ST-3 / ADJ-16 |
| MF-50/MF-53 | 加盐哈希手机号：盐是 `.env` 键且**与主库同目录、同进 `backup.sh` 归档**（不需登机即可判）；穷举口径实测 7×10⁹、本机 1.69×10⁶/s ⇒ ≈69 分钟单核。但**同库 `migrations.py:85 phone TEXT NOT NULL UNIQUE` 本就是明文**、AAD 也是明文号 ⇒ 穷举不产生新号码集合，成立的是"匿名声明不成立"。**否决**"补进 MF-52/MF-53"（MF-53 症状是"追不到人"，本条相反，照它修会打断 `time_prefs` 的冷却关联键） | ADJ-16 |
| MF-52 | ①写日志/写审计前不过 `_nl_safe`（`api_settings_save:609-617` 与 `db.audit:623/625` 直拼请求体原文）；②`_anchor_file_state:852-863 except ValueError: return {}` 把"指纹 JSON 损坏"当"从未写过锚点"⇒ 整段库内指纹判据被跳过；③本条缺"验收不变量"一格 | GAP-2 ②4 / GAP-1 表三 |
| MF-53 | 恢复场景必须写：锚点是跨日单一追加文件、库是每日快照 ⇒ 恢复到 D-7 后 `_anchor_file_state` 只有 `<`/`==` 两支 ⇒ 当日校验整体不执行；**照 README 把 `state/` 一并覆盖会让锚点被回退、D-7 之后的防删除证据永久消失**（链头在库、链仍自洽 ⇒ `audit_health.healthy=True`）；`audit_verify.py:83` 的库路径自成一式（可能验的是**另一个库**的链仍输出"通过"）；`audit_chain.py:882 except Exception: pass` 把"查不动"也判通过 | GAP-2 / ST-3 / GAP-3 |
| MF-54 | 计数须含：`.env` 第三份解析在 `backup.sh:88-94 env_get`；"600 秒缓冲"经新键被**整键丢弃回 60**（不是夹取）；已了结词表在链上被重新解释共 5 处；`api_signin` 状态码靠**中文子串匹配** msg（单条与批量各抄一遍字面量 ⇒ 改文案即静默改 HTTP 契约）；`/api/logs` 两份封顶口径；本条缺"验收不变量"格且证据只给计数未列 `文件:符号` | GAP-3 / GAP-4 / GAP-1 |
| MF-55 / MF-45 | ①`accounts_api.py:59-64` 的 `user_paused` 分支**无条件**覆写当日状态为"已取消"，不看是否已有终态 ⇒ 签成功后自暂停即显示"已取消"，与 `sign_events` 相反；②批量审计计数两口径（`batch_targets` 无条件 append vs `done = len(ops)` 只数满足前置的行）；③**MF-45 未写同页另一侧**：`_my_account_indices_of` 的 `a.get("owner")==email` 在 `YIBAN_ADMIN_USER` 取字面量 `admin` 时与无主裸账号的 owner 撞车 ⇒ 内置管理员 /mine 列出全部裸账号并开放编辑/删除 | GAP-2 ②8 / GAP-3 |
| MF-56 ① | 补 web 进程内状态落点：`app.py:1766-1815` 的 16 张表 + `capacity.py:149 _mail_alert_ts` + 手动签到的 30s 防抖 / `procs` / `batch_running` / `last_batch_ts` 四份字典（`-w>1` 同倍放大；现网 `-w 1` 不触发的脚枪）。**反例要写进去**：notify 推送节流在盘上（`ledger.py:24/108-112`）⇒ 不是所有节流都在内存。原引 `app.py:592` 是 `MAIL_SMTPS_MAX`，错行 | ADJ-7 / GAP-4 |
| MF-57 | 见本节 MF-49 订正第 5 条下方 **`out/GAP-4-late.md` §3 全段**：下界（30s/条目 ⇒ 5 分钟、加 DNS 5~8 分钟）与上界（15 操作×15s≈225s×N=10×R）两套算法都要；`MAIL_SMTPS_MAX=10` 仅写侧校验、读侧不截断；`timeout=15` **硬编码** `transport.py:105/107`、是 per-socket-operation 空闲超时不是墙钟截止、不覆盖 `getaddrinfo`；R=4/N=10 是**假定值**；排版层实测 9ms 非瓶颈；`MAIL_SUMMARY_MAX_CHARS=200_000` 只量纯文本而同一封 MIME 实测 918.9KB（×14.7）；`locks.py:74 with lock:` 无限等待且零日志（告警在 `_acquire` 里而它没被调到） | GAP-4 §3 / GAP-3 |
| MF-58 | ①注册口 `api_register:242-244` 对内置管理员邮箱给**独有文案**且排在 scrypt 之前（不耗时）⇒ 匿名者零成本定位超管邮箱；②口令门被挡下的前 N-1 次既不告警也不写审计，第 N 次的 `db.audit` 还额外要求 `cooldown > 0`，且 `YIBAN_ADMIN_DELETE_MAX=0` 一键整体关闭高危额度**无痕迹**；③**清单腐化的逐字复现实例**：`app.py:2380-2390` 列了 `/api/accounts/<idx>/delete`，实测注册面上不存在（真实是 `DELETE /api/accounts/<int:idx>`，`accounts_api.py:897`），对应视图 `:664-675` 只占额度不调 `_high_risk_gate` | GAP-2 ②6 / ST-2 |
| MF-59 | 补第二/第三处入口：`store/accounts.py:replace_accounts:603` 缺省 `status='active'` vs `add_account:383` 与 DDL（`migrations.py:90`）缺省 `'pending'` ⇒ 整体替换/导入绕审直进主链；`my.py:472` 配额已扣而 `_start_verify_job:559` 抛 `VerifyGateBusy` 只 warning、响应仍 `ok:true` 且不含 `job_id` | GAP-2 ②3 |
| MF-40 | ①`migrations.py:1195-1199` 先写 tmp 才 chmod 是同族第二处（方向已给）；②SQL 标识符未转义残留（`:352` 未双写单引号、`:576-587` 未双写双引号）最坏后果是迁移抛 `OperationalError` ⇒ 启动被阻断，正落在本条不变量上 ⇒ 不另立号；③"重建型迁移缺列/行守恒断言"一句；④另补 C-19：v20 回填的**明文驻留无上界**（`_BACKFILL_DAYS=14` 是扫描窗口不是保留期，全仓唯一 `DELETE FROM sign_tasks` 是 `db.py:685` 按 phone，而源文件 `sign-state-*.json` 在 `log` 桶默认 **365 天**） | ST-3 / ST-3 §5 / ADJ-15 |
| MF-46 | ①脚本侧第三份 `.env` 写入实现 = `seed_accounts.py:32-56`（:36 宽行模型、:44/:53 拼值零校验、:54 非原子、:56 事后 chmod；`web/services/env_io.py:285-286` 注释自证"scripts/ 各写入方尚未收敛"）⇒ 不变量 `grep -c "def upsert_env\|def write_env" scripts/` == 0；②`:241-249` 的"不校验本次值"只对盘上旧行跑 `has_line_break`，`out.append(f"{key}={value}")` 的 value 不过任何校验；③**前缀放行 `YIBAN_*` 属本条第三支**（`scripts/child_env.py:40`，不是"docker/child_env.py:56-57"；宿主侧 `signin_api.py:139` 也在调但 `:142` 把 `YIBAN_DB_FILE`/`YIBAN_ENV_FILE` 钉回 ⇒ 手动签对 DB 重定向免疫，**cron 那路三键全穿透**；`env_io.py:91 resolve_path` 自己就读 `.env`，不需要子进程这一环）⇒ 与 MF-46 是"注入器 / 生效器"的**成环关系**，最大增量是注入 `YIBAN_PROXY` 让真实凭据经攻击者代理外泄 | ST-4 / ADJ-13 |
| MF-48 | 补三个同族删除入口：`scale_driver.py:372-373` 在参数拼出的可预测路径上 `shutil.rmtree` 且删前不校验"目录里有我们的标记文件"；`capacity_probe.ensure_platform:296-301` 红线只有 Linux+root；`backup.sh:restore` 目标只判空串。另 `.env` 读失败静默继承宿主的**第三、第四份实现**（`child_env.py:32-46`、`web/services/env_io.py:267-268`）；`db_export` 一族三条（`os.open` 的 mode 只在新建生效 ⇒ 重跑时整段写入期间保持旧宽模式、抛异常则 `:51` 的 chmod 永不执行；无 `O_NOFOLLOW` ⇒ 符号链接可把凭据写到攻击者选的位置；`--plaintext` **不是只读**——`accounts.py:190-207/303` 的明文自愈会 UPDATE+commit） | GAP-3 / GAP-2 / ADJ-10 |
| MF-51 | "审计只记 `smtps_count`"这一取证未登记；执行体变更告警正文只含 `YIBAN_EXECUTORS[<slot>] 改行/加行`、不含 proxy ⇒ 事后无法还原改了哪条出口 | GAP-1 / GAP-3 |
| MF-34 / MF-65 | 同族三处"响亮失败但无人听见"：`state_cleanup.py:87-95` 只有 `removed>0` 才写 `cleanup.log`（**全部删除都失败时也返回 0 且零留痕**）、`_append_log` 的 `except OSError: pass`、`state_dir` 不存在时只 print + return 1；`yiban-fallback.sh:77` 建目录失败被吞后 `>> "$LOG_FILE"` 也失败 ⇒ 错误只进 stderr 且无 MTA | GAP-3 |
| MF-42 | 修法须点名**哨件在位性**与**可恢复性证明**（MF-76 的未闭合半条）；并补"rc=4（源库损坏、归档照留）/rc=5（缺 sqlite3、快照未经 integrity 核验）契约本身正确，但 `:42` 的 cron 模板 `>> /var/log/yiban/backup.log 2>&1` 使 rc **无任何消费者**" | GAP-1 / GAP-2 / GAP-3 |
| MF-6 | 补 C-08 的验收落点：现有容器用例一律直接调容器侧自己的谓词，全文件 `assert_called` 仅 2 处且都是 `window.from_env` | 本流对账 |
| （新增不变量） | `/api/my-` 的 `startswith` 前缀通配（`app.py:1911`）= **命名即授权**：R3a S3 / R3f b5 / R3h B2-2 三条独立撞实，新增越权面不需改守卫；现网不触发（6 条 `my-*` 写端点逐条查到 session 收窄）⇒ 与 MF-58 共用 gate manifest 修法，先记在 MF-58 修法下 | ST-2 §1-adjacent |
| （否证回填） | 双斜杠绕过（`//api/accounts` 让三处 `startswith("/api/")` 失效）**不成立**：Werkzeug `Request.__init__` 是 `"/" + path.lstrip("/")`，matcher 只在初次匹配失败后才 `re.sub` 合并斜杠、不回写 environ（锁 `werkzeug==3.1.8`）。依据是上游源码、本机无 flask ⇒ 转成一条必须真跑的 CI 用例 `I5` | ST-2 |

### 本流否证追加（收尾轮，别再去修）
`存储型 XSS 打到管理员`（`confirmDialog` 走 `textContent`，被指控的分支是死代码；见 MF-103）· `急停/周末对签到完全无效`（放行手动腿是设计且有测试锁定；自暂停真的拦住，见 MF-101）· `窗口已结束却不 return ⇒ 整轮打真实登录`（第二道同源门必然挡住；见 MF-102）· `--only 一次点击 = N 次真实登录`（是 race ≤N，被 `claims.py:150-152` 挡；见 MF-74）· `一次性令牌被 GET query / Referer 外泄`（目标恒为代码内常量主机、默认流全程不改 Referer、无第三域路径；见 MF-69/70 的收窄形态）· `手机号可被批量枚举`（600~21600/h 都对得上，但 89÷10¹⁰ ⇒ 满速期望 5100 小时一次命中）· `管理口 400 回显构成号码外泄`（回显的是调用方自输值，日志/审计侧已遮）· `check_smtp_host 放行 localhost`（`:91-92` 显式拒）· `状态文件在多台机上全局可读`（`runner.py:142`/`cli.py:754` 首行 `os.umask(0o077)` + unit 模板 `UMask=0077`）· `verify_zero_egress 从未被调用`（调用点在 `mock_env.py:422` 且会退 1；真缺陷是"代理路径下恒 SKIP ⇒ 报绿"）· `loadtest 会拿真实用户凭据打易班`（默认走 `test.env` + 假密码，真凭据需显式把 `--db/--env` 指到生产路径）· `备份连断是当前状态`（历史漂移，V8 已订正）· `24 条 innerHTML 都在 calendar.js`（24 是全仓总数；calendar 只有 6 条且 0 处可控）· `草案 25 条`（实际 22 条，25 是末条原编号）。

### 未编号候选（单源、未独立裁决，落笔前须复现；详情在 `out/MF-CANDIDATES.md` 表一）
C-05 会话缓存 miss→登录→写回 三步无跨进程占位（判中，`session_cache.py:146/192-215` + `locks.py:33` 自证是 `RLock`）· C-43 代理清单"空段跳过校验"`,,,` 即落盘＝全站直连无补偿信号 · C-50 一次性邮箱黑名单静默 fail-open（缺文件零日志、读失败却有 warning）· C-31/C-32 GAP-3 的"落盘静默"与 `logging.basicConfig` 无 `force` 两支 · C-14 兜底质心点未过 `point_in_polygon`——**前提已被本项目 09-21 实测推翻**（真实空缺是自交/退化环，非"凹必漏"），且提报方未在快照重跑 ⇒ 入库前必须独立复现，严重度按复现结果重定 · C-10/C-15/C-18/C-33/C-35/C-40/C-45（`_stale_idx_guard` 那半条已判为 MF-61 补句）。

### 收尾轮修复顺序（接在原"修复顺序"之后）
0. **MF-68 + MF-78 + MF-79 + MF-80 + MF-81 + MF-82** —— 先把"会打到真实易班"与"备份/运维脚本不可逆"两类关掉；这六条都不需要改引擎逻辑，属启动前置断言与脚本判码。
1. **MF-91 先行**（元条目）：它决定后面每一条"验收不变量"要不要带活体反例，晚做就得重跑一遍。
2. MF-76/MF-77（备份可恢复性与轮转）与 MF-75/MF-71/MF-72（分类与假成功）两批并行。
3. MF-83/MF-84/MF-86/MF-87/MF-89/MF-90（配置面 + 凭据 + 隐私出口）。
4. MF-92..MF-100（自检面与假信号），最后 MF-101/102/103（低危三条可打包）。
5. **派发 1（假上游故障注入旋钮 + L3 剩余 16 格）属修复流**：本轮再次确认有 5 条（MF-71 腿②、MF-72、MF-90、C-05、C-12 族）的现网三态**只有注入才能坐实**，旋钮落地前不要把这几条当"已复现"。

## 待裁决（11 条：十条已裁决或关闭，仅 #11 备份口令轮换等你手动执行）

| # | 事项 | 现状 | 建议 |
|---|------|------|------|
| 1 | ~~`db --backup` 是否幂等~~ | **已裁决（读码 2026-09-25）**：`_db_backup` 写 `args.backup or (db_file+".backup")`，目标已存在时**直接覆盖**，仅在返回字段里带 `backup_exists` ⇒ S 黑箱判「静默覆盖」成立，R10e 的「幂等」不成立 | 并入 MF-60 族：已存在时要求 `--force` 或自动时间戳 |
| 2 | ~~`component_layer.html` 等三个死档是否升为「门禁静默失效」（P1）~~ | **已裁决（2026-09-25 按推荐采纳）**：定 **P2**，归 MF-61「要么接线要么删」批，须挂时点防无限期搁置 | 定 P2：拦的不是真代码，避免 P1 稀释 |
| 3 | ~~假担保清单计数口径~~ | **已裁决（2026-09-25）**：采纳终稿口径 **116 禁动（§2.C 86 项）/ 30 可改 / 文档面 7 = 123** | 已采纳 |
| 4 | ~~`YIBAN_WORKERS` 校验与 K≡1 是否同一处~~ | **已裁决（读码 2026-09-25）**：不是同一处——`egress.py:369-374` 的 1~64 钳制管**旧口径 worker 数**；K≡1 是 `schedule.executor_count` 被**配置的出口数**夹死；两套会叠加（出口 1 ⇒ K=1，与 WORKERS 无关） | 并入 MF-56 一并修 |
| 5 | ~~注释流 4 条洗白（MF-62..65）~~ | **已确认销账（2026-09-25）**：按处置节执行结果关闭——不 revert、「从注释反查回代码」已立 MF-67、MF-64 已改准 | 已销账 |
| 6 | 许可与公开仓：`waf.py` 血缘三口径；GitHub 公开仓真名样式示例；桌面明文备份口令 | **部分裁决（2026-09-25 按推荐采纳）**：①血缘以 PROVENANCE 为唯一权威，README 与 `docs/dev/README` 改一句话指向；②公开仓真名示例换合成名（随下次发布生效；真名已进公开历史，不 rewrite，仅姓名样式、接受并记录）；③明文口令走 #11 | ①②立 M3 小批；③尽快手动轮换 |
| 7 | ~~旧服务器跨机零互斥面~~ | **已关闭（2026-09-25 用户确认）**：旧服务器已释放、无再开启可能 ⇒ 跨机零互斥面消失，只读核查取消（清单作废） | 无需进一步动作 |
| 8 | ~~手动签到这条腿到底该不该受急停/周末管？~~ | **已裁决（2026-09-25 用户拍板）**：**手动腿保持豁免，现状即意图**——UI 契约、`test_global_pause.py:59`、`runner.py:232-234` 注释全部维持；设计文档 L3-4 本就记载豁免、无需改。厘清：L4-2 的冲突实为**派发拓扑**（`--only` 应单进程）而非门豁免 ⇒ 归 MF-74 修法（收敛扇出 + 三条确定性危害，M3 同批改实现与测试）；MF-101 只剩 spawn 前过滤 `user_paused` 一半照修 | 门半条关闭；派发半条按 L4-2 修 |
| 9 | ~~`waf.py` 分类的 `>2000` 短路是规格还是缺陷~~ | **已裁决（2026-09-25 采纳推荐）**：判据本身按**缺陷**处理——失效方向是 fail-open（真实拦截页被放行、按「网络抖动」打满重试），收益可忽略 ⇒ 修 MF-71 时连 `test_login_protocol_shape.py:617-622` 同批改钉新行为（显式挑战判定 + 不可重试档 + 活体反例），不是「只补分类表」 | 修法照 MF-71 验收不变量走 |
| 10 | ~~semgrep v2 规则集与 gate manifest 方案要不要入库~~ | **已裁决（2026-09-25 采纳推荐）**：只入库「能当硬门」的 6 条规则 + `test_route_gate_manifest.py`（含数量地板，防元测试静默全绿）；其余 13 条留仓外作评审队列 ⇒ **落地归 M3 修复流首批** | 6 硬门 + 数量地板；ratchet 类不进门禁 |
| 11 | 桌面那份文档里的**明文备份口令** | 操作清单已给出（`DISPATCH-REMAINING.md` 末节 7 步，含"旧口令在保留期内不能丢"） | 涉及生产写操作，本流不代做，等你手动执行 |

## 覆盖面声明（别把这份当"查过了"）
L1 84 份报告覆盖划分表全部 86 个单元 ID（`R12ef`/`R13ij` 各合写两单元）；`web/static/vendor/**` 31,331 行第三方与 `web/static/css/**` 4,508 行样式**未审**，`tests/**` 属注释流范围；V1–V8 只覆盖被点名的 8 条高危；L2 覆盖任务书点名的 5 条链；**L3 只有 4/20 格有真 HTTP 全链路证据**（余下卡在假上游无故障注入能力，见派发 1）；S 只覆盖清单内测试项目。
**收尾轮（2026-09-25 下午）新增**：semgrep 11 规则 265 命中已逐条四类归类（`out/SEMGREP-TRIAGE.md`，闭合 265 = 真缺陷 20 / 已知面 63 / 误报 114 / 噪音 68），候选 61 条去重成 50 簇（`out/MF-CANDIDATES.md`），并派 **16 个独立裁决代理**逐条复现 ⇒ 新立 MF-68..103（36 条）。其中 **32 条经独立裁决**（裁决累计驳回 14 条主张，清单见该节末"本流否证追加"）、**4 条（MF-97/98/99/100）是单源取证未二次裁决**，另有 10 余簇未编号待复现（见上"未编号候选"）。
**仍未做**：跨用户并发压测、真实浏览器端到端、v3 开态实测、旧服务器、以及任何依赖读 `.env` 内容/`/etc/nginx` 全文的判定；`/etc/yiban/*` 内容与备份口令文件**一律未读**。
