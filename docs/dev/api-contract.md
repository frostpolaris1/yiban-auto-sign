# Web API 契约（前端对接唯一依据）

> 本文是前后端解耦后的**交付物**。前端按本文开发，不必读后端源码。
> 底本：`origin/develop` tip `30629a12`。生成方式：逐个读 `web/routes/*.py` 的视图体
> 与 `web/app.py` 的闸门/中间件，**不是**按命名推断。
> 机械对账：`tests/test_api_contract_coverage.py` 把本文端点与
> `create_app().url_map` 逐条比对。少一条、多一条、或本文解析出 0 条，都判红。
> 变更纪律见 §3。

## §0 阅读指引

前端只需要先掌握三件事。

1. **怎么带会话**：登录后浏览器持有会话 Cookie（名 `yiban_admin`，`HttpOnly`，
   `SameSite=Lax`）。后续所有请求同源自动携带，前端**不要**读它、也不要手动设
   `Authorization`。前端只需在登录成功后记住 `role`（`admin` / `user`）。
2. **CSRF 怎么处理**：除少数豁免端点外，所有 `POST`/`PUT`/`DELETE` 必须带
   `X-CSRF-Token` 请求头。Token 从 `GET /api/me` 的 `csrf_token` 字段取。详见 §1.2。
3. **错误长什么样**：失败一律 `{"error": "<中文文案>"}`（部分端点另带 `reason`）。
   HTTP 状态码语义见 §1.3。前端只应展示 `error` 文案，不应解析它。

`未核` 标记：本文凡标 `未核` 的字段表示未能从实现中确认，前端不得依赖。

## §1 跨域约定（只写一次）

### §1.1 认证层级

后端有四档，按"谁能通过"从宽到严排列。每档对普通用户与主管理员的判定口径不同。

| 档位 | 判定处 | 说明 |
| --- | --- | --- |
| 公开 | `web/app.py` 的 `require_login` 豁免表 | 无需会话 |
| 任意已登录 | `require_login` 的普通用户放行表 | 普通用户或管理员均可 |
| 管理员 | `require_login` 默认分支（`role == "admin"`） | 注册管理员与主管理员均可 |
| 主管理员 | 视图内 `_is_builtin_admin_session()` | 仅 `.env` 内置管理员（`auth_source == "builtin"`） |

- **公开**端点（`require_login` 明确豁免）：
  `POST /api/login`、`POST /api/register`、`POST /api/me/restore`、`POST /api/setup/admin`；
  `GET /api/announcement`、`GET /api/changelog`、`GET /api/registration_paused`。
- **任意已登录**放行面（普通用户可用的全部路径）：前缀 `/api/my-`、
  `/api/verify-jobs`，以及精确路径 `/api/clock`、`/api/me`、`/api/logout`、
  `/api/me/password`、`/api/me/delete`、`/api/stream/my-signin`。
  其余 `/api/*` 对普通用户返回 403，并写一条 `forbidden_path` 审计。
- **主管理员**是"管理员"的严格子集。`role == "admin"` 的注册管理员**不能**通过
  主管理员档，得 403（文案各自不同，见各端点）。
- 未登录访问任一非公开 `/api/*` → 401 `{"error": "未登录"}`。
- 会话绝对期：自登录起 `YIBAN_SESSION_ABS_DAYS` 天（默认 7，钳制 1~30）后失效，
  需重新登录。滑动续期为 14 天。
- 服务端吊销：登录/改密/登出会换发 `sid`。旧 `sid` 的 Cookie 副本随即失效。

### §1.2 CSRF

- **要 CSRF 的方法**：`POST` / `PUT` / `DELETE`，且路径以 `/api/` 开头。
- **豁免（改用同源校验）**：`POST /api/login`、`POST /api/register`、
  `POST /api/me/restore`、`POST /api/setup/admin`。这四个端点校验 `Origin` 头与 `(scheme, host)` 同源；
  无 `Origin` 头放行（同站导航、curl）。跨站即 403
  `{"error": "请求来源异常，请刷新页面后重试"}`。
- **要 CSRF 的取法与带回**：`GET /api/me` 响应里的 `csrf_token`（64 位 hex）。
  请求头名 `X-CSRF-Token`，值与 `session["csrf_token"]` 恒定时间比对。
- **CSRF 失败**：403 `{"error": "请求校验失败，请刷新页面后重试"}`。
- **GET 不受 CSRF 影响**，但两条 SSE 端点（`GET /api/stream/*`）额外做同源校验，
  因为 `EventSource` 不能自定义请求头。跨站即 403。

### §1.3 错误响应形态与状态码语义

- 成功：`2xx`，体为 JSON 对象（`jsonify`），除非端点另有说明（导出为文件流）。
- 失败：`4xx` / `5xx`，体为 JSON 对象，**必有** `error` 键（中文文案）。
  部分端点另带 `reason`（机器可判的短标识）。
- `/api/*` 的错误响应一律 JSON。页面路径的错误响应是 HTML（见 §2.11）。

| 状态码 | 语义 | 典型 `error` |
| --- | --- | --- |
| 400 | 请求内容不合法（参数错、取值错、格式错） | 视端点 |
| 401 | 未登录 | `未登录` |
| 403 | 已登录但无权限；或跨站/CSRF 失败；或口令门未过 | `无权限` / `请求校验失败，请刷新页面后重试` / 视端点 |
| 404 | 资源不存在 | `未知接口` / `账号不存在` / `用户不存在` |
| 405 | 方法不允许 | `方法不允许` |
| 409 | 状态冲突（需人工处理 / 需刷新） | `账号列表已变化，请刷新页面后重试` / 见 §1.7 |
| 413 | 请求体过大（上限 64 KiB） | `请求体过大` |
| 429 | 触发限速或冷却 | 视端点，均含"频繁"或"冷却" |
| 500 | 服务器内部错误 | `服务器内部错误，请稍后重试` |
| 502 | 上游（易班）失败 | `实测失败：…`（仅耗时实测端点） |
| 503 | 依赖繁忙，稍后重试 | `校验繁忙，请稍后重试`（带 `Retry-After: 2`） |

- 全局 `/api/*` 限速：窗口 10 秒。写请求与匿名请求每 IP 上限 60 次；
  **已登录 GET** 走独立放宽桶，每 IP 上限 240 次。超限 429
  `{"error": "请求过于频繁，请稍后再试"}`。
- 请求体上限 64 KiB（`MAX_CONTENT_LENGTH`）。非 JSON 或非对象的 JSON 体 → 400，
  `error` 为 `请求体不是合法 JSON` 或 `请求体应为 JSON 对象`。

### §1.4 脱敏口径（前端不得尝试还原）

后端在出站前已遮罩下列字段。前端**原样展示**，不得再遮一次（会变形），
也不得尝试还原。

| 数据 | 遮罩形态 | 例 |
| --- | --- | --- |
| 手机号 | `前3位 + "****" + 后4位` | `138****8000` |
| 邮箱 | `本地部前3字符 + "***" + "@域名"` | `abc***@example.com` |
| 归属展示名（`owner_display`） | 本地部为号形态 → 手机号遮罩；否则 `前3字符 + "***"` | `138****0000` / `adm***` |
| 账号口令 | **从不下发**，只给布尔 `has_password` | — |
| 设备识别码 | **原文从不下发**，只给布尔 `has_phone_code` 与展示用 `phone_code_masked`（`前4…后4`） | `abcd…wxyz` |
| 代理串（出口） | 只回 `scheme://host[:port]`，去掉 `user:pass@` | `http://1.2.3.4:8080` |
| 执行体身份 | 只回公开标签（角色 + 槽位序号），不回主机名/进程号 | `worker-1` / `fallback` / `single` / `unknown` |
| 审计 `actor` | 邮箱遮罩；非邮箱原样（`admin` / `system`） | `abc***@example.com` |
| 邮件配置 `smtps[].user`、`admin_to` | 地址打码 | — |
| 推送密钥 | **从不下发**，只给 `has_secret` 之类的布尔 | — |

遮罩是幂等的：已含 `*` 的串原样返回。

**设备识别码为什么给掩码而不是继续"零下发"**：识别码是设备指纹，红线是**不伪造**它
（签到回传的只能是账号行里那个值，或平台登记的原文）。展示面此前只有布尔，用户在页面上
看不出"这条账号到底绑了哪台设备"，而签到被上游拒时无从自查。首尾各 4 位的掩码够用户
**认出**自己的设备，中间 56 位不可见、不可逆推，既不伪造指纹也不扩大暴露面（真正要读
指纹的路径仍只有签到提交那一处，且回传值取自账号行）。原文仍**只**在 `GET /api/accounts`
与 `GET /api/my-accounts` 之外的零处出现——本表所列是唯一出站口径。

### §1.5 分页

- 参数：`limit`、`offset`（查询串）。**两者都不传时不分页**，返回整表
  （与分页前逐字相同，向后兼容）。
- 传任一即分页：`limit` 夹到 `[1, 200]`，缺省 100；`offset` 夹到 `>= 0`，缺省 0。
  参数存在但不是十进制整数时回落缺省（不报错）。
- 分页时响应**增补**四键元数据：`total`（整表条数）、`limit`、`offset`、
  `returned`（本页实际条数）。不传参数时这四键**不出现**。
- 分组与检索（仅 `GET /api/accounts`、`GET /api/users`）：
  - `group`：账号取 `pending|active|deleted`，用户取 `pending|normal|vacant`；未知值回 400。
  - `q`：最多 64 字符，服务端按**遮罩后的可见字段**检索。手机号支持 `138****8000` 形态；
    用户同时匹配完整邮箱、归属展示名与遮罩邮箱（`abc***@example.com`）。
  - 传 `group` 或分页参数时，响应增补 `group_totals`（三组全局总数）；账号列表另补
    `stats`（全部正常账号的今日 KPI，不随当前页收窄）。`total` 是该组经 `q` 过滤后的行数。
  - 只传 `group` 不传 `limit`/`offset` 时返回整组（未分页），服务端会解密整组；
    千级以上规模应同时带分页参数，避免一次解密过多行。
- 条件请求（仅上述两条列表）：成功响应带 `ETag`、`Cache-Control: private, no-cache`、
  `Vary: Cookie`；`If-None-Match` 命中（含 `W/` 弱标签）时回 `304` 空体。前端只在能处理
  `notModified` 的列表请求上启用，计数类请求（`count_only=1`）不启用。
- 例外：`GET /api/logs` 的总数键名沿用既有 `total_lines`，分页时只补
  `limit` / `offset` 两键。
- 例外：`GET /api/audit-logs` 用**显式分页** `page` / `page_size`，信封见 §2.7。
- 稳定排序键：
  - `GET /api/accounts`：库内顺序（即 `sort_order`），下标是整表绝对下标。
  - `GET /api/users`：库内顺序。
  - `GET /api/users/deleted`：`deleted_at` 升序。
  - `GET /api/logs`：日志文件行序（时间序）。

### §1.6 SSE

- 端点：`GET /api/stream/progress`（主管理员）、`GET /api/stream/my-signin`
  （任意已登录）。
- 响应 `Content-Type: text/event-stream`；带 `Cache-Control: no-cache` 与
  `X-Accel-Buffering: no`。
- 事件类型全集（5 类）：`snapshot`、`progress`、`heartbeat`、`resync`、`end`。
- `progress` 事件 payload 字段：`id`、`ts`、`day`、`node`、`node_label`、
  `executor`、`executor_label`、`message`。**不含手机号**。
- `snapshot`（管理员流）字段：`totals`（各节点计数）、`by_executor`、`cursor`、
  `day` 等（口径见 §2.10）。`snapshot`（用户流）只给 `cursor`、`day`、`generated_ts`。
- `heartbeat` payload：`{"ts": …, "cursor": …}`。
- `end` payload：`{"reason": …}`。`reason` 取值：`disabled`、
  `too_many_connections`、`server_busy`、`idle_timeout`、`max_duration`、
  `session_expired`。
- `resync` payload：`{"reason": "cursor_invalid"|"gap_too_large", "cursor": …}`，
  其后必跟一个新的 `snapshot`。
- 续传：浏览器自动回带 `Last-Event-ID` 头。前端不必手写。
- 连接预算：心跳 15 秒；每会话上限 3；全局上限按流别分开（管理员流与用户流各
  默认 10）；空闲 300 秒（无 `progress`）关闭；会话复查 60 秒；单连接硬上限
  120 分钟。超预算时端点直接回一个 `end` 事件（不报 HTTP 错）。

### §1.7 内容协商、编码与时间格式

- 请求体一律 JSON（`Content-Type: application/json`）。
- 响应体一律 UTF-8 JSON，除非端点声明为文件流（`GET /api/logs/export` 是
  `text/plain` 附件）。
- 时间基准：**北京时间（UTC+8）**，库里存无时区 naive 串。
- 日期串格式：`YYYY-MM-DD`（业务日）。
- 时间戳串格式：`YYYY-MM-DD HH:MM:SS`（部分带 `.ffffff` 微秒）。
- `GET /api/clock` 另给 `server_ts`（epoch 秒）与 `tz_offset_min`（本地相对 UTC
  分钟偏移），供前端校准与平滑走秒。
- 一个特例：`POST /api/settings` 写入被 `EnvWriteRefused` 拒绝时返回 409，
  体为 `{"error": "<指引文案>", "reason": "env_write_refused", "problems": [...]}`。
  `problems` 是定位载荷：`{"kind": "line", "line": N, "snippet": "…"}` 或
  `{"kind": "key", "key": "…"}`。前端据此渲染"一键清理"入口（清理走
  `POST /api/settings/env-cleanup`）。

## §2 端点清单

按域分组。每条的"鉴权档"用 §1.1 的四档名。
`<int:idx>`、`<int:slot>`、`<int:user_id>`、`<int:index>`、`<int:job_id>`、
`<path:filename>` 是 Flask 路径转换器，前端填实际数字/串。

### §2.1 认证会话

#### `POST /api/login`

- 鉴权档：公开。CSRF：豁免，走同源校验。
- 请求体：`username`（string，必填）、`password`（string，必填）。
- 成功响应：`{"ok": true, "role": "admin"|"user"}`。
- 注销冷静期内：`{"ok": true, "recoverable": true, "msg": "账号已注销，7 天内可恢复"}`。
  此态**不建立会话**，前端应引导走 `POST /api/me/restore`。
- 主要错误：400 无；401 `用户名或密码错误`；429 `登录尝试过于频繁，请稍后再试`
  （每 IP 60 秒 10 次）或 `密码错误次数过多，已锁定 N 分钟`（同 IP+用户名连续
  10 次失败锁 60 秒）。
- 副作用：成功写 `login_ok` 审计；重建会话并签发 `sid`（内置管理员写 `.env` 的
  `YIBAN_ADMIN_SID`，注册用户写 `users.sid`）。
- 不变量：用户名比对统一小写。登录失败计数按 `(IP, 用户名小写)` 组合。

#### `POST /api/setup/admin`

- 鉴权档：公开（仅当 `.env` 尚未配置管理员时可用）。CSRF：豁免，走同源校验。
- 请求体：`username`（string，必填）、`password`（string，必填）。
- 成功响应：`{"ok": true, "role": "admin"}`，并直接建立内置管理员会话。
- 主要错误：400 账号格式或口令策略不符；409 `管理员已配置，请直接登录`；
  500 `写入 .env 失败，请检查文件权限后重试` 或写入后自检失败。
- 副作用：写 `YIBAN_ADMIN_USER`、`YIBAN_ADMIN_PASSWORD_HASH`、
  `YIBAN_ADMIN_PW_VERSION=1` 与 `YIBAN_ADMIN_SID`；删除明文
  `YIBAN_ADMIN_PASSWORD`；写 `admin_setup` 审计。
- 不变量：写文件与二次判定在同一把跨进程锁内；明文口令不落盘；并发首配只有
  一个请求成功。

#### `POST /api/register`

- 鉴权档：公开。CSRF：豁免，走同源校验。
- 请求体：`email`（string，必填）、`password`（string，必填）、
  `agree`（**必须为 JSON 布尔 `true`**，否则 400）。
- 成功响应：`{"ok": true}`。
- 主要错误：400 `请先阅读并同意《用户协议》和《隐私政策》` /
  `请输入有效的邮箱地址` / `该邮箱已注册` / 密码策略不符；403
  `注册已暂停，请联系管理员添加账号` 或 `注册人数已达上限，请联系管理员`；429
  `注册过于频繁，请稍后再试`（每 IP 600 秒 5 次成功注册）。
- 副作用：创建 `role=user` 用户，与 `user_register` 审计同事务。
- 不变量：邮箱唯一；内置管理员邮箱被保留；注销冷静期邮箱不可注册（文案与
  "已注册"一致）。

#### `POST /api/logout`

- 鉴权档：任意已登录。CSRF：**要**。
- 请求体：无（可空）。
- 成功响应：`{"ok": true}`。
- 副作用：写 `logout_ok` 审计；轮换 `sid`（登出即吊销被盗 Cookie 副本）；
  清空会话。
- 不变量：轮换失败只记日志，仍回 `{"ok": true}`。

#### `POST /api/me/restore`

- 鉴权档：公开（冷静期用户无会话）。CSRF：豁免，走同源校验。
- 请求体：`email`（string，必填）、`password`（string，必填）。
- 成功响应：`{"ok": true, "role": "admin"|"user"}`（**恢复即登录**，直接建立会话）。
- 主要错误：400 `邮箱和密码为必填项` 或 `邮箱或密码错误，或账号已过恢复期`；409
  `该邮箱已被注册，无法恢复`；429 `操作过于频繁，请稍后再试` 或
  `尝试过于频繁，请稍后再试` 或 `密码错误次数过多，请稍后再试`；500
  `恢复失败，请稍后再试`。
- 副作用：恢复用户与其易班账号；签发 `sid` 并落库；写 `user_self_delete_restore`
  与 `login_ok` 审计。
- 不变量：冷却计数按 `kind="restore"` 分流（每邮箱 60 秒 1 次、每 IP 60 秒 5 次）。
  "账号不存在/已过期"与"密码错误"文案统一，防探测。

#### `GET /api/me`

- 鉴权档：任意已登录。CSRF：不适用（GET）。
- 查询参数：无。
- 成功响应：`{"ok": true, "auth": bool, "role": "admin"|"user"|null,
  "username": string, "email": string, "admin": bool, "mail_notify": bool,
  "is_builtin_admin": bool, "csrf_token": string, "sign_order": string,
  "sign_dist": string, "time_pref_allowed": bool, "sign_window": "HH:MM ~ HH:MM"}`。
  - `admin` 是旧前端兼容字段（等价 `role == "admin"`）。
  - `email` 等于 `username`（普通用户顶部显示用）。
  - `sign_order` 取值 `sequence` / `random`；`sign_dist` 取值 `uniform` /
    `normal` / `front`。
  - `time_pref_allowed` = `.env` 的 `YIBAN_ALLOW_TIME_PREF == 1`。
  - `sign_window` 是**有效窗口**（已按前后裁剪收敛；吃空时回退默认窗口）。
- 主要错误：401。
- 副作用：惰性生成并写入会话的 `csrf_token`（唯一下发点）。

#### `POST /api/me/password`

- 鉴权档：任意已登录。CSRF：**要**。
- 请求体：`old_password`（string）、`new_password`（string）、
  `confirm_password`（string）。三者必填（不一致即 400）。
- 成功响应：`{"ok": true, "msg": "密码已更新，下次登录使用新密码"}`。
- 主要错误：400 `两次输入的新密码不一致` / `新密码不符合要求：…` /
  `当前密码不正确`；404 `用户不存在`；429 `尝试次数过多，请稍后再试` 或
  `密码错误次数过多，请稍后再试`。
- 副作用：内置管理员写 `.env`（`YIBAN_ADMIN_PASSWORD_HASH` 并清空明文、递增
  `YIBAN_ADMIN_PW_VERSION`、换发 `YIBAN_ADMIN_SID`）；注册用户写 `users` 表并递增
  `pw_version`、轮换 `sid`。两者都写审计并发告警邮件。
- 不变量：**改密后当前会话随版本号失效**（内置管理员分支），需重新登录。
  注册用户分支的当前会话保持有效（同步 `session["sid"]`）。
  口令策略：内置管理员走 12 位三类提档；注册用户走通用策略（≥10 位多类）。

#### `POST /api/me/delete`

- 鉴权档：任意已登录。CSRF：**要**。
- 请求体：`password`（string，必填）。
- 成功响应：`{"ok": true, "msg": "账号已注销，7 天内可撤销"}`。
- 主要错误：400 `请输入当前密码` / `当前密码不正确` /
  `当前账号不可注销` / `当前账号不可注销（系统最后一个管理员）`；404
  `用户不存在`；429 `操作过于频繁，请稍后再试`（每邮箱 60 秒 1 次、每 IP 60 秒
  5 次）或 `密码错误次数过多，请稍后再试`；500 `注销失败，请稍后再试`。
- 副作用：软删除（7 天宽限），清除其账号与时间偏好；写
  `user_self_delete_request` 与 `user_self_delete_confirm` 审计；清空会话。
- 不变量：**防 IDOR**——只从会话取当前用户，请求体任何目标参数一律忽略。
  内置管理员不可注销；最后一个注册管理员不可注销。

### §2.2 我的账号（普通用户自助）

#### `GET /api/my-accounts`

- 鉴权档：任意已登录。CSRF：不适用。
- 查询参数：无。
- 成功响应：`{"ok": true, "accounts": [AccountView…]}`。
- `AccountView` 字段：`index`（本人视图内下标）、`name`、`display_name`、
  `phone`（**已遮罩**）、`phone_model`、`phone_code_masked`（**已遮罩**，识别码原文
  不下发）、`status`（`active`/`pending`/`rejected`）、
  `reject_reason`、`state_icon`、`state_status`、`state_message`、`queue_ahead`、
  `logs`（本账号今日最近 5 行，已脱敏）、`deleted`、`deleted_at`、
  `deleted_by_me`、`user_paused`、`pause_forbidden`。
- 主要错误：401。
- 不变量：普通用户看到本人归属的全部账号（含待删除，用于展示"已删除"）；
  管理员看到归属本人邮箱的未删除账号。`queue_ahead` = 本人账号之前今日尚未了结的
  已生效账号数。

#### `POST /api/my-accounts`

- 鉴权档：任意已登录。CSRF：**要**。
- 请求体：`name`（string，可空，≤50 字）、`phone`（string，必填，`1` 开头 11 位）、
  `password`（string，必填）。**设备型号与识别码不接受**（发了也丢弃）。
- 成功响应：`{"ok": true, "msg": "已提交，等待管理员审核后参与签到"}`。
  开启异步验证时**增补** `job_id`（int）与 `status`（`"verifying"`）；
  队列繁忙时 `status` 为 `"verify_deferred"` 并改写 `msg`。
- 主要错误：400 `每个用户只能提交一个账号，可编辑或删除后重新提交` /
  `手机号 <号> 已被使用` / 校验失败文案；403 `账号数量已达上限，请联系管理员`；
  429 `账号提交过于频繁，请稍后再试` / `账号验证尝试过于频繁，请稍后再试` /
  验证冷却文案；503 `校验繁忙，请稍后重试`（带 `Retry-After: 2`）。
- 副作用：写 `pending` 账号（`owner` = 本人邮箱）；与 `my_account_add` 审计同事务；
  发"新账号申请待审核"通知。
- 不变量：一律回待审核（不按角色分叉出"管理员提交即生效"）。每人限 1 个未删除账号。

#### `PUT /api/my-accounts/<int:idx>`

- 鉴权档：任意已登录。CSRF：**要**。
- 请求体：`name`、`phone`、`password`（留空=保留），可带 `_snapshot`（JSON 字符串，
  含 `phone`，用于防错位）。设备字段被丢弃。
- 成功响应：`{"ok": true, "msg": "已保存"}`；改绑手机号或原状态为 rejected 时
  `msg` 为 `"已重新提交，等待管理员审核"`。
- 主要错误：400 `账号已删除，请先恢复` / `手机号 <号> 已被使用` / 校验失败；
  404 `账号不存在`；409 `账号列表已变化，请刷新页面后重试`。
- 副作用：更新账号；改绑则清旧号时间偏好；写 `my_account_update` 审计；
  口令变更后异步起校验探针。
- 不变量：改绑手机号**一律回待审核**；仅改口令/识别码保持原状态。
  软删除账号禁止编辑。

#### `DELETE /api/my-accounts/<int:idx>`

- 鉴权档：任意已登录。CSRF：**要**。
- 请求体：可带 `phone`（防错位）。
- 成功响应：`{"ok": true, "msg": "已删除，7 天内可在本页撤销恢复，超期自动清除"}`。
- 主要错误：400 `该账号已在待删除状态，可在本页撤销恢复`；404 `账号不存在`；
  409 `账号列表已变化，请刷新页面后重试`。
- 副作用：软删除（`deleted_by` = 本人）；写 `my_account_delete` 审计；发本人邮件。

#### `POST /api/my-accounts/<int:idx>/restore`

- 鉴权档：任意已登录。CSRF：**要**。
- 请求体：可带 `phone`（防错位）。
- 成功响应：`{"ok": true, "msg": "已恢复「<名>」", "accounts": [AccountView…]}`。
- 主要错误：400 `该账号不在待删除状态` /
  `你已有生效账号，无法恢复（每人限 1 个）`；403
  `该账号由管理员删除，如需恢复请联系管理员`；404 `账号不存在`；409
  `账号列表已变化，请刷新页面后重试`。
- 副作用：撤销软删除；写 `my_account_restore` 审计。
- 不变量：仅"本人自删"（`deleted_by` = 本人）可自行恢复。

#### `PUT /api/my-accounts/<int:idx>/pause`

- 鉴权档：任意已登录。CSRF：**要**。
- 请求体：`paused`（`1`/`true`/`on`/`yes` 为暂停，其余为恢复），可带 `phone`。
- 成功响应：`{"ok": true, "msg": "已暂停签到，主程序将自动跳过"|"已恢复签到",
  "paused": bool}`。
- 主要错误：400 `账号已删除，请先恢复`；403 `管理员账号不支持自暂停`；404
  `账号不存在`；409 `账号列表已变化，请刷新页面后重试`；429
  `操作过于频繁，请稍后再试`（仅"暂停"方向计冷却）。
- 副作用：写 `my_account_pause` / `my_account_resume` 审计。

#### `POST /api/my-accounts/<int:idx>/device-refresh`

- 鉴权档：任意已登录（数据范围由归属邮箱收窄）。CSRF：**要**。
- 请求体：`phone`（**必填**，防错位；缺失或不匹配 → 409，且不发起登录）。
- 成功响应：`{"ok": true, "msg": "已读取服务端登记设备并保存",
  "device": {"phone_model": string, "phone_code_masked": string,
  "authorized": bool, "persisted": true}}`。
- 主要错误：400 `账号已删除，请先恢复` / `账号未生效（待审核或已拒绝），请先通过审核再补全设备`；
  404 `账号不存在`；409 `账号列表已变化，请刷新页面后重试`；429
  `账号验证尝试过于频繁，请稍后再试` / 验证冷却文案；502 `未取到设备登记：…`；503
  `校验繁忙，请稍后重试`（带 `Retry-After: 2`）；500 `已读取设备登记，但写入账号失败，请稍后重试`。
- 副作用：**再铸一次会话**（登录，读 `getState` 需要会话）＋ 成功时写账号行的
  `phone_code` / `phone_model`；写 `my_account_device_refresh` 审计。
- 不变量：**读不到一律不改库**——库里既有识别码/机型不得被空值冲掉；识别码原文
  在响应里**从不出现**（只给 `phone_code_masked`）；只有会签到的账号（未软删、审核态生效）
  才可补全。`reason` 为 `not_registered` / `partial` 时 HTTP 仍为 200（上游正常应答但
  没有完整设备对），`ok` 为 `false`。

#### `GET /api/my-time-pref`

- 鉴权档：任意已登录。CSRF：不适用。
- 查询参数：无。
- 成功响应：`{"ok": true, "pref": "HH:MM"|null, "pref_slot": int|null,
  "slots": [{"slot_min": int, "label": "HH:MM", "pct": int, "disabled": bool,
  "edge_note": string}], "allowed": bool, "window": "HH:MM ~ HH:MM",
  "edge_sec": int, "edge_front_sec": int, "edge_back_sec": int,
  "has_account": bool, "estimated": string|null, "estimate_note": string}`。
  - `pct` 是**粗粒度 10% 档**（满员恰好 100），刻意不下发真实人数。
  - `disabled` = 该片完全落入裁剪区（灰显不可选）；`edge_note` 非空表示部分裁剪。
  - `estimated` 读当日台账 `run_at`；无计划或读不通时 `estimate_note` 为"待生成"。
- 主要错误：401。
- 不变量：`window` 与 `slots` 都按**有效窗口**算。

#### `PUT /api/my-time-pref`

- 鉴权档：任意已登录。CSRF：**要**。
- 请求体：`slot_min`（int 或 `null`）。`null` = 清除自选。
- 成功响应：`{"ok": true, "msg": "已保存自选 HH:MM，今日生效|明日生效[，该时段已选满，将就近安排到附近时段]"}`；
  清除时 `msg` 为 `"已清除自选，恢复自动分配"`。
- 主要错误：400 `请先提交易班账号` / `账号审核通过后即可选择签到时间` /
  `时间片取值无效` / `所选时间片不在可选范围内，请重新选择`；429
  `切换过于频繁，请稍后再试`。
- 副作用：写 `time_pref_set` / `time_pref_clear` 审计。
- 不变量：`slot_min` 必须 5 的倍数、落在有效窗口内、且不整片落入裁剪区。
  bool 与小数被拒。冷却为弹性递增（60 秒窗口内前 20 次自由，超出后 30s→60s→…
  封顶 300s）。

#### `GET /api/my-calendar`

- 鉴权档：任意已登录。CSRF：不适用。
- 查询参数：`month`（`YYYY-MM`，必填；非法即 400）。
- 成功响应：`{"ok": true, "month": "YYYY-MM",
  "days": {"YYYY-MM-DD": {"<遮罩手机号>": "✅"|"❌"|""}}, "sunday_sign": 0|1,
  "saturday_sign": 0|1}`。
- 失败响应：读状态目录失败时 `{"ok": false, "error": "无法读取签到状态目录，请稍后重试或联系管理员"}`，
  状态码 500。
- 主要错误：400 `月份格式不正确，应为 YYYY-MM`。
- 不变量：只回显**已生效**（`active` 且未软删）账号的历史。日期键覆盖当月每一天。

#### `GET /api/my-logs`

- 鉴权档：任意已登录。CSRF：不适用。
- 查询参数：`date`（`YYYY-MM-DD`，可选；缺省取最近有日志的一天，优先今天）。
- 成功响应：`{"ok": true, "date": "YYYY-MM-DD", "logs": [string…]}`（最多 50 行，
  已脱敏）。
- 主要错误：400 `日期格式不正确，应为 YYYY-MM-DD`。
- 不变量：只回显已生效账号的日志。

#### `GET /api/my-mail-notify`

- 鉴权档：任意已登录。CSRF：不适用。
- 成功响应：`{"ok": true, "mail_notify": bool}`（未登录或查无此人时默认 `true`）。

#### `PUT /api/my-mail-notify`

- 鉴权档：任意已登录。CSRF：**要**。
- 请求体：`enabled`（**必须为 JSON 布尔**，否则 400）。
- 成功响应：`{"ok": true, "mail_notify": bool}`。
- 主要错误：400 `取值无效`；401 `未登录`；404
  `当前账号不支持此设置（内置管理员请用管理员邮件配置）`。
- 副作用：写 `mail_notify` 审计；关闭时发本人确认邮件（绕过开关）。

#### `GET /api/verify-jobs/<int:job_id>`

- 鉴权档：任意已登录（仅本人或主管理员可见）。CSRF：不适用。
- 成功响应：`{"ok": true, "job": {"job_id": int, "status": string,
  "error": string, "phone": string（遮罩）, "created_at": string|null,
  "started_at": string|null, "finished_at": string|null}}`。
- 主要错误：403 `无权限`；404 `任务不存在`。
- 不变量：归属校验按"本人 + 内置主管理员"收窄（注册管理员不能读别人的任务）。

#### `DELETE /api/verify-jobs/<int:job_id>`

- 鉴权档：任意已登录（仅本人或主管理员）。CSRF：**要**。
- 成功响应：`{"ok": true, "job": {…}}`（同 GET 的 `job`）。
- 主要错误：403 `无权限`；404 `任务不存在`；409 `任务已结束，无法取消` 或
  `任务已开始执行，无法取消`。
- 副作用：先收口超龄 `running` 任务，再取消 `pending` 任务。

### §2.3 账号管理（管理员）

#### `GET /api/accounts`

- 鉴权档：管理员。CSRF：不适用。
- 查询参数：`group` / `q` / `limit` / `offset`（可选，见 §1.5）；`count_only`（`"1"` 时只回计数）。
- 成功响应（`count_only=1`）：`{"ok": true, "total": int, "pending": int,
  "rejected": int}`。
- 成功响应（默认）：`{"ok": true, "accounts": [{"index": int, "name": string,
  "phone": string（遮罩）, "phone_model": string, "has_password": bool,
  "has_phone_code": bool, "phone_code_masked": string（遮罩，识别码原文不下发）,
  "display_name": string, "owner": string（遮罩）,
  "owner_display": string, "status": string, "reject_reason": string,
  "user_paused": bool, "deleted": bool, "deleted_at": string,
  "time_pref": string|null, "time_pref_edge": "first"|"last"|null,
  "last_executor": object|null}], "states": {"<遮罩手机号>": "<状态码>"},
  "state_msgs": {"<遮罩手机号>": string}, "state_durs": {"<遮罩手机号>": number|null},
  "config_file": string}`。传 `group` 或分页时增补 `group_totals` 与 `stats`
  （`success|failed|waiting|skipped`，全部正常账号口径）；分页时另补四键元数据。
- 主要错误：401；403（普通用户）。
- 不变量：`index` 是**整表绝对下标**（分页只切窗口，不改下标基准）。前端用它拼
  行操作 URL。`states` / `state_msgs` / `state_durs` 的键是遮罩手机号。

#### `GET /api/accounts/<int:idx>/detail`

- 鉴权档：管理员。CSRF：不适用。
- 成功响应：`{"ok": true, "account": {…}}`，字段同 `GET /api/accounts` 的单行，
  但 `phone` 与 `owner` 是**完整值**（未遮罩）。**仍不含明文口令**（只有
  `has_password`）。
- 主要错误：404 `账号不存在`；429 `查看账号详情过于频繁，请稍后再试`
  （每会话 60 秒 60 次）。
- 副作用：聚合只读审计 `account_detail_read`；超限另写一行拒绝留痕。
- 不变量：这是列表外**唯一**回传完整手机号的面。`idx` 可 0 递增枚举，故有限速。

#### `POST /api/accounts/<int:idx>/device-refresh`

- 鉴权档：管理员。CSRF：**要**。
- 请求体：`phone`（**必填**，防错位，管理面 fail-closed；缺失或不匹配 → 409，且不发起登录）。
- 成功响应：`{"ok": true, "msg": "已读取服务端登记设备并保存",
  "device": {"phone_model": string, "phone_code_masked": string,
  "authorized": bool, "persisted": true}}`。
- 主要错误：400 `账号已删除，请先恢复` / `账号未生效（待审核或已拒绝），请先通过审核再补全设备`；
  404 `账号不存在`；409 `账号列表已变化，请刷新页面后重试`；429
  `账号验证尝试过于频繁，请稍后再试` / 验证冷却文案；502 `未取到设备登记：…`；503
  `校验繁忙，请稍后重试`（带 `Retry-After: 2`）；500 `已读取设备登记，但写入账号失败，请稍后重试`。
- 副作用：**再铸一次会话**（登录，读 `getState` 需要会话）＋ 成功时写账号行的
  `phone_code` / `phone_model`；写 `account_device_refresh` 审计。
- 不变量：同本人端点——**读不到一律不改库**；识别码原文在响应里从不出现；只有会签到的
  账号才可补全。本端点与账号验证**共用同一个外呼闸门**（全局席位 + 每用户配额）与
  **同一份**认证失败冷却账。

#### `POST /api/accounts`

- 鉴权档：管理员。CSRF：**要**。
- 请求体：`name`（≤50 字）、`phone`（必填）、`password`（必填）、
  `phone_model`（≤50 字）、`phone_code`（≤128 字，`__clear__` 为清空哨兵）、
  `email`（可选）、`initial_password`（`email` 未注册时必填，用于自动建号）。
- 成功响应：`{"ok": true, "msg": "已添加，等待审核通过后参与签到",
  "accounts": [AccountRow…]}`。异步验证时增补 `job_id` / `status`。
- 主要错误：400（手机号已存在 / 邮箱格式 / 邮箱用户名过长 / 归属用户已有账号 /
  `内置管理员邮箱不可注册` / 初始密码策略 / 域名审查）；403
  `账号数量已达上限（N），请联系管理员扩容` / `注册人数已达上限，请联系管理员`；
  429 / 503 同 §2.2 的验证类文案。
- 副作用：写账号（不填邮箱 = `owner=admin` 且直接 `active`；填邮箱 = `owner=邮箱`
  且 `pending`，邮箱未注册时连带建用户）；与 `account_add` 审计同事务。
- 不变量：`phone_code` 的 `__clear__` 是协议令牌，落库前折算成空串。

#### `PUT /api/accounts/<int:idx>`

- 鉴权档：管理员。CSRF：**要**。**高危口令门**：改写易班口令或改绑手机号时。
- 请求体：`name`、`phone`、`password`（留空=保留）、`phone_model`、`phone_code`、
  `_snapshot`（JSON 字符串，乐观锁快照）、以及高危门的 `confirm_password` /
  `confirm_delay_ack`。
- 成功响应：`{"ok": true, "accounts": [AccountRow…]}`。
- 主要错误：400（`账号已删除，请先恢复` / 手机号已存在 / 校验失败 / 高危门文案）；
  403 / 429（高危门未过，文案见 §1.3 与门禁表）；404 `账号不存在`；409
  `账号列表已变化，请刷新页面后重试` 或 `账号已被其他管理员修改，请刷新后重试`。
- 副作用：更新账号；改绑则清旧号时间偏好、回 `pending`；凭据变更时清熔断计数、
  发当事人邮件与管理员事后告警、异步起校验探针；与 `account_update` 审计同事务。
- 不变量：改绑手机号一律回待审核并清旧拒绝理由。只改/清识别码免口令门（但仍
  标审计位、发信）。防错位：管理面写口 `fail_closed=True`（不带 `phone` 即 409）。

#### `POST /api/accounts/batch`

- 鉴权档：管理员。CSRF：**要**。**高危门**：`action="purge"` 过统一高危门；
  `action="delete"` 只占删除额度（不过口令）。
- 请求体：`action`（`approve`|`reject`|`purge`|`restore`|`delete`，必填）、
  `ids`（int 数组，必填，≤10）、`phones`（字符串数组，**必填**且与 `ids` 等长，
  用于防错位）、`reason`（`reject` 必填，≤100 字）、高危门的
  `confirm_password` / `confirm_delay_ack`。
- 成功响应：`{"ok": true, "msg": "已<动作> N 个账号", "accounts": [AccountRow…]}`。
- 主要错误：400 `未知操作` / `请选择要操作的账号` / `单次批量操作最多 10 个账号` /
  `批量拒绝需要填写理由` / `账号「…」的归属用户已有生效账号，无法恢复（每人限 1 个）`；
  403 容量门文案；404 `所选账号不存在`；409
  `账号列表已变化，请刷新页面后重试`；429（`purge`/`delete` 超额度）；
  500 `批量操作失败，已全部回滚`。
- 副作用：整体事务（失败全回滚）；`reject` 每户发一封拒绝邮件；写 `account_batch`
  审计（含脱敏目标清单）。
- 不变量：**缺 `phones` / 长度不等 / 遮罩号不匹配 三者任一都 409**。`ids` 里
  JSON `true` 不算下标（严格 `type(i) is int`）。

#### `DELETE /api/accounts/<int:idx>`

- 鉴权档：管理员。CSRF：**要**。占删除额度（不过口令）。
- 请求体：`phone`（防错位，管理面 fail-closed）。
- 成功响应：`{"ok": true, "msg": "已删除「<名>」，N 天内可在待删除列表恢复",
  "accounts": [AccountRow…]}`。
- 主要错误：404 `账号不存在`；409 `账号列表已变化，请刷新页面后重试`；429
  `删除操作过于频繁，请稍后再试`。
- 副作用：软删除（`deleted_by="admin"`）；与 `account_delete` 审计同事务。

#### `POST /api/accounts/<int:idx>/restore`

- 鉴权档：管理员。CSRF：**要**。
- 请求体：`phone`（防错位，fail-closed）。
- 成功响应：`{"ok": true, "msg": "已恢复「<名>」", "accounts": [AccountRow…]}`。
- 主要错误：400 `该账号不在待删除状态` / `该用户已有生效账号，无法恢复（每人限 1 个）`；
  403 容量门；404；409。
- 副作用：撤销软删除；与 `account_restore` 审计同事务。

#### `POST /api/accounts/<int:idx>/purge`

- 鉴权档：管理员。CSRF：**要**。**高危口令门**（`irreversible=True`）。
- 请求体：`phone`（防错位）、`confirm_password`、`confirm_delay_ack`。
- 成功响应：`{"ok": true, "msg": "已彻底删除「<名>」", "accounts": [AccountRow…]}`。
- 主要错误：400 `该账号不在待删除状态` / 高危门文案；404；409；429
  `删除操作过于频繁，请稍后再试`。
- 副作用：物理清除；写 `account_purge` 审计。
- 不变量：只允许彻底删除**已软删除**的账号。

#### `POST /api/accounts/<int:idx>/review`

- 鉴权档：管理员。CSRF：**要**。
- 请求体：`action`（`approve`|`reject`，必填）、`reason`（`reject` 时用，≤100 字）、
  `phone`（防错位，fail-closed）。
- 成功响应：`{"ok": true, "msg": "已通过 <遮罩号>，将参与定时签到"}` 或
  `{"ok": true, "msg": "已拒绝，用户可查看理由并重新提交"}`。
- 主要错误：400 `该账号无需审核` / `该账号无需拒绝` / `未知操作`；403 容量门；404；409。
- 副作用：更新状态；写 `account_review` 审计；`reject` 发提交者邮件。
- 不变量：`approve` 过容量闸门（通过审核 = 该行开始计容量）。

#### `POST /api/accounts/<int:idx>/move`

- 鉴权档：管理员。CSRF：**要**。
- 请求体：`dir`（`-1` 或 `1`，必填）、`phone`（防错位，fail-closed）。
- 成功响应：`{"ok": true, "accounts": [AccountRow…]}`。
- 主要错误：400 `无法移动`；404 `账号不存在`；409。
- 副作用：调整顺序；写 `account_move` 审计。

### §2.4 用户管理（管理员）

#### `GET /api/users`

- 鉴权档：管理员。CSRF：不适用。
- 查询参数：`group` / `q` / `limit` / `offset`（可选，见 §1.5）；`count_only`（`"1"` 只回计数）。
- 成功响应（`count_only=1`）：`{"ok": true, "total": int, "review": int}`
  （`review` = 有待处理账号的用户数）。
- 成功响应（默认）：`{"ok": true, "users": [{"id": int, "email": string,
  "display": string, "role": "admin"|"user", "created_at": string,
  "account_count": int, "pending_count": int, "review_count": int}],
  "builtin_admin": string}`。传 `group` 或分页时增补 `group_totals`；分页时另补四键元数据。
- 主要错误：401；403。
- 副作用：聚合只读审计 `users_list_read`。
- 不变量：`id` 是单条操作的**不透明定位符**；明文邮箱不进 URL path。
  `display` 由服务端算好，前端不得自行 `split("@")`。

#### `GET /api/users/deleted`

- 鉴权档：管理员。CSRF：不适用。
- 成功响应：`{"ok": true, "items": [{"email": string, "deleted_at": string,
  "remaining_days": int, "status": "cooling"|"purge_pending"}]}`（按 `deleted_at`
  升序）。
- 副作用：聚合只读审计 `users_deleted_read`。
- 不变量：`remaining_days` 整天向下取整，0 = 不足一天。

#### `POST /api/users/deleted/purge`

- 鉴权档：**主管理员**。CSRF：**要**。**高危口令门**（`irreversible=True`）。
- 请求体：`emails`（string 数组，必填，≤10，每个 ≤64 字符）、`confirm_password`、
  `confirm_delay_ack`。
- 成功响应：`{"ok": true, "purged": [string…], "skipped": [string…],
  "msg": "已彻底清除 N 个用户[，跳过 M 个（非已注销状态）]"}`。
- 主要错误：400 `请选择要清除的用户` / `单次最多清除 10 个用户` /
  `邮箱格式不正确`；403 `仅主管理员可操作`；429。
- 副作用：物理清除用户及其全部账号与时间偏好；与审计同事务。
- 不变量：只清 `deleted=1` 的用户；冷却期内也可清（管理员裁决权高于宽限承诺）。

#### `POST /api/users/batch`

- 鉴权档：管理员。CSRF：**要**。**高危门**：`delete`（删除额度，`irreversible`）
  与 `reset_password`（凭据额度，`risk_always`）。
- 请求体：`action`（`reset_password`|`delete`，必填）、`emails`（string 数组，
  必填，≤10，每个 ≤64 字符）、`password`（`reset_password` 必填）、
  `confirm_password` / `confirm_delay_ack`。
- 成功响应：`{"ok": true, "msg": "已重置密码 N 个用户"|"已删除 N 个用户"}`。
- 主要错误：400 `未知操作` / `请选择要操作的用户` / `单次批量操作最多 10 个用户` /
  `邮箱格式不正确` / `新密码不符合要求：…` /
  `批量操作包含最后一个注册管理员的删除/降权，已整体回滚`；429；500
  `批量操作失败，已全部回滚`。
- 副作用：整体事务；`reset_password` 后轮换各目标 `sid`；写 `users_batch` 审计。
- 不变量：内置管理员不可批量操作；普通管理员不可重置/删除其他管理员（软跳过）。
  角色变更**不支持批量**。

#### `POST /api/users/<int:user_id>/role`

- 鉴权档：**主管理员**。CSRF：**要**。**高危口令门**（凭据额度，`risk_always`）。
- 请求体：`role`（`admin`|`user`，必填）、`confirm_password`、`confirm_delay_ack`。
- 成功响应：`{"ok": true, "msg": "<遮罩邮箱> 已设为管理员|已取消管理员"}`。
- 主要错误：400 `未知角色` / `内置管理员不可修改角色` /
  `仅正式用户可设为管理员（需有已生效账号且无待审核）` / `至少保留 1 个管理员`；
  403 `仅主管理员可修改管理员权限`；404 `用户不存在`；429。
- 副作用：更新角色；与 `user_role` 审计同事务。
- 不变量：提权与降权**同一端点、同一判据**（取证面必须统一）。批量入口已移除。

#### `POST /api/users/<int:user_id>/password`

- 鉴权档：管理员（目标为管理员时需主管理员）。CSRF：**要**。
  **高危口令门**（凭据额度，`risk_always`，仅当调用者是 admin 时）。
- 请求体：`password`（必填）、`confirm_password`、`confirm_delay_ack`。
- 成功响应：`{"ok": true, "msg": "<遮罩邮箱> 密码已重置"}`。
- 主要错误：400 `新密码不符合要求：…`；403
  `仅主管理员可重置管理员密码`；404 `用户不存在`；429
  `重置操作过于频繁，请稍后再试`。
- 副作用：更新口令哈希、递增 `pw_version`、轮换目标 `sid`；与
  `user_password_reset` 审计同事务。

#### `POST /api/users/<int:user_id>/delete`

- 鉴权档：管理员（目标为管理员时需主管理员）。CSRF：**要**。
  **高危口令门**（删除额度，`irreversible=True`）。
- 请求体：`mode`（`accounts_only`|`full`，缺省 `full`）、`confirm_password`、
  `confirm_delay_ack`。
- 成功响应：`{"ok": true, "msg": "<遮罩邮箱> 已完全删除"|"<遮罩邮箱> 的易班账号已清空（用户保留，可重新提交）"}`。
- 主要错误：400 `未知操作` / `内置管理员不可删除` / `至少保留 1 个管理员`；
  403 `仅主管理员可删除管理员`；404 `用户不存在`；429
  `删除操作过于频繁，请稍后再试`。
- 副作用：`full` 删用户及其账号（单事务）；`accounts_only` 只清其账号；与
  `user_delete` 审计同事务。`accounts_only` 另发管理员事后告警。
- 不变量：**404 排在口令门之后**（不存在的 id 同样先被门禁拦下，防探测枚举）。

### §2.5 设置与调度

#### `GET /api/settings`

- 鉴权档：管理员。CSRF：不适用。
- 成功响应（键集，全部为 JSON 类型）：
  `ok`(true)、`start_delay_max`(int)、`gap_max`(int)、`default_start_delay_max`(int)、
  `default_gap_max`(int)、`capacity_estimate`(`{accounts_cap, current_accounts,
  potential_load}`)、`sign_mode`(`sequence`|`random`)、`sign_order`、`sign_dist`、
  `window_edge_sec`(int)、`edge_front_sec`(int)、`edge_back_sec`(int)、
  正态区间四键（`mu_lo` / `mu_hi` / `sigma_lo` / `sigma_hi` 形态，具体键名见
  `env_io.SCHEDULE_DIST_KEYS`；`未核`：本文未逐字确认这四个键的拼写）、
  `allow_time_pref`(0|1)、`sign_window`(string)、`window_fallback`(bool)、
  `window_fallback_text`(string)、`capacity`(`{users, users_max, accounts,
  accounts_max, accounts_breakdown{normal,user_paused,cred_paused},
  accounts_audit}`)、`sunday_sign`(0|1)、`saturday_sign`(0|1)、`global_pause`(0|1)、
  `registration_pause`(0|1)、`batch_mode`(false)、`account_verify`(0|1)、
  `probe_enable`(0|1)、`probe_time`(string)、`probe_interval`(string)、
  `health_report_time`(string)、`health_report_weekday`(string)。
- 主要错误：401；403。
- 不变量：布尔键统一按 `1/true/on/yes` 为真解析（不用整数解析）。

#### `POST /api/settings`

- 鉴权档：管理员。CSRF：**要**。**分档口令门**：A 档（`MASTER_ONLY_KEYS` 加正态
  四键）仅主管理员可写；口令要求跟 `YIBAN_PW_GATE` 档位走——缺省 `risk` 档下，同出口
  的已登录会话直接生效，换出口才要一次；`full` 档每次都要，且 A 档不吃 TTL 豁免。
  B 档（`GATED_KEYS`：`sign_order` / `sign_dist` / `sign_mode` / `allow_time_pref`）
  任意管理员可写；`full` 档口令判定同 A 档但可吃 TTL 豁免；`risk` 档同出口免口令。
  `global_pause` 0→1 任意管理员可做（`full` 档要口令；非 `full` 档同出口免口令、
  另加不可逆确认），1→0 仅主管理员。
- 请求体：上面 `GET` 的键中可写的子集。**只写请求携带的键**（缺失不重置）。
  高危门字段 `confirm_password` / `confirm_delay_ack`。
  `sign_window` 格式 `HH:MM ~ HH:MM`。`window_edge_sec` 为旧前端兼容（对称写），
  `edge_front_sec` / `edge_back_sec` 各自独立（0~300 秒，30 的倍数）。
- 成功响应：`{"ok": true, "msg": "设置已保存（cron 下次触发自动生效）"}`
  （缓冲被夹时 `msg` 追加说明）。
- 主要错误：400（延迟非整数 / 窗口格式非法 / 首尾缓冲非 30 倍数 /
  探针时间非法 / 报告时刻或星期非法 / 容量硬门 `按新设置预估账号容量仅 N 个…` /
  `配置值不合法（…）` 带 `reason:"invalid_value"`）；403
  `仅主管理员可修改调度设置`；409（§1.7 的 `env_write_refused`）；429
  `操作过于频繁，请稍后再试`。
- 副作用：`write_env_batch` 一次原子写；写 `settings_save` 审计（逐键记旧→新）。
  **不外发变更告警**（留痕由审计承担）。
- 不变量：先全量校验、再统一写入（不存在"报错但设置变了"的部分写入）。
  非主管理员提交 A 档键即 403（只看键是否出现，不看值）。

#### `POST /api/settings/env-cleanup`

- 鉴权档：**主管理员**。CSRF：**要**。
- 请求体：`line`（int，1-based 行号，必填）。
- 成功响应：`{"ok": true, "removed_line": int, "message": string}`。
- 主要错误：400 `line 参数无效（须为 1-based 行号）` 或该行"不含行分隔符/行号越界"。
- 副作用：移除该物理行；写审计 `env_line_cleanup`。
- 不变量：只允许移除**确含行分隔符**的行（本端点不是 `.env` 编辑器）。

#### `GET /api/changelog`

- 鉴权档：公开。CSRF：不适用。
- 成功响应：`{"ok": true, "text": string}`（读项目根 `CHANGELOG.md`，启动后缓存）。

#### `GET /api/registration_paused`

- 鉴权档：公开。CSRF：不适用。
- 成功响应：`{"paused": bool}`（**无 `ok` 键**）。

#### `GET /api/scheduler/executors`

- 鉴权档：**主管理员**。CSRF：不适用。
- 成功响应：`{"ok": true, "workers": {"configured": int, "single_mode": bool,
  "assignments": [{"index": int, "egress": string, "role": "worker",
  "label": string, "state": string, "last_seen_at": string|null}],
  "env_keys": {"list": string, "single": string, "manifest": string}},
  "executors": [{"slot": int, "type": "worker"|"fallback"|"disabled",
  "egress": string, "label": string, "name": string|null,
  "state": string|null, "last_seen_at": string|null}],
  "fallback": {"egress": string, "interval_sec": int, "env_key": string,
  "role": "fallback", "label": string, "alive": bool, "enabled": bool,
  "env_key_enable": string, "status": "off"|"running"|"declared_not_running"|
  "running_not_declared", "in_window": bool},
  "window": {"effective_sec": int|null, "start": string|null, "end": string|null},
  "activity": {"day": string, "in_window": bool, "by_executor": [object],
  "totals": object}, "measured": object|null, "recommendation": object|null,
  "current_accounts": int}`。
- 主要错误：403 `仅主管理员可查看执行体配置`。
- 副作用：清单缺失且读到旧键时**一次性迁移写回**清单键（旧键保留一个版本周期）。
- 不变量：`egress` 只回脱敏描述串；`executors[]` 的 `state`/`last_seen_at`
  只有 `worker` 行有值，其余为 `null`（字段照给）。

#### `PUT /api/scheduler/executors`

- 鉴权档：**主管理员**。CSRF：**要**。**口令门**：逐键比对"这次真会写下去的值"
  与文件现值，值真变才要 `confirm_password`。
- 请求体（可部分提交）：`workers`（int，`WORKERS_MIN..WORKERS_MAX`）、
  `proxy_list`（逗号分隔串）、`proxy_fallback`（串）、`capacity_measured`
  （int，0=清除）、`fallback_enable`（`0`/`1`/`true`/`false`），
  以及 `confirm_password`。
- 成功响应：`{"ok": true, "applied": [string…],
  "note": "已写入配置；下一轮定时任务或容器重启后生效"}`。
- 主要错误：400（执行体数量越界 / 代理格式错 / 实测容量越界 / 兜底开关取值错 /
  `没有可更新的字段` / `代理配置不能包含换行`）；403；409（写拒绝）。
- 副作用：`write_env_batch`；清单存在时同步维护清单键；写审计（只记键名）；
  非 full 档发事后告警。
- 不变量：**只写 `.env`，不重启也不拉起进程**。代理串可能带凭据，绝不进审计。

#### `PUT /api/scheduler/executors/workers/<int:index>`

- 鉴权档：**主管理员**。CSRF：**要**。**口令门**：值真变才要。
- 请求体：`egress`（string 或 `null`；**缺键即 400**；`null`/空串 = 直连）、
  `confirm_password`。
- 成功响应：`{"ok": true, "index": int, "egress": string}`。
- 主要错误：400 `缺少 egress 字段（该槽位要直连请显式传 null 或空串）` /
  代理格式错 / `槽位 N 未被使用（当前执行体数 M）` / `槽位 N 不在执行体清单里`。
- 副作用：只改该段；其余段逐字保留；写审计（只记槽位名）。
- 不变量：只改目标段，不重排、不规范化其余段。

#### `PUT /api/scheduler/executors/fallback`

- 鉴权档：**主管理员**。CSRF：**要**。**口令门**：值真变才要。
- 请求体：`egress`（string 或 `null`；缺键即 400）、`confirm_password`。
- 成功响应：`{"ok": true, "index": "fallback", "egress": string}`（`egress` 是
  该槽位**此刻生效**的描述串）。
- 主要错误：400 `缺少 egress 字段…` / 代理格式错。
- 不变量：只改兜底段，其余键逐字不动。

#### `POST /api/scheduler/executors/rows`

- 鉴权档：**主管理员**。CSRF：**要**。**口令门**：追加一定会改配置，**必须**带
  `confirm_password`。
- 请求体：`type`（`worker`|`fallback`|`disabled`，缺省 `worker`）、
  `proxy`（string 或 `null`/空串 = 直连）、`name`（string 或 `null`/空串 = 不设名）、
  `confirm_password`。
- 成功响应：`{"ok": true, "slot": int, "type": string, "egress": string,
  "name": string|null, "note": "已写入配置；…"}`。
- 主要错误：400（代理格式 / 名称超长 / `fallback` 已有 1 行 / 槽位上限 63 满）。
- 副作用：清单写回；写审计。
- 不变量：槽位号 = 清单现有最大 + 1，并跳过保留期内用过的号（下标只增不复用）。

#### `PUT /api/scheduler/executors/rows/<int:slot>`

- 鉴权档：**主管理员**。CSRF：**要**。**口令门**：`type` 或 `proxy` 真变才要。
- 请求体：`type`（可选）、`proxy`（可选，空串/null = 直连）、`name`（可选，
  空串/null = 清名）、`confirm_password`。三者都不给即 400。
- 成功响应：`{"ok": true, "slot": int, "type": string, "egress": string,
  "name": string|null, "note": "…"}`。
- 主要错误：400 `没有可更新的字段（type / proxy / name 至少给一个）` /
  `槽位 N 不在执行体清单里` / 代理格式 / 名称超长。
- 不变量：改成 `disabled` 即停用（出口保留、不参与分配、仍占槽位）。其余行逐字保留。

#### `DELETE /api/scheduler/executors/rows/<int:slot>`

- 鉴权档：**主管理员**。CSRF：**要**。**口令门**：删行一定会改配置，**必须**带
  `confirm_password`。
- 请求体：`confirm_password`。
- 成功响应：`{"ok": true, "slot": int, "type": string, "deleted": true,
  "note": "…"}`。
- 主要错误：400 `槽位 N 不在执行体清单里` / 口令门文案。
- 不变量：删行**不重排**其余槽位。

#### `POST /api/scheduler/executors/measure`

- 鉴权档：**主管理员**。CSRF：**要**。
- 请求体：`phone`（可选，指定账号；缺省自动挑第一个可签账号）。
- 成功响应：`{"ok": true, "seconds": number, "sample": string（遮罩号）,
  "per_executor_capacity": int, "recommended_per_executor": int,
  "cooldown_sec": int, "next_allowed_in": 0, "note": string}`。
- 主要错误：403；404 `没有可用于实测的账号（不存在、未过审、已删除或已被暂停）`；
  409 `签到窗口内不做实测（避免抢占本轮资源）`；429 `实测冷却中`（带
  `next_allowed_in` 秒）；502 `实测失败：<原因>`。
- 副作用：**用真实账号访问易班一次**（只读链路，不提交签到）；全局冷却
  （`YIBAN_MEASURE_COOLDOWN`，默认 600 秒，跨进程有效）；写 `executors_measure`
  审计。结果**不落 `.env`**。
- 不变量：量的是"窗口外最小链路"（5 次请求），秒数天然偏小、容量偏乐观。页面
  必须如实提示。冷却**先占位再联网**（登录失败也消耗一次冷却）。

#### `GET /api/scheduler/executors/progress`

- 鉴权档：**主管理员**。CSRF：不适用。
- 成功响应：`{"ok": true, "day": string, "is_today": bool, "in_window": bool,
  "totals": {"total":int,"settled":int,"open":int,"done":int,"skipped":int,
  "pending":int,"claimed":int,"failed":int,"stolen":int},
  "totals_unreadable": bool, "by_executor": [object], "executor_totals": object,
  "note": string}`。
- 主要错误：403 `仅主管理员可查看签到进度`。
- 不变量：**只给聚合计数，绝不给逐账号列表**。`day` 取最近一次有记录的业务日。
  `totals` 读不通时全 0 并置 `totals_unreadable: true`（"读数不可用" ≠ "当日无记录"）。

#### `GET /api/scheduler/executors/ownership`

- 鉴权档：**主管理员**。CSRF：不适用。
- 成功响应：`{"ok": true, "day": string, "prev_day": string|null,
  "compared": bool, "tracked": int,
  "by_executor": [{"role": string, "index": int|null, "label": string,
  "slot": int|null, "accounts": int}],
  "migration": [{"from_role": string, "from_index": int|null, "from_label": string,
  "to_role": string, "to_index": int|null, "to_label": string, "accounts": int}],
  "note": string}`。
- 主要错误：403 `仅主管理员可查看归属分布`。
- 不变量：只给聚合计数。比较基准是当日的前一个**自然日**；该日无记录时
  `compared: false` 且 `migration: []`。

#### `GET /api/announcement`

- 鉴权档：公开（GET）。CSRF：不适用。
- 成功响应（公开）：`{"ok": true, "text": string}`（已发布文本）。
- 成功响应（管理员会话**额外**增补）：`draft`、`draft_by`、`draft_at`、
  `published_by`、`published_at`。
- 不变量：公开响应恒为两键 `ok` / `text`（登录页与全站横幅取它）。

#### `PUT /api/announcement`

- 鉴权档：管理员。CSRF：**要**。
- 请求体：`text`（string，≤200 字，不得含换行/行分隔符）。
- 成功响应：`{"ok": true, "msg": "草稿已保存，待主管理员发布"|"草稿已清除（线上公告未变；清空草稿后点「发布」即可下线）"}`。
- 主要错误：400 `公告内容过长（最多 200 字）` /
  `公告内容不能包含换行或行分隔符（单行存储）` / `草稿作者标识非法，未能保存`。
- 副作用：写草稿两键（正文 + 作者/时刻，一次原子写）；写
  `announcement_draft_save` 审计；发非紧急告警。
- 不变量：草稿**不改变任何对外可见内容**。

#### `POST /api/announcement/publish`

- 鉴权档：**主管理员**。CSRF：**要**。**口令门**（`always_required=True`，不吃豁免）。
- 请求体：`confirm_password`（必填）。
- 成功响应：`{"ok": true, "msg": "公告已发布"|"线上公告已下线", "text": string}`。
- 主要错误：400 `当前既没有待发布草稿，线上也没有公告` /
  `草稿含行分隔符（.env 疑似被手工改动），未能发布`；403
  `仅主管理员可发布公告`；口令门文案。
- 副作用：单批写入（草稿 → 正式，或清空即下线）；同步内存缓存；写
  `announcement_publish` 审计；发紧急告警（`force=True`）。
- 不变量：**一收一发走同一条通道**（空草稿 + 线上有内容 = 下线）。

### §2.6 日志、审计与巡检（管理员）

#### `GET /api/logs`

- 鉴权档：管理员。CSRF：不适用。
- 查询参数：`date`（`YYYY-MM-DD`，可选，缺省取最近有日志的一天）；
  `q`（子串过滤，大小写不敏感）；`all`（`"1"` 返回当日全量，封顶 5000 行）；
  `level`（`warn`|`all`，缺省 `warn`）；`limit` / `offset`（可选分页）。
- 成功响应：`{"ok": true, "logs": [string…], "total_lines": int,
  "returned": int, "truncated": bool, "dropped_lines": int, "q": string,
  "level": string, "collapsed_lines": int, "log_file": string, "date": string,
  "is_today": bool, "probe_events": [{"time": string, "phone": string（遮罩）,
  "status": string, "message": string}], "sign_events": [{"time", "phone",
  "status", "attempt": int, "message"}], "recent_log_date": string,
  "recent_probe_date": string, "recent_sign_date": string}`。
  分页时**增补** `limit` / `offset` 两键（总数键名仍是 `total_lines`）。
- 主要错误：400 `日期格式不正确，应为 YYYY-MM-DD` /
  `level 参数不合法（可选 warn / all）`。
- 副作用：聚合只读审计 `logs_read`。
- 不变量：日志行内手机号已脱敏。`truncated` = 结果集比本次返回的行数更大。
  响应**不含** `states`（账号图标的事实源是 `GET /api/accounts`）。

#### `GET /api/logs/export`

- 鉴权档：管理员。CSRF：不适用。
- 查询参数：`date`（`YYYY-MM-DD`，**必填**）。
- 成功响应：`text/plain` 附件，`Content-Disposition: attachment;
  filename=sign-<date>.log`。正文是**脱敏后**的逐行日志（与展示层同一可见性过滤
  管线）。
- 主要错误：400 `日期格式不正确，应为 YYYY-MM-DD`；404 `<date> 无签到日志`；
  429 `导出过于频繁，请稍后再试`（每 IP 60 秒 6 次）。
- 副作用：成功导出写 `logs_export` 审计（400/404 不写）。
- 不变量：任何登录身份都无法经 HTTP 取得未脱敏号码。

#### `GET /api/admin/sign-events`

- 鉴权档：管理员。CSRF：不适用。
- 查询参数：`phone`（可选，指定账号时间线）、`days`（1~90，缺省 7）、
  `limit`（1~200，缺省 100）、`stage`（`sign`|`probe`，缺省不过滤）。
- 成功响应：`{"ok": true, "days": int, "stage": string, "count": int,
  "events": [{"ts": string, "phone": string（遮罩）, "status": string,
  "message": string, "stage": string, "attempt": int, "dur_sec": number|null}],
  "daily_stats": [object], "accounts_stats": object}`。
  `accounts_stats` = `{"total": int, "by_status": {状态: int},
  "by_day": [{"day": "YYYY-MM-DD", "status": string, "accounts": int}],
  "days_missing": ["YYYY-MM-DD", …]}`。
- 副作用：聚合只读审计 `sign_events_read`。
- 不变量：手机号一律打码；条数封顶 200。
  **`accounts_stats` 是终态口径，来源是按日状态文件 `sign-state-<day>.json`
  （`web/services/logs.py::accounts_terminal_summary`），不读 `sign_events`**：
  `total` = 窗口内出现过的账号并集；`by_status` = 每状态窗口内去重账号数
  （桶间可有交集，不得相加当 `total`）；`by_day` = 日 × 终态，日内每号恰一桶。
  `days_missing` 列出窗口内**没有终态载体**的日期（不得静默当 0）。
  `stage` 只作用于 `events` 与 `daily_stats`（事件表口径），对 `accounts_stats` 不适用。
  前端不得把 `daily_stats.cnt` 跨天跨状态直加。

#### `GET /api/audit-logs`

- 鉴权档：管理员。CSRF：不适用。
- 查询参数（**白名单**，出现其它键即 400）：`page`（≥1，缺省 1）、`page_size`
  （1~200，缺省 50）、`action`、`actor`、`target`、`from_ts`、`to_ts`。
- 成功响应（分页信封）：`{"rows": [{"row_id": string, "ts": string,
  "actor": string, "action": string, "target": string, "detail": string}],
  "page": int, "page_size": int, "total": int, "has_more": bool}`。
- 主要错误：400 `不支持的过滤字段: X` / `分页参数必须是整数` /
  `page 必须大于等于 1` / `page 超出范围` / `page_size 必须在 1..200 之间`；
  500 `审计日志读取失败`。
- 副作用：聚合只读审计 `audit_logs_read`。
- 不变量：**翻页终止判据只看 `has_more`**（`has_more == page * page_size < total`），
  不得用 `len(rows) == page_size` 推断。`row_id` 是不透明定位符（HMAC 前 16 hex），
  不返回自增 id。`actor` 已遮罩。

#### `GET /api/admin/audit-status`

- 鉴权档：管理员。CSRF：不适用。
- 成功响应（三态之一）：`{"ok": true, "state": "healthy"|"tampered"|"indeterminate",
  "verdict_code": int, "reason": string, "cached": bool, "data_at": string|null,
  "snapshot_stale": bool, "chain_ok": bool|null, "broken": int|null,
  "anchor_status": string|null, "anchor": object|null,
  "indeterminate": {"present": bool, "reason": string}, "known_debt": [object],
  "write_failures": int|null, "empty_hash_rows": int|null, "note": string}`。
- 副作用：聚合只读审计 `audit_status_read`。
- 不变量：**只读体检快照**（不重算全表哈希）。快照缺失/损坏/超龄时一律答
  `indeterminate`，绝不当成健康。超龄只降"健康"，**不降** `tampered`。

#### `GET /api/admin/run-events`

- 鉴权档：管理员。CSRF：不适用。
- 查询参数（**白名单**，出现其它键即 400）：`day`（`YYYY-MM-DD`）、
  `executor`（`single`|`fallback`|`worker-N`|`unknown`，允许带方括号）、
  `limit`（1~500，缺省 200）。
- 成功响应：`{"ok": true, "retention_days": int, "window": {"start_day": string,
  "end_day": string, "day": string, "in_window": bool, "min_day": string|null,
  "max_day": string|null, "has_data": bool}, "rounds": [object],
  "rounds_truncated": bool, "rounds_limit": int, "events": [object],
  "events_truncated": bool, "events_limit": int}`。
- 主要错误：400 `不支持的过滤字段: X` / `日期格式不正确，应为 YYYY-MM-DD` /
  `limit 必须是整数` / `limit 必须在 1..500 之间` / `executor 参数不合法`；
  500 `运行进度读取失败`。
- 副作用：聚合只读审计 `run_events_read`。
- 不变量：`executor` 只接受**公开标签**（身份原串永不出现）。窗口外日期回
  `in_window=false`、`has_data=false`、空表（**不静默出空表**：页面据此给"跨月
  回溯请走审计日志页"的指引）。

#### `GET /api/admin/executor-health`

- 鉴权档：管理员。CSRF：不适用。
- 查询参数：**无**（出现任何查询键即 400）。
- 成功响应：`{"ok": true, "executors": [{"identity": string, "role": string,
  "index": int|null, "heartbeat_file": string, "state": "running"|"finished"|
  "idle"|"stale"|null, "age_sec": number|null, "heartbeat_at": string|null,
  …（吞吐字段）}]}`（`未核`：吞吐字段的确切键名，本文未逐字确认）。
- 主要错误：400 `不支持的过滤字段: X`；500 `执行体健康卡读取失败`。
- 副作用：聚合只读审计 `executor_health_read`。
- 不变量：身份只认心跳文件名（记录体自称的 `role`/`index` 不可信）。无心跳时
  `state`/`age_sec`/`heartbeat_at` 均为 `null`（页面写"无心跳"，不显示 0）。
  本端点**只读**（不触发执行体清单迁移写回）。

#### `GET /api/clock`

- 鉴权档：任意已登录。CSRF：不适用。
- 成功响应：`{"ok": true, "now": "YYYY-MM-DD HH:MM:SS", "server_ts": int,
  "tz_offset_min": int, "sign_status": string, "color": string}`。

#### `POST /api/ping`

- 鉴权档：管理员。CSRF：**要**。
- 请求体：无。
- 成功响应：`{"ok": true, "reachable": bool, "detail": string}`。

#### `GET /api/time-prefs/stats`

- 鉴权档：管理员。CSRF：不适用。
- 成功响应：`{"ok": true, "slots": [{"slot_min": int, "label": string,
  "disabled": bool, "edge_note": string, "count": int, "cap": int}]}`。
- 不变量：管理端保留精确人数与块容量（用户端只给粗粒度百分比）。

### §2.7 通知通道（邮件 / 推送）

#### `GET /api/mail-config`

- 鉴权档：管理员。CSRF：不适用。
- 成功响应：`{"ok": true, "enabled": bool, "admin_notify": bool,
  "smtp_host": string, "smtp_port": int, "user": string, "admin_to": string,
  "smtps": [{"id": string|null, "host": string, "port": int,
  "user": string（打码）, "has_pass": bool}]}`。
- 主要错误：401；403。
- 不变量：`pass` 绝不回显（只给 `has_pass`）。旧格式条目无 `id` 时为 `null`。
  条目级 `admin_to` 不参与序列化。

#### `PUT /api/mail-config`

- 鉴权档：**主管理员**。CSRF：**要**。**高危门**：关闭邮件通道（开→关）与
  收件人改道（值真变）过统一高危门（凭据额度，`risk_always`）；SMTP 凭据变更
  直连 `_reconfirm_admin_password`（要口令、不占额度）。
- 请求体（可部分提交）：`enabled`（bool）、`admin_notify`（bool）、
  `admin_to`（string，逗号分隔；键存在即以提交值为准，空串 = 显式清空）、
  `smtps`（数组，≤10 条，每条 `{id?, host, port=465, user, pass}`）、
  `confirm_password` / `confirm_delay_ack`。
- 成功响应：`{"ok": true, ...落盘的键…}`（`enabled` / `admin_notify` /
  `smtps_count` / `smtps_from` / `smtps_to` / `admin_to` / `admin_to_from`）。
- 主要错误：400（取值非 bool / 收件人格式或超 10 个 / `smtps` 非列表或超 10 条 /
  第 N 条 host 空或端口越界 / 第 N 条 id 无效或重复 / 内网 host 被拒 /
  `缺少有效配置项`）；403 `仅主管理员可操作`；409（写拒绝）。
- 副作用：一次 `write_env_batch`（密文 + 开关合成同批）；写 `mail_config` 审计
  （打码的"从哪→到哪"）；收件人改道在**落盘前**向**原**收件人发预警。
- 不变量：`pass` 留空时按"稳定 id 认领 + `(host,port)` 未变"沿用旧凭据；改目标
  = 换中继，**不**沿用旧凭据。条目身份是稳定 `id`，位置不参与身份。

#### `GET /api/notify-config`

- 鉴权档：管理员。CSRF：不适用。
- 成功响应：`{"type": string, "secret": …（打码或布尔）, "cooldown": int,
  "urgent_only": bool, "daily_max": int, "daily_remaining": int|null,
  "urgent_daily_max": int, "urgent_daily_remaining": int|null,
  "quota_visible": bool, …}`（`未核`：`secret` 的确切字段名与是否另有 `has_secret`）。
- 不变量：额度**余量**（`daily_remaining` / `urgent_daily_remaining`）仅主管理员
  可读，否则置 `null`；`quota_visible` **恒定下发**（区分"无权查看"与"不限"）。

#### `PUT /api/notify-config`

- 鉴权档：**主管理员**。CSRF：**要**。**高危门**：触碰密钥（`type` 或 `secret`
  任一在场）过统一高危门（凭据额度，`risk_always`）。调额度/节流参数免门。
- 请求体（可部分提交）：`type`（`serverchan`|`custom`|`""`）、`secret`、
  `cooldown`（≥0，秒）、`urgent_only`（bool）、`daily_max`（≥0，0=不限）、
  `urgent_daily_max`（≥0）、`confirm_password` / `confirm_delay_ack`。
- 成功响应：`m.notify.get_config()` 的形状（同 GET）。
- 主要错误：400 `未知的通知类型` / `Server酱 SendKey 应以 SCT 开头` /
  `Server酱 SendKey 长度不合法` / `自定义地址过长（最多 2048 字符）` /
  `自定义地址仅允许 HTTPS 且非回环/内网地址` / `冷却参数无效` /
  `每日上限参数无效` / `紧急每日上限参数无效`；403；429。
- 副作用：`write_env_batch`（密钥 AES-GCM 加密）；写 `notify_config` 审计。
- 不变量：`secret` 加密排在口令门**之后**（失败请求零写盘痕迹）。
  `urgent_only` 与 `cooldown` 两个状态都**显式落盘**（写空值 = 删键 = 回落默认，
  与开关意思相反）。

#### `POST /api/notify-test`

- 鉴权档：**主管理员**。CSRF：**要**。
- 请求体：无。
- 成功响应：`{"ok": true, "msg": "测试消息已发送，请检查手机/接收端"}`。
- 主要错误：400 `测试消息发送失败（未配置或推送被拒，详见服务日志）`；403。
- 副作用：发一条测试消息（`force=True` 跳过节流与额度）；**每次外呼必留一条
  含操作者的 `notify_test` 审计**（失败也写）。

### §2.8 手动签到

#### `POST /api/signin`

- 鉴权档：管理员。CSRF：**要**。
- 请求体：`phone`（string，必填）。
- 成功响应：`{"ok": true, "msg": "已触发 <号> 手动签到（后台执行，日志约 30 秒内刷新）"}`。
- 主要错误（拒因→状态码由单源判定表给出）：404 `账号 <号> 不在配置中`；400
  `账号 <号> 不可手动签到（未生效或已删除）` /
  `账号 <号> 已自暂停签到，跳过（可在「我的账号」恢复）`；429
  `签到队列忙（定时签到进行中），请稍后再试` / `签到冷却中（约 M 分 S 秒后可重试）` /
  `手动签到触发过于频繁（W 分钟内最多 L 次），请稍后再试` /
  `账号 <号> 正在签到，请 N 秒后再试`；500 启动失败文案。
- 副作用：起 `signin.py --only <号>` 子进程；写 `signin_manual` 审计。
- 不变量：单条与批量共用同一冷却基准（`YIBAN_BATCH_SIGN_COOLDOWN_SEC`，默认 60 秒，
  0=关闭）。30 秒 per-phone 防抖。另有全局次数上限
  （`YIBAN_SIGNIN_RATE_MAX` / `YIBAN_SIGNIN_RATE_WINDOW_SEC`，默认 10 次/600 秒）。
  冷却放行后才计次数。

#### `POST /api/signin/batch`

- 鉴权档：管理员。CSRF：**要**。
- 请求体：`ids`（int 数组，必填）、`phones`（string 数组，用于防错位，缺省时
  不做对齐校验）。
- 成功响应：`{"ok": true, "count": int, "skipped_paused": int,
  "msg": "[N 个已自暂停，跳过；]已加入批量签到队列（…）"}`。
- 主要错误：400 `请先勾选要签到的账号` /
  `选中的账号均不可手动签到（未生效或已删除）` /
  `选中的账号均已自暂停签到，跳过（N 个），可在「我的账号」恢复` /
  `单次批量签到最多 10 个账号`；409 `账号列表已变化，请刷新页面后重试`；429
  `签到队列忙（定时签到进行中），请稍后再试` /
  `已有批量签到正在执行，请稍后再试` / 冷却文案 / 次数上限文案。
- 副作用：合并为**单个**队列子进程（只发一封汇总邮件）；写 `signin_batch` 审计。
- 不变量：`count` 是**实际派发**的账号数（自暂停剔除只计入 `skipped_paused`）。
  全局互斥（同时只允许一个批量队列）。

### §2.9 SSE

#### `GET /api/stream/progress`

- 鉴权档：**主管理员**。CSRF：不适用（GET），但**额外做同源校验**。
- 请求头：`Last-Event-ID`（可选，浏览器自动回带）。
- 成功响应：SSE 流（见 §1.6）。事件类型 `snapshot` / `progress` / `heartbeat` /
  `resync` / `end`。
- 主要错误：403 `请求来源异常，请刷新页面后重试`（跨站）或
  `仅主管理员可查看实时进度流`（非主管理员）。
- 副作用：建连时一次聚合只读审计 `run_events_stream`（绝不每拍写）。
- 不变量：payload **不含手机号**。连接超预算时回一个 `end` 事件（不报 HTTP 错）。

#### `GET /api/stream/my-signin`

- 鉴权档：任意已登录。CSRF：不适用（GET），但**额外做同源校验**。
- 请求头：`Last-Event-ID`（可选）。
- 成功响应：SSE 流。`progress` 事件只推**本人账号**的事件（服务端按本人 phone
  集合过滤，phone 不进 payload）。快照只给 `cursor` / `day` / `generated_ts`。
- 主要错误：401 `未登录`；403 `请求来源异常，请刷新页面后重试`（跨站）。
- 不变量：快照**不含**全局 `totals` / `by_executor`（那是管理员面口径）。连接预算
  用 `YIBAN_SSE_USER_CONNECTIONS_MAX`（与管理员流分家）。

### §2.10 页面路由、静态资源与重定向（**不是数据接口**）

本节路由返回 **HTML 页面**或**文件**，不是 JSON 数据接口。前端**不得**把它们当
数据源（`fetch().json()` 会失败）。它们由服务端渲染与浏览器导航消费。

- **页面路由**（管理端/用户端 HTML）：`GET /data/dashboard`、`GET /data/logs`、
  `GET /data/audit`、`GET /work/accounts`、`GET /work/users`、
  `GET /work/settings`、`GET /work/pilot`、`GET /my/account`、`GET /my/calendar`、
  `GET /user/account`、`GET /user/calendar`、`GET /login`、`GET /terms`、
  `GET /privacy`。
  守卫行为：管理端页未登录 → 302 到 `/login`；非管理员 → 302 到 `/user/calendar`。
  用户端页未登录 → 302 到 `/login`；管理员 → 302 到 `/data/dashboard`。
- **根路径**：`GET /` 按登录态 302 到对应首页（未登录 → `/login`）。
- **旧路径 302**（值写端点名，保留查询串）：`GET /logs`、`GET /accounts`、
  `GET /users`、`GET /settings`、`GET /mine`、`GET /mine/calendar`、`GET /user`。
- **静态与协议**：`GET /favicon.png`、`GET /gongan-beian.png`（部署者自放，
  未放置 404）、`GET /robots.txt`、`GET /static/<path:filename>`（Flask 内建，
  服务 `web/static/`）。
- 页面路径的响应**禁缓存**（`Cache-Control: no-store`）；静态资源长缓存 30 天。

逐条列出（守卫按此对账）：

#### `GET /`

- 鉴权档：公开。响应：302 重定向（HTML 导航用）。

#### `GET /accounts`

- 鉴权档：公开。响应：302 到 `/work/accounts`（旧路径）。**HTML 重定向**。

#### `GET /data/audit`

- 鉴权档：管理员（HTML 页面）。响应：HTML。

#### `GET /data/dashboard`

- 鉴权档：管理员（HTML 页面）。响应：HTML。

#### `GET /data/logs`

- 鉴权档：管理员（HTML 页面）。响应：HTML。

#### `GET /favicon.png`

- 鉴权档：公开。响应：`image/png` 或 404。

#### `GET /gongan-beian.png`

- 鉴权档：公开。响应：`image/png` 或 404。

#### `GET /login`

- 鉴权档：公开（HTML 页面）。响应：HTML。已登录时按角色 302 到首页（循环保护）。
  未配置管理员时 302 到 `/setup`。

#### `GET /setup`

- 鉴权档：公开（HTML 页面）。响应：HTML。
- 未配置管理员时渲染首次启动表单；已配置时 302 到 `/login`。
- 未配置管理员时，`/` 与 `/login` 也 302 到本页。

#### `GET /logs`

- 鉴权档：公开。响应：302 到 `/data/logs`（旧路径）。**HTML 重定向**。

#### `GET /mine`

- 鉴权档：公开。响应：302 到 `/my/account`（旧路径）。**HTML 重定向**。

#### `GET /mine/calendar`

- 鉴权档：公开。响应：302 到 `/my/calendar`（旧路径）。**HTML 重定向**。

#### `GET /my/account`

- 鉴权档：管理员（HTML 页面）。响应：HTML。

#### `GET /my/calendar`

- 鉴权档：管理员（HTML 页面）。响应：HTML。

#### `GET /privacy`

- 鉴权档：公开（HTML 页面）。响应：HTML。

#### `GET /robots.txt`

- 鉴权档：公开。响应：`text/plain`。

#### `GET /settings`

- 鉴权档：公开。响应：302 到 `/work/settings`（旧路径）。**HTML 重定向**。

#### `GET /static/<path:filename>`

- 鉴权档：公开。响应：静态文件（Flask 内建 static handler，服务 `web/static/`）。

#### `GET /terms`

- 鉴权档：公开（HTML 页面）。响应：HTML。

#### `GET /user`

- 鉴权档：公开。响应：302 到 `/user/account`（旧路径）。**HTML 重定向**。

#### `GET /user/account`

- 鉴权档：任意已登录（HTML 页面）。响应：HTML。

#### `GET /user/calendar`

- 鉴权档：任意已登录（HTML 页面）。响应：HTML。

#### `GET /users`

- 鉴权档：公开。响应：302 到 `/work/users`（旧路径）。**HTML 重定向**。

#### `GET /work/accounts`

- 鉴权档：管理员（HTML 页面）。响应：HTML。

#### `GET /work/pilot`

- 鉴权档：管理员（HTML 页面）。响应：HTML（Vue 翻新试点页）。

#### `GET /work/settings`

- 鉴权档：管理员（HTML 页面）。响应：HTML。

#### `GET /work/users`

- 鉴权档：管理员（HTML 页面）。响应：HTML。

#### `GET /work/queue`

- 鉴权档：管理员（HTML 页面）。响应：HTML（工作队列试点页，lcpx 2026-10-25）。

### §2.11 工作队列（管理员，lcpx 试点）

#### `GET /api/queue`

- 鉴权档：管理员。CSRF：不适用。**只读聚合，不写任何状态**（lcpx 试点
  2026-10-25；实现 `web/routes/queue_api.py`）。
- 查询参数：`bucket`（**必填**，`failed`|`unsigned`，其它值 400 `未知队列分组`）、
  `limit`/`offset`（§1.5 分页口径，传任一即分页并增补四键元数据）、
  `q`（≤64 字符，检索口径与 `GET /api/accounts` 同一份实现）。
- 成功响应：`{"ok": true, "bucket": string, "today": "YYYY-MM-DD",
  "weekday": "周X", "window": "HH:MM ~ HH:MM",
  "counts": {"pending": int, "rejected": int, "active": int, "failed": int,
  "unsigned": int},
  "rows": [<账号行>], "total"?: int, "limit"?: int, "offset"?: int, "returned"?: int}`。
  账号行 = `GET /api/accounts` 的行字段（同一 `mask_account` 脱敏单出口），
  **但剔除 `phone_code_masked`**（本端点只读未解密快照，该字段会是密文切片；
  要看识别码走 `GET /api/accounts/<idx>/detail`）：`index`/`name`/`phone`（遮罩）/
  `phone_model`/`display_name`/`owner`（遮罩）/
  `owner_display`/`status`/`reject_reason`/`user_paused`/`deleted`/`has_password`/
  `has_phone_code`/`deleted_at` 等，另加：
  `state`（当日**有效**终态码，无记录按 `pending`）、`state_msg`（终态文案，唯一取点
  `accounts_data.plan_state_message`：未了结账号是「计划 HH:MM」或「待生成」）、
  `state_time`（终态时刻 "HH:MM:SS"，可空）、`plan_at`（"HH:MM"，可空串）、
  `time_pref`（自选时段 "HH:MM"，可 null）。
- 分组口径（终态唯一事实源 = 按日状态文件，与看板/账号表同源）：
  **今日失败** = 已生效、未软删、当日有效终态为 `failed`（用户自暂停不影响其已有
  结论）；**今日未签** = 已生效、未软删、未被用户暂停、当日无结论（无记录或
  `pending`）。用户自暂停且无结论的账号按账号页同一合成视为「用户已取消」，
  不进任何一组。
- 排序：`unsigned` 按当日计划时刻升序（执行序，"待生成"垫底）；`failed` 按终态时刻
  降序；同键按 `index` 稳定。`index` 是整表绝对下标，可直接用于
  `/api/accounts/<idx>/*` 既有操作端点。
- `counts` 不随 `q`/分页收窄；待审核组不在本端点（走既有
  `GET /api/accounts?group=pending`）。
- 不变量：分组/计数/检索只读未解密快照（零 AES-GCM）；行内手机号与归属一律
  服务端已遮值，前端不得尝试还原。

#### `GET /api/queue/<int:idx>/events`

- 鉴权档：管理员。CSRF：不适用。**只读**。
- 查询参数：`days`（1~90，缺省 30）。
- 成功响应：`{"ok": true, "count": int, "returned": int, "truncated": bool,
  "events": [{"ts": string, "phone": string（遮罩）, "status": string,
  "message": string, "stage": string, "attempt": int, "dur_sec": number|null}]}`
  （`events` 按时间升序，**封顶保留最近 50 条**（砍最旧一端）；`count` 是窗口内全量条数，
  `returned` 是本次返回条数，`truncated` 表示是否发生截断）。
- 主要错误：404 `账号不存在`；503 `账号数据暂不可读，请稍后再试`。
- 副作用：聚合只读审计 `queue_events_read`。
- 不变量：按**账号下标**解析号码——明文手机号不得出现在请求 URL（访问日志落盘
  红线）；响应内手机号一律遮罩。

## §3 契约变更纪律

1. 改任何端点的**路径、方法、鉴权档、字段、状态码**，必须**同批**改本文件。
   本文是前端唯一依据；不同批改 = 前端按旧契约开发必然返工。
2. 漂移由 `tests/test_api_contract_coverage.py` 兜住：
   - `create_app().url_map` 里每一条 `(方法, 路径)` 必须在本文出现；
   - 本文出现的每一条必须存在于 `url_map`；
   - 两侧任一为空即红（防"扫空当绿"）。
3. 本文件必须入库（`docs/dev/*` 默认被 gitignore，白名单在 `.gitignore` 里显式
   加了 `!docs/dev/api-contract.md`）。未入库 = 前端拿不到。
4. 新增字段时**只增不改**：删字段或改字段含义属破坏性变更，须在 PR 描述里声明。

## §4 守卫

- 文件：`tests/test_api_contract_coverage.py`。
- 判据：见 §3 第 2 条。
- 突变自证：从本文删一条端点 ⇒ 红；给本文加一条假路径 ⇒ 红（命令与输出见 PR 描述）。
