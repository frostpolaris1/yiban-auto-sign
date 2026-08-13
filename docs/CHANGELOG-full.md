# 更新日志（详细版·内部）

> **本文件为内部详细版**：含技术实现细节、安全漏洞原理与攻击路径留档，仅限开发者/管理员查看，**不对外展示**。
> 网页端（页面底部「更新日志」弹窗）展示的是项目根 `CHANGELOG.md`（脱敏通俗版，适合普通用户阅读）。

## v0.13.2（2026-08-12）
- 用户管理 3 个表格「操作」列表头去掉 `text-right`（右对齐）——表头与数据行（⋯ 按钮 / 不可修改，均列内居中）统一居中，表格级对齐（识图确认偏差 ≤2px）
- 内置管理员行显示简化：`（内置管理员 · .env）` → `（主管理员）`——与 0.13.0 权限分级后的「主管理员」术语统一（用户可见文案仅此一处，其余为代码注释/错误消息）

## v0.13.1（2026-08-12）
- **表格结构统一修正**（用户反馈 0.13.0 方向搞反）：
  - 根因：0.13.0 只统一了内层容器（双层→单层），外层结构未动——账号管理 pending / 用户管理三组仍是「琥珀整卡 or 白卡 + p-4 md:p-6 padding + 标题行无边框」，与优秀案例（正常账号/待删除账号：无 padding、标题行 border-b 贴边、表格满宽）不一致
  - 修复：待处理账号、待审核用户、正式用户、空用户 4 组全部对齐 active/deleted 结构——外层卡片去 padding、标题行改 `px-4 py-3 border-b` 贴边、提示行改 `px-4 py-2 border-b`（对齐待删除组提示样式）
  - 搜索框 `md:mx-auto`（居中）→ `md:ml-auto`（靠右，6 处）
- **待审核类区分方案**（用户建议）：琥珀整卡 → 白色卡片 + 琥珀色标题栏（`bg-amber-50 dark:bg-amber-900/20` + 琥珀边框），文字 `text-amber-800`；不损失可见性（识图确认对比度良好）
- **待删除账号折叠**：标题栏新增 ▾/▸ 按钮（`#deleted-toggle-btn`），`state.deletedCollapsed` 状态，折叠隐藏表格容器（`#accounts-deleted-table`）+ 批量条（两处批量条 toggle 均兼容折叠态）
- **表头不换行**：35 处文字 th 加 `whitespace-nowrap`（脚本正则统一），用户管理「账号数」列 w-[8%]→w-[10%] 根治换行
- 验证：DOM 类断言（pending 白卡+琥珀栏/搜索 ml-auto/35 th nowrap/折叠 id）+ 折叠功能（▾→▸→▾ 表格显隐）+ 识图两轮（表头单行、琥珀栏可读、结构统一、无错位）
- 教训：结构统一必须对齐完整外层结构（卡片 padding/标题行边框/提示行），只改内层容器类会产生"视觉不一致"的假统一；正则改 HTML 前先 grep 确认所有变体

## v0.15.0（2026-08-13）
**综合修复大版本**（计划 `docs/plan-0.15.0-fixes.md`；4 个并行流 × worktree 独立分支修复，合并回 server-web 统一验证）：
- **流 1 签到脚本**（fix/0.15-signin，16 项）：
  - F1 高危：`_load_accounts_from_file` 排除 rejected（审核拒绝真正生效；`--only` 同源过滤）
  - A1 代理 URL 脱敏（urlsplit 去 userinfo）；A3 配置错误异常只带 phone 不落明文密码
  - A2 退出码语义：全 skip→exit 2，run.sh 写 SKIPPED（不再误标 SUCCESS 吞备份）
  - M1 first_round 无条件置 False（全成功路径间隔恢复生效）；M2 重试基线 = remaining + uniform(0,30)（不再可为 0）
  - A-M1 通知去重（仅最终放弃通知）；A-M4 Location .get + 可诊断错误
  - A-M5 WAF 关键词入风控类 + 全部 resp.json() 前先 is_waf_blocked
  - A-M6 **js2py 替换**：`_solve_ydclearance` 纯正则/字符串复刻易盾模板（oo 数组/变换 A/B/C），删除 eval_js 与导入（CVE-2024-28397 消除）；触发判定改 Set-Cookie/JS 特征
  - A-L1/L3/L4/L5/L8 + 功能 L3/L13（跨午夜状态文件日期、skip 不记 ✅）
- **流 2 Web 后端**（fix/0.15-web，18 项）：
  - F3 批量 unset_admin/delete 防呆（动态重算 admins + 至少保留 1 管理员）
  - M3 拒绝编辑已 deleted 账号（双路径）；M4 restore 校验一人一号（单/批量）；M5 api_signin 校验可签到账号
  - M8 改密失败锁定与登录一致（达阈值 429）；M9 软删除占用规则统一（admin 添加排除 deleted）
  - L1 超期按秒 + mtime 兜底；L2 排队排除 deleted；L4 setdefault 防 500；L5 move int 捕获；L6 识别码 `__clear__` 清空；L7 解析失败拒写保护；L10 管理员 mine 可见域；L12 计数/审核排除 deleted；L14 正式用户仅 active
  - 安全 M3 密码策略：下限 10 位两类字符（全路径）、管理员口令哈希存储（兼容过渡）、scrypt:65536:8:1
  - 安全 M1 安全头（nosniff/X-Frame-Options:DENY/CSP/Referrer-Policy）；M6 改密轮换 SECRET_KEY
- **流 3 前端与合规**（fix/0.15-frontend，10 项）：
  - F2 软删除死路：deleted 卡片「移除记录」按钮（用户页+管理员我的账号）+ 全 deleted 时表单仍显示
  - M6 编辑表单 required 动态移除；L6 识别码清除按钮交互；L16 calState 内存键改 phone（DOM id 仍索引键）；M5 签到下拉只列 active
  - 安全 M7 协议修正（密码存储事实描述 + 数据保留/删除权/未成年条款，同步 docs/user-agreement-prompt.md）
  - B1 .gitignore 加 `服务器访问信息.md`；B7 VENDOR.md 溯源（tailwind 3.4.17 + sha256/MiSans/md-render）；M4 部署文档（0600 权限/umask/备份策略）；L9/L8/L17 已知限制文档化
- **流 4 CI 与依赖**（fix/0.15-ci，6 项）：
  - B4 依赖下限：werkzeug>=3.1.3 / requests>=2.32.0 / urllib3>=2.2.2 / pycryptodome>=3.19.1 / flask>=3.1.0；删除 js2py（流 1 移除代码，本流删依赖）
  - B6 requirements.lock（25 包精确锁定）+ CI 改装 lock + pip-audit 扫描 + Dependabot（pip + github-actions）
  - A-M7 keepalive 固定 SHA f72ff1a + job 级最小权限；A-M8 signin job `contents: read`；删除 .gitee-ci.yml 死配置
- 合并：395a724（流2）→ f60fa21（流1）→ aa77a6a（流3）→ fbd4ae1（流4+冲突解决）；requirements.txt 冲突采用流 4 版本
- 验证：ruff/AST/node --check 全过；浏览器回归——明文兼容登录、CSP 下页面正常、安全头生效（curl 实测）、列表脱敏、签到下拉只列 active、协议新条款、F2 端到端（软删除→用户页移除记录→表单重现）；测试数据已还原
- 遗留（已确认/文档化）：易班密码加密存储（后续专项）、TLS 反代部署（需环境变更）、历史泄露 filter-repo（需用户单独确认）、并发编辑乐观锁/索引寻址（文档化）

## v0.14.0（2026-08-13）
**安全+性能对抗性审查修复大版本**（审查报告 `docs/review-security-perf-2026-08-12.md`，两轮审查：S1-S12/T1-T10 安全 + P1-P19 性能 + R1-R2 规范，按 4 组修复）：

### 组 1 安全高危
- **S1** display_name JS 模板注入（存储型 XSS）：新增前端 `jsEscape()`（转义反引号/`\`/`${`），5 处 confirm/prompt 包裹（index.html:929/1009/1012/1051/1788）
- **T1** 批量 purge 绕过软删除：`api_accounts_batch` purge 分支加 `deleted` 状态检查（仅能彻底删除已软删除账号，与单删一致）
- **S2** 日志手机号明文：后端 `_mask_phone/_mask_email`（幂等）应用于 14 处 logger + `api_logs` 响应层（states 键 + 日志行内 `[手机号]` 脱敏，前端 maskPhone 幂等兼容）；sign.log 文件保持完整（parse_sign_log 解析依赖），仅展示层/响应层脱敏
- **S3+F1** 日历 DOM id 含手机号 + 重复 id：id 改用索引键（`cal-wrap-mine-{i}`/`cal-wrap-u-{i}`/`cal-grid-{key}`/`cal-log-{key}`），calShift/calLoadLog 增加 `data-key`，`data-phone` 仅作操作传参；顺带修复同一 card 双 cal-wrap 重复 id bug（P6）
- **S4** 用户管理邮箱脱敏：前端 `maskEmail`（abc***@example.com），列表显示 + title 均脱敏；data-email/checkbox 传参保留完整

### 组 2 安全中危
- **S5** API 传输层完整号（用户决策"最安全"）：`mask_account` 默认脱敏 phone/owner（网络层不再全量泄露）+ 新增 `/api/accounts/<idx>/detail`（masked=False，管理员守卫）——编辑表单（openForm 改 async 拉 detail）、行菜单手动签到（`doSigninByIndex`）、签到下拉（value 改 index + `doSigninFromSelect`）均按需取完整号；`accountMatch` 搜索兼容（输入完整号 mask 后匹配）；提交防 **** 入库（dataset.full 缺失时拒绝）
- **S6** 内置管理员 pw_version：登录记 `.env YIBAN_ADMIN_PW_VERSION`，改密递增，`_effective_role` 比对（改密后旧会话失效）
- **S7** 登录频率限制：60s 窗口 10 次/IP（`_login_rate`，比全局限速更严，防密码喷洒）；注册限速已有（S8 部分缓解，保留"已注册"提示）
- **S9** 临时密码明文：改为管理员填写初始密码（前端 `f-initial-password` 字段 + 后端 `initial_password` 校验），不再生成明文临时密码经 API 传输
- **T2** 手动签到 TOCTOU：`_signin_lock` 原子化检查+占位；**T3/T9** `_signin_procs` 字典终止旧进程 + 父进程关闭 log_fh；**T4** reject_reason 换行清洗（防日志伪造）；**T6** 公告后端 500 字限制；**T7** .gitignore 加 backup-*/；**L1** 子进程启动失败不暴露路径
- **F2** 名称输入 maxlength 20→50（与后端一致，手机号字段保留 20）；**F3** md-render.js esc 补单引号

### 组 3 性能
- **P4** accounts/users 内存缓存（TTL 5s + save 后直接更新缓存），解决 10s 轮询读盘与 P15（_effective_role 每请求读 users）
- **P1** 日历聚合读取：`os.scandir` 单次遍历替代每天一次 exists+open（30 次→1 次）
- **P2+P19** _my_account_view 单次 parse_sign_log + 排队数单次遍历累计（O(n²)→O(n)）
- **P3** 初始密码哈希移出 _file_lock（scrypt ~100ms 不再阻塞）
- **P5** 轮询条件渲染：accounts+states 快照（`_lastRenderSnap`）无变化不重建 DOM
- **P13** 6 个搜索框 150ms 防抖（debounceSearch）
- **P8/P16** changelog/公告内存缓存；**P18** 静态资源 Cache-Control max-age=86400
- P12 登录遍历经 users 缓存后为内存 O(n)（<1ms），保持现状

### 组 4 规范
- R1/R2：signin.py SIM110 any() + B904 raise from ×2（ruff 清零）
- F4 ICONS 死代码删除；F5 多余 </section> 删除；F7 主题按钮 aria-label + 日历加载失败提示
- F6 Tailwind 重复 dark: 类（影响极小，暂缓）、P17 大数组序列化（>500 账号时考虑分页，暂缓）

### 验证
- ruff / python ast / node --check 全过
- 浏览器回归：列表脱敏（138****8000）、状态图标匹配（states 脱敏键）、编辑表单 detail 拉取+未修改保存数据完整、签到下拉 value=index 显示脱敏、用户管理邮箱脱敏（zha***@example.com）、搜索完整手机号命中

### 遗留（已确认）
- 易班密码加密存储（用户确认留后续专项）；T5 js2py.eval_js RCE 面（ydclearance 反爬 JS 沙箱，需专项评估替换方案）；S10 TLS 部署（无 HTTPS 环境）；F6/P17 暂缓项

## v0.13.6（2026-08-12）
- 编辑账号弹窗手机号脱敏（接 0.13.5，用户要求"修改用户账号信息时也不显示手机号"）：
  - `openForm` 编辑回填 `maskPhone(a.phone)` 打码显示；完整号码存入 `dataset.full`（仅内存）
  - 表单提交：输入值含 `****` 且存在 dataset.full（= 未修改）→ 用完整号提交；手动改新号 → 直接提交新号（`****` 永不入库）
  - 手机号输入框下新增提示文案（仅编辑模式显示）："为保护隐私，手机号已打码显示；如需修改请填写完整新号码"
  - 「我的账号」页编辑（m-phone）不改——管理员自己的信息
- 验证：打码显示 + 未修改直接保存 → accounts.json 仍为完整原号；手动改 13512345678 → 保存生效；测试数据已还原

## v0.13.5（2026-08-12）
- 管理员视角手机号脱敏（用户要求"看用户信息时自动部分隐藏隐私，如 139****1234"）：
  - 新增前端 `maskPhone(p)`：11 位手机号 → 前 3 + `****` + 后 4；非 11 位原样返回
  - 脱敏位置：账号管理三个表格手机号列（pending/active/deleted 行模板）、操作弹窗文案（彻底删除/拒绝理由/通过确认/删除确认 4 处）、手动签到下拉显示文本
  - 保留完整：`data-phone` 属性（操作传参）、编辑表单回填（操作需要）、「我的账号」页与删除确认（管理员自己的信息）、搜索匹配（输入完整号可搜到，仅显示打码）
  - 教训：批量 replace_all `${esc(a.phone)}` 误伤了 4 处（data-phone/cal-wrap id×2/我的账号显示）——已逐个恢复；HTML 模板 replace_all 前必须 grep 确认所有出现位置的语义
- 验证：表格列 `138****8000`/`131****1000` 实测、下拉 `张三 (138****8000)` 实测、搜索完整号仍命中 1 行实测

## v0.13.4（2026-08-12）
- 批量多选改为会话级开关（用户确认"不记住，每次默认关"）：
  - 后端：`/api/settings` GET 不再读 `.env YIBAN_BATCH_MODE`（恒返回 `batch_mode: False`）；POST 不再写入该键（`write_env_key` 移除）；日志去掉批量操作字段
  - 前端：`loadSettings` 强制 `state.batchMode = false`；`toggleBatchMode` 切换后直接重绘表格（renderAccounts/renderUsers），不再调 `saveSettings`（不再持久化）；`saveSettings` body 移除 `batch_mode`
  - 设置页说明补充"默认关闭，开启仅本次会话有效，刷新后自动恢复关闭"；按钮显示逻辑不变（off→「开启」蓝 / on→「关闭」灰），默认关后自然显示「开启」
  - 遗留：生产/本地 `.env` 中的 `YIBAN_BATCH_MODE=on` 残留不再生效（代码已不读取），未清理以免误动配置
- 验证：默认进入 0 checkbox 列/0 批量条/按钮「开启」蓝 → 手动开启 6 列+6 条出现/按钮「关闭」灰 → 刷新后全部恢复默认（即使环境变量带 on 也无效）

## v0.13.3（2026-08-12）
- 更新日志弹窗 Markdown 渲染（用户反馈：md 格式只显示文字）：
  - 新建 `web/static/vendor/md-render.js`（迷你渲染器，零依赖，与 tailwind.js 同为本地化 vendor）
  - 支持语法：`#/##/###` 标题、`-` 列表、`**加粗**`、`` `行内代码` ``；先 HTML 转义再应用标记（防 XSS，日志内容来自服务器文件仍做防御）
  - 弹窗容器 `<pre>`（纯文本）→ `<div class="md-body">`（渲染 HTML），样式由 md-render.js 注入（标题/列表/加粗在 text-xs 容器内协调）
  - index.html 与 login.html 同步三处改造（引入脚本 / openChangelog 用 innerHTML / 容器改 div）；openChangelog 保留 `window.renderMarkdown` 存在性兜底（脚本加载失败时降级纯文本）
- **CHANGELOG.md 粒度回退**（用户反馈"最好还是保留小版本更新内容"）：恢复 v0.13.x/v0.12.x 等逐小版本记录（基于 git 历史 4dca365 版恢复），条目内容仍按用户视角原则书写；此前用户整理版（d01020a）把 0.13.1/0.13.2 合并进 v0.13.0、0.12.x 合并进 v0.12.0 的概述结构不再使用

## v0.13.0（2026-08-12）
- **管理员权限分级**（用户反馈：设置管理员应仅主管理员可用）：
  - 后端：`api_user_role` 与批量 `set_admin/unset_admin` 增加主管理员校验（session username == `.env` 内置管理员，否则 403"仅主管理员可修改管理员权限"）；`set_admin` 目标限定正式用户（有已生效账号且无待审核，否则 400）
  - 前端：`/api/me` 新增 `is_builtin_admin` 字段；`state.isMasterAdmin` 控制——单行操作栏「设为/取消管理员」仅主管理员 + 正式用户组（usersNormal）显示；正式用户批量条管理员按钮加 `batch-admin-only` 类（非主管理员 init 时隐藏）；待审核/空用户批量条移除管理员按钮（静态删除）
  - 其余功能（重置密码/清空账号/删除用户、批量重置密码/删除）所有管理员保留，不变
  - 验证：API 双视角（主管理员设正式用户 200/设待审核空用户 400/批量跳过；普通管理员 role API 与批量 set_admin 均 403、重置密码 200）+ 浏览器双视角（主管理员菜单含设为管理员、普通管理员菜单不含、批量条按钮隐藏）全部通过
- **表格样式统一**（用户反馈：操作栏方角 vs 表格圆角割裂）：
  - 排查结论：账号管理三组中 pending 是双层卡片（外层卡片 + 内层圆角表格卡片），active/deleted 是单层（裸滚动容器直接嵌外层卡片）；用户管理三组（usersPending/usersNormal/usersVacant）均为双层——同一页面两种容器风格本身即不一致
  - 修复：采纳用户建议"简化待处理账号使其一致"——4 处双层容器类去掉 `bg-white dark:bg-zinc-800 rounded-xl border` 前缀，统一为裸滚动容器，批量条与表格同处一个外层圆角卡片内，割裂消除
  - 全站排查：签到日志（面板一体）、系统设置（无表格）、我的账号（卡片式）、user.html（卡片式）均无同类问题
- 教训：HTML 结构修改脚本用 `s[:m.end(1)]` 拼接会截断尾部文本——正则替换必须 `s[:m.start()] + group + s[m.end():]` 完整保留剩余内容（本会话脚本断言失败未写入，未造成损坏）

## v0.12.6（2026-08-12）
- checkbox 列留白修正（用户反馈 0.12.5 贴左缘过近）：`pl-1 pr-2 w-8 text-left` → `px-4 py-3 w-12`（6 处 th + 4 处 JS 渲染 td 模板）
  - padding 与其余列完全一致（px-4 py-3 表格级留白）；移除 text-left，checkbox 继承 tr 级 text-center 居中，与列内文字对齐规则统一
  - `w-8`→`w-12`：列宽 32→48px，保证 px-4×2 + 16px checkbox 完整容纳、居中无偏移
  - 像素级验证（image-recognizer 子代理实测）：卡片左边框 x≈287.5、checkbox 左缘 x≈330（距左 ≈42px，含 16px padding + 居中偏移），其他列 px-4 一致；此前 pl-1 仅 4px
- 教训：checkbox 列此前反复在贴左/偏远间摆动——标准答案是与数据列同一 padding（px-4 py-3）+ 列内居中，不要另设特殊值

## v0.12.5（2026-08-12）
- 批量开关着色与文案语义统一：on→灰（显示"关闭"）、off→蓝（显示"开启"）
- 整体间距/对齐审查修复（代码审查 12 项 + 识图 7 项）：
  - **高**：批量条 DOM 错位修复——active 表 batch-bar 原嵌标题行内、usersNormal 原夹在标题与计数间且一次修复误移出 `</html>`（已移回正确独立行，父=卡片容器实测确认）；统计卡 padding p-3 md:p-4 → p-4
  - **中**：checkbox 列 td/th 左距 `px-2` → `pl-1 pr-2`（贴左缘）；空状态 py 统一 py-8（py-6/py-10→py-8）；标题 mb-1→mb-2 统一
  - **低**：登录/注册按钮补 text-sm + 注册统一主色蓝；公告条重复 px-4 清理（mb-5 md:mb-4 保留）；侧边栏 brand py-5→py-4
- 验证：浏览器 DOM（两批量条父=卡片、表格 4/5 行渲染）+ JS 语法 + 静态
- 教训：python 正则修复 HTML 时 `s[m.end():]` 若 end 位置取错会把内容追加到文件末尾——每次 HTML 结构修改后必须浏览器实测验证 DOM 父级

## v0.12.4（2026-08-12）
- 批量操作防误触精简（用户反馈折行与风险）：
  - 移除批量「彻底删除」（purge）按钮——pending/active/deleted 三个批量条全部去除；active 批量改「删除」（新 API action `delete`=软删除，进入待删除列表可恢复，与单个删除语义一致）；deleted 批量剩「恢复」
  - 批量「通过」加 confirm（拒绝已有 prompt 理由输入=二次验证；彻底删除单个/批量已有 confirm）
  - 批量条内容精简后手机端同行显示（通过/拒绝/取消选择、删除/取消选择、恢复/取消选择）
- 状态图例二字化（双端）：⏳待签/✅成功/❌失败/🔄重试/➖跳过——手机端不再折行
- 移动端折行排查：图例（已修）、批量条（flex-wrap+精简）、统计卡 2x2、表格横滑为既有设计；其余无风险
- 验证：批量 delete API（软删 2→待删除→恢复）✓ + 手机端识图（图例一行/批量条同行/checkbox 靠左/无溢出）✓
- 开发流程：feature/batch-safe-legend 分支

## v0.12.3（2026-08-12）
- 批量开关按钮文案反转（显示点击后的动作：on→"关闭"/off→"开启"）
- 批量操作条**常驻**（batchMode 开启时始终显示"已选 N 项"），显示由 renderAccounts/renderUsers 的 batchMode 切换控制（与 .batch-col 同处）；updateBatchBar 只更新计数；未勾选点操作 toast 提示
- 搜索框居中：标题行去 justify-between + 搜索框 `w-full md:w-56 md:mx-auto`（手机端整行宽居中自然，桌面端 224px 居中）；手机端批量条 flex-wrap（内容多换行整齐）
- 复选框列 `text-left`（td/th 靠左，紧贴表格左缘）
- 内置管理员行操作列去 text-right（与账号页操作列统一居中）
- 验证：PC DOM（开关"关闭"/条常驻 count=0/搜索 ml=mr=170.8/checkbox left/内置行 class 无 text-right）+ 手机（wrap/全宽 309px）+ 识图（搜索框全宽/批量条整齐/checkbox 靠左/居中；表格横滑滚动条为既有设计）
- 开发流程：feature/batch-bar-optimize 分支（用户要求不用主分支）

## v0.12.2（2026-08-12）
- **表格级居中对齐**（用户人工校验确认）：6 个表格 table 级 `text-center` + 操作列 `justify-center` + 表头 th 去 `text-right`；同时修复此前插入批量复选框时误删的 **thead tr class**（text-center/text-xs/bg-zinc-50/sticky top-0 恢复）——表头背景与吸顶回归
- **邮箱/用户名长度限制**：EMAIL_RE 收紧为 `[\w.+-]{1,32}@...`（用户名部分 ≤32，常量 EMAIL_USER_MAX）；api_register/api_account_add 前置"用户名部分过长"错误；api_users_batch 校验 email ≤64；login.html 前端正则同步 + 提示文案
- **显示截断**：userRow 邮箱 td、内置管理员行、owner_display 3 处加 `max-w-[220px/160px] truncate` + `title`（悬停完整邮箱）；存量超长邮箱不受注册限制影响，仅显示截断
- 验证：API 5/5（33 字符拒/32 字符过/绑定拒/批量拒/存量在）+ 浏览器（truncate class + title 生效）+ 识图（居中+表头背景+无错位）
- 流程规范：本次开发在 feature/table-center-email-limit 分支完成（用户要求不使用主分支开发）

## v0.12.1（2026-08-12）
- 修复批量操作 4 个 bug（用户实测反馈）：
  1. **key is not defined**：patch 脚本误把 cb 声明插入 renderAccounts 统计循环（batchSel[key] 引用未定义 key）→ 渲染崩溃 → toast 报错；且首次修复脚本在保存前崩溃（f-string 错误）导致删除未落盘，二次修复并验证
  2. **列错位**：accountRow/pendingAccountRow 的 onchange 中 `a.index` 缺 `${}`（事件触发时 a 不可见 → ReferenceError）；userRow 的 `'+esc(u.email)+'` 为字面量（选中失效）
  3. **用户管理严重错位**：内置管理员行 5 td vs 表头 6 列（batchMode 开）→ 补 batchMode 时空 td
  4. 全选修复（0.12.0 已修 toggleSelectAll 分组，Node 模拟验证 4/1）
- 完整用户视角测试：API 回归 26/26（软删/恢复/彻底删/批量/权限/CSRF/恶意输入/7 天清理）+ 浏览器（key 报错消失、勾选/取消、列对齐 batchMode 开/关、内置行、普通用户编辑取消/密码收起）+ 识图视觉确认
- 教训：**bash 内联 python + f-string 拼接 JS 模板字符串极易出错**（转义/崩溃不保存），复杂字符串修改必须用脚本文件 + 保存后立即验证

## v0.12.0（2026-08-12）
- **软删除撤销**：accounts.json 账号新增 `deleted`/`deleted_at`；`DELETED_RETENTION_DAYS=7`；`load_accounts()` 惰性清理超期项（锁内写回）；管理员 DELETE 改软删除、新增 `restore`/`purge` API；signin.py 过滤 deleted（不参与签到）；单账号限制排除 deleted（用户可重新提交）；用户端显示「已删除」状态徽章（隐藏编辑/删除）；用户自删仍物理删除；管理端「待删除账号」分组（搜索+限高+恢复/彻底删除）
- **批量多选**：`.env YIBAN_BATCH_MODE=on` 开关（设置页持久化）；`POST /api/accounts/batch`（approve/reject/purge/restore，reject 需共同理由）；`POST /api/users/batch`（set_admin/unset_admin/reset_password/delete，内置管理员跳过+防呆）；前端 6 表复选框列（batchMode 时渲染，行+表头全选）+ 批量操作条（选中数/按表定制按钮）
- **设置页**：服务器时间与签到窗口卡片置顶（第一位），批量操作开关卡片第二
- 已知问题：toggleSelectAll 初版误全选全部账号（应只选当前组）已修复；IAB 交互不稳定（locator/dom_cua 反复超时），浏览器侧以 DOM 断言+API 断言为准
- 验证：API 全流程（软删/恢复/彻底删/批量 approve/批量设管理员+重置密码/用户端 deleted 状态+重提交）✓；浏览器（复选框渲染 6 表头+23 行、批量条出现、待删除组 3 行、设置页顺序+开关"开启"）✓

## v0.11.1（2026-08-12）
- 修复普通用户页 PC 端日历/日志双栏过窄：根因 cal-wrap `max-w-sm`(384px) 锁死容器，`lg:grid-cols-2` 两列各 184px 致格子 22px、日志窄条换行；改 `max-w-sm lg:max-w-none` 后容器 781px、两列各 383px、格子 51px（实测）；index.html 管理端「我的账号」同根因一并修
- 账号卡片操作区 `items-start` → `items-center`（与左侧三行信息垂直居中）
- 编辑表单新增「取消」按钮（user.html cancel-edit-btn + cancelEdit()；index.html mine-cancel-btn + cancelMineEdit()，均 reset 后重渲染收起表单）
- 修改密码默认收起点击展开（user.html togglePassword / index.html toggleMyPassword，▸/▾ 箭头 + aria-expanded）；注意 classList.toggle 返回值是 hidden 状态，箭头逻辑按 hidden 判定
- 手机端图例 gap-x-3→gap-x-2 微调
- 验证：node --check 通过；浏览器实测双端全项（密码收起循环 ▸→▾→▸、编辑取消出现/隐藏、管理端同款）；识图复验 PC 修复生效

## v0.11.0（2026-08-12）
- 品牌 logo 替换：用户提供 Q 版角色图（544×544 RGBA 透明底，无文字）→ web/static/vendor/logo.png；替换 index.html 侧边栏品牌区（蓝色方块 SVG → img，w-12 h-12=48px）与 user.html 顶栏（w-9 h-9=36px）
- 教训：32px 缩放下角色细节全糊（识图读作"像素猫"）——放大到 48px 后角色可辨；验证时命中 Windows 端口共享坑（旧进程服务旧模板，需杀全部 LISTENING PID 重启）
- 仅页面 logo，favicon 未动（用户选择）

## v0.10.4（2026-08-12）
- 修复管理员页公告横幅与下方标题间距过近（移动端反馈）：公告条 `mb-3`(12px) → `mb-5 md:mb-4`（移动端 20px / 桌面端 16px）；computed margin 实测 390px 视口 20px、1280px 视口 16px
- 注：user.html / login.html 公告条同为 mb-3 结构（静态/非 sticky），用户未反馈，暂未调整

## v0.10.3（2026-08-12）
- 修复 MiSans 渲染偏黑：根因是 npm 包字重编号（330/380/450，MiSans 体系）与 CSS 请求（400/500/600）错位——浏览器把 400 匹配到 380（Medium 字形）、500 匹配到 450（Demibold 字形），全站偏粗。修正 misans.css 映射：330→Regular 文件、380 档改声明 500 且 src 换 Regular 文件（细字形）、450 档改声明 600（Demibold 保留标题粗体）；校验字重分布 330/500/600 各 100 face
- 修复低分屏模糊：woff2 无 hinting，Windows 桌面等低分屏渲染模糊——新增 `@media (max-resolution: 1.5dppx)` 回退系统字体（微软雅黑 ClearType 优化），高分屏（手机/Retina）保持 MiSans；body 补 -webkit-font-smoothing/-moz-osx-font-smoothing
- 验证：静态断言（字重分布/文件名映射）+ IAB DOM 快照（页面加载 + misans.css link 存在）；evaluate 被 IAB 只读限制拒绝

## v0.10.2（2026-08-12）
- UI 字体升级 MiSans（npm misans@4.1.0 子集化分片方案，OFL 免费商用）：
  - 本地化 web/static/vendor/fonts/misans/（369 个 unicode-range 分片 woff2 + 合并 misans.css 297 条 @font-face，6.9MB 入库，浏览器按需加载）
  - 字重匹配：Regular(330→400 档)/Medium(500)/Demibold(600) 对应页面 font-normal/medium/semibold；font-display:swap 无空白期
  - body 字体栈 "MiSans" 第一优先（3 模板）；font-mono 标识符（手机号/邮箱/文件名/时间）保留等宽
  - 三模板 <head> 引用 /static/vendor/fonts/misans/misans.css
  - 验证：静态断言（css 200/引用无缺失/字体栈替换）+ IAB 不可用（webview not ready）留待部署后浏览器确认
- .gitignore 补 demo-log/（本地调试目录，此前遗漏）

## v0.10.1（2026-08-12）
- 修复字体不一致：正式用户表内置管理员行的用户名缺 font-mono（普通用户邮箱列已用），补 class 后全站同类标识符（邮箱/手机号/文件名/时间区间）字体统一；computed style 验证 font-family 等宽栈 + 14px 与邮箱列一致

## v0.10.0（2026-08-12）
- 管理端 5 个表格（待处理账号/正常账号/待审核用户/正式用户/空用户）统一增强：
  - 限高：滚动容器 `max-h-[420px] overflow-y-auto` + thead `sticky top-0 z-10`（表头吸顶）
  - 搜索：每表标题栏右侧搜索框，实时过滤（账号表按名称/手机号/用户名，用户表按邮箱），搜索词内存态（10s 轮询刷新后过滤保持），无结果显示「无匹配结果」
  - 人数：补全「正常账号」「正式用户」计数徽章；搜索态显示「X 个匹配 / 共 Y 个」
  - 数据流：renderAccounts/renderUsers 过滤渲染；loadUsers 缓存 allUsers/builtinAdminName 供搜索事件复用

## v0.9.7（2026-08-12）
- [安全] 修复内存无界增长：IP 计数表（限速/登录失败/注册）增加条目上限（10000），公网扫描器无法耗尽服务器内存
- [安全] 重置/修改密码后旧登录会话自动失效（users.json 新增 pw_version，改密递增；旧数据无该字段不校验，兼容存量会话；内置管理员 .env 无版本机制，已知限制）
- 修复设置接口对非法输入报 500 的问题，延迟秒数限制在 0~3600
- 大日志文件改为从尾部读取（_tail_lines 最多 2MB），避免每次请求整文件载入内存
- 日志接口不再返回服务器上的完整日志路径（仅文件名）
- 账号名称/设备型号/识别码增加长度上限（50/50/128）；日期参数校验改为全匹配正则
- 文件写入增加落盘同步（fsync），极端断电场景不丢数据
- 内置管理员显示名保留 .env 原始大小写（_builtin_admin_display 与 _builtin_admin_email 分离）

## v0.9.6（2026-08-12）
- [安全] 修复并发竞态：账号提交/注册/管理操作的「检查+写入」加操作级锁（_file_lock 改 RLock，13 个 handler 包住完整序列），杜绝绕过单账号限制与并发覆盖丢数据
- [安全] 登录失败锁定改为按「IP+用户名」组合计数（_login_fails 键升级为三元组）：同一网络下的用户不再因他人爆破尝试被连带锁定；攻击者多用户名爆破的余量由登录告警兜底

## v0.9.5（2026-08-12）
- [安全] 修复存储型 XSS：手机号未校验格式且未转义拼接进前端按钮事件（攻击者可提交恶意手机号，管理员打开账号管理页即被注入脚本、以管理员身份操作）；修复=PHONE_RE 格式校验（1 开头 11 位）+ 前端动态值全部改 data 属性 + 事件处理从 dataset 读取（accountRowMenu/userRowMenu/calShift/calLoadLog）
- [安全] 修复登录 CSRF：登录/注册接口增加同源校验（_is_same_origin 检查 Origin 头），防止受害者被跨站登录进攻击者账号后泄露易班账号密码
- [安全] 前端 esc() 用于 JS 字符串上下文的隐患消除（userRow 邮箱改 data-email/data-role）
- 代码规范：pyproject ignore 补 RUF001/RUF002/RUF003（中文全角标点误报）；清理基线遗留 E741×2/SIM105×1（基线 ruff 434 错 → 全绿）

## v0.9.4（2026-08-12）
- 修复手机端公告横幅与顶部「易班签到管理」标题栏重叠：滚动时公告横幅固定在标题栏正下方，不再被遮住（sticky top-14 md:top-0）

## v0.9.3（2026-08-12）
- 全项目代码格式统一（ruff format），回归测试全部通过

## v0.9.2（2026-08-12）
- 修复 TUI 终端面板保存随机延迟时可能报错的问题（write_env_int 原子写误粘贴 bug）
- 引入代码规范检查配置（ruff）

## v0.9.1（2026-08-12）
- 修复管理端页面卡死问题（v1.7.6 误删 modal-account 与 </main>）
- 修复移动端无法滚动（touchmove 绝对坐标误用）
- 日历状态改为数字着色，修复竖屏遮挡
- 周日点击显示「无需签到」
- 日志查询速度优化（倒序扫描）
- 用户页版本号位置归位、提交区整体隐藏、公告栏优化
- 账号管理「归属」列改为「用户名」

## v0.9.0（2026-08-11）
- 平板电脑界面优化：页面滚动更稳定
- 签到日志页优化：自动显示最新记录，页面高度适配各种设备
- 公告横幅优化：滚动固定显示、样式对齐
- 签到日历优化：格子更紧凑，手机和电脑上都能完整显示整月
- 系统设置：随机延迟修改后自动保存
- 操作按钮优化：改为「⋯」菜单，界面更整洁、防止误触
- 新增：点击页面底部版本号可查看更新日志

## v0.8.0（2026-08-11）
- 新增签到日历：每账号月历视图，四态颜色（成功/失败/待签/休息）+ 点击查看当日日志
- 日历星期从周一开始
- 用户页布局优化：签到情况置顶，其他功能按使用频率排序
- 邮箱前缀修复

## v0.7.0（2026-08-11）
- 所有用户自助修改密码（账号/邮箱不可改）
- 代码审查第 1 梯队修复：依赖/日志/导入/原子写

## v0.6.0（2026-08-11）
- 手机端适配：表格保持桌面宽度横向滑动、框标题恒定、随机延迟框响应式
- 表格对齐优化：统一表格容器样式与列宽
- 修复账号表头缺「审核」列导致的幽灵空白列
- 修复表格行 hover 高亮左灰右白问题

## v0.5.1（2026-08-10）
- 账号列表纳入 10 秒轮询：用户重新提交后管理员页自动同步审核状态
- 角色实时判定——提权/降权立即生效，旧会话权限同步失效

## v0.5.0（2026-08-10）
- 多管理员支持
- 用户管理 + 管理员我的账号
- 管理页面禁用缓存（防浏览器缓存旧 JS 导致无限刷新循环）
- 登录无限刷新循环根因修复（JS classList.toggle 误改双类名）
- 登录失败告警

## v0.4.0（2026-08-10）
- 账号管理两分组（待处理置顶 + 拒绝带理由可重填）
- 用户管理三分组
- 用户端拒绝显示
- 注册页用户协议勾选
- 用户协议正式文本上线（完整八节）
- 添加账号绑定用户下拉（列出已注册无账号用户）
- 后台添加账号支持用户邮箱（自动注册用户并进入待审核）
- 全局公告（所有页面顶部显示）
- 签到日志脱敏（技术细节降 DEBUG，结果行去组件名）
- 注册限速 + 全局限速（应对恶意攻击）
- CSRF 防护（校验失败自动恢复：前端 403 时自动重拉 token 重试）
- 暗色主题切换

## v0.3.0（2026-08-10）
- 普通用户排队机制（我的账号显示前方排队人数）
- 用户页改为访问时静态加载（去掉 15s 轮询，减少服务器请求）
- 页面版本号系统上线（登录页/后台/用户页底部显示）
- 多管理员防呆：内置管理员（.env）不计入「至少保留 1 个」校验

## v0.2.0（2026-08-09 ~ 2026-08-10）
- 服务器网页管理系统——Flask 后台替代 TUI
- 邮箱注册登录
- 管理员审核用户提交的账号
- 用户管理界面（待审核/正式用户分组）

## v0.1.0（2026-08-09 ~ 2026-08-12）
- 手动签到功能（M 键子进程执行，日志与 cron 同路径）
- 队列重试机制（失败账号放队尾分散重试，风控类≤2次/其他≤4次）
- 随机延迟选项（启动延迟 + 账号间隔，TUI 设置栏可调）
- e003 登录风控根因修复（KillYiBan 同款登录设为默认）
- TUI 签到状态组件（⏳未到/🔔窗口/✅已过/🌙周日）
- TUI 全面定制（左右布局/名称栏/状态图标/序号排序/Tokyo Night 配色）
- TUI 配置工具（Textual 表单式输入，生成 accounts.json）
- 账号配置支持单账号完整输入 + 并发控制
- 服务器安全加固脚本（hardening/）
- GitHub Actions 双平台 CI（Gitee Go 备份）
- 设备绑定校验
- WAF 风控拦截识别与重试
- README 文档体系
