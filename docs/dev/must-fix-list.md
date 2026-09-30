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

**处置（2026-09-27 批2 Task2-1，repair/m3-batch2）**：四步按序闭合——
①兜底：`masking.MaskingFormatter`（M2 期 `2437bfb` 落 CLI/Web 两入口；本任务 `97fa697` 补
**容器调度器入口**——此前完全缺席：无 logging 初始化、8 处裸 print）；②点修：
`session_cache` 作废/清理分支（`2437bfb`）+ 解密失败分支（`92a5d67`，与 MF-7 并闭）；
③展示/导出同一实现（`2437bfb` 委托 `mask_phones_in_text`）；④不变量测试：formatter 级
（`2437bfb`）+ **入口级挂载钉 5 例**（`d0ca45c`，三入口各钉，含 RED 实证）。
**口径订正（重要）**：本条原文"没有兜底脱敏机制"系**生产取证快照**（v0.4.7@6d4eafa，
09-23）表述，对 HEAD 已失真；"全仓 addFilter/logging.Filter 零命中"字面至 HEAD 仍真
（修法走 Formatter 而非 Filter 路线）。登记表文首入库（`8a78ee6`）晚于 M2 修复提交
（`2437bfb`）约 14h——"修在登记之后"只对生产取证事件时点成立。
生效待下次部署；联动批 3 MF-100：部署验收前置项="三入口挂载已被测试钉住"+ sched.log
抽查遮罩行。运维知会：容器 sched.log 留痕行形态变为 `[时间] [级别] scheduler: …`
（原裸 `[scheduler]` 前缀），runbook 人肉 grep 习惯需一句提示。

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
> **收尾轮已兑现（2026-09-25 下午）**：派发 2/3 的产出登记为 **MF-68..103**（36 条，见"补充检出"节）。其简报写"下一空号 MF-67"时先于注释流 MF-67 入库，且 8 个分片代理各自自报编号造成撞车；合并入库时整体 **+1** 重排，**最终号-内容映射以该节为准**。**收尾轮结束时空号推进至 MF-104**（MF-67 已被注释流占用，勿双写）。

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

**处置（2026-09-27 批2 Task2-2，repair/m3-batch2，`97edc12`）**：验收不变量落——通用凭据键规则与
authorization 专项同改挂新值域判据（登记"同族规则没一起修"的根因即此，同族同罪一起修）：
裸值首段照收，此后每跨一个空格/逗号/分号序列看下一段在下个分隔符前**是否含 `=`**——含 `=` 判为
新 `key=value` 对、值到此结束（cookie 逐对口径 `a=1; b=2` 保留，`path=/` 等非凭据属性原地留下，
登记"不能简单放宽"约束满足）；不含则视为值的一部分继续吞（**宁过遮不漏**：值后无 `=` 的散文被
一并遮掉是刻意代价）。引号形态跨空格整体消费不变。登记实测五例 + `password=abc,def` 全部转等值
断言（无明文尾），authorization "Bearer 吃方案名"归一语义不变。对照钉三类（正文含逗号非键值形态、
后续非凭据 `key=` 对不连坐、cookie 逐对）钉"不误伤正文"。**残余**：值以"空格+含 `=` 段"续接
（`token=a b==`）时尾段与真键值对文本上不可区分、按逐对口径让位——与 MF-111 的 Basic base64 尾巴
同根，批 2 内已随 `489af60` 并一条口径收口（键形态轴 + 含等号段判定，见 MF-111 注记）；键名百分号
编码绕过面维持既有判据不变。

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

**处置（2026-09-27 批2 Task2-1，repair/m3-batch2）**：✅ 销项——解密失败分支调用点
遮号 `92a5d67`（`session_cache.py:181-183`），作废/清理分支 `2437bfb`；调用点传参
钉测试 `test_decrypt_failure_log_masks_phone_at_call_site`。覆盖面订正：装配点由原文
"两处"更新为**三处**——CLI `cli_support.py:242`、Web `web/app.py:1682`、容器
`docker/scheduler.py:115`（后者 97fa697 新增）；全仓生产代码仅此三处装配（grep 亲核），
不经 formatter 的 sink（裸 StreamHandler/caplog/测试自建）仍是残余面——常驻入口已由
挂载钉全覆，残余仅测试/临时脚本自建 handler 通道（见 MF-8 订正）。

## MF-8 手机号兜底脱敏覆盖面比宣称窄 ✅
`masking.mask_phones_in_text` 只认**连续 11 位数字**：`+8613800138000`、`138-0013-8000`、
`138 0013 8000` 实测原样穿过；且保护与否取决于"该 handler 有没有挂 formatter"。
`test_log_masking_formatter.py` 原头部曾把它写成"任何一条日志记录经格式化后都不再含裸号"——
注释面已由 A3-r2/B2 改成如实表述，**代码面未动**。复跑：
`python -c "import sys;sys.path.insert(0,'.');from yiban import masking as m;
print(m.mask_phones_in_text('+8613800138000'), m.mask_phones_in_text('138-0013-8000'))"`。M3，与 MF-1/MF-4 同族。

**处置（2026-09-27 批2 Task2-1，repair/m3-batch2）**：**保持开放**（设计取舍，非缺陷
回归）——连续 11 位以外的分段/编码形态（`+86…`/`138-0013-8000`/`138 0013 8000`）实测
仍原样穿过，扩大口径与"不误伤坐标/时间戳类数字串"判据相抵（`116.397428,39.90923`
需原样）。覆盖面表述按现状收窄：常驻三入口（CLI/Web/容器）已由入口级挂载钉全覆
（`d0ca45c`，含 RED 实证）；残余面=绕开 formatter 自建 handler 的测试/临时脚本通道。
若将来要求覆盖分段形态，需另立裁决并给出不误伤判据。

## MF-9 `sanitize_url` 完全不解析 fragment ✅
只取 `parts.query`，故 `https://x/cb#access_token=ABCDEF` 原样返回。OAuth 隐式流把令牌放 fragment；
本项目链路上是否真会出现**未验证**（不夸大为"已确认泄露"）。〔A3〕M3 先确认是否可达，再决定是否补。

**处置（2026-09-27 批2 Task2-2，repair/m3-batch2，`3eb5639`）**：**判可达后补**（登记"先确认是否
可达"已做，证据表在 2-2 报告 §2）——requests 按 RFC 7231 主动把 Location 的 fragment 传播进最终
`resp.url`，登录链唯一 `allow_redirects=True` 的落点即经 `security.py` 诊断出口；requests 异常整句
（`attempts.py`/`probe.py` 内嵌 URL）同源；仓内 URL 字面量不产 fragment 但上游是服务端可控的
Location 链、不可证伪，真站是否回 `#access_token=` 仍不夸大为"已确认泄露"——兜底层不可赌外墙。
落点：`sanitize_url` 对 fragment 与 query 用**同一张判定表**（参数名片段表 / ≥24 位高熵 / 手机号
形态），不透明 fragment（`#section-2`、`#/route`、空 `#`）原样保留（`keep_blank_values=False` 防把
无值段收成键改写成 `#section-2=`）；`security.py` 的 `location_desc` 本就只留 scheme/host/port/path、
此路无附加面。反例 4 例入 `test_masking_ssrf_gaps.py::UrlFragmentTest`。无残余（三调用点组装顺序
与 rc/egress 未动）。

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

**处置（2026-09-26 批1 Task10，repair/m3-batch1，五段拼装）**：
② rc 契约——监督归集负数/信号/未知码为真失败（优先序 4>10>1>3>2>0 保持），run.sh SUCCESS 只认
真 rc；`SECOND_DONE_MARKER` 单点写（监督写）且封存前置=库内事实（`has_undone_accounts_today`
= 领取池 ∪ 状态文件并集，经 `--second-run-check` 单一判定权威；不可得 ⇒ 不封存+双声音+rc 升 1）；
`YIBAN_FALLBACK_ENABLE=1` 无进程 ⇒ 启动即双声音告警并拉起。
① 事件驱动——领取池行迁移即事件源（`fallback_event` 签名四元组：v2 计数+心跳、v3 计数+epoch，
双池并集、owner 排除、wake-set ⊆ takeable-set）；让位收窄到同一账号（同事务仲裁拒领），纯状态
文件部署保留整段停摆降级形态；5s 轮询预算精确。
⑤ v3 回炉——`requeue_failed` 沿用 v2 `retry:`/`final:` 档位协议（零字面量复制），走
`requeue_task` state+epoch 门；round-start 回炉 + `requeue_final` 显式档（补签轮透传）+
会话内 `requeue_during_run` + `claim_all` 全分片扫尾。B 类——`--fallback` 走 v2/v3 分流。
e2e 主证据：真 SIGKILL 链路（真 run.sh→真监督→真 OS 子进程），三判据绿且判别力已验（回退旧
口径⇒用例红）。
③ **撤除未排期未执行**——6 条前置 checklist 见
`docs/dev/fallback-removal-preconditions-20260926.md`（4 闭 2 开：单进程吞吐判据=测量任务、
四条契约迁移需用户点头）。
残余：现网 cron.d 无 fallback 条目（部署面事实，T2 记录）；现网 fallback 开关状态仍未取证。

### MF-44 告警链：四种"发了但没到"和"该发但静默"叠成完全无声
合并 L27、L28、R4c、R10a、R10b。`send_admin_alert` 返回 False 被 `notify_mail.py:184` 丢弃 ⇒ `degraded` 恒假、周报走**同一条 SMTP**、`channel_health.py:409` 在被吞失败后**照写去重标记**（docstring 与实现相反）；记账三面不一致：额度有 `BudgetTicket` 退还，**去重位在扣额度之前写盘且永不回滚**，`notified` 取走即置位无退还；Server酱 返回合法 JSON 但非对象 ⇒ `AttributeError` 逃逸、`_refund_daily_budget` 被跳过（**额度 5→4 且永不恢复**）；custom 出口按 `status_code<400` 判成功 ⇒ 3xx 与"200+错误 body"都算已送达并真实扣额；`URGENT_ONLY=false` **静默保持开启**、`DAILY_MAX=-1` 钳成不限额、`is_configured` 与 `send` 在 TYPE/白名单两处口径分叉。邮件正文侧：对 `yiban.masking` 零依赖、`sanitize_text` 不遮裸号、告警正文含被爆破账号明文邮箱、`_fold` 漏 9 个换行族字符可伪造正文行、`user` 字段零校验可致 SMTP 命令注入。
验收不变量："送达"与"已发送"必须是两个状态且都入库；任何额度占用必须有对应送达回执或退还；一条"SMTP 拒收 → 仍有声音"的端到端用例。
`[需确认]`

**处置（2026-09-27 批2 Task2-10，repair/m3-batch2，`767ebea`/`a30e72d`/`7a8142d`/`99ed943`，收尾
`500d75d`/`3ad8e31`；同族三条 UI 面已在 2-8/`807f9de` 落）**：三条验收不变量逐条——
①"送达/已发送"两状态且都入库：`channel_health` 去重标记只在 `send_notification` **返回真**时落
（旧实现只覆盖异常路径、被吞失败照写 ⇒ 一次瞬断放大成整天静默，docstring 与实现的矛盾改齐）；
SMTP 逐收件人结果（`SMTPRecipientsRefused.recipients` 与部分拒收时 `sendmail` 返回 dict，两个
零消费者）接线进**既有审计链** `mail_refused`（含 code/条目/幂等键/服务端回显文本，地址打码）
——补的是"可机读每收件人投递结果"，不新造第二套状态存储（登记措辞订正照原文：不是"零记录"，
逐地址日志本来有）。②额度占用必有回执或退还：Server酱"合法 JSON 非对象"不再 `AttributeError`
逃逸（旧行为跳过 `_refund_daily_budget` ⇒ 额度 5→4 永不恢复，反例前后对照在 2-10 报告）；custom
成功判据收成 2xx + 顶层 `code`/`errcode` 非 0 的"200+错误 body"判未送达；`send()` 出口分发收进
一处 try/except、异常同样走退还；耗尽告知新增 `restore_exhaustion_notice`（日报未送达把 pop
取走的告知放回、只退还仍处耗尽态的账，账本不新增键）。③"SMTP 拒收→仍有声音"端到端钉入告警族
行为钉（`500d75d`）。配置口径三处：`URGENT_ONLY` 新增 `_env_flag`（send 与 get_config 同一份
判据，旧 ValueError 回退缺省 1=想关却静默保持开启）；`DAILY_MAX` 负值判非法回退缺省并出声一次
（旧 `max(0,·)` 钳 0=最危险档"不限额"）；`is_configured` 与 `send` 的 TYPE/白名单分叉收进唯一
判据 `channel_usable()`。执行体清单变更告警采方案 (a) 具名账本 `admin_change`（独立日额 3、
代码内缺省零新 env 键；`urgent=True` 保留是"仅重要告警门"不同轴，非冗余）。正文面（接 MF-49
单一原语纪律）：`sanitize_text` 末段接 `mask_phones_in_text`（裸号在唯一原语收口而非调用点）、
爆破告警"尝试用户名"改 `_mask_email`；`_fold`/`_nl_safe` 统一为 `escape_line_breaks` 单原语
（字符集取 `ENV_LINE_BREAK_CHARS`，与 `.env` 写侧同一行模型）；`user` 发送前一处单行化（SMTP
命令注入，按章程不建校验框架）。新增用例 10 条（≤15 上限，每族至多一钉）。
**残余/移出**：`_classify_send_error` 证书失败与断网同文案（登记判"低、不占号"，未动）；逐地址
串行/每地址重建连接 ⇒ 归 MF-57；收件人数与 SMTP 条数仍是假定值（现网未取证）；`mail→store`
分层边因投递账首次出现（批 6 理分层时看）；**部分拒收**（多收件人未来态）直接判未送达不换条目
——单收件人现网不可达，多收件人启用时重新审视；`notify/ledger.py` 体积入 OVERSIZED 登记
（下一步拆 notice 子模块）。

### MF-45 急停在面板上不可见（与 MF-46 成环）
合并 L30、L58、R14h。`sign-calendar-view.js:30-44` 漏 `global_paused`/`no_position` ⇒ 急停渲染成"排队待签"；日历**图例只覆盖 12 种状态里的 2 种**（双单元撞实）；三个窗口告警标记**全仓无生产复位点** ⇒ 常驻进程第 2 天起彻底无声；web 侧急停/周末门经 `day_off(env=None)` 落回 `os.environ` ⇒ **在 web 侧恒不生效**（引擎读 `.env` 真值，所以实际会暂停、界面却说没有）。
验收不变量：状态枚举与图例同源（一份表两侧消费）；任一告警标记有显式复位点并有用例。`[需确认]`

**处置（2026-09-26 批1 Task9，repair/m3-batch1）**：状态显示层收口到 `yiban/status.py` 的
`DISPLAY`（12 个状态码全覆盖，符号沿用 `SYMBOL`/`ICON`）——日历页由 `legend_items()` 服务端渲染
图例、由 `display_payload()` 下发同一份表供账号卡状态行与日期格消费，新增状态码自动同时进两侧
（用例直接改表断言）；`global_paused`/`no_position` 不再渲染成"排队待签"，日期格对非成功/失败态
补状态符号角标（原先一律空白格）。三个窗口告警标记（配置非法 / 缓冲收缩 / 窗口回退）新增唯一
生产复位点 `reset_daily_alerts`（业务日翻页复位，挂在每日必过的 `_schedule_config` 上），
"复位后可再次触发"有跨业务日用例。web 侧门判定改读 `.env` 真值（`_day_off_reason(file_env)` 由
调用方现读注入，与引擎同一份 `schedule.day_off`）⇒ 急停/周末在日历上与引擎实际行为同口径；
**门语义一行未改**（手动腿豁免仍由 `tests/test_global_pause.py` 锁定）。
残余（不属本条范围）：`components/my-accounts.js` 的账号卡今日状态仍是第二份"前方排队 N 人"
文案（不含门判定）、`pages/data_dashboard.js` 另有一份状态标签/配色表（MF-54/MF-55 同族）。

### MF-46 `.env` 的两套行模型 + 不校验本次值 + UI 静默换值 ⇒ 一次无关保存能改急停
合并 L01、L39、L60、L66。**V2 已活体复现**：web 侧 `write_env_batch:243`/`ensure_secret_key:280` 用宽 `splitlines()`（实测 10 个分隔符），引擎 `write_env_keys` 只认 `\n\r`，差集 8 个即潜伏面；注释里潜伏 U+0085 ⇒ 一次"只改签到模式"的保存把 `YIBAN_GLOBAL_PAUSE=1`、甚至 `ADMIN_PASSWORD_HASH=pwned` **实体化成真配置行**，不过 A 档口令门、不进审计（还反写"全局暂停=不变"）、写后 `find_env_key_collisions` 归零＝痕迹被抹。反向：`write_env_keys` **对传入 value/key 一字不校验**（可把 NEL 写进盘）；`export KEY=` 在 shell/引擎/web 三处解析不一致且只有一处出声；`paint()` 把未知枚举静默换成**首项**并污染 `snap` ⇒ 后续无关保存会把它写进 `.env`；前端对行分隔符**零校验**。
修法方向：一处行模型、一处校验器（含全部换行族），写入前后各做一次"键集合 diff"并强制入审计。
验收不变量：任意"本次未请求的键发生变化"必须让写入失败；一个含 8 个宽分隔符字符的入参在两侧都被同一句拒绝。`[需确认：现网 .env 内容不可读]`

**处置（2026-09-26 批1 Task8，repair/m3-batch1）**：单一行模型收口 `yiban/infra/env_io.py`
（`split_env_lines` 唯一切行 + `validate_env_*` 单一校验器，分隔符清单锚定 `str.splitlines` 全 Unicode
实测 10 个）；写前/写后**解析键集合 diff**，"未请求的键变化"⇒ 拒绝 + 字节级回滚（`_restore_env_bytes`，
BOM/CRLF 保真）+ 审计 `env_write_refused`；web 全部写点单口 `write_env_batch`（12 端点共享
`errorhandler(EnvWriteRefused)`→409+清理指引），引擎 3 处经 `write_env_keys`，seed 第三写入方并入；
两侧拒绝消息逐字相等；`paint()` 不再静默换枚举/污染 snap；前端同源分隔符常量拦截。
V2 手法（U+0085 潜伏注释）路由级真跑拒绝、.env 逐字节不变。**读取容忍/写入拒绝边界**：历史脏文件
可读（启动只读告警），保存 fail-closed 需人工清理。
残余（批尾 5b）：`export KEY=` 三处解析分叉（MF-46③）、notify 密钥自动生成路径 `except ValueError`
吞 `EnvWriteRefused` 致 500（次级键自生成路径）。

---

## C 簇 · 互斥与重复真实登录（P0，"认领两次"族的总账）

### MF-47 `(phone,day)` 互斥有六条绕过路径，且 `touch` 从未被调用
合并 V6、L03、L04、L41、R3g、R5d。① `reclaim=bool(args.only)` 使 done 行**不校验 owner/租约**即改回 claimed；② `owner = excluded.owner` 命中即**短路整个租约判据**，缺省身份 `single@{host}` 不含 PID ⇒ cron 与 web 手动零互斥；③ 领取与登录跨事务、收尾在整轮末尾 ⇒ 崩溃后 900s 被接管再登一次，而 **`claims.touch` 生产端无调用者** ⇒ 心跳永不续、任何 >900s 的账号对所有人可接管；④ v3 `requeue_task` 的 WHERE 不含 state、epoch 自愿 ⇒ 迟到重排把 done 复活成 pending；⑤ flock 分名（`.w{i}`/`.fallback`）且超时"无锁继续"，运行锁三条 fail-open（超时放行 / `OSError→None` 零日志 / 无 fcntl→未加锁句柄）**四个调用点全不检查返回值**；⑥ `claims.purge` 未接时钟跳变守卫（同库另 5 处都接了）⇒ 前跳 >14 天整删当日互斥面。另有 `_claim` 未初始化即放行（链3）、容器 scheduler 与宿主 cron 互斥面为 0（现网未部署容器形态）。**V5 已用临时库实验证明 v2/v3 同日双放行、V6 用双进程证明同名 owner 可双领取。**
验收不变量：领取必须"同事务内校验+写入"，owner 必须含 PID/代次；心跳由执行侧周期性写入并有断言（"任何 >900s 仍 claimed"必须被 reaper 处理，而不是被接管）；purge 走跳变守卫。`[需确认]`

**处置（2026-09-26 批1 Task3，repair/m3-batch1）**：领取互斥收口——`try_claim` 单语句
校验（upsert+WHERE 同语句判定，租约/epoch 围栏不可绕）、`runtime_owner` 落 PID/代次
（执行体身份可追）、执行体内 per-account yield（同轮撞车降为账号级让位）、
`reap_unreported`/`reap_abandoned` 双路收尸。活体反例族：同日同账号双执行体抢单、心跳
过期接管、epoch 落后拒绝。3-A/3-B 两轮审查 + 终审接缝抽验（与 Task 10 唤醒前缀同源）。
残余（终审复核无害）：reap 无 day 参数/`lease_until != ''` 惰性条件——claim_batch 按
日过滤兜底，属冗余安全位。

### MF-48 三处零守卫清库 + `--dry-run` 假演练
合并 L38、L63、L68、L20、L55。`generate_demo_data.py` 守卫是 `db_path == "demo-log/demo.db" or yes` 的**字面串比较**，`--yes` 即清空五表且**删的正是 `audit_logs`、链校验反显 ok**、无倒计时零留痕；`seed_accounts.py` 更硬——**有清五表能力且零守卫**（连 `--yes` 都没有），还会改写生产 `.env`；`load_env_file:274` 读不到就静默继承宿主环境 + `clear_session_cache:284` 无门 DELETE ⇒ 误指生产库即 **89 号真实重登风暴**。包装层两个脚本的 `main(argv)` 是死参数 ⇒ **`--dry-run` 被静默丢弃并真删，rc=0 像演练成功**；`backup.sh` 旗标只认 `${1}` ⇒ `--require-encrypt` 移位即门禁静默失效；`state --yes` 无口令无审计、**按文件名删掉 `db --backup` 写进状态目录的副本**。
验收不变量：任何清库/删除类入口必须显式声明目标指纹（非路径字符串比较）+ 需要确认 + 入审计；被丢弃的参数必须报错而不是静默；"dry-run"三入口语义统一。`[已复现：demo/seed/包装层参数丢弃]`

**处置（2026-09-26 批1 Task5，repair/m3-batch1）**：公共防线 `yiban/store/purge_guard.py`（指纹
`PURGE-<sha256[:16]>` = 解析路径+字节数+六表行数，只读不建库；确认回显；审计 fail-closed，且校验
留痕落点与目标库一致）——demo/seed/state --yes 三入口全接线；dry-run 三入口统一（真不删 + 将删清单
含空 cred-state）；包装层死参数与 backup.sh `${1}` 旗标解析修掉（未知/多余参数报错非 0）；
`load_env_file` 静默继承删除、`clear_session_cache` 加指纹门（误指生产库 ⇒ 拒绝）；
备份副本按 SQLite 魔数内容保护（非文件名）。demo wipe 两段式（先清 audit_logs → 写存活留痕 → 再清
业务表）。**残余面（批1 Task5b 清扫）**：`reset_state_dir`、`capacity_probe.ensure_platform` 红线、
`child_env.py`/`web/services/env_io.py` 第三/四份 `.env` 读、`db_export` 族、`backup_sentinel.py`
死参数 `main(argv)`。cron 策略清理按裁决不设指纹门（定时保留清理属部署自身配置，门针对人工误指）。

---

## D 簇 · 隐私与凭据（P1，一条总闸 + 若干出口）

### MF-49 脱敏要分三类处置（不是"生产没实现"）
合并 L09、L10、L24、L25、L40、L45、L48、L51。基线**有**兜底 `MaskingFormatter`（`2437bfb` 引入，全仓仅 2 处 `setFormatter`：`cli_support:197`、`web/app.py:1641`）⇒ 缺口三类：**① 现网未部署**（三处装配计数 0，版本号同为 0.4.7 判不出，裸号行 09-22/23/24 = 258/314/368，`probe.py:100` 60/60 明文）；**② Formatter 结构上管不到**（`cli config/capacity/state/db/version` 无装配、`docker/scheduler` 的 `print`、`run.sh` 的 `2>&1`、`signin_api` 的 `stdout=log_fh`、`report_fatal_error`/`--json.errors`、`Account.__repr__`、**子进程 argv**、**进程 environ**、邮件正文、`sign_events.message`、坐标明文）；**③ 消费侧反向**（`q=` 先遮后滤 ⇒ 升级后日志页从"能搜到"变"搜不到"，`my.py:114-117/656` 按磁盘原文匹配；现网 `_mask_log_phones` 窄口径另有 124 个/日外发）。**出口字段**：`owner_display` 外发邮箱 @ 前完整本地部分（现网 9/96 即手机号）、`account-table.js:ownerText` 首选它、`account-form.js` 里 `email.split("@")[0]` 是同规则的**第二份定义**；`/api/users` 整表明文邮箱被 `apiCached` 写进 **sessionStorage**（含 `csrf_token`，logout 失败不清）；users 单条操作把明文邮箱编进 **URL path** ⇒ nginx `combined` 记 `$request`（同源 Referrer 再外送约 10 次）。审计侧 `actor` 列 61% 明文邮箱、`purge` 明文其余掩码（118→106 形态、12 碰撞）。
修法方向：**单一脱敏原语 + 全部输出面（log/邮件/JSON/argv/environ/URL/DOM/storage）消费同一份**，加可执行不变量；展示层禁止第二套口径；现网部署 + 版本号能区分有无兜底。
验收不变量：构造一条含裸号/明文邮箱的记录 ⇒ 断言每个出口都是遮罩形态（现在只测日志）。`[已复现]`

**处置（2026-09-27 批2 Task2-9a+2-9b，repair/m3-batch2；进程与协议面 `0c7c170`/`0a2a15b`/`05059e0`，
存储与展示面 `dbe37e5`/`39eec55`/`182e9f4`，正文单原语接线 2-10 `99ed943`）**：第②类"Formatter
结构上管不到"的**每个出口都有钉**（全部复用 `yiban.masking` 单一原语，未造第二套；基线 `607c7aa`
上先红 15/17、终态逐出口至少一条断言）——
**进程/协议面（2-9a）**：CLI 维护子命令（config/capacity/state/db/version）分派前接既有装配点
（堵 `logging.lastResort` 裸写 stderr 旁路）；stderr 人话出口 `_say` 过 `mask_phones_in_text`；
stdout JSON 出口 `_emit_json` 序列化前对字符串叶子过 `_masked_tree`（数字叶子原样、单行
`json.loads` 契约不破）；config 失败 stderr + `--json.errors`、成功明细双遮幂等；
`report_fatal_error` 在**生成点**收口一次（fan-out 的 stderr/`--json.errors`/`2>&1` 落盘三出口同源）；
signin 子进程 `stdout=log_fh` 重定向落点文件以同拓扑端到端真跑钉住；`run.sh` 全文不引用凭据键、
每条 `2>&1` 只承载 `"$PY"` 子进程（静态双钉，不为 shell 造第二套遮罩）；argv 全部拉起路径清点
——唯一入 argv 的敏感项是 `--only <号>`（标识符），口令/密钥/代理 userinfo/归属邮箱任何路径不进
argv（硬判据钉）；environ 钉"凭据仍在 environ、同 uid 可见"既有事实（防误改静默打断密钥注入契约）；
`Account.__repr__` 改 `dataclass(repr=False)` + 遮罩 repr（号/邮箱遮、凭据只留配没配）；邮件三形态
（`to_plain`/`to_html`/`to_markdown`）在 `Mail.__init__` 构造点共享一处遮罩；推送与邮件同构造同收口。
**存储/展示面（2-9b）**：`owner_display` 本地部遮罩（`masking.mask_email_local`，号形态→
`138****0000`、其余→前 3+`***`），前端 `email.split("@")[0]` 第二份定义删除、改消费服务端字段；
含敏感字段的响应（`/api/users` 整表含 csrf_token、`/api/me`）**禁入 sessionStorage**（缓存准入
只放无敏感字段响应/标量投影），logout **发请求前无条件清**（含失败路径）；users 单条操作明文邮箱
出 URL path ⇒ 改不透明 id（`/api/users/<int:id>/…`，nginx `$request`/Referrer 外送面闭合）；审计
`actor` 写入口 `actor_tag` 遮罩先于哈希（新行存遮罩形态），查询 `_actor_forms` **写遮读双形态**
匹配（升级边界的历史明文行不丢）；`admin_to` 类地址视图超 200 字节预算整体让位给计数 + `_cut`
显式标注（2-8 审查移交，防后缀退化截烂）。
**两条已登记残余**：① argv `--only <号>`（ps 全局可读）收口需改调用契约（stdin/0600 文件变体），
**归批 3 与 MF-100 联动**；② `q=` 先遮后滤与 `_mask_log_phones` 外发只清点登记、检索语义未动，
**归批 3**（第③类消费侧 + 覆盖性订正节两半各需的断言届时补）。第①类"现网未部署"随下次部署生效
（三入口装配已由 Task2-1 挂载钉住）。
**挂账说明（防后批再当新缺陷找）**：登记②类末两项 `sign_events.message` 与**坐标明文**由
`sanitize_text` 收口（2-10 起 `sanitize_text` 末段接 `mask_phones_in_text`，写路径共用该单原语）；
其中"坐标明文"属 **MF-8"保持开放"口径所豁免**——分段/编码形态（`116.397428,39.90923` 类数字串）
兜底不遮，为不误伤坐标/时间戳，扩大该形态需另立裁决与不误伤判据（见 MF-8 注记）。
**批 2 全支终审非阻塞残留（属去重/收口，均记入缩减批，不批内修）**：①`yiban/cli.py` 与
`yiban/mail/layout.py` 各有一份 `_masked_tree`（两份叶子口径相同但语义不同：cli 版会递归 dict 并
把 tuple 转 list；layout 版保留 tuple、不处理 dict——Mail 内容形状只有 str/tuple/list，当前无
实际缺口）；②存量第二口径（非本批引入）：`yiban/store/accounts.py` 的 `_mask_phone_display`、
`yiban/mail/config.py` 的 `_mask_addr`、前端 `core.js` 的 `maskEmail` ⇒ 缩减批收口清单。

### MF-50 凭据加密密钥已不可安全轮换
合并 L18。工具链被 S1 删除后，README 手工流程只重加密库内两列、**漏掉同钥加密的 `.env` 密文**（`YIBAN_MAIL_SMTPS_ENC`、SendKey）⇒ 换钥即告警通道静默死亡；且流程只让改 `.env`，而现网 web 的钥由 systemd `EnvironmentFile=/etc/yiban/accounts-key` 注入、**env 优先级高于 `.env`** ⇒ 照做必致"web 一把钥、engine 另一把钥"；无 `kid`，动手前后都无法自证。密码学本身经生产实测通过（GCM/AAD/tag、重复 nonce 0/96）；但**无 rehash 升级路径**（现网 1 行 `role=admin` 仍是 `scrypt:32768:8:1`，N 减半）、`_encrypt_field` 把空凭据静默写 `""`、一行坏密文拖停整轮。
验收不变量：密文带 `kid`；启动时断言"两侧读到同一把钥"，不一致即拒绝启动而非静默解密失败。`[已复现（拓扑侧）]`

**处置（2026-09-27 批2 Task2-7，repair/m3-batch2）**：验收不变量全落——①密文升 v2 携带
kid（HMAC-SHA256 单向指纹 16hex，两族入口同形状、同钥跨面同值），v1 无 kid 密文**永久可读**、
读出按需重写、不强制全量迁移；v2 错钥诊断带"密文 kid vs 当前钥 kid"双指纹（可自证）；
②两侧同钥断言 fail-closed：env 档与 `.env` 档并存且解出不同钥 ⇒ 拒启——断言本体在
`load_key` env 档分支（运行期纵深）+ `assert_key_sources_agree`（web create_app / 引擎
runner.main 启动接线，引擎分叉按既有"配置错误"rc=1，不新增退出码），启动日志打印 kid；
③README 轮换节改准：列全同钥密文三面（库内两列 / SMTPS_ENC / SECRET_ENC）+ 两侧来源同步
（EnvironmentFile 与 .env 一并换）+ 手工重加密命令序列 + kid/启动断言作自证 + 过渡态处置
（先统一两侧恢复启动、不得撤断言）。顺带：`_encrypt_field` 空凭据显式语义留 DEBUG 痕。
残余（登记原文、非本任务验收）：scrypt rehash 升级路径（现网 1 行 N 减半）与"一行坏密文
拖停整轮"未在本笔扩大范围。

### MF-51 SMTP 条目以数组位置为身份 ⇒ 删一行即凭据错配
合并 L59。`collectSmtps` 用位置当身份、后端 `notify.py:164,175` 按索引沿用旧 user/pass ⇒ 中间删一行就凭据错配；只改 host 时**旧授权码随新域名一起发出**，risk 档零口令零确认。同文件族：额度 UI 只显余额（看不出被"从未发出的信"吃掉，接 MF-44）、`configured = max(1,…)` 把 0 执行体包装成"并行 1"、空清单无提示。
验收不变量：配置条目必须有稳定 id，禁止以位置作身份；改中继/收件人属"改告警去向"，必须入审计并留可还原目标。`[已复现：configured 三条]`

**处置（2026-09-27 批2 Task2-8，repair/m3-batch2）**：两条不变量全落——①条目身份改走
稳定 id：前端建行自产 `data-smtp-id`（形状与后端 `_SMTP_ID_RE` 同口径，删除/重排只动 DOM
不动 id）、`collectSmtps` 随行携带；后端两轮认领（第 1 轮按 id；第 2 轮迁移口径按
(host,port) **唯一**匹配，歧义 fail-closed 不猜），且凭据只在**目标未变**时沿用——只改
host/端口 = 换中继，旧授权码绝不跟去新域名（后端硬判据，不依赖前端自觉；前端
`driftTargets` 同步把"留空沿用"占位当场改口）。id 非法形状/请求内重复 → 400 零落盘；
旧格式密文（无 id）照旧可读可发，GET 回 null 由前端现造、保存时认领并落定 id。
②改告警去向入审计：`mail_config` 审计 detail 新增 `smtps_from`/`smtps_to`（逐条
host:port 清单，旧值取落盘前）与 `admin_to_from`（既有 `_mask_addr` 脱敏，不新造第二套）；
超 200 字符预算时显式裁剪带 `_cut` 标注、保持 JSON 可解析（宁可裁剪也不截烂）。同族三条：
③推送额度改"已用 X/上限 Y（剩 R）"占用口径并写明"占用先于发送、未必等于已送达"；
④`workers.configured` 如实计数（0 行报 0），单执行体形态改由 `workers.single_mode`
显式表达；⑤空清单三处出声（SMTP 空清单状态行说破"一封都发不出去"、占位行、
执行体"并行 0"清单提示）。反例钉点：`SmtpEntryIdentityTest`（后端矩阵：删中间行/重排/
只改 host/迁移/歧义/预算裁剪）+ `tests/test_smtp_identity_frontend.py`（node 真跑：
id 随行不随位、漂移改口、额度三分支）。残余（非本任务验收）：告警链占用/退还机制
归 2-10/MF-44。

---

## E 簇 · 审计完整性（P1，红线）

### MF-52 审计锚点自检在自动判据层面等于没有
**V7 独立裁决 CONFIRMED**：`_anchor_file_state` 只写了"行数变少/相等"两支，**"变多"无人管** ⇒ **仅追加 1 条垃圾行**就让两道判据同时返回"无异常"；14 形态实测其余方向均显式 FAIL（安全），但**非法 UTF-8 抛 `UnicodeDecodeError` 被 `web/app.py:2463` 吞成一条 WARNING ⇒ 当日校验整体不执行**。无密钥即可伪造被采信的 v2 锚点行（`prev_line_hash` 是无密钥 sha256，其余字段是库内明文副本；改内容仍被 HMAC 兜住）。**兜住锚点的指纹存在库里**、而现网锚点与库同为 `yiban:yiban 600` ⇒ 有双写权限者可抹掉最近 N 条审计且 `audit_health.healthy=True`；垃圾行不可自愈。规模（V7 亲数）：28 站点 / 25 吞异常 / 17 与清白同形 / 16 零日志 / 12 同形且零日志。
修法方向：解析失败必须判"**无法定论**"而不是"无异常"；判据补 `!=`；校验器改取 `max_id` 最大真行；指纹跨权限存放。`[已复现（权限实测）]`

**处置（2026-09-26 批1 Task6，repair/m3-batch1）**：四路修法全落——三支判据补"变多"+内容比对；
解析失败/坏行/读不出 = 独立"无法定论"态（`audit_verify` exit 2 区别于篡改；web 吞异常改邮件+推送
可见告警）；校验器以库内 `max_id` 真行与行哈希为基准；指纹三方一致性（库内 + 锚点旁路 + 独立见证
`/var/lib/yiban-audit/`，root cron 单调写/应用只读，部署面在 manifest/install.sh 内）。
**攻防闭合（两轮实弹复审）**：①追加填充绕过见证 ⇒ 前缀不变性（见证行必须原位未变）；
②删尾 + 伪造清洗事件翻绿 ⇒ 被见证行**无条件**回库核对（见证行按构造 ≤1 个见证间隔新，合法保留期
清理碰不到；人工全清=root 动作需重置见证）；③见证降级（absent/unreadable/corrupt 且 installer
已建目录）⇒ healthy=False + 告警（控制面可用性本身入判定）。三条攻击通路已钉为活体反例回归。
遗留（非阻塞）：`audit_anchor_meta.lines`（DB 侧、应用可写）相邻 `int()` 未加固——同"控制面损坏⇒
崩溃"类，归 Task 7 开头收口。

**降级裁决（2026-09-28 缩减批 6b，用户级）**：独立见证整族（`record_audit_anchor_witness` /
见证指纹文件 / root 侧 cron 与部署件）与"三方指纹"一并裁掉，锚点判据收敛为**两方**
（库内指纹 + 锚点旁路文件自报：行数三支 + 行内 `prev_line_hash` 链 + 末行 `last_hash`）；
清洗事件豁免收窄为**锚点定点行不再享有清理留痕豁免**（`anchored is None` 无条件判 tampered）；
全表重链只裁纵深、**保留 `migrate_v3` 的 hash 回填本体**（残缺旧库升级仍须把链回填自洽）。
随之收窄的验收不变量：MF-52 的"跨权限独立见证对双写掩盖的检出"与 MF-53 的"整链重签由见证
head 抓"两条**已失效**——链条在本机自洽时，有 root（或应用写权限）者可抹痕而不被发现，
此为**已接受的威胁模型**。保留取证底线不变：只追加审计行 + HMAC 链 + 每日锚点哈希 +
断链/缺行告警（`chain_ok`/`anchor_ok`/三态锚点/`purge_total`/`last_cleanup`/
`write_failures` 与 `audit_verify.py` 判码 0/1/2）。已知降级：极长期空闲部署（链尾早于
保留期窗口）的锚点定点行被合法清理后会由"静默 OK"变告警；本工具每天签到写审计行，实际不触发。

### MF-53 审计"写了但追不到人、丢了你不知道"
合并 L05、L06、L22、L32、L64。链构造本身成立（`prev_hash` 与 INSERT 同事务、AUTOINCREMENT 保跨进程不分叉），但：**业务写与审计写永远两个事务** ⇒ 中间被杀则"做了无留痕、欠账仍为 0"（欠账检测结构性看不见这类丢法）；无会话/请求 id，来源列只有可伪造 IP 的哈希；`audit_head_hash` 读失败与空链同返回 `""`；欠账单调无归零口径 ⇒ urgent 邮件永久刷屏；`_rechain_audit_logs` 分批 commit 击穿原子承诺；`audit_verify.py` 无顶层异常兜底 ⇒ `database is locked` 以 exit 1 **冒充"检出篡改"**，而唯一下游"修复"是重跑 `migrate_v3`（其分批 commit 会把可恢复态变成永久断链），无锚点时把"没查"印成"通过"，删尾/整链重签两类都检不出；`state_cleanup.py` 的 `--dry-run` 见 MF-48。
验收不变量：审计与业务同事务（或写审计失败即回滚业务）；"未查"与"通过"必须是两个不同返回值；校验器对"锁住了"和"检出篡改"给不同退出码。`[需确认]`

**处置（2026-09-26 批1 Task7，repair/m3-batch1）**：同事务原语（`audit_spec`+`record_in_txn`，审计失败
即回滚业务）接线 **8 处生产调用点**——accounts 增删改恢复 4 处 + me 自改口令 / my 提交易班凭据 /
auth 建号 / users_api 角色与重置与两型删除与批量（kill 注入 e2e：业务与审计同在或同不在）；
1 处诚实例外 `user_deleted_purge`（purge 清单仅写后可知，保留写后审计——残余窗口可闭，归批尾清扫）。
`audit_verify` 判码分离：0 通过 / 1 篡改 / 2 无法定论（含库锁）/ 2 未查——`migrate_v3` 不再作为
"修复"出路；删尾由 max_id/行哈希抓（独立见证与"整链重签由见证 head 抓"已于缩减批 6b 裁掉，见 MF-52 降级裁决）。重链单事务化
（失败回滚保留原链）；`audit_head_hash` 读失败三态 ≠ 空链；请求作用域 id（web 请求级/CLI 进程级，
用户侧 `[req=` 冒充已消毒）；欠账告警按"账目变化"触发（止 urgent 刷屏）+ 告警基线按送达推进
（送达失败不推进，重试）。
残余（批尾清扫单 5b）：auto-register 建号无审计行（预存在）、`delete_accounts_by_owner` 零行删除
仍写审计（口径不一致）、两处注释措辞。

---

## F 簇 · 数字口径（P1/P2）

### MF-54 "同一事实 N 份定义"总账（建议立成一条独立任务，不逐点修）
实测计数：**周末门 4 份解析器两套值域**（`=true` 时引擎照签、日历标休并短路不查日志）；**窗口几何 5 份默认常量 + 4 份宽度算术 + 4 种分钟栅格**；`role→首页` **7 处**；**容量三数并立 585/4680/29952**；字段权限清单 6 处；模板内硬编判断 8 类；`.env` 行模型 2 份；`_mask_email` 真源 1 份但 `email.split("@")[0]` 第二份在 `account-form.js`。已知后果：MF-45 的日历误导、MF-46 的注入、L34 的迟启动强杀、L50 的看板 10.1×。
修法方向：**定一份权威 + 其余处只读它**（常量集中到后端单一模块，前端经接口取），并把"不得有第二份"做成 grep 级门禁。`[需确认]`

**处置（2026-09-28 批4 Task4c，repair/m3-batch4c，`c4a354f`）**：**单点销项，总账仍开放**——本批只消费总账里的一处：「周末门 4 份解析器两套值域」收口为唯一权威 `yiban.engine.schedule.weekend_flags`（`_env_flag`：1/true/on/yes 为真），`day_off`、面板状态行与 `/api/my-calendar` 载荷都只读它；`my.py` 仍回 `int(bool)` 保 0/1 形状契约（前端 `calendar.js` 用 `=== 1`）。其余计数项（窗口几何 5 份常量、`role→首页` 7 处、容量三数并立、字段权限清单 6 处、模板硬编 8 类、`.env` 行模型 2 份、`account-form.js` 的第二份 email 拆分）**按本 MF 自己的定性"立成一条独立任务、不逐点修"留待缩减批**，本批不碰。

### MF-55 统计口径与"只抬不降"的默认分支
合并 L50、链3、R14f。看板"签到账号总数"把按 `(day,status)` 去重的 `cnt` 跨天跨状态**直加**，现网实测 **953 vs 真值 94（10.1 倍）**，而后端注释恰警告过此式；**一份错法喂三个出口**（分布卡跨天、日历格跨状态、染色 fail 优先）；防探针混算的唯一防线是 URL 里 10 个字符（`test_dashboard_stats_caliber_js.py` 既没钉它也没钉 `rateOf` 分母），且该测试的 `statusKind` 是页面的**不等价重写**（`already`→skip）；**未知新状态码默认落 skip ⇒ 成功率只抬不降**；探针软失败落库记 `status=success`（生产 255 行全 success、0 failed）；6 个计数器把未验 ACTIVE 当正常账号。
验收不变量：状态枚举必须穷举处理、未知值显式报错；`cnt` 类跨维度聚合禁止（要么后端出终值、要么前端按维度分组）。`[已复现]`

**处置（2026-09-28 批4 Task4a+4b，repair/m3-batch4a/4b，`3267599`/`f0bde1f`/`64b69cb`/`04357b7`）**：**正文与①②③销项，第 6 点转 MF-59**——①自暂停账号的"已取消"合成只在"当日无结论"时生效（`is_concluded_status` 排除法；"先签成功、再自暂停"不再被涂成"已取消"，面板与 `sign_events` 台账不再互相打脸）；②批量操作的审计目标清单与 `ops` 同源（`done=len(ops)` 与清单此前是两套口径）；③看板"签到账号总数"改由后端出权威终值（`sign_event_accounts_summary`，`data.py` 新增 `accounts_stats`），前端三个出口只渲染——`cnt` 跨天跨状态直加（现网 953 vs 真值 94）的错法在前端消失；探针软失败如实记 `failed`（`64b69cb`）；并补三条聚焦钉：`stage=sign` 防线、`rateOf` 分母、`cnt` 禁直加。**第 6 点「6 个计数器把未验 ACTIVE 当正常账号」未在本批实现，正式转入 MF-59**（见该条注记）。

---

## G 簇 · 吞吐与容量（P1）

### MF-56 限速不成立 + 执行数被夹成 1 + 输入常数未测 + 122 悬崖
合并 L12、L34、L67、L3、L31、L33、L14、L23、L21。① 每引擎进程各 new 一份桶、`Λ` 不落库 ⇒ **真实全局速率 N×Λ**；桶键是 `executor_id` 而非物理出口 ⇒ 共用一个 Squid 的 K 进程各持一桶；② `executor_count` 被 `egress_count` 夹死 ⇒ **现网 K 恒 1**，与文档"5000 需 20–22 执行体"差 20 倍；③ 容量输入量 `1.87~3s` **不是实测**（mock 注入 6×300ms / 配置缺省）⇒ 所有继承它的数字继承的是假设；④ 悬崖：**121 账号临界，122 起当天必签不完，而引擎要到 361 才告警 ⇒ 122–360 是静默死带**；现网 89 个账号，余量 23 分钟；500 档缺 4 小时、万档需 106.7 小时；⑤ MF-3 单桶口径（`capacity_accounts` 无峰值无储备，末落点距 `eff_hi` 仅 1 秒，n=1560 约 43 账号在死带内；v3 不吃 `gap`，g=60 时 v2=75 / v3=3744）、MF-2 vshard 集中（N=200/V=64/K=64 最重 5.44×、42% 执行体 0 片、清单缺失静默回退 K=1⇒100%）；⑥ `capacity_probe` 与引擎**不是同一个式子**且判据不读 success/failed、造号写死窗口 ⇒ 白天跑全落 `skipped_window` 仍报"够用"。
修法方向：桶与出口同键且跨进程共享（落库或单点服务）；解开 `executor_count` 的假约束；把 t 换成实测分位数并入库；容量告警阈值降到"窗口 − 重试储备"实算值。
验收不变量：两进程同时领取时全局速率 ≤ Λ（当前必超）；`122 ≤ 账号数 < 361` 必须触发告警。`[已复现：K≡1、现网 89 号余量 23 分钟]`

**处置（2026-09-28 批4 Task4c，repair/m3-batch4c，`719553d`/`aef9d4e`/`ad07790`/`b59ce9d`）**：**②③④⑥销项；①⑤明示归 v3 灰度批**——④容量告警阈值降到「窗口 − 重试储备」实算值（`capacity_of(..., retry_reserve=True)`，**只喂告警**，计划/展示/保存闸门不传故取值不变）：v2 侧把每账号周期放大到 `MAX_ATTEMPTS×(avg+gap)`，缺省满窗口阈值由 361 降到实算 **120**，**122–360 静默死带封死**；独立夹具真跑 `runner.main` 复核：`n=89` 静默、`n=121`/`122`/`360` 均出告警，阈值随 `ws/gap/avg` 变动随动（77/56/62），非硬编码。③告警的 avg 输入换成实测分位数（`warn_avg_attempt_sec` 读既有 `sign_events.dur_sec` 的 p95，**无新表/新列**，样本不足即回退缺省档；只喂预检）。⑥`capacity_probe` 转调 `capacity_accounts`（与引擎同式）且判据读真实 `success`/`failed`/`skipped_window`（此前白天全落 `skipped_window` 仍报"够用"）。②`executor_count` 的出口夹取经**二选一取"改文档"**（`b59ce9d` 仅改 docstring + README：K≡1 是 v3 目标语义、唯一生产调用点在 v3 预检、现网开关缺省关）。①跨进程共享桶（`Λ` 落库或单点服务）与⑤vshard 集中**属架构改动，明示不在本批**，README 与 docstring 已声明，归 v3 灰度批。`[已复现：122 ≤ N < 361 必告警已验证]`

### MF-57 一封邮件能把整站冻 150 分钟
合并 L36、L29、L37、L31、L9 的持锁部分。465 路径单条约 15 个阻塞操作、15s 是**每操作**超时且不含 DNS ⇒ 单收件人上界 37.5 分钟、A 线 4 收件人 **150 分钟**，批量拒绝还要在锁内 ×K；改密/注销在进程级 `_file_lock` 内做 2 次 scrypt + 2 次 SMTP（违反 `locks.py:31/32` 自家"哈希留锁外"），现网 `gunicorn -w 1 --threads 8` ⇒ 锁一堵八条线程全等；`accounts_api` 在锁内做同步 SMTP；`_render`/MIME 构造在 `try` 外 ⇒ 孤立代理对引发**锁内 500 + 审计欠账**；无连接复用。锁层另有：`held` 标志泄漏后**该线程此后所有同名锁全跳过、零告警**（V 判高）、相对路径+不同 cwd 使同名锁 Δ0.01s 同时进入、超时后降级继续 rc=0、`lock_kind()` 自称"非 None 即互斥生效"是假担保。
修法方向：通知一律异步出锁（队列 + 后台发送进程），慢操作（scrypt/SMTP）永不在锁内。
验收不变量：`_file_lock` 持有时长上限断言（秒级）；一次 SMTP 挂起不影响任何 API 响应。`[需确认]`

**处置（2026-09-28 批4 Task4c，repair/m3-batch4c，`a3e6a70`/`0b14dd2`）**：**结构性泄漏封死 + 邮件不抛回持锁方 + AST 棘轮守卫；"通知异步出锁"架构改动留待后续**——①`held.discard` 移到最外层 `finally`、`_acquire` 包进 `try` ⇒ `BaseException`（含 Ctrl-C/超时强杀）泄漏 `held` 标志（泄漏后该线程此后所有同名锁全跳过、零告警）**结构性封死**（变异：base 跑新用例红）；`lock_kind` docstring 撤掉"非 None 即互斥生效"的假担保，消费者只把返回值当"平台有无后端"用（语义仍成立）。③`_render`/MIME 构造收进 `try` ⇒ 坏字符（孤立代理对）不再抛回持锁调用方（变异 3 红）。「锁内不做 SMTP/scrypt」以**静态 AST 判据**钉住增长面（`test_web_file_lock_slow_ops.py` + 清单防腐烂断言；**裁定保留**：现架构下 14 处锁内慢调用是已登记债，秒级计时断言在本机必随机打挂，且仓内已有同类源码守卫先例）。**②"通知一律异步出锁（队列 + 后台发送进程）"是架构改动，本批不做**——锁内慢调用现由 AST 守卫拦住增量，验收不变量"秒级持有时长上限"未落地，仍开放。

---

## H 簇 · 门禁与鉴权（P1，多为"声明与执行已漂"）

### MF-58 门不在装饰器层：漏挂即无门，且声明与实现相反
合并 V3、L26、L47、L54、L58、L53（部分已降档）。口令门在**服务层按手工枚举清单 opt-in**（`app.py:2380-2390`）⇒ 新写路由默认不过门；`/api/signin*` 三档全不要口令（V3 判"属 S1 拍板范围内，但三处矛盾"：红线"易班锁号冷却"只覆盖验证路径、文档称"只判主管理员"为假、同类不可逆有倒计时而它没有）；`risk` 档判据基准在**签名不加密**的 cookie（`login_ip`），现网 nginx 是覆盖式 ⇒ 远程伪造不成立，残留=本机直连 `127.0.0.1:17892`（含 SSRF）可任意伪造、且**代码对拓扑无自检**（Proto 段有）；`login_ip`/审计来源因此可被本机进程污染；改密不踢会话（`me.py:173` 保留 + `GET /login` 302 弹回）+ 改密不清会话缓存；倒计时 presence-only 且凭据沿多段链前递（一次手势放行整链不可逆）；模板/文案/前端三份权限声明互斥（"任意管理员可改"实为 `MASTER_ONLY_KEYS` 必 403，前端还渲染成可用）；`reject_default_admin_password` 把 `.env` 读失败当"无明文"静默放行（fail-open 零日志）；档位本身零留痕（放行不落审计，事后无法区分"验过口令"与"同出口免检"）。
修法方向：门禁声明从"清单"改成"路由注册时强制声明类别"（未声明即拒绝注册）；拓扑自检；改密吊销会话。`[需确认]`

**处置（2026-09-28 批4 Task4a，repair/m3-batch4a，`d44b320`/`3569c33`）**：**部分销项，强制声明结构仍开放**——注册口内置管理员邮箱探测面封死（独有文案"内置管理员邮箱不可注册"改与"该邮箱已注册"同文案 + 同补一次 dummy scrypt；注册面 = 公开面，独有文案又排在口令散列之前 = 零成本超管邮箱探针）；高危删除额度整体被关（`limit<=0` 或 `cooldown<=0`）时在既有 `db.audit` 补留痕（每 app 实例至多一条，防逐请求刷审计表）；敏感口令门"窗口内首次被挡"补审计（此前前 N−1 次错口令白撞无痕），达阈值那一行审计不再随 `cooldown` 走（`cooldown=0` 的部署也能事后取证）；**门禁覆盖面清单从手抄件改成与 `url_map`／视图源码双向自动比对**（`test_gate_manifest_sync.py`：幻影条目与暗门条目各红一侧，改名漏登即绿→红）。**修法方向里的根本结构（路由注册时强制声明类别、未声明即拒绝注册）本批不改**——现网仍是"服务层按枚举清单 opt-in"，新写路由默认不过门；拓扑自检、改密吊销会话亦未做。留待后续。

### MF-59 未验证凭据以 ACTIVE 落库并被引擎真外呼
合并 L08、L44。`VerifyGateBusy` 在 `accounts_api.py:316-321` 只 `logger.warning`（注释称"置 rejected"）；6 跳判定链已给全：异步 → 扣配额 → 无邮箱即 `status=ACTIVE` → 落库 → 队列满返回 None → raise；引擎 `engine/accounts.py:140-141` 只跳 pending/rejected ⇒ 从未验过的凭据进主链。同形第二处：`attempt/jobs.py` 席位耗尽 → 置 rejected 并拒账号、**不扣额度不审计**（现网 `verify_jobs` 0 行故多为潜伏）；`_reject_account` 在同一条不变量上一侧 fail-closed 一侧 fail-open。
验收不变量：状态只能由"验证结论"置为 ACTIVE；任何兜底分支不得把未知当通过。`[需确认]`

**处置（2026-09-28 批4 Task4a，repair/m3-batch4a，`6cbf892`）**：**①②销项，兜底链与计数器仍开放**——①整表替换／导入缺 `status` 的行默认由 `active` 改 `pending`（`yiban/store/accounts.py::replace_accounts`，与 `add_account` 及建表 DDL 同口径）："整体替换"曾是绕开审核直进主链的**第二入口**（引擎只跳 pending/rejected，`active` 会被真实签到外呼）；带显式 status 的"导出→导入"回环不受影响（显式状态原样保留）。②`VerifyGateBusy` 兜底（配额已扣而校验任务没建成）不再静默回 `ok`——如实回 `status=verify_deferred` + 后续动作提示（此前用户以为在验证、尝试额度却已消耗）。**原属 MF-55 第 6 点的「6 个计数器把未验 ACTIVE 当正常账号」仍开放**：本批未实现，作为本条的残留项登记——"状态只能由验证结论置为 ACTIVE"的不变量尚未在全链路落地（`VerifyGateBusy` 的 `status=ACTIVE` 兜底路径、`_reject_account` 一侧 fail-open 一侧 fail-closed、6 处计数器待后续批）。

---

## I 簇 · CLI / S 黑箱（P1/P2）

### MF-60 "自称只读"的命令造出看着健康的空库；agent 无法预校验任何东西
合并 L62、S。`config` 自述只读不联网，实测当场建出 69632B / 6 表 / `uv=0` 的伪库，随后 `db --integrity` 对它判 **"ok"**；`migrate=False` 仍建库/建表/切 WAL，且**漏一个入口**（`list_duplicate_owners.py:43`；本组另 5 个维护脚本**全部**会顺手写库——`init_db` 关不掉）；`.env` 与库路径全按 cwd 相对解析、唯一改道变量 `YIBAN_DB_FILE` 不在任何 help 里 ⇒ `.env` 写了账号仍报"未配置任何账号"却同时回显 `env_file:.env`；`sign --bogus --json` ⇒ rc=2 且 **stdout 零字节**（`--json` 契约破产）；六种非法参数输出**逐字节相同**、rc=1 同时承载配置错/参数错/真失败；零账号闸门挡在参数校验前 ⇒ **无法做任何预校验**；`probe` 的跳过/撞锁/真跑**全是 rc=0**（外部监控永远看不见它坏）；`print_config_summary` 用 `print` 破坏 `--json` 单行契约；CLI 面**没有 restore**；`db --backup` **静默覆盖上一份**（与 R10e 的"幂等"定性互相矛盾，见待裁决）；入口 `python -m yiban.cli` 要猜 4 次、WSL 无可用解释器（新人上手成本）。
修法方向：把"只读"承诺变成代码断言（只读连接 + 不建表）；退出码分族；`--json` 永远输出结构化错误（含 rc=2 路径）；补 `restore` 或在 help 明示不存在及替代路径。
验收不变量：任一"只读"子命令跑完后 `git status`/库文件 mtime 不变；非法参数必须有可区分退出码。`[已复现（黑箱自测）]`

**处置（2026-09-28 批5 Task5a+5b，repair/m3-batch5，`f15cefb`/`d7a2802`/`b113fb9`/`3606933`/`26da571`/`e5e1aaa`/`e4a3298`/`76eddec`）**：**四条修法方向全落**——
①「只读」变**代码断言**：新增 `store.connection.open_readonly`（静默库 `mode=ro&immutable=1`、有 WAL 待读帧走 `mode=ro`）与 `store.accounts.load_accounts_readonly`；`config`/`sign --check-config` 不再经 `init_db`——空 cwd 跑完**零新文件**、既有库 mtime/schema 逐字不变（基线**会**建 69632B 伪库，反例红）；`init_db` 增显式 `create=False`（默认行为不变），维护脚本（`audit_verify`/`ledger_check`/`list_duplicate_owners`——含那个**漏登入口**）改走只读连接。
②`--json` **任何**退出路径（含 rc=2 用法错误、未知子命令、多余参数）都打**一行**结构化错误含 `exit_code`/`error_kind`（基线六种非法参数 stdout **全 0 字节**）；`print_config_summary` 改走 stderr，不再破单行契约。
③退出码分族：既有 `0/1/2/3/4/10` 含义**逐字不改**（`cli.md` §3 原表未动一行），分族表达在新增稳定字段 `error_kind`（`cli.md` §3.1 扩表）。
④`probe` 判码分族：真跑通过=0、真跑失败=1、**跳过=2**、**撞锁=3**（此前三结局全 0）；消费方（`docker/scheduler.py`、cron 模板）经复核**不读** probe rc ⇒ 非回归。
另补两处：`db --restore`（默认 dry-run、`--yes` 须回显 `--fingerprint`、恢复前**校验备份可读且账号可解密**、覆盖前**自动留时间戳副本**、恢复后校验 integrity 与 schema 版本一致）；`db --backup` 目标已存在时**拒绝静默覆盖**（须 `--force`，采纳待裁决 #1）；`--help`/`cli.md` 列明四个路径环境变量并说明相对路径按 cwd 解析。
规模：`cli.py` 842 → 853 → 拆出 `yiban/engine/db_maintenance.py` 后 **696** 行（走体积门自带拆分路径，非注释凑数）。`[已复现]` → 四条全部销项。

### MF-61 前端"假成功 / 假可用"一族（P2，逐点改）
合并 L48、L49、L52、L40、L65、L46。`useServerMsg` 零调用点传 true ⇒ batch/purge 的后端 msg 永不上屏；`state.busy` 只暂停轮询、非防抖 ⇒ `signin/restore/move/purge` 可双击重发；`__clear__` 空操作却回 200 + toast"已保存"（**现网 21/96 个号可见**）；选中键用数组 `index` 而 `move` 改持久化顺序 ⇒ 单人即可误删他人账号；`configured=max(1,…)`；`apiCached` 把 `/api/users` 整表邮箱与 CSRF 写进 sessionStorage；`time-field.norm` 只查形状不查范围 ⇒ 显示 `25:00`/落盘 `23:00`/生效 `06:30~23:00` 三段不等；`windowSec()` 窗口倒置返回 0 ⇒ 缓冲上限放到最大（fail-open）；"3 个不剥注释的门"在给一个**永不发布**的 `component_layer.html` 把关（`_stub_macro.html`、`tailwind_config.html` 同为死档）。
验收不变量：写操作后必须回读校验；`index` 禁止作身份键；形状与范围校验同一处；死档要么接线要么删。

**处置（2026-09-28 批5 Task5c，repair/m3-batch5，`13138a0`/`d9ca392`/`14e13fa`/`bc53752`/`8c01242`/`c281f5e`）**：**9 点逐点核实——6 点仍开已修/删，3 点批 2 已修（仅取证、未动）**——
仍开已修：①`useServerMsg` 四个计数型调用点补 `true`（batch/purge 的"跳过 N 个"不再被吞）；②账号写操作加**在途防重入**守卫，且 `run` 的请求改**延迟构造 thunk**（原先进守卫前就已发出、守卫形同虚设）；④选中集键由数组 `index` 改**手机号**（`move` 重排后不再误指他号，`ids[i]↔phones[i]` 对齐）；⑦`time-field.norm` **同处**校验形状与范围（`25:00` 不再显示/落盘/生效三段不等）；⑧`edgeMaxMin` 窗口不可用时 **fail-closed 为 0**（原 fail-open 放最大），与 `window.edge_cap_sec` 逐值同式（原把 fail-open 钉成期望的用例改钉 fail-closed）。
已修未动（批 2 已修）：③`__clear__` 真清空（`fold_phone_code` 哨兵 → `""`）；⑤`configured = len(active)`（不再 `max(1,…)`）；⑥`apiCached` 只缓存标量投影，`/api/users` 整表邮箱与 CSRF 不落 sessionStorage。
死档（待裁决 #2）：`component_layer.html`/`tailwind_config.html`/`_stub_macro.html` 经**全仓引用面复核确为死档**，**连同只给它们把关的 7 个用例一并删除**；被削的门禁逐处定性为"合法删 / 必要改钉"（活代码断言未放松，变异注入仍报红）。`[需确认]` → 9 点全处置。

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

**处置（2026-09-26 批1 Task4，repair/m3-batch1）**：领取按弃权原因分档——`give_up` 写 `result` 前缀
（`retry:`/`final:`，`RETRYABLE_GIVE_UP_STATUSES` 单一来源，零库迁移）；默认参数对 `final:` 档拒领，
显式路径=补签轮/手动（`allow_failed=True`）；**常驻兜底不算显式路径**（无界循环），回落默认档只接
`retry:` 与未领账号。旧格式无前缀行 **fail-closed**（升级当日旧行默认不可再领，靠补签/手动显式接手）
——已知边界：既不跑补签也不跑手动的部署当日搁浅该批行。`attempts` 上限经裁决不采（会把防重复登录
变成漏签新源）。

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

**处置（2026-09-26 批1 Task4，repair/m3-batch1）**：`--only` 派发收敛为单进程内联执行（e2e：引擎
`Popen==0`、attempt==1、成功 rc=0；撞全局锁仍 rc=3）；`terminate()` 改杀进程组（`start_new_session`
+ 自组守卫，歧义即放弃 killpg 走单杀），孤儿化消失；危害②由扇出收敛 + 批1 Task3-B 锁 fail-closed
共同消失（未再改锁）；三处对齐完成（runner 派发注释重写为事实、旧正向断言测试改钉新行为、L4-2 加
对齐说明）。门豁免语义未动（待裁决 #8 设计保持）。

#### MF-75 容器调度器一次异常即让当天五项全废且不再重启（高，现网不触发）
`docker/scheduler.py:341-393 main_loop` 循环体无兜底，其中 `_run_signin_child:228` 的 `Popen` 没有 `except OSError`——对照同文件 `_start_fallback_child:299` 的调用点 `:319-323` 接住并 print，即同一文件两种判法。外层无人救：`supervisord.conf` 未写 `startsecs/startretries`（默认 1s/3 次），崩溃发生在重启后第一 tick（`hm>=FIRST` 立判、`_mark_slot` 排在子进程之后）⇒ 秒级三连进 **FATAL 不再重启**；`docker-compose.yml:56-62` 的 healthcheck 只 curl web 端口 ⇒ `restart: unless-stopped` 永不救。后果是首签/补签/探针/兜底/清理同进程全废全天。
**双跑改判**：`docker stop` **不产孤儿**（supervisord 是 PID 1，namespace 一起死）；真孤儿源是 `supervisorctl restart sched`（`stopasgroup/killasgroup` 未设）与崩溃重启，且容器子进程**其实拿引擎全局锁**（`runner.py:352`），只是全量模式 600s 后 fail-open（`cli_support.py:117-135`）＋同机 owner 同为 `single@{host}`（`round.py:169`）令 `claims.py:157` 恒放行 ⇒ 归 MF-47 补句，本条只登异常面。启用容器（`docker-compose up -d` 且不停宿主 cron）即变"已在现网"。〔C-08 · ADJ-15〕

**处置（2026-09-26 批1 Task12-A，repair/m3-batch1）**：四份分类口径归一为单一真值源
`yiban.security.HARD_FAIL_TOKENS`/`is_hard_fail_message`/`hard_fail_pattern`——挑战
解析 15 处 raise 文案与腿② `Expecting value:` 全部落**显式不可重试档**
（`HARD_FAIL_MAX_ATTEMPTS=1` + 清会话联动，不借风控词表）；probe 硬失败正则改同源
拼接（手抄词表消除）；`is_waf_blocked` 的 len>2000 短路按裁决 #9 移除（挑战形态不受
长度限制、长页纯关键词维持不拦防误伤）；同批改钉 `test_login_protocol_shape.py`。
审查 10 项必查全过（8 子串 2^8 组合穷举对照、15 文案逐条喂判据亲跑）。
挂账（批 2 登记）：探针的 `Expecting value` 词元会把瞬态网关故障（5xx 回 HTML）判硬
失败——建议出"按状态码/响应形状细分"的收紧裁决。

**处置（2026-09-26 批1 Task12-B，repair/m3-batch1）**：`login_killyiban` 成功门新增
**签发方回执判据**——`code==0` 且 `data` 键缺失/为 null ⇒ 拒绝（不落"登录成功"、不写
缓存、词元入 `HARD_FAIL_TOKENS` 落不可重试档）；未采用"data 非空"更严读法（mock 录制
真成功 = `{"code":0,"data":{},"msg":""}`，更严会判死真成功）。测试侧：会话缓存
四用例废除整体 mock 改真库行断言（消除"对假成功是瞎的"）；假成功注入 `login-shallow`
旋钮（默认关闭）+ run.sh 全链 e2e。审查 8 项必查全过。
销项清单：①**登机核对项**——记录现网最终认证真成功载荷形状，若出现 `data:null` 形状
同批改词元源（方向安全：误拒=一轮响亮自愈重登）；②`:382/:407` 形参 csrf 被缓存值覆盖
——本条"现状"另半句，行为语义另案（批 2+ 评估），本批测试已按该语义写断言不误吞。

**处置（2026-09-26 批1 Task13，repair/m3-batch1）**：调度器异常面——签到子进程 Popen
接住 OSError 留痕、main_loop 单 tick 兜底（信号直通）、心跳 + `--check-health` 探活
出口；容器面 supervisord `startsecs=5`/`startretries=10` + compose web/sched 双检。
配套 run.sh **LOCK_DIR fail-closed**（mkdir 失败 rc=1 拒跑、显式 `YIBAN_LOCK_DIR`
属主自担、删 /tmp 静默回退）。批 1 隔离演练六步实跑确认（判码抽验含 LOCK_DIR 活体）。
残余：docker 链生产演练待上线日执行。

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

**处置（2026-09-27 批2 Task2-3，repair/m3-batch2，`40a87c1`）**：两条不变量全落——①函数体第一句
`with env_lock.env_write_lock(env_file)` 包住"读原文→渲染→diff→commit→重读 diff→回滚"全程，
10 个写入点逐个过表：判定读必须与落盘同临界区的 4 处外层锁**保留**（account_crypto/audit_chain/
tracking/`ensure_secret_key` 与改密批写、执行体三处读-改-写等同线程嵌套直接放行——`locks.py` 的
per-path RLock + 线程本地 `_HELD` 语义先读清，无死锁；锁序 `_KEY_LOCK`/`_AUDIT_KEY_LOCK` 恒在
env 锁之前、无反向取用）；纯冗余外层去重 2 处（`write_env_batch`、`probe._env_update_probe`）；
登记不持锁点 `seed_accounts` 的建头挪入 `_ensure_env_headed` 且调用点显式入锁（开工时 `upsert_env`
已随批 1 并入 `write_env_keys`）。②grep 级判据：`tests/test_env_writers_take_lock.py`——行为格 4
（内取证明/真锁文件/同线程嵌套放行/**另一线程被挡到释放才落盘**=互斥真实生效）+ AST 扫
`web/ yiban/ scripts/ docker/` 的 env 形状目标写通道必须引用 `write_env_key*`/`env_write_lock`
（豁免仅 env_io 两个原子写助手，各只剩 write_env_keys 体内一处引用，豁免面失守即红）+ 形状格
（取锁语句先于读文件）+ seed 格 2。判据的诚实边界（写入守卫 docstring）：按目标表达式命名判 env，
改名规避可穿透文本判据，但穿透者须同时违反函数级判据才漏网；shell 侧 run.sh 只 source 读，
登记口径为 10 个 Python 写入点。**顺带消除**（登记"9 真持锁"点里的宽行模型残留）：
`probe._env_update_probe` 自写 `splitlines()` 读-改-写并入单一写入口，同时治好精确前缀滤行折不掉
`YIBAN_PROBE_ENABLE = 1` 带空格影子行（once 自动关闭可能被旧行顶掉）；tmp 改随机后缀（崩溃残留
不可预测名）。残余：拿不到锁 30s 告警后降级的既有可见性契约未动（登记判"今天不可达"仍成立）。

#### MF-84 `ensure_secret_key` 用宽松读判"全新部署"⇒ 读不到就生成新钥并折叠旧键（中）
同文件口径自相矛盾是最硬证据：`yiban/infra/env_io.py:31-32` 明写"读失败误判未配置会静默生成新钥覆盖旧钥，宁可启动失败"，该 strict 支在 `account_crypto.py:282-290`、`audit_chain.py:79` 被采纳，**唯独 `web/services/env_io.py:267` 用宽松读做同一形状的决策**。机理关键是 `:267`（宽松）与 `:279-280`（裸读）**两次不同源读取**。删除真发生：`:288-290` 无条件滤掉旧键行，`:291-293` 追加 `YIBAN_REGISTRATION_PAUSE=1` 却**不折叠旧的 `=0` 行** ⇒ 两行并存、后写覆盖先写、注册被静默关闭。权限正常时最可能的触发是"空/残缺文件窗口"（`upsert_env` 截断、`vim` 默认 unlink+新建 ⇒ 走 `FileNotFoundError` 支）；`UnicodeDecodeError` 不吞、会炸启动（排除项）。
现网取证一条（只读）：日志里在非首启机器上重复出现"已自动生成 YIBAN_SECRET_KEY"即命中。不并入 MF-46，但须引用其影子行机制，并与 MF-58（同支宽松读的读侧 fail-open）、MF-48 交叉引用。〔C-17 · ADJ-13〕

**处置（2026-09-27 批2 Task2-3，repair/m3-batch2，`7723f2d`）**：三处机理逐一收口——判"全新部署"
改 **strict 同源一次读**（AST 形状格钉：函数体内 `parse_env_file` 恰一处且 `strict=True`，
`read_env`/`os.path.exists` 不再出现；口径与 `account_crypto.py:282-294`、`audit_chain.py:79` 两支
已采纳 strict 支逐字同族）；**读败一次不写盘**（旧实现该格先误判全新、内部读同样 OSError 的"半途
写穿"消失，磁盘逐字节不变），降级为进程内随机钥 + WARNING 点名 `YIBAN_SECRET_KEY`——旧钥保住；
追加键走 `render_env_write`/`key_line_pattern` 单一实现**折叠同键旧行**（引用 MF-46 影子行机制，
`=1`/`=0` 并存支消失）。活体反例对：同一枚瞬态读窗垫片（`_OneShotReadWindow`，登记指认"空/残缺
文件窗口"的复现）——旧判定形状换钥顶盘 ⇒ 新实现零写盘，对照即验收。逐格回归：截断空文件/仅注释/
文件不存在三格建钥+PAUSE=1 与现状一致（既有钉全绿）、已有密钥格零落盘、`UnicodeDecodeError`
不吞（登记排除项语义逐字保留）。**取证口径**：本条"现网取证一条"仍未做（需登机读日志，移交用户
运维）；读败格返回新随机钥而非旧值，是既有 docstring 契约"宁可告警后带病运行"（会话重启失效），
与"不写盘"合起来才是完整验收——复核时勿读成"改成了启动失败"。

### M 簇 · 凭据与隐私出口
#### MF-85 弱密钥只 `WARNING` 不阻断，而模板自带一把**逃过全部三条判据**的示例钥（中）
实算（跑 `_decode_key` 同款判据）：`.env.example:18` 的 `0123456789abcdef`×4 解出 `01 23 45 67 89 ab cd ef`×4 ⇒ 全零 False、`len(set)=8` False、`bytes(range(32))`/逆序均 False ⇒ **三条全逃**。根因是判据 3 比的是**字节值**连续，而模板串连续的是**十六进制字符**（`account_crypto.py:292/300/302/304/306`）。
打折项：`:18` 是**注释态**、`.env.docker.example` 与 compose 都不注入该键、默认自动建钥走 `:108 secrets.token_bytes(32)` ⇒ 触发需人工取消注释；"仓库挂公开 GitHub"这一支撑"高"的前提在基线与 PROD-FACTS 里查不到（实证据是 `git pull gitee server-web`）⇒ 不据此升档（公开仓一事仍留在待裁决 #6）。修法裁为**阻断 + 模板换占位**（精确比对公开串零误杀）；否决 KDF/通用熵检测——Web 侧不管理这把钥（无写侧校验点可挂），改成读侧启动即崩会把外泄风险换成全站不可用并撞 MF-50 的不可轮换。〔C-30 · ADJ-16〕

**处置（2026-09-27 批2 Task2-3，repair/m3-batch2，`bf802af`）**：按裁定"阻断 + 模板换占位"落——
公开示例钥精确比对挂 `_decode_key` 唯一解码口，一条判据覆盖三条取值路径（`load_key` 环境变量档/
`.env` 档/建钥写前重读档），即**拒绝启动 + 拒绝在建钥路径把公开串折成新钥**两面同关；比对不分
大小写（`bytes.fromhex` 同解 ⇒ 大写写法同拦）；错误文案点名"公开示例模板"并给生成命令、不回带
钥值原文；`.env.example:18` 换注释态占位（含 `<`，误用即"应为 64 位十六进制" fail-loud，刻意
设计），全文不再含公开串。零误杀实测：200 把 `token_hex(32)` 随机钥全放行；"三条判据抓不住模板"
的实算反向钉同步入册（防判据回退）；全零/单字节/顺序三条弱钥维持 WARNING 不阻断（存量语义不变）。
KDF/通用熵检测否决理由写入常量上方注释。残余：无（登记裁定的修法面全落；触发仍需人工取消注释
这一前提不变——本条收的是"取消了也进不来"）。

#### MF-86 `phone_code` 不计入 `creds_written` ⇒ 只改设备识别码不要口令、不标凭据改写、不发变更信（中）
读写口径互斥：读侧只认 password+phone 共 3 处（`accounts_api.py:376-377`，同式在 `logs.py:297-300`）；写侧与 password 同档 4 处（`store/accounts.py:161/443-446/454-457/467-473`）。门与信号原文行：`accounts_api.py:378-381`（门调用点）+ `app.py:2269-2275`（full 必输 / risk 未命中换环境即放行 / off 永不）、`:426-432`（"改写凭据"位）、`:437-458`（当事人信）、`:464-475`（管理员 urgent）。
**前提订正**：`protocol.py:250/365` 两条登录函数参数里没有 `phone_code`，它只进 `sign_in_form:237-243` 的 `"Code"` ⇒ **不是账号接管**，且无下发通道（`accounts_data.py:152`）；成立的是完整性/可用性轴的静默改写。现网三态：`6d4eafa` 的 `accounts_api.py:374` 同形 ⇒ **已在现网**，且现网 `PW_GATE` 缺省 `risk`。〔C-41 · ADJ-9〕

**处置（2026-09-27 批2 Task2-4，repair/m3-batch2，`12cd058`）**：按订正口径执行（拦完整性轴的
静默改写，不夸大接管）——读①门与信号：管理端编辑先把识别码**折算成将进 SET 的最终值**再判
`creds_written`（`code_written = 折算值 ≠ 旧值`，哨兵清除同样算改写；折算必须排在判定之前，否则
`__clear__` 在判据里凭空蒸发——正是本条病根之一），只改/只清设备识别码从此过门、标"改写凭据"
审计位、发当事人信（新增识别码条目文案）、非 full 档另发管理员紧急告警；读②熔断：`logs.py:282`
补 `phone_code` 同档分支（调用方未带旧值一律不比较，防"没提供"当"改过"）；读③当事人信条目、
写侧 4 处（本就同档）不动即对齐。三档门语义逐字未动（判定函数零改动，动的是操作归类）：现网
risk 缺省下"同出口改码"仍免口令放行，新增占一格高危额度 + 双信号；换出口从此要口令（即本修复
要的效果）；full 档"每个受保护操作都要口令"的既有承诺第一次覆盖设备码。反例矩阵 21 例含零误报
钉（只改无关字段三档不进门禁零信号、留空回填旧值不相等不炸）。残余/移交：高频批量换码若撞 429
摩擦过大，裁额度族而非回退归类（运维反馈通道）；当事人信文案语义准确、若文案组另有口径可只改
字符串（测试只钉"设备识别码"关键词）；前端 toast 如实展示归 MF-61 批 5 条（见 MF-87 注记联动）。

#### MF-87 `__clear__` 哨兵在进 SET 前被 pop ⇒ 清空凭据是静默空操作而接口回 200（中）
`accounts_api.py:386-387` 与 `my.py:702-704` 把哨兵 `pop` 掉 ⇒ 不进 SET；全仓 4 个消费点无一折算成 `""` ⇒ 用户点"清除设备识别码"看到"已保存"，库里值原封不动。MF-61 只从前端侧记过"`__clear__` 空操作却回 200 + toast"，**后端为何空操作的机制未登记**（本条即机制），二者勿分两处修。〔ADJ-9 新挖 · 关联 MF-61〕

**处置（2026-09-27 批2 Task2-4，repair/m3-batch2，`ee912b9`）**：机制层收口（与 MF-61"勿分两处修"
的联动即此）——`__clear__` 不再被 pop，单点 `fold_phone_code` 折算成 `""` 随 UPDATE 落库，4 个
消费点（管理端编辑/用户端编辑/两条添加路径）统一接入同一折算（两条添加路径从此不会把协议令牌当
字面量送易班验证或落库）；`CLEAR_SENTINEL` 唯一真源迁入 `accounts_data.py:62`、`web/app.py` 再导出
保 `m.CLEAR_SENTINEL` 名。"接口不以 200 掩盖空操作"的实现路径=**让操作真的发生**：清空后同请求
返回的账号列表 `has_phone_code` 如实翻假（行断言 + 返回如实双钉），不另造错误码。grep 级钉：
全仓 `pop("phone_code"` 为零、`"__clear__"` 字面量 py 侧仅一处真源（前端 `account-form.js` 同名
串是令牌**产生方**，语义由 Python 单点收敛）、`web/routes/` 内折算调用恰 4 处（缺一处即一处
空操作）。移交：前端"清除"交互的 toast/表单态如实展示仍归批 5 MF-61 条——其成立前提（后端不再
说谎）已由本条满足，批 5 执行者勿重复改后端折算点。

#### MF-88 个人提交口把"号码是否在册"变成可定向确认的预言机（中）
`my.py:443` 的判重打在**全站账号表**上（`accounts_data.py:94-106/179-184`），且早于任何真实外呼（`my.py:458`）⇒ 零配额、**零留痕**（`:454-458` 区间无 `db.audit`）。可达者是"已登录且名下无未删账号"的会话（现网 110−89 ≥ 21 个天然可达）。
**两条原报数字被推翻**：限速是 `app.py:541-542` 的 60 次/10 秒 ⇒ **≈21 600 次/小时/IP**（不是 360）；但 89 个在册目标 ÷ `^1\d{10}$`(=10¹⁰) ⇒ 满速期望约 5 100 小时一次命中 ⇒ **"批量枚举"不成立**，只剩"定向确认某个号在不在册"。管理口 7 处 400 回显（`accounts_api.py:175/233/284/288/371/410/414`）行号复核为真，但回显的是**调用方自输**的 `clean['phone']`、审计与日志侧均已 `_mask_phone`（`:429/:477`）⇒ 不构成外泄；`_duplicate_phone_error:187-202` 自订的是"不泄露**归属**"而非"遮号码"⇒ 原报的"正面冲突"不成立。〔C-38 · ADJ-9〕

**处置（2026-09-27 批2 Task2-5，repair/m3-batch2，`d5773a1`）**：按订正口径执行——不做批量枚举
假设、判重打全站表语义不动、`_duplicate_phone_error` 契约与 400 文案不动，只把**定向确认面**压到
与详情/导出同档：判重预检**只在"命中"时**扣会话配额（`DUPCHECK_WINDOW=60`/`DUPCHECK_MAX=5` 模块
常量、零新 env 键，取值对齐 DETAIL/EXPORT/VERIFY 同档；键=会话用户名，按 IP 会把校园网共享出口
的正常用户互相挡死；复用既有 limiter 原语 `_bump_window_count`/`_ip_store_trim`，`_rate_lock` 内
原子，未另造窗口计数实现），每命中写 `my_account_add_dup_hit` 审计（target 走 `_mask_phone` 遮罩
形态、actor 可归因）；超配额即 429 且不再给出在册判断，被拒痕迹每窗口至多 1 行（防 429 反刷审计
表——详情面登记过的同型洞）；未重号的正常提交零扣额零留痕（零回归面）。反例 5 例：连续命中⇒
限速 + 必留痕 + 全行无完整号、正常路径零回归且按会话隔离、"本人刚删"差异化分支同受约束、翻窗
复位。边界/残余：管理口预筛判重未收口（登记只指控个人提交口，管理口面订正为不构成外泄；若裁决
同档收口属独立一条，搭 `_verify_attempt_allowed` 配额即可，本批不扩权）；限速为进程内 dict，继承
全项目 limiter 既有限制与 MF-95 已裁的 `-w 1` 前提；`DUPCHECK_MAX=5` 为裕量拍值，真预言机探测换号
也被同一额度卡住，误伤时运维改常量（与其余 limiter 同法，非 env 键）。

#### MF-89 状态文件用内置 `open()` 建 tmp ⇒ 终文件继承 umask，"创建即 0600"的既有契约只覆盖部分通道（低，两条待取证）
13 站点逐个复核（tmp 命名行 / `open()` 行 / `os.replace()` 行三行号全对上 ST-3 清单）；仓内已有合规助手 `state_io._write_private_json`（`os.open(...,0o600)`）并被 `test_probe.py:204`、`test_notify_webhook.py:1361` 钉死。内容分档：**明手机号 6 处**（`state_io.py:229/:246`、`alerts.py:321/:353`、`runner.py:591`、`cred_state.py:129`）、仅计数/时刻/pid 7 处；**凭据字段 0 处、审计明文 0 处**。两条升级口已堵：`sign-state.message` 上游已脱敏（`attempts.py:208-212`）、`cred-state.json` 只有 `fail_days/last_fail/paused_since/probe_date`。
不成立的是"多台机全局可读"：`runner.py:142` 与 `cli.py:754` 都在 `main()` 首行 `os.umask(0o077)`，`web/deploy/yiban-web.service` 模板自带 `UMask=0077`（仓内共 12 条 umask 077 代码锚点 + 1 条 systemd 声明）；按进程切比按站点切有用——web 只写其中 3 处（cred-state / notify-ledger / notify-throttle），其模式**唯一押在实装 unit 的 `UMask=` 行上**。tmp 的真窗口不是半写而是"整表内容 + 宽模式 + 崩溃残留 ≥1 天"（`state_gc.py:83-84 _TMP_MAX_AGE_SEC=86400`）。缺的两条取证：状态目录模式（README:171 裸 `mkdir` 会是 0755）与是否存在第三用户 ⇒ 任一为"宽"则升中。
与 `migrations.py:1195-1199`（已判入 MF-40）**同族同判**：`os.replace` 不改 mode、chmod 追不上残留 tmp，"补 chmod"劣于"只走一个通道"。〔C-28 · ADJ-8〕
验收不变量：`tests/test_state_file_writes.py` 里**外层套 `os.umask(0o000)`** 再断言终文件 0600 + 无 tmp 残留——不加这一句，测试会继承 077 而恒绿，这正是这 13 处今天漏网的原因。

**处置（2026-09-27 批2 Task2-6，repair/m3-batch2，实现 `2d73ce0` + 判据钉 `713673b`）**：按登记判据
"补 chmod 劣于只走一个通道"收口——13 站点 + 登记后分支新增 1 处（容器心跳 `_touch_heartbeat`，
共 14）全部改走单通道 `yiban/infra/private_json.write_private_json`（`os.open(...,0o600)` →
`.tmp<pid>-<tid>` → `os.replace` → 失败清 tmp 再原样抛）；`state_io._write_private_json` 保留为
既有锚点（委托实现），cred_state 的 token_hex 随机名与 ledger 的 pid-tid 名上收为通道标准。
内容语义/文件名/JSON 形状逐字节等价（`ensure_ascii` 差异经核对为纯 ASCII 内容，落盘一致）；
`runner.py:142`/`cli.py:754` 的 `umask(0o077)` 与 web unit `UMask=0077` 未推翻、降为纵深。
验收不变量即判据：umask(0o000) 矩阵 15 格（14 站点 + 单通道直写 + worker 心跳回归）逐条断言
终文件 0600 + 无 tmp 残留；源码守卫扫 8 个写盘文件出现 `open(*tmp*, "w")` 即红，另有"守卫自身
必须命中旧形态"的活体反例（判据失效可被发现）。state_gc 语义复核：半成品判据=`.tmp` 子串 + 24h +
非 SQLite 魔数，三种历史 tmp 形态全在清扫范围不变；通道"失败即清自己 tmp"使 state_gc 从唯一
清理者降为崩溃兜底——"整表内容 + 宽模式 + 残留 ≥1 天"三要素中宽模式已消除、残留仅剩 SIGKILL 路径。
排除项已核对：`audit_chain` 见证件故意 0644（MF-40 见证契约，非继承 umask 形态）、`migrations`
已修、`web/security` 自有 Windows replace 契约、`mock_env` 非生产状态写。残余：守卫排除表是显式
文件清单（新增写盘模块须同步登记）；`ensure_dir=False` 在"目录恰被清理"极端竞态留微秒级复活窗口
（与改造前等价、非回归）；登记两条待取证（状态目录模式/第三用户）在单通道下自然失效，无需再取证。

#### MF-90 告警无幂等：服务端已接收之后才超时会换条目**重发同一封**，上界 10 份（中）
机制源码级闭合：`smtplib.SMTP.__exit__` 的 QUIT 会抛未被放行的 `SMTPResponseException` ⇒ `transport.py:114` 捕获 ⇒ 换下一条目再投同一封；全仓无 `Message-ID`/幂等键 ⇒ 上界 = `web/app.py:592 MAIL_SMTPS_MAX = 10`。
**行号与措辞订正**：`sent = sent or _send(...)` 全仓 grep 0 命中（`:111` 实为 `sendmail`，聚合真身在 `:143-147`）；`web/security.py:hash_ip` **不存在**（唯一定义在 `tracking.py:109`）。分轴后另两支不占号：OR 聚合 + `SMTPRecipientsRefused.recipients` 零消费者 ⇒ **补句 MF-44**（并写明"零记录"过头——`:112/:118-122` 逐地址记了日志，缺的是结构化投递账）；逐地址串行/每地址重建连接 ⇒ **同 MF-57**；`_classify_send_error`（`:40-42`）把证书失败与断网同文案 ⇒ 低、不占号（`:103 create_default_context()` 已挡住外泄，`:28-34` 是写明的反端口扫描取舍）。收件人 4 与 SMTP 条数是**假定值**（现网未取证）。〔C-21轴2 · ADJ-16〕

**处置（2026-09-27 批2 Task2-10，repair/m3-batch2，`7a8142d`）**：幂等键落地收上界机制——
`Message-ID` 由 `(subject, 收件人, 正文)` 的 sha256 前 32 hex **确定性**生成（刻意不用随机 nonce，
否则同告警不同键、去重失效），随信发出；正文在 SMTP 条目循环**之外**定稿 ⇒ 同一封 failover 各条
严格同一封（含头），接收方可凭键去重——"上界 10 份 → 1 份"自此有机制基础。QUIT 抛异常换条目的
failover 行为本身保留（那是送达鲁棒性，非本条病根；病根是"换条目=换一封认不出的信"）。重投可观测
钉：换条目接手留"同封重投：条目 N/M 接手（id=…）"info 行、失败 warning 同带 id，重投上界=
`smtp_list()` 长度且不再无声。残余（归 MF-44 注记）：分轴后另两支不占号的处置随 2-10 一并落
（结构化投递账⇒ MF-44 B5、逐地址串行 ⇒ MF-57 未动）；收件人数/SMTP 条数仍假定值，现网取证移交
用户运维。

### N 簇 · 自检面与数字口径（绿了但什么都没证明）
#### MF-91 伞形条目：判据的输入由被检对象的写入者供给 ⇒ "缺陷 ⇒ 判据红"这条边被构造性切断（高，元条目）
本簇四条（MF-92/93/94 + 已登记的 MF-52）共享一个可用一句写完的元命题：**期望集或输入来自被验证的那一方，且测试用夹具绕开同源点 ⇒ 没有任何一层会喊**。分工必须写死，否则会与 MF-54 混修：MF-54 治"**源**的分叉"（不变量＝第二份定义不存在，grep 可门禁）；本条治"**证据的独立性**"（不变量＝每道自检必须自带一条"把输入改坏 ⇒ 工具必须红"的活体反例）。**MF-54 的修法在 MF-93 这类条目上反而有害**（把两份定义收成一份自证的定义，一致得更彻底）。
条目只管元命题 + 硬门禁 + 一张"每道自检的独立证据来源"登记表；点条目不并进来，只打标记。〔ADJ-10 总问题〕

**处置（2026-09-27 批 3 分诊，repair 流）**：**不立硬门禁**。理由是章程第 4/6 条（缩减门禁与织网件）：本条主张的"每道自检必须自带一条『把输入改坏 ⇒ 工具必须红』的活体反例"，若落成代码门就是又一道元门禁。其洞见（**证据的独立性**）已内化为本项目修复流的**审查必查项**——每个任务的审查代理都被要求"亲手构造反例实跑、禁止只信报告"，并在批 1/批 2 中反复据此抓出虚假声称（见批 1 索引提交曾漏、2-9b 报告计数不符等）。故以**流程纪律**而非代码门禁落地；归缩减批二次确认。

#### MF-92 `ledger_check` 拿补账的输入去对账补出来的表 ⇒ 且窗口外反向**恒红**（高）
`ledger_check.py:128-133` 用 `sign-state-<day>.json` 验 `sign_tasks`，而 `migrations.py:861/934` 的补账来源正是同一份 `terminal_task_state` ⇒ 对被 v20 补出的那批行 check 1 构造上恒真。**措辞要改**：不是"恒通过"——check 1 只 `SELECT phone` **不比状态**，而 v20 是 `INSERT OR IGNORE` ⇒ planner 先写的行赢，"JSON 说 success / 台账说 failed"**永久失明**；而窗口之外（v20 一次性、只回看 14 天，删账号还会连带删台账）它**恒红**。骗过的门有两处：设计文档 `ledger-dual-version-design-20260923.md:258` 把它写成"迁移的唯一验收门"并承诺**三方对账**，交付时被瘦成单向；`tests/test_ledger_check.py:80-86` 的夹具刻意绕开 backfill ⇒ 13 例全绿证的不是生产数据流。另 `:144-155` check3 的 `other = total − translated − backfilled` 是残差定义（不构成独立证据）。〔C-26 · ADJ-10〕

**处置（2026-09-27 批3a Task3a-1，repair/m3-batch3a）**：三处主张全落——①check 1 由"比存在性"改**比状态**，期望值复用 `migrations.terminal_task_state` **单源**（不另写第二份判据，规避 MF-54 反模式）；②**窗口外缺行改判"无法定论"**（`rc=2`）——与 `scripts/audit_verify.py` 的"0 通过 / 1 篡改 / 2 无法定论"**三分法同源**，且 `ledger_check` 自己的 docstring 本就写着"`1` 只留给明确探测到的差异，其余一律 `2`"，故这是**向该脚本既有契约收敛**，**不是**改 rc 契约（run.sh 0/1/2/3、cli 0/1/2/3/10 两文件零改动）；③check 3 的 `other = total − translated − backfilled` 显式标注为**残差定义、非独立证据**。**活体反例（同一构造夹具，两树各跑真脚本）**：窗口内"JSON=success / 台账=failed"由 **rc=0「对账平」**（"永久失真实"复现）→ **rc=1「状态不一致：JSON=success→done，台账=failed」**；窗口外（−30 天）缺行由 **rc=1「对账不平」**（恒红复现）→ **rc=2「不在补账窗口内：无法定论」**；窗口内缺行**仍 rc=1**（判据未放宽）。窗口 `[applied−13, applied]` 与 v20 的 `range(_BACKFILL_DAYS=14)` 逐一对齐；版本号按名字反查 `_MIGRATIONS`，未抄第二份字面量 `20`。残余（非阻塞）：`sqlite_master` 表存在性 4 行守卫最接近"遇不到"（去掉只把 rc=2 换成崩栈文案、判码不变），可留可删。

#### MF-93 容量估算是"一份算式跨两个配置层"⇒ 保存闸门按偏大值放行（中）
`capacity.py:93` 只传 `gap`，`avg/enabled` 落 `schedule.py:69` 的 `os.environ`；**web 进程从不把 `.env` 装进环境**（`web/*.py` 内 `load_dotenv`/`os.environ[` 零命中），而 `gap` 偏偏读 `.env`（`settings_api.py:310-312`）⇒ 同一次估算跨两层。复算：W=4200/gap=10 时 avg=3⇒**323**、avg=12⇒**191**（台账写 190，差一），高估 **69%**（≈台账的"约 70%"）。骗过的门是闸门自己：展示面 `:202/:213` 与判定面 `:312-314` 共用同一个偏大值 ⇒ 两出口永远互相对齐；`test_capacity_*.py` 18 处全用 `mock.patch.dict(os.environ,…)` 造输入，测的是另一个前提。现网 89 < 191 ⇒ **当前不越线**，但界面白送约 130 个名额的错觉。〔C-27/L11 · ADJ-10〕

**处置（2026-09-27 批3a Task3a-1，repair/m3-batch3a）**：容量预估收敛到**同一配置层**——展示面（`settings_api.py`）与判定面同调单一漏斗 `web/app.py::_capacity_estimate`，`avg/开关` 跟随 `.env` 真值，不再是"`gap` 读 `.env`、`avg/enabled` 落 `os.environ`"的跨层取值。**真跑对照**（窗口 4800s / gap=10 / `.env avg=12`、进程环境无该键）：修复前 **370**（avg 落进程缺省 3，跨两层实锤）→ 修复后 **218**（`=(4800−12)//22+1`，与 `.env` 真值同步）；进程环境再塞陈旧 3 仍 **218**（`.env` 覆盖，与 run.sh 的 export 优先级一致）；`.env` 改 3 ⇒ 两侧同步 **370**。`sign_window`/`edge_config` 读值面**未扩**（登记未点名，不顺手扩大战场）；引擎/CLI 调用点（`runner.py`、`cli.py`）不传 env ⇒ `os.environ` 缺省路径逐字不变。**未新增 env 键、未改键语义**。

#### MF-94 邮件配置"写 `.env`、读进程环境"+ README 教的部署方式会把新值冻住（中，含 L43 订正）
真不同源在**读序**：`mail/config.py:_get` 是 env 优先、`.env` 兜底，而 `README.md:634` 指示的部署正是把整份 `.env` 拷成 `EnvironmentFile` ⇒ 保存的新值被启动期冻结拷贝压住，**restart 也修不好**。与 MF-51（按数组位置沿用凭据 ⇒ 才是"界面 A 凭据 B"的真路径）**不是同一条**；与 MF-50 同拓扑、不同后果面。另有 1 个新增低危点：`host` 写成 `127.0.0.1:465` 会因 `_is_ipv4_literal_like` 的"4 段全数字"判据被放成"真域名"（构造性绕过，但 `getaddrinfo` 必失败 ⇒ 后果低）。`SMTP_PORT` 非法→回退 465 有**两份实现**且发送侧零日志。
需订正：**台账 L43 的两条主张在基线上为假**（`check_smtp_host:91-92` 显式拒 `localhost`；设置页渲染与发送侧同用 `smtp_list()` ⇒ 不存在"界面说 A 真走 B"）⇒ 本条即其收窄后的幸存形态。三态：②③④⑤代码级即可判；①需现网取证 `/proc/<gunicorn pid>/environ` 是否含 `YIBAN_MAIL_*`（按红线未登机）。〔C-22/L43 · ADJ-10〕

**处置（2026-09-27 批3a Task3a-1，repair/m3-batch3a）**：**选 (a) 改部署教法**——README 的 `EnvironmentFile` 装法改为**只注入 `YIBAN_ACCOUNTS_KEY` 一行**，并写明"整份 `.env` 拷成 EnvironmentFile 会把「系统设置」页写入的新值压进启动快照、连 `systemctl restart` 都不生效"，另给旧部署的收窄处置（收窄成一行 + daemon-reload + restart 即恢复"保存即时生效"）与轮换第 4 步同步。与 `yiban/mail/config.py::_get` 的"**env 优先、`.env` 兜底**"读序**自洽**（新教法不再制造冻结），与单元模板 `web/deploy/yiban-web.service` 的 `EnvironmentFile=/etc/yiban/accounts-key` 一致。(b) 统一读序会改发送侧读值来源、撞升级子句，主动放弃。**未登机取证**：提交内容级扫描 `ssh|scp|/proc/|journalctl|systemctl|curl http` **零命中**。残余（登记原文、非本任务验收）：三态①需现网 `/proc/<pid>/environ` 是否含 `YIBAN_MAIL_*`（按红线**未登机、记待取证**）；两低危点（`127.0.0.1:465` 被当"真域名"放行、`SMTP_PORT` 双份回退实现）按章程"只修本体、不碰发送契约"**未动**。

#### MF-95 注销冷静期把 `/api/login` 变成零留痕的凭据验证器（中，代码已在现网、当前可达集合 0）
`auth.py:92-99` 判据与 `:157-159 recoverable` 出口 ⇒ 口令输对但账号处于冷静期时既不写 `login_ok` 审计（`db.audit` 在 `if role:` 块 `:104-126` 内）也不计失败（`:160`）。应用侧确实零留痕：`audit_chain.py:359/404` 是唯一写入口且未被走到，`page_visits`/`server_metrics` 已由 v14 删除，两个限速表是进程内 dict ⇒ 只剩 nginx 一条同为 200 的 access log（不可归因）。
**两处原报口径必须改**：①限速绑的是 `auth.py:71` 的 **10 次/60 秒/IP（600/h）**，`app.py:541-542` 的 60/10s（21600/h）是更宽的全局桶 ⇒ "无限次"与"21600/h"都不成立（XFF 由 nginx 覆盖式写、不可伪造；`-w 1` 计数不分叉）；②时延放大器**方向反了**：冷静期账号对错都跑 2 次 scrypt，不存在/活跃只 1 次 ⇒ 2× 标出的是"7 天内注销过的邮箱"（**免口令枚举**），而口令对错由响应体直告。
三态：`6d4eafa` 的 `auth.py:92-99/153-155` 同形 ⇒ 代码已在现网；`deleted=1` 是冷静期的**超集**，"软删 0 ⇒ 此刻可达集合 0"成立，但窗口由任一用户自助注销打开 7 天 ⇒ 写"已在现网、当前可达集合 0"，**不采纳**"现网不触发"。〔C-42 · ADJ-15〕

**处置（2026-09-27 批3a Task3a-2，repair/m3-batch3a2）**：只做"补留痕"半条——冷静期口令正确的
路径经**既有** `db.audit` 落一条 `login_recoverable`（三元组与 `login_ok` 同构：username 遮罩+截断、
IP 走 `hash_ip`、detail 不落凭据；没有建会话，故与 `login_ok` 分动作名），失败/不存在路径零改动。
上面两处口径订正随本注记留档为**登记侧纠正**，无需代码：限速口径 = `auth.py` 的 10 次/60 秒/IP
（600/h，`app.py` 60/10s 是更宽的全局桶），"无限次/21600/h"均不成立；时延放大器方向反了，2× 标出
的是"7 天内注销过的邮箱"，口令对错由响应体直告。**残余（章程降级，另立裁决才做）**：免口令枚举
（时延/响应体两路）本任务不作验收项，未动。判据钉：`test_login_recoverable_writes_one_attributable_audit`。

### O 簇 · 面板与接口给假信号
#### MF-96 `POST /api/notify-test`：`force=True` 跳过冷却与两本每日额度、零审计（中）
`web/routes/notify.py:406-414` 确实只判 `_is_builtin_admin_session()`、无 `_high_risk_gate`、零 `db.audit`（notify.py 的审计只有 `:238/:398`；全站唯一 `after_request`（`app.py:2088`）只设响应头）。外呼为真但**不是 SMTP**：`transport.py:182-188 → send(force=True)`，`:153` 把 cooldown（`:158`）与两本每日额度（`:161`）一起跳过，出口是 `:71-100` 的 ServerChan HTTPS POST 或经白名单的 custom webhook；**不能群发到任意地址**（view 不读任何请求参数，收件方 100% 来自服务端配置）。"无限额"订正为：仍吃全站 `RATE_MAX=60/10s`（`app.py:541-542,1870-1890`）+ CSRF（`:1979-2011`）+ nginx 50r/s。"不在 `app.py:2380-2390` 清单"= **同 MF-58** 的根（那份 docstring 是"必须当次输口令"的落点表，不是审计清单，提报定性偏了）。现网：该路由在部署线 `6d4eafa` 存在（`:459/:476`），触发需主管理员会话被盗。〔C-44 · ADJ-7〕
验收不变量：第 4 次调用必须 429 且**零外呼**；每次外呼必须落一条含操作者的审计。

**处置（2026-09-27 批3a Task3a-2，repair/m3-batch3a2，控制者裁定：只落审计半条）**：每次调用
`api_notify_test` 落一条 `notify_test` 审计（含操作者；成功与失败都记——force 出口的"失败"多为
请求已发出后被拒/异常，只记成功会漏真外呼）。**刻意不加**"第 4 次 429 + 会话配额"那半条验收：
该按钮是管理员自用排障入口，按用户章程第 2 条（防"过强防护阻碍管理员日常"）由控制者裁定降级，
全站 `RATE_MAX=60/10s` + CSRF + nginx 限速维持原样；登记验收不变量以**后半句**（每次外呼落一条含操作者
的审计）为准，**前半句**（第 4 次调用 429 + 会话配额）作废留此注记。
判据钉：`test_notify_test_each_call_leaves_attributable_audit`。

#### MF-97 `/api/logs` 的三元组各说一件事 ⇒ 前端"是否还有更多"必然判错（中，已在现网）
`web/routes/data.py:api_logs:91-102`：`total_lines = len(masked_all)` 在 `if q:` **之前**；`_LOG_VIEW_CAP = 5000` 在 `if show_all:` 分支**体内**赋值；`truncated = len(masked_all) > _LOG_VIEW_CAP` 在过滤**之后** ⇒ 三个数不同轴。同族两份封顶口径（`-80:` 裸字面量 vs `_LOG_VIEW_CAP`）属 MF-54 补句。复核状态：**单源未独立裁决**（纯计数逻辑，可由读码直接判）。〔C-48 · GAP-4〕

**处置（2026-09-27 批3a Task3a-2，repair/m3-batch3a2）**：三元组收成同轴——`total_lines` 取**过滤后**
的同一份 `masked_all`，`_LOG_VIEW_CAP` 上收为 `data.py` 模块级单源常量，`truncated` 判据统一为
"结果集 > 本次返回行数"（封顶截断与缺省尾 80 截断共用一个判据，缺省视图的"还有更多"从恒 False
改为说真话）。既有 `test_logs_search_filters_full_day` 的 `total_lines=4` 断言钉的正是"过滤前"病灶，
随契约订正为 2。同族那半条两份封顶口径（`-80:` 字面量）未动，归 **MF-54 补句**。判据钉：
`test_logs_triple_same_axis_at_cap_boundary`（命中数恰好 =cap 与 cap+1 两侧各断一组三元组）。

#### MF-98 "我的日历"把"状态目录读不到"渲染成"这个月没签"，还把伴生文件当成额外的天（中，幻影键已在现网）
`web/routes/my.py:615-627`：`except OSError: pass` ⇒ `ok:true` + 全月空白 + 零日志；`date = entry.name[11:-5]` 不过滤伴生文件 ⇒ `X.json.lock` 被切成 `"X.json"`、`X.json.tmp1234` 切成 `"X.json.tm"`。造键源 `runner.py:572`（每天必造 `.lock`）+ `locks.py:62` + `state_gc.py:7/81-85`（`_TMP_MARK=".tmp"`、`_TMP_MAX_AGE_SEC=86400`）。幻影键部分**已在现网**（只要写过 `sign-daily-<date>.json` 就必有 `.lock`，`python -c` 切片实跑验证），空白日历部分未知（需权限异常/目录分叉）。属 MF-45/MF-61"面板给假安心"的反面同族 ⇒ 交叉引用不合并。〔C-34 · ST-3，实跑取证〕

**处置（2026-09-27 批3a Task3a-2，repair/m3-batch3a2）**：两处都改，一行级。①取日期改按"后缀+形态"识别
（只收 `startswith(prefix) and endswith('.json')` 且中段恰为两位数字者），`.lock`/`.tmp<pid>-<tid>` 伴生文件
不再被截成幻影键；②`except OSError` 不再 `pass`——写一条 `logger.warning` + 返回 `ok:false`/500，读不到
不再是"这个月没签"的肯定空白。**升级子句核对（读不到要出声是否改响应契约）**：唯一 HTTP 消费点是
`web/static/js/calendar.js` 的 `/api/my-calendar` 拉取（:232-284），走 `YB.api`——core.js:165 对
`data.ok === false` 抛错、calendar.js:275 catch 已有"卡内错误态 + 重试"分支 ⇒ `ok:false` 本就是既有
消费语义，未新增字段、未改 `ok` 的含义（只是不再谎报 true），前端一行未动，不触升级子句。
判据钉：`test_my_calendar_companion_files_and_unreadable_dir`（一条用例同时钉幻影键与读不到出声，
含修复前红→修复后绿的实跑证据）。

#### MF-99 日志页用宽行模型切日志、不匹配就 `continue` ⇒ 被撑开的后半行整块静默消失（中，取证未知）
`web/services/logs.py:91 raw.decode(…).splitlines()`，`parse_sign_log:94-116` 内 `:107` 不中即 `:110-111 continue`；`_log_lines_for:139-152` 还要 `startswith(f"[{date_str} ")` ⇒ 半行永久丢失且无计数。对照窄侧 `:85 f.readline()`。可达性依赖与 MF-86 同一个"无字符集校验"字段。与 MF-46 的 `.env` 行模型同族但对象是日志文件 ⇒ **勿并**。复核状态：单源（ST-4），机制读码可判、触发需生产日志里的裸 U+0085。〔C-36 · ST-4〕

**处置（2026-09-27 批3a Task3a-2，repair/m3-batch3a2，章程取最小）**：不改切行模型（那会动所有行的
归属），只让丢弃可观测——`_log_lines_for` 新增可选 `stats` dict 出参，把"因解析不出而被丢弃的行"
计数（当日前缀但过不了 `SIGN_LOG_RE` 的残行 + 非本日且同样解析不出的半行/续行；外日**完整**残留行与
可见性过滤属设计内剔除，不计），`/api/logs` 以 `dropped_lines` 回显"另有 N 行没算进去"。收录行集合
与原口径逐字一致。判据钉：`test_logs_dropped_lines_observable`（一条逻辑行含裸 U+0085 ⇒ 前半收录、
后半计数；干净文件 ⇒ 计数 0 不误报）。**残余**：`parse_sign_log`（`my.py:84` 的「最近记录」切片）
有同型 `continue`，未一并计数——非日志页返回面，章程内主动简化。触发前提未变：需生产日志出现裸
U+0085（可达性仍绑 MF-86 的无字符集校验字段）。

#### MF-100 脱敏上线后运维按完整手机号 `grep`/`journalctl` 恒返回空，且没有替代口径与 runbook 禁则（中，代码外面）
盘上不再有 11 位连续数字 ⇒ 按完整号码的 shell/journalctl 检索恒空，会把人引向"这台机器没签过这个号"的错误结论。修法缺口：MF-49 的方向只覆盖"单一原语 + 展示层禁止第二套口径 + 版本号可判别"，**没有** a) 后 4 位/遮罩形态的反查工具 b) runbook 禁则。本流独立复核 `journalctl|runbook|后 4 位|反查` 在登记表命中 0。与 MF-49③（UI 侧 `q=`）分两半：代码内 vs 代码外 ⇒ 独立成条、双向交叉引用。〔C-49 · GAP-4〕

**处置（2026-09-27 批 3 分诊，repair 流）**：**代码面不实现**。本条缺口是"脱敏上线后按完整号码检索恒空、无替代口径与 runbook 禁则"，修法给的 (a) 后 4 位/遮罩形态**反查工具**属新增代码（章程反对的织网），(b) runbook 禁则属**运维文档面**——随缩减批整理运维文档时一并写入，本批不新增代码。与 MF-49③（UI 侧 `q=` 检索语义）分两半、双向交叉引用；代码内那半条仍在批 3 清单。归缩减批二次确认。

#### MF-101 手动签到不在 spawn 前过滤 `user_paused` ⇒ 批量"N 个账号"计数虚高（低）
`signin_api.py:200/:294` 不在派发前剔除已自暂停的号（全文件 `user_paused` 0 命中），单条提示与批量计数都会虚高；真正拦住的是引擎侧 `round.py:464`（rc=2、`attempt_signin=0`、日志"⏹️ 用户已取消签到"）。
**必须同时记录这次改判**：原指控"一键暂停/周末/熔断/自暂停四类门对手动腿完全无效，急停后照样真实登录"经实跑**不成立**——①急停、②周末确实放行（但这是写进 UI 契约的**设计**：`work_settings.html:632/636`「手动签到不受影响」，由 `tests/test_global_pause.py:59` 锁定，自 `c700ae2` 即如此）；③用户自暂停**实际拦住**；④熔断放行 1 次且成功后清除记录、失败顺延 `probe_date`（半开试探语义）。三份取证共同断言的"子进程 exit 0、全链路无信号"实为 **exit 2**，并经 `signin_api.py:127 → manual_sign.py:74-76,98-105` 往当天日志写"⚠️ 手动签到未完成…本轮未实际签到"，另有 ⏹️ 状态与 `:259` 审计行。设计意图冲突已裁决（2026-09-25，待裁决 #8）：手动腿保持豁免，本条只剩 spawn 前过滤 `user_paused` 一半照修。〔C-01 · ADJ-1〕

**处置（2026-09-26 批1 Task9，repair/m3-batch1）**：只修"spawn 前过滤"这一半——
`web/routes/signin_api.py` 两处派发点在起子进程前剔除 `user_paused` 账号：单条在
`_spawn_signin` 里拒（400 + "已自暂停签到，跳过（可在「我的账号」恢复）"），批量在
`api_signin_batch` 收敛 `phones` 前剔除并计数（响应新增 `count` / `skipped_paused`，文案
"N 个已自暂停，跳过"），批量计数、`--only` 账号串、审计明细与后台日志因此逐字一致
（全选皆自暂停 ⇒ 400，不派发空队列）。**引擎侧 `round.py` 的拦截保持不动**（第二道保险）。
门语义未动：急停/周末豁免仍由 `tests/test_global_pause.py` 锁定，本批一行未改。

#### MF-102 窗口谓词两式不一致 ⇒ 每天恰有 1 个墙钟秒"文案已打、门未关"（低）
`window.py:198-201` 预检用 `remaining_sec <= 0`，`:203-205 is_closed` 用 `>`，而 `_minute_of_day`（`:245-246`）只到整秒 ⇒ 在 hi（缺省 07:49:00.000~0.999）有 1 秒窗口文案已打、门未关；该请求仍在 `YIBAN_SIGN_END` 之内，只越过自设掐尾缓冲。修法：**预检改用 `_win.is_closed(now)` 复用同一谓词**。
**严禁照原指控"补 `return`"**：`runner.py:399-412` 之后有第二道同源门必然挡住（v2 `round.py:323 _window_closed` 是 `while pending:` 弹出第一条判定、排在唯一请求出口 `:374` 之前；v3 `executor_v3.py:581` 同理排在 `claim_batch:589` 之前；`tests/test_effective_window.py:188` 断言 `attempt.call_count == 0`）。补 return 会让 `results` 留空、账号落进 `runner.py:528` 的"未执行"默认桶 ⇒ 退出码 1 + 失败邮件 + `run.sh` 写不出 SKIPPED，正是 `:267-270` 记过并修掉的坑。另清点 21 条"宣告整轮不干"的文案，除 `runner.py:401/409` 外**全部自带 break/return/continue** ⇒ 单点非家族。〔ADJ-3 残留〕

**处置（2026-09-27 批 3 分诊，repair 流）**：**不修**。影响面 = 每天恰有 1 个墙钟秒的"文案已打、门未关"，且该请求仍在 `YIBAN_SIGN_END` 之内、只越过自设掐尾缓冲；登记已证**第二道同源门必然挡住**（v2/v3 各一处，且有用例断言 `attempt.call_count == 0`）⇒ 无实际后果。按章程第 4 条（裁"压根遇不到"的情况）不修；若将来顺手改（预检复用同一 `is_closed`谓词）则**须零测试增量**，否则不做。

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

## CI 维护检出（MF-104..106，2026-09-25 GitHub Actions 体检）

> 来源：只读体检报告 `D:/code/_scratch/gh-actions-check-20260925.md`（gh 实跑取证，run id 已录）。
> 结论先行：Actions 名存实亡——CI 工作流历史仅 1 绿（首跑 08-14），其后 114 连红；mirror 工作流 0 绿；
> sign-in 工作流 08-07 起手动禁用。以下三条是修好后 CI 才有意义的缺陷。

**处置（2026-09-27 批 3 分诊，repair 流）**：**不扩扫描集**。登记对该条的**原指控已否证**——被点名的 `appendBody` 字符串分支是**死代码**（`confirmDialog` 走 `el()` 的 `text` 分支 =`textContent`），全仓 `display_name` 出口逐条读完 **0 处进 innerHTML**，故这是**潜伏 footgun而非漏洞**。修法给的"把 `core.js`/`calendar.js` 纳入 JS 不变量扫描集"**正是章程 A1 要裁的织网件**（且该扫描集本身在存量的"第二份口径"第二类里）；作**已知面**记录，不新增文件集。归缩减批二次确认。

### MF-104 loadtest 测试只 patch `sys.platform` 不 patch `geteuid` ⇒ 非 root Linux CI 三连红（CI-only）
- **现象**：`scripts/loadtest/mock_env.py:397 ensure_platform()` 在 `os.geteuid() != 0` 时 exit 2；
  相关测试只 patch `sys.platform` 未 patch `geteuid` ⇒ GitHub Actions（非 root Linux runner）必红 3 条。
  Windows（无 geteuid）与 WSL root 通过——**这同时解释了 MF-37"基线只在特定环境可复现"的环境之谜**
  （Windows 无 geteuid / WSL root / CI 非 root Linux，三种环境三种命）。
- **证据**：体检报告 §4；run 35740634978（server-web@6d4eafa 11 红中含此族）。
- **验收不变量**：判据可注入或测试 patch `os.geteuid` 后，`TZ` 与 root 与否四象限同绿；活体反例：
  非 root 环境不 patch ⇒ 红仍可见（不许把判据改成恒真）。
- **现网三态**：CI-only（现网 cron 为 root，不触发）。
- **修法**：与 MF-39 同批（本地门禁扩 web/ 时顺手）；属 CI 维护批内容。

### MF-105 时区脆弱测试：`test_clock_jump_backward_blocked` 用裸 `datetime.now()` 对北京时间守卫断言 ⇒ UTC runner 必红
- **现象**：main 分支 CI 仅存的红（run 证据在体检报告 §4）。测试用 runner 本地时区的 naive
  `datetime.now()`，而 clock_guard 用北京时区 `clock.now()` ⇒ UTC 环境把 2 小时回拨算成 6 小时前跳，断言翻转。
- **验收不变量**：测试显式锚定时区（改用与守卫同源的 `clock.now()` 或 freeze 时间）后，
  `TZ=UTC` 与 `TZ=Asia/Shanghai` 双向跑同绿。
- **修法**：CI 维护批，与 MF-104 同批。

### MF-106 CI 工作流维护簇（仓库内容可修的部分 + owner 动作清单）
- **现象**：① CI 的 ruff 钉 0.15.22、本地 0.16.8，判定可能分叉；② CI ruff 用 `check .` 含 web/，
  web/ 有 2 处 RUF100（与 MF-39 同一面，CI 侧先爆）；③ `mirror.yml` 的 src/dst 写成 3 段式而
  hub-mirror-action 要求 2 段 ⇒ 自首提交 `1d5012c` 起 0 次成功，且 `GITEE_PRIVATE_KEY`/`GITEE_TOKEN`
  两 secret 不存在——纯噪音工作流；④ `Yiban Sign-in` 定时工作流被 WAF 打死后手动禁用，
  其三个 secret 均不存在 ⇒ 在 CI 里跑真实签到既不可靠也危险；⑤ CI 无 `workflow_dispatch`，无法手动触发。
- **验收不变量**：workflow 文件 yamllint/actionlint 零错误；mirror.yml 删除或修成 2 段式；
  sign-in 工作流删除（或文件头注明永久禁用理由）；CI 带 `workflow_dispatch`；
  ruff 版本与本地门禁对齐；`web/` 2 处 RUF100 清零（接 MF-39）。
- **owner 动作（仓外）**：push develop（✅ 2026-09-25 已完成 b6457e6..fd0a08f）；**Dependabot 幽灵 PR 已定案（用户质疑后取证升级，2026-09-25）**：版本更新本无设置开关（机制=默认分支上的 dependabot.yml 文件本身），用户从未开过任何选项、且按文档注释禁用正确——但 **5 个 PR（#13/#14/#15/#16/#20）的基线提交经 `git merge-base` 取证全部包含禁用提交 `3f22376`**（最早 08-27 基线 663362a、最晚 09-24 基线 1d24006 内 yml 为整文件注释态）⇒ GitHub 对已禁用配置持续开 PR 一个月，属平台侧异常。处置：① main 删除 yml 已备本地提交 `5d793b1`（待 push，注释禁用被证明不够，按文档「完全禁用」路径删文件）；② `gh pr close 20 --delete-branch`（#13-16 已关）；③ 观察一周，复发则携 PR 基线取证开 GitHub Support 工单。取证：`D:/code/_scratch/dependabot-triage-20260925.md`；若保留 mirror 则配两个 Gitee secret。
- **修法**：仓库内容部分归 CI 维护批（Task 7）；owner 部分移交用户。

**收尾时空号推进至 MF-107**。

### MF-107 `test_cookie_path_narrowed_when_base_path_env_set` 依赖同文件前序用例的状态：单跑必红、全文件/全量绿（CI 安全子集门会误红）
- **现象**（2026-09-25 develop push 门禁①发现）：`-k "security or …"` 子集与单只串行跑均红
  （`tests/test_web_security_gates.py::Batch18FixesTest`）；**同文件全跑 79/79 绿、全量 2928/0 绿**
  ⇒ 用例依赖同文件前序用例建立的状态（疑似 app factory 的 `YIBAN_BASE_PATH` 环境或模块级单例），
  是测试隔离缺陷，非产品回归；`fd0a08f` 上已存在（当日 diff 仅 docs/.github）。
- **验收不变量**：该用例单跑绿（自带 setup 或显式声明依赖）；同文件与全量保持绿。
- **修法**：用例自备状态、不依赖文件内顺序；归测试质量批（与 MF-38 干扰家族同批）。
- **现网三态**：不适用（纯测试面）。

**处置（2026-09-27 批3a Task3a-1，repair/m3-batch3a）**：用例**自备前置**——把登录请求挪进 `mock.patch.object(ENV_FILE…)` 块内，使 `.env` 打桩罩住整段 `create_app` + 登录，不再依赖文件内前序用例建立的状态（原病灶：登录落在 patch 之外，读 `env_file` 落在"明文在、哈希缺"的 fail-closed 态）。**断言集合新旧逐条恒等（程序化比对 2/2）**，零删、零放宽——修的只是"请求位置 + 为什么"。**单跑绿**（`1 passed in 1.5s`）、同文件全量 **79 passed**、全量绿。残余（非阻塞）：同族其它用例的顺序共享**未做族级清扫**（本条只治登记点名的那一条）。

### MF-108 CI 日期边界测试族：裸 `now()`/字面日期与北京钟错位 ⇒ 机器时区日期≠北京日期的时段全量大面积红（CI-only）
- **现象**（2026-09-25 批次 0 合并 push 首跑发现）：被测代码统一走 `yiban.clock` 北京钟，
  而一批用例自带裸 `datetime.now()`/`date.today()`/字面日期，两者**日期**在"机器时区日期≠北京日期"
  的时段（UTC 16:00–24:00，即北京 00:00–08:00）错位 ⇒ 全量大面积红。实测 CI（develop@19d6f13，
  run 36160538401，16:24 UTC 触发）：**36 failed / 3011 passed / 348s**；同代码本机（北京时区）
  同时段仅 2 败（同文件同族）；此前白天全量绿 ⇒ 历史上 CI 也有"深夜跑必红、白天跑绿"的潜规则。
- **证据**：`TZ=UTC` 本机复现抽样 **4/4 同名**（`test_logs_by_date` 两条、
  `test_breaker::test_write_sign_state_records_dur`、`test_state_gc::test_retention_boundary_is_by_filename_date`）；
  断言原文可见双钟错位（`sign-2026-09-26.log != sign-2026-09-25.log`、
  `datetime(2026,9,26,2,21) not <= datetime(2026,9,25,23,59)`）。MF-105 是本族单例
  （批 0 Task 7 只修了 `test_clock_jump_backward_blocked` 等三处个例），未做族级清扫。
  失败清单 36 条全数符合该模式（scheduler_gate/container_scheduler/schedule_retry/state_gc/
  masking_ssrf_gaps/run_sh_workers/breaker/logs_by_date/no_position/host_exit_semantics/
  multi_executor_engine/registration_pause/admin_creds_masked_ops/login_e2e_mock/manual_sign_reporting/verify_jobs）。
- **验收不变量**：`grep -rnE "datetime\.now\(|date\.today\(|utcnow\(" tests/` 中凡涉"当日"语义者
  一律锚到 `yiban.clock`（或显式 freeze）；**`TZ=UTC` 下全量 0 失败**（MF-105 判据推广到族）。
- **现网三态**：不涉现网（纯测试面）；现网影响 = CI 深夜跑必红 ⇒ CI 信号不可信。
- **处置**：随批 1 第 0 段（CI 双轨 + 测试瘦身）同批修；修前 CI 全量轨排程避开 UTC 16:00–24:00 窗口。

**处置（2026-09-26 批1 Task0，repair/m3-batch1）**：按天文件名/状态判定全部锚定
`yiban.clock` 业务钟（CI 首跑 36 红的 UTC/北京日期边界家族修复）；双 TZ 回归自此
常态化（全量默认 + `TZ=UTC` 各一遍）。批内日期敏感件沿用同纪律（Task 10 时间窗夹具
锚业务钟、跨午夜敏感的 e2e 以构造定结果）。残余：运维脚本宿主钟（state_cleanup/
backup_sentinel/generate_demo_data）另登记 MF-109（批 2+）。

**处置（2026-09-28 缩减批 6a，repair/m3-shrink6a）**：**双 TZ 常态化收窄为"单遍全量 + 时区专项子集"**。CI 实况本就单遍（ci.yml 快车道无 TZ 步；nightly 全量默认 TZ 单遍），常态化的"全量默认 + TZ=UTC 各一遍"只存在于本地习惯口径。收窄后：nightly 全量保持单遍，新增"时区专项子集"步（-k "date or midnight or rollover or utc or tz or retention or saturday or weekend or cross_day"，133 条 ~10s，TZ=UTC）作金丝雀；子集词集同步写进 release-gate.md §2①，改时钟相关代码时本地照此双跑。子集实测 TZ=UTC 全绿。

### MF-109 运维脚本按天文件用宿主 `date` 命名/判定，与引擎业务钟（`yiban.clock` 北京钟）分叉（条件触发，现网不触发）
- **现象**（2026-09-26 批 1 Task 0 全量审查时发现，tests-only 未改生产）：`run.sh`/`yiban-fallback.sh`/
  `backup.sh`/`pull-prod-backup.sh` 给按天键文件命名与判定（`sign-status-<date>.txt`、`sign-<date>.log`、
  备份包名、轮转保留期按文件名日期）用的是**宿主 `date`**；引擎/web 统一 `yiban.clock`。宿主时区≠北京
  （如 UTC 宿主）时，UTC 16:00–24:00 窗口内脚本"当天"与引擎业务日**差一天**：状态/日志落到错误日期名下、
  发布门 §3/§5 按业务日的采集命令找错文件、备份保留期判定错位。
- **证据**：📄 静态读码可核——`grep -n 'date +' run.sh scripts/backup.sh scripts/pull-prod-backup.sh`
  （状态/日志/备份名）对照 `yiban/clock.py` 的北京钟口径；CI 日期族（MF-108）实证了"双钟分叉"模式
  在 UTC 环境必然现形，脚本面是同模式的未修层。
- **验收不变量**：所有按天命名/判定的运维入口统一取**单一业务日源**（与引擎同源），宿主 `TZ` 不再影响
  文件名与保留期判定；`TZ=UTC` 下真跑一轮 `run.sh`，状态/日志文件名与引擎业务日一致。
- **现网三态**：**现网不触发**（生产宿主为北京时区）；条件缺陷——换宿主/改时区/容器化部署即触发。
- **归置**：不阻塞批 1 红线；随批 2（告警/隐私与运维一致面）或容量批处置。

**处置（2026-09-27 批 3 分诊，repair 流）**：**不修**。现网宿主为北京时区 ⇒ **现网不触发**（登记原文即写"条件触发"）；触发条件是换宿主/改时区/容器化部署。按章程第 4 条（裁"压根遇不到"的情况）不修，改在运维文档补一句"宿主时区须为业务时区（北京）"。与 MF-108（CI 日期族，批 1 已修）同模式但对象是运维脚本，勿混。归缩减批二次确认。

**处置（2026-09-28 缩减批 6a，repair/m3-shrink6a，用户拍板必修、撤销批 3 的降级）**：**修**。
按天命名/判定统一取**单一业务日源**（`yiban.clock` 北京钟，与引擎/web 同一事实源）——
四个 shell 入口（`run.sh` / `scripts/yiban-fallback.sh` / `scripts/backup.sh` /
`scripts/pull-prod-backup.sh`）各立 `business_day()`：主路 `python -c "from yiban.clock
import today"`，退化 `TZ=Asia/Shanghai date`（宿主缺 tzdata 时），再退化宿主 `date`
（保持可运行的降级形态）；三个 python 入口（`state_cleanup.py` 的日志"截止"文案、
`backup_sentinel.py` 的当日归档判定、`generate_demo_data.py` 的演示数据锚点）直接改走
`yiban.clock`（backup_sentinel 的"两边同钟"注释随 backup.sh 口径翻转同步改写）。
`run.sh` 的 0/1/2/3 判码与执行语义逐字未动（只换日期来源；`PY` 解析上移到按天命名
之前属顺序搬移，判定块零改动）；`scripts/backup.sh` 自检退出码 6/7/8 未动。
**验收对拍（本机真跑）**：TZ=UTC 与默认 TZ 各真跑一轮 `run.sh`（暂停门 rc=2 SKIPPED、
零外联），状态/日志/触发标记文件名均 `2026-09-28` = `yiban.clock.today()`；`backup.sh`
TZ=UTC 真跑归档名 `yiban-2026-09-28.tar.gz`；另以 stub 宿主 `date`（2026-09-27）模拟
UTC 16:00-24:00 错位窗——三个脚本的 `business_day()` 仍返回引擎业务日 09-28（python
主路压过宿主钟），python 缺失时才退化到宿主钟。README 部署章节补时区建议一行。

### MF-110 测试标签块/生成式索引的门约定失效：批0 新增测试无标签块、索引过期、CI 未接 `--check`（脚本自称已接）
- **现象**（2026-09-26 批 1 期间翻查写作规范时发现）：`scripts/test_index.py` 约定 `tests/test_*.py`
  头部五字段标签块（标签/覆盖/对应实现/关键断言/依赖）并生成 `docs/dev/test-index.tsv`，
  docstring 自称"CI 在跑测试之前执行 `--check`"。实测：① ci.yml/nightly.yml **无** test_index 调用（门未接线）；
  ② 本机 `--check` 红——批 0 新增测试文件（test_deploy_prod_artifacts / test_loadtest_isolation /
  test_migrations_fail_closed 等）缺标签块字段；③ 批 1 Task 2b 实删 26 条用例后索引必然失同步（未重生成）。
- **证据**：📄 本机 WSL `python scripts/test_index.py --check`（多条 `! … 缺字段`）；
  `grep -rn test_index .github/workflows/` 零命中。
- **验收不变量**：`--check` 全绿；CI（nightly）真实执行 `--check`；此后新增测试文件自带标签块。
- **现网三态**：不涉现网（开发约定面）。
- **处置**：批 1 补一个小任务（批 0 新增测试文件补标签块 + 重生成索引 + nightly 接 `--check`）；
  批 1 后续任务派单要求"新建测试文件自带标签块"。

**处置（2026-09-27 批 3 分诊，repair 流）**：**不为它接 CI 门**。本条的验收不变量原写"CI（nightly）真实执行 `--check`"，但缩减批 A1 明列**删"测试标签块/生成式索引门"**（docstring 的重复登记簿，典型织网件）⇒ 现在接上去等于先把要删的门修好。保留本仓既有的**本地习惯**（改测试后跑 `--write`/`--check` 保持索引同步，如批 2 各任务收尾所做），**不接入 CI**。索引门本体的去留归缩减批。

**处置（2026-09-28 缩减批 6a，repair/m3-shrink6a）**：**销项（随门整族裁）**。索引门与生成器（`scripts/test_index.py` + `docs/dev/test-index.tsv`）整族删除，`--write`/`--check` 纪律随之作废；同族一起裁的还有模块体积门（`tests/test_module_size_gate.py`）与计数钉子。MF-110 不再有"--check 全绿"的验收不变量——门没了，验收随之作废。

### MF-111 引号 dict 键形态的凭据值不被兜底遮罩（低-中）
`{"access_token": "a b"}` 这类 JSON 式引号键值原样穿过 `sanitize_text`——引号键规则
（`yiban/masking.py` 的引号配对支）刻意只覆盖 `password`/`phone_code` 两条，其余凭据键的
引号键形态不在值域轴内。来源：批 2 Task 2-2 实现自报（登记 MF-4 修的是**值形态轴**
（空格/逗号截断），本条是**键形态轴**，故未随 MF-4 扩）。证据：构造输入实测（审查复核中）。
处置时点：批 2 随净化器收口（与 MF-4/8 同族，需给"不误伤 JSON 正文"判据）。
**同源残余面（Task 2-2 审查补充）**：`Authorization: Basic ZGVmOg==` 的 base64 尾巴留白
（`authorization=*** ZGVmOg==`，新旧行为一致、既有）——与 MF-4 自报残余 `token=a b==` 同根：
含 `=` 的后续段被判为新 key=value 对。两条并一条脱敏口径处理（键形态轴 + 含等号段判定）。

**处置（2026-09-27 批2 Task2-9b，repair/m3-batch2，`489af60`）**：本条为批 2 期间（Task 2-2）
登记、批内即修——develop 侧登记时原为"未处置登记"，修法落点即 `489af60`。引号 dict 键形态
（`{"access_token": "a b"}`）单独立规：值取**配对同种引号串并保留引号**（合法 JSON 打码后仍
合法——登记要求的"不误伤 JSON 正文"判据），不配对待裸值整段吞（宁过遮不漏），JSON 原子值
（数字/true/false/null）不参与；`Authorization: Basic` 的 base64 尾巴与 MF-4 自报残余
`token=a b==` 同根并一条口径（含等号段判定并入键形态轴规则），尾巴不再留白。反例入
`test_masking_tokens.py` 族（键形态 + 原子值不误伤对照）。

### MF-112 `test_ledger_check.py::test_unexpected_exception_message_is_sanitized` 隔离运行必红（测试隔离缺陷，中）
该用例聚焦单跑必红：批 1 清库守卫的短路发生在该用例注入点之前（依赖别处测试泄漏的
`YIBAN_DB` 环境才能在**全量**里转绿）——是"测试之间的隐式耦合"，非被测行为缺陷。
来源：批 2 Task 2-2 报告自报 + `git archive` 净 HEAD 复现（非批 2 引入）。处置时点：
批 2 测试基建小修（下个任务组带），修法=用例内显式建库/显式短路，禁赖环境泄漏。

**处置（2026-09-27 批2 Task2-5，repair/m3-batch2，`65c2f7e`）**：本条为批 2 期间（Task 2-2）
登记、批内即修——develop 侧登记时原为"未处置登记"，修法落点即 `65c2f7e`。按登记修法落"自建
前置条件"：用例内 `mock.patch.dict(os.environ)` 显式钉 `YIBAN_STATE_DIR`/`YIBAN_DB_FILE`/
`YIBAN_ENV_FILE` 三键到本用例临时路径（库由 `setUp` 真实建出，缺库守卫自然放行）——不绕过
守卫、不赖环境泄漏；修复前后证据：同命令单跑 FAILED→1 passed、整文件 12 passed、全量仍绿。
同族排查清单（登记要求的"一并排查"）：`test_unexpected_exception_exits_two` **同族且更阴**
（守卫短路也回 2 也带"无法定论"⇒ 聚焦跑"绿"但注入分支从未执行的空转绿），同改自建前置并补钉
`未预期异常` 字样证明确实走到兜底 except；其余 10 例无隐式依赖（9 例子进程面已显式构造 env、
1 例纯直连临时库）不动。残余：无（该文件进程内用例的前置条件已全量显式化）。

### MF-113 随机标识形状与子串判据互相咬：审计作用域 id 可掐出 11 位号段（测试基建缺陷，低-中）
来源：批 2 Task 2-7 任务书外发现（原修 MF-50 时全量偶发 1/3500 红）——审计 detail 里的
作用域 id 旧形 `web-<16hex>` 全数字连段概率 ≈(10/16)^16 可被 `tests/test_audit_chain.py:1104`
的 `1[3-9]\d{9}` 搜索掐出"裸手机号"连段（对抗串 `web-8138001380001234` 旧形命中、新形不命中）。
已修：生成器形状改 `%Y%m%d-%H%M%S` 切段（20000 抽样零 11 连段；旧时刻串 14 连段 → 新 0 连段）。
残余（备忘）：①`web/app.py:1533 WEB_VERSION` 仍为 14 位连段时刻串（2026 不命中手机判据，无当前
暴露）；②同类风险=任何随机 hex/数字标识都可能咬到其它 `re.search` 型子串判据。
处置时点：本批已随 2-7 修形状**并**入；形状契约测试已在案；WEB_VERSION 同族如需统一排入
批 3/4 顺手项。禁把"放松判据"当修法（判据是脱敏检出面，只改生成侧形状）。

**处置（2026-09-27 批2 Task2-7，repair/m3-batch2，`c50ce64`）**：本条为批 2 期间（Task 2-7
任务书外发现）登记、批内即修——develop 侧登记时原为"未处置登记"，修法落点即 `c50ce64`（随
MF-50 的 2-7 并入）。按登记裁定执行"只改生成侧、判据一分不松"：作用域 id 生成器从旧形
`web-<16hex>` 改 `%Y%m%d-%H%M%S` 切段形状——全数字连段概率 ≈(10/16)^16 归零，对抗串
（`web-8138001380001234`）旧形命中、新形不命中；20000 抽样零 11 连段、旧时刻串 14 连段 → 新
0 连段；形状契约测试入 `test_audit_chain.py` 族已在案。残余（备忘两条照登未扩）：`WEB_VERSION`
14 位时刻连段（2026 形态不命中手机判据、无当前暴露）与"随机 hex/数字标识 × `re.search` 子串
判据"同类风险，统一排批 3/4 顺手项。

**下一空号：MF-115**。

## 批 4 收尾期间新登记（MF-114）

### MF-114 全量套件在特定时钟下挂起：`test_supervisor_recursion_guard.py` 三个用例（测试基建缺陷，中）
来源：批 4 收尾时全量套件在 **79% 处挂起**（进程处于不可中断内核等待，SIGTERM/SIGABRT 均无效、
faulthandler 无法转储、`/proc/<pid>/wchan` 不可读）。已用 bisect 证明**基线 `dc83192` 同样挂起**
——**非批 1–4 引入**；本批对该文件的 `git diff` 为空。
初判成因：该文件三个用例调用 `runner.main` 走**真实时钟**，只有用时钟补丁的那个用例能过；三个用例
又缺 `STATE_DIR` 隔离，直接写真实 `/var/log/yiban`。挂起点疑在"补签轮/兜底常驻"路径上、与真实
时间绑定（故深夜才复现）。
处置时点：**先存登记，不入本批**（与批 1–4 无关，不阻塞交付）。修法方向：三个用例统一钉时钟
（或注入 `now`）+ 隔离 `STATE_DIR`/`LOG_FILE`，把"是否挂起"变确定性判据；另需定位 `runner.main`
里依赖真实时间的等待点。**排除该文件后全量套件 3631 passed / 6 skipped / 0 failed（本机默认 TZ）**。

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

**追记（2026-09-28 缩减批 6a）**：该项的"落地"计划**正式撤销**（缩减批 A1：路由门禁 manifest 数量地板属元门禁/流程门，从未落地、不再落地；semgrep 硬门方案随之不入库）。相关登记仅存历史。
| 11 | 桌面那份文档里的**明文备份口令** | 操作清单已给出（`DISPATCH-REMAINING.md` 末节 7 步，含"旧口令在保留期内不能丢"） | 涉及生产写操作，本流不代做，等你手动执行 |

## 覆盖面声明（别把这份当"查过了"）
L1 84 份报告覆盖划分表全部 86 个单元 ID（`R12ef`/`R13ij` 各合写两单元）；`web/static/vendor/**` 31,331 行第三方与 `web/static/css/**` 4,508 行样式**未审**，`tests/**` 属注释流范围；V1–V8 只覆盖被点名的 8 条高危；L2 覆盖任务书点名的 5 条链；**L3 只有 4/20 格有真 HTTP 全链路证据**（余下卡在假上游无故障注入能力，见派发 1）；S 只覆盖清单内测试项目。
**收尾轮（2026-09-25 下午）新增**：semgrep 11 规则 265 命中已逐条四类归类（`out/SEMGREP-TRIAGE.md`，闭合 265 = 真缺陷 20 / 已知面 63 / 误报 114 / 噪音 68），候选 61 条去重成 50 簇（`out/MF-CANDIDATES.md`），并派 **16 个独立裁决代理**逐条复现 ⇒ 新立 MF-68..103（36 条）。其中 **32 条经独立裁决**（裁决累计驳回 14 条主张，清单见该节末"本流否证追加"）、**4 条（MF-97/98/99/100）是单源取证未二次裁决**，另有 10 余簇未编号待复现（见上"未编号候选"）。
**仍未做**：跨用户并发压测、真实浏览器端到端、v3 开态实测、旧服务器、以及任何依赖读 `.env` 内容/`/etc/nginx` 全文的判定；`/etc/yiban/*` 内容与备份口令文件**一律未读**。

---

## 批 6c-1 登记（2026-09-29 · 台账单池化，基座 = sign_tasks）

**用户裁决**：选 A——单池基座改用 `sign_tasks`（四柱最全），删 `sign_claims` 代码路径；接受"生产从没跑过的流程切过来"的风险（放假窗口，漏签无后果）。双侧轨开关 `YIBAN_SCHEDULER_V3` 随之消失。

**本条顺带销项（原登记项）**
- **B 类分支错误（原 `--fallback` 硬编 v2/`sign_claims`）** → 已消：兜底常驻经 `workers.run_fallback_worker` 走**同一个** `executor_v3.run_executor_v3`（`claim_all=True` + `requeue_during_run=True`），`runner.py` 的 `--fallback` 早返回不再是一条独立的 v2 腿；v3 的 `sign_tasks.failed` 当日回炉口随之打通（原条目的两条前置已满足前一条）。
- **"两池互斥 / 灰度开关不进生产"** → 概念消失：不再是两池，双轨与开关已删。
- **C-19（v20 回填明文驻留无上界）** → 已消：新增 `queue_store.purge`（`day < cutoff`，复用与 `claims.purge` **同一**时钟跳变守卫）+ `db.purge_sign_tasks` + `cleanup.run_daily_cleanup` 调用；此前全仓唯一 `DELETE FROM sign_tasks` 是按 phone。

**降级 / 残留（登记，非本批验收）**
- **ND-6c1-1**：`sign_claims` 仍有 **3 处**触碰——监督收尸（空表恒 0 行）、账号级联 DELETE（本批前就已同时删 `sign_tasks`）、保留期清理 DELETE。均为**空表上的无害操作**，不产生新领取事实。严格"零写入"需连带删四柱（超时回收）的 v2 侧收尾用例，故按**最小安全侧**保留。
- **`round.run_queue_retry` 冻结保留**：已无生产调用点（定时/手动/兜底/容器/web 手动全走 `executor_v3`），但**未物理删**——删它会连带失去约 20 条非 `sign_claims` 语义（窗口/熔断/脱敏/暂停）的唯一覆盖入口；`scripts/signin.py` 兼容壳仍可裸名调它（会静默写冻结表、绕开新台账），已在函数与壳的 docstring 标"外部集成不得调用"。
- **`capacity_of` 的 `enabled` 缺省现恒为 v3 口径（323→3360）**：批 4 容量口径**逐值不变**的前提是四处生产调用点全部**显式传参**（`runner.py` / `web/services/capacity.py` / `web/routes/settings_api.py` / `yiban/cli.py`）；缺省值本身成了留给未来调用者的陷阱，建议后续去掉缺省（本批未做，避免改既有调用点语义）。
- **`executor_count`** 现无生产调用点（仅测试）。
- **`_attempt` 的 reclaim 保险**（`phone ∉ accounts` 绝不 `_finish`）是**冗余第二道防线**，无独立用例（变异证明：单独停它新用例仍绿；它被 `allowed_phones` 允许集兜住）。
- **`SLOT_WIDTH_META_KEY`** 全仓无生产读者（grep 结论）。

**升级面（部署必读）**：现网实测 `user_version=17`，`sign_tasks`/`egress_state` 在现网**不存在** ⇒ 升级后**首次启动会在生产库上真跑 v18→v20**（建表 + v19 加 epoch 列 + v20 回填惰性历史行，均幂等且均为 `vshard=-1`/`owner=backfill` 的**不被领取**行）。与以往"无需迁移直接使用"不同，**升级前先做备份**。CHANGELOG v0.5.0 已写明。

**门禁证据（控制器亲跑）**：全量 `3658 passed / 7 skipped / 0 failed`（默认 TZ，`-p no:randomly`）；`ruff check .` 0；四柱用例**一行未删**（`test_claims_fencing.py`/`test_claims_mutex.py`/`test_claims_reap_e2e.py` 原样通过）；独立审查两轮：**FAIL(1)** 已修并**复验 PASS**（`--only` 手动轮越界收尾别的账号 ⇒ 静默漏签；含 OBS-1 起跑回收收窄、OBS-3 E2E 同分片几何钉死，均带变异判别力证据）。

## 批 6c-2 登记（2026-09-29 · B3/§5 可裁面 + 分片定档 + 窃取改写裁撤）

**用户裁决**：ND-1 只裁 owner 改写、保留分片并入（生产实测 K=2）；ND-2 出口分片无可裁面；ND-3 定档 64、密度整形/槽宽压缩不动；ND-4 删 loadtest 族（mock_yiban 例外迁 `tests/fake_yiban_server.py`——它是两个生产 E2E 套件与发布门槛 §2④ smoke 的假上游，地貌表唯一一处误判已纠正）；ND-5 `ledger_check.py` 保留；ND-6 批 4 容量口径逐值不变。用户追加：C3 必须带恢复指引（已落 hrw docstring / README / scheduler-v3 §9 三处）；`YIBAN_VSHARDS` 覆盖键与 μσ/限速键 web 入口为后续批候选，本批不做。

**本批销项**
- **离线压测/容量工具族整族出仓**（8 脚本+README+cli `--measure`+家族测试 5 文件）；实测值改由部署者自行量取后录入 `YIBAN_CAPACITY_MEASURED`（语义不变）。删除前的容量基准实测留数：2000 用户/simulated 档，单执行体 4680 账号/窗口、建议 3120、硬件上限 K=12（证据归档 `D:\code\archive\m3-batch6c2-docs-20260929\capacity-benchmark-20260929\`）。**重要事实**：探针链在删除前已因 purge 护栏加固而不可运行（三次补丁才跑通）——死工具链，删除正当性充分。
- **虚分片多档选择器**（64/128/256 按规模换挡）→ 定档 64；σ 包络按 64 重算（365 天逐日仿真，非照抄）。对 <500 账号部署零行为变化（原规则本就走 64）。恢复指引含旧口径、选档公式 √(K(1−1/K)/V) 与必补测试三类。
- **`steal_shards` owner 改写** → 删；分片并入保留（K≥2 崩溃恢复的必要条件，`DeadShardMergeTakeoverTest` 直证「回收→并入→领到」）。生产行为变化仅：接管日志改报分片集（同日同执行体只播报一次）、接管窗口期（≤4 分钟）展示页归属列显示旧主。
- **YIBAN_SCHEDULER_V3 死键残余叙述**（scheduler-v3 五处）清零；万级算法设计文档出库归档。

**降级 / 残留（登记，非本批验收）**
- **`test_mock_fault_injection_knobs.py`（783 行）随族删除**：注入旋钮契约（waf/nonjson/login-shallow 专设断言）与 `YIBAN_E2E_ENGINE_LOOP` 引擎档位 E2E 覆盖归零；真实判据侧（waf.py/security.py/protocol.py）仍由 `test_login_protocol_shape`/`test_login_e2e_mock` 钉住 → **6c-3 对表显式记账**。
- **`capacity_of` 的 `enabled` 缺省陷阱**（沿 6c-1）：缺省恒 v3 口径，四处生产调用点已显式传参，缺省值本身是给未来调用者的坑。
- **`v_for` 的 `n_accounts` 形式参数**：ruff 现无 ARG 规则不告警；启用 ARG001 时需改名或豁免。
- **`test-inventory.md`** 为自述快照（2026-09-23），本批仅摘除已删文件行，全面刷新归 6c-3 对表。
- **`_TAKEN_OVER_PEERS` 同日"死→复活→再死"不重播接管日志**：文档已声明的取舍（日志去重键 (peer,day)），无功能影响。
- **`production-isolation-rehearsal-plan`（历史件）**引用的 mock 工具链已不存在，文首已加状态注记；真要执行该演练须先重建工具链或改写命令。

**升级面（部署必读）**：本批**零 schema 变更、零数据迁移**（与 6c-1 的"首启跑 v18→v20"叠加后仍只需那一次备份）。定档与窃取改写对现网（99 账号、K=2）的可见变化：无（定档前后同走 64）；接管日志文案变化；接管窗口期归属列短暂显示旧主。

**门禁证据（控制器亲跑/独立复跑）**：C2 全量 `3552 passed / 6 skipped / 0 failed`；C3 全量 `3552 / 6 / 0`（σ 首轮真红 1 例后修，具咬合力）；C4 全量 `3551 / 6 / 0`（净减 1 为用例合并）；审查修复后受影响面 127 passed + ruff 0；**收尾全量（含全部审查修复）`3551 passed / 6 skipped / 0 failed`（668s）**。**整批对抗审查**：FAIL(2)（均为"测试机基准"残留文案：实测 note、cli.md×2、README）已修（`d50fad6`）并复验；其余各面 PASS——热修改安全声明实证（`_plan_v` 落库值优先 + 三个安全网测试变异验证）、σ 包络 365 天逐日复现、四柱 SQL 未动、变异敏感性逐项咬红、geomap 无第二处同类误判。OBS-2（注入旋钮覆盖归零）转 6c-3 记账；OBS-3（整合报告缺失）已补 `task-6c2-report.md`。

## 批 6c-3 登记（2026-09-29 · 全仓测试对表精简）

**用户裁决**：终态按对表证据执行（~3,385 目标带），不硬凑 1,800–2,000——差距全部落在用户自定的保留红线内（四柱/已修 bug 回归执行器/安全脱敏/迁移/发布契约），OpenClaw test-audit 的价值标准（"optimize for confidence, not deletion count"）同样不支持硬凑。十条 ND 按控制器处置执行（其中 ND-8 改判：run.sh 凭据插值静态 2 条保留——它是该安全契约的唯一覆盖）。

**本批执行（4 提交，基线 7249635 → bdbf21c）**
- A（`d7d5a69`）静态/文档/元测试裁 48：doc-gate/kpi 源级断言/前端纯装配守卫 10 条/公开钥自证元测试等；js_modules 的 4 条 XSS/泄漏面保留。
- C（`1b03610`）附录 A 存活项：C-1 四出口同构扫并一条 subTest 参数化（底座统一为覆盖只增不减）、C-2 前端静态兜底删（node 真跑的 dropdown 留）；C-3/C-4 改判保留。
- B（`cbc98a5`）边界桩机制复制 campaign 合并：store 45+web 38 复制组 → 机制总账 + 各域行为测试全留；**变异验证两轮**（执行人 + 控制器独立抽查：`add_account` 错域转发恰好咬红 2 条台账）。
- D（`bdbf21c`）条件项合并 + 收尾：env 行模型/审计锚点重叠段（CRITICAL 主 owner 未动）、前端单一实现三处；test-inventory 全面刷新（口径改 pytest 收集并附复现命令）；review-fix-plan 21 条对账：**13 销账 / 8 立任务**（含 E2 附：`test_host_exit_semantics` 断 `_compute` 副本不经 `runner.main` 且与生产判定漂移）。

**终态数字**：收集 **3,557 → 3,414**（−143）；passed **3,551 → 3,408**；skip 恒 6。每批全量独立绿：3503→3499→3432→3408，0 failed；ruff 全程 0。控制器收尾全量 **3408 passed / 6 skipped / 0 failed**（665s）+ 变异抽查独立通过。

**残留 / 立任务（登记）**
- **拆分兼容层退役**（后续批候选）：`signin` 壳有 3 处生产消费方（web/app.py:86、accounts_data.py:42、executor_env.py:32），store/db.py 读写转发在役——boundary 两文件 297 条钉的是在役兼容层非 test-only 缝；退役属生产重构（A4 性质），非测试裁剪。
- `test_host_exit_semantics` 副本与生产判定漂移（E2 附任务）；`probe.py:189` 的 `YIBAN_ENV_FILE` 单点未走 `resolve_path`（E1+E7 收口点）；`test_gate_narrowing_e2e.py` 缺 `标签：` 行（inventory 兜底归类，待补）。
- geomap 自身算术不一致（A 层表头 −63~66 vs 逐条 ≈72）：终态以实收集数为准，已在报告登记。
- 两处前端覆盖缺口随源级守卫消失且无行为替身（rekey 依赖"页面加载 core.js"半边、egress 依赖"只拨开关不带空请求体"半边）——已在保留文件 docstring 注记，D-3 总账可顺路接住。
- `review-fix-plan-20260921.md` 本批以已跟踪文件入库（含对账结论）——与 must-fix-list 同批在批 6 收尾时一并移出仓库归档。

**门禁证据（执行人每批全量 + 控制器复跑）**：见上终态数字；B 批变异两轮独立咬红；ruff 全程 0；四柱四件套每批抽验全绿；skip 数 6 不变。

## 2026-09-30 生产热修与遗留（部署首晨事故）

**已修（server-web d4bfd6d，CI 绿，已部署）**
- `/my/calendar` 500：管理端渲染器签名收 `extra` 但函数体从未并入 `render_template`（用户端同构函数有合并）——部署首日 MF-45 模板消费面首次上生产即爆发；金标准只渲染用户端日历故 CI 拦不住。新增真渲染回归 + 变异验证（撤修复即复现生产 TypeError）。
- `sign_events` 整批丢失：v3 状态迁移/收尾事件 `attempt=None`，`sign_events.attempt` NOT NULL + 批量单事务 ⇒ 一行失败整批回滚，部署后每轮事件全丢（统计页空白）。事件留痕 None→0 + 持久层两处归 0 防线 + 3 例测试。

**立任务**
- **A：兜底执行体空转**：无台账行/终态跳过账号（今晨为 3 个 user_cancelled）被兜底每轮当作"有活"，按 `min(interval,5)` 连续扫描至窗口关闭（09-30 实测 119 轮 ×2 行日志）。同根问题：`workers.py` 兜底路径调 `run_executor_v3` 未传 `event_sink`，兜底处理的事件不落统计。修法方向：全 skip 结果视为空闲走整段间隔；兜底事件接 sink。属行为改动，走正常 SDD 小批，不热修。
- **B：热修回流 develop**：`hotfix/calendar-and-events`（75187f7）已推 origin+gitee；develop 本地有未推送的 66cebb8（前端批工作，刻意未动）——批 7 开分支时先 merge 该热修分支带入，再继续。

**数据说明**：09-30 的 sign_events 明细缺口不回补（按天统计该日空白为已知事实）；状态事实源（sign-daily / sign-state）完整，月历与补签不受影响。

**发布门槛账本**：09-30 晨轮（96/99 成功、0 失败，d963f39）计入签到主链路首次真实验证；生产现运行 d4bfd6d，"≥3 有效轮次跨 ≥2 天"自 10-01 起以 d4bfd6d 计（10-01、10-02 两晨）。
