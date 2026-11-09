# Host API v1 — frozen shape

The OpenAPI 3.1 YAML is the machine-readable form and the source of the Go
transport types. This document mirrors it; for role assignments the YAML is
authoritative and a gate keeps both tables equal.

## Conventions

- JSON, UTF-8, RFC3339 timestamps.
- Auth: HttpOnly session cookie. Writes require `X-CSRF-Token`.
- Error envelope:

```json
{"error":{"code":"forbidden","message":"...","details":{},"trace_id":"..."}}
```

- Pagination: `?cursor=&limit=` (default 50, max 200). Notifications are the
  exception: default 20, max 100.
- Cross-scope object: **404**. Action forbidden: **403**.
- Rate limits: the inbound limiter answers **429** with `Retry-After` in
  seconds. Login, reset password, change password, verify and sign are limited
  per client IP and per account key; forgot password is limited per client IP
  only; register is limited per client IP and per normalized email; account
  restore is limited per client IP and per normalized email. Account create
  and update are limited per caller. The admin test-email endpoint is
  limited to one attempt per master per 60 seconds (process-local; a restart
  clears it) and answers 429 with `Retry-After`. Other 429 answers
  (time-preference cooldown, capacity) omit `Retry-After`.
- Public registration is closed by default. `GET /api/auth/registration` is
  public; `PUT /api/auth/registration` is master only; `POST /api/auth/register`
  creates a role user account and signs in.
- No phone number, credential or upstream raw text in any response.

## Conditional requests

`GET /api/me/photos/{id}`, `GET /api/accounts` and `GET /api/users` send
`ETag` on both the `200` and the `304` answer. The photo validator is the
sha256 of the stored bytes; a list validator is the sha256 of the JSON body
(quoted hex). `If-None-Match` uses the weak comparison function: `W/"x"`
matches `"x"`, comma lists and `*` are honoured. Repeated `If-None-Match`
header lines are merged into one comma-separated value per RFC 9110 section
5.3 before binding, so a well-formed list answers `304` or `200`, never
`400`.
A `304` carries `ETag` and carries no `Content-Type`, no `Content-Length`
and no body. The `304` response declares the `ETag` header in the YAML.
Authentication and scope resolution run before the validator is computed:
a session that cannot read a page never gets a `304` for it.
The photo `200` path reads the record twice for an owner: `Get` for the
validator, then `Open` for the stream. An admin whose owner read misses adds
one more `Get` against the admin scope before `Open`. Merge the reads into
one port call only if this path becomes a hotspot.

## Gates

`scripts/contract-gate.sh` runs two checks on this YAML. spectral lints it
against the `spectral:oas` ruleset at warning severity. oasdiff compares it
against a base git ref and fails on ERR-level breaking changes; without a base
ref it prints a visible skip. CI runs the gate only when a change touches
`server/contracts/`. spectral is pinned in `server/contracts/tools`.

## Versioning

The `info.version` field in `openapi.yaml` (currently `1.0.0`) is the **API
contract version**, not the product release version. It only changes when the
contract itself takes a breaking change; additive changes keep it untouched.

The product release version lives in the repository root `VERSION` file (the
single source for the Go line) and is tagged `v$VERSION` at release time. The
two counters are independent by design and are never synchronized:
`scripts/release-check.sh` item `go-version` asserts the product side only
(`VERSION` present and semver, built binaries self-reporting it, release tag
`v$VERSION` pointing at HEAD) and never reads `info.version`.

## Roles

| Role | Scope |
|---|---|
| master | all schools, admins, settings, audit, egress |
| admin | bound schools only |
| user | own accounts |

## Endpoints

`contracts/openapi.yaml` is the authoritative role table. This table mirrors
it row by row; `internal/entry/http/oapi/contract_gates_test.go` fails when
either side drifts.

| Method | Path | Roles | Phase |
|---|---|---|---|
| POST | `/api/auth/login` | public | P1 |
| POST | `/api/auth/logout` | public | P1 |
| GET | `/api/auth/me` | master/admin/user | P1 |
| GET | `/api/auth/password-policy` | master/admin/user | PW-1 |
| POST | `/api/auth/forgot-password` | public | P1 |
| POST | `/api/auth/reset-password` | public | P1 |
| POST | `/api/auth/change-password` | master/admin/user | P1 |
| GET | `/api/auth/registration` | public | S05 |
| PUT | `/api/auth/registration` | master | S05 |
| POST | `/api/auth/register` | public | S05 |
| POST | `/api/me/delete` | master/admin/user | lac2 |
|  |  |  | 本人注销登录账号：`current_password` 二次口令（缺 400/错 403/连败 429＋Retry-After）；软删 `users` 行并连带软删名下全部生效账号；注销全部会话并清 cookie；master 403；7 天宽限（`core/user.SelfRestoreGrace`）；审计 `user.self_delete`，detail 不含邮箱/手机号。 |
| POST | `/api/me/restore` | public | lac2 |
|  |  |  | 未登录可调：邮箱＋登录口令；仅恢复 7 天内本人软删的登录账号及注销时生效的那一行账号；口令错、账号不存在、超窗、邮箱已被活跃行占用、管理员删除一律同一条 401，不泄露存在性；容量满 429；恢复不建立会话；按客户端 IP 与归一邮箱限流，429 带 `Retry-After`；审计 `user.self_restore`，detail 不含邮箱/手机号。 |
| GET | `/api/users` | master | P1 |
| POST | `/api/users` | master | P1 |
| POST | `/api/users/batch` | master | 870 |
|  |  |  | 一次对 1–10 个用户执行同一动作（disable/enable/reset_password/delete/restore）：逐条独立事务、部分失败可见，`items` 与请求 `ids` 同序；与单条端点同口径——有变化写一行审计，幂等无变化（如已停用再 disable）不写；`temp_password` 只在成功的 `reset_password` 项出现一次；二次口令门形与单条端点一致，二次口令失败零写入；逐条失败只影响该条；未知动作、ids 空/超限/重复整批 400。 |
| PATCH | `/api/users/:id` | master | UX-1 |
| POST | `/api/users/:id/password-reset` | master | UX-1 |
| DELETE | `/api/users/:id` | master | UX-1 |
| POST | `/api/users/:id/restore` | master | UX-1 |
| POST | `/api/users/:id/purge` | master | qru |
|  |  |  | 物理删除已软删用户：名下账号、任务行、审核对与定位 actor 一并清空；`window_overrides` 保留且 `created_by` 置空；审计 `user.purge`；活用户 409。 |
| GET | `/api/users/lookup` | master/admin | UX-1 |
|  |  |  | `exact=true` 时 q 按完整邮箱全等匹配（至多一条）；缺省前缀查询。按 id 绑定属主的调用必须带 exact（F1）。 |
| GET | `/api/schools` | master/admin/user | P1 |
| POST | `/api/schools` | master | P1 |
| PATCH | `/api/schools/:id` | master | P1 |
| GET | `/api/schools/:id/admins` | master | P1 |
| POST | `/api/schools/:id/admins` | master | P1 |
| DELETE | `/api/schools/:id/admins/:uid` | master | P1 |
| GET | `/api/accounts` | master/admin/user | P1 |
| POST | `/api/accounts` | master/admin/user | P1 |
|  |  |  | 可选 `confirm_defaults`：客户端声明建号全部沿用 detect 返回的默认值。它是声明而非授权——开关开时服务端仍用存储凭据重登平台并复核 `UniversityId` 与学校绑定；未带、false、开关关或复核失败都留在 pending 走人工；复核通过写 `account.auto_approve` 审计并通知管理员，失败写 `account.auto_check_failed`（detail 只含结果词）。 |
| POST | `/api/accounts/detect` | master/admin/user | ykxf |
|  |  |  | 用提交的手机号与口令做一次平台认证并读回设备登记：凭据零落库、零日志、零审计值；平台院校按 `schools.platform_university_id` 投影到本地学校——活跃校复用，未绑定则自动建校（名称取平台、编码自动生成、status active）并写 `school.auto_create` 审计（detail 只含 `university_id`），disabled 校视同未命中（不重复建校，`school` 回 null）；响应只回设备码状态位 `device_code_present`，不回设备码本体；平台失败统一 502 中性信封；按客户端 IP 与提交手机号限流，429 带 `Retry-After`。 |
| GET | `/api/accounts/summary` | master/admin | N190 |
|  |  |  | 只数未软删账号：`pending` 与 `rejected` 两计数，供看板待处理 KPI 与账号页提示条。 |
| POST | `/api/accounts/batch` | master/admin | 870 |
|  |  |  | 一次对 1–10 个账号执行同一动作：逐条独立事务、逐条审计、部分失败可见，`items` 与请求 `ids` 同序；`reason` 只给 `review_reject`；二次口令门形与单条端点一致，失败零写入；容量与状态错误按 per-item `code` 返回。 |
| POST | `/api/accounts/batch/sign` | master/admin | 870 |
|  |  |  | 一次对 1–10 个账号逐个入队手动签到：每条走单条同口径（不可见/跨校 per-item `not_found`、非 UUID per-item `validation_failed`、非 active per-item `conflict`、队列冲突 `already_signed`/`sign_running`/`window_closed`），`items` 与请求 `ids` 同序，`task_id` 只在成功项出现；多个启用任务是同一打卡的多个点位，服务端随机选一个入队，不要求用户选择；不要求二次口令（与单条一致）；限流与单条同口径——IP 整批限流（入站白名单）＋账号键逐项限流（命中项 `rate_limited`，其余照常）；旧线 60 秒进程冷却与宿主级单队列不做，并发语义归限流、worker 容量与账号级唯一任务行；ids 空/超限/重复整批 400。 |
| GET | `/api/accounts/:id` | master/admin/user | P1 |
| PATCH | `/api/accounts/:id` | master/admin/user | P1 |
| POST | `/api/accounts/:id/review` | master/admin | P1 |
| POST | `/api/accounts/:id/pause` | master/admin/user | P1 |
| DELETE | `/api/accounts/:id` | master/admin/user | P1 |
| POST | `/api/accounts/:id/device-refresh` | master/admin/user | P4 |
| POST | `/api/accounts/:id/restore` | master/admin | P4 |
| POST | `/api/accounts/:id/purge` | master/admin | qru |
|  |  |  | 物理删除已软删账号：凭据、出口、定位与任务行一并清空；审计 `account.purge` 且审计行保留；活账号 409。 |
| POST | `/api/accounts/:id/verify` | master/admin/user | P3 |
|  |  |  | 用户可自助校验凭据：user 只能校验自己账号，越权与跨校同为 404（不泄露存在性）；结果结构与 master/admin 同形，限流与 sign 同账号键桶。 |
| POST | `/api/accounts/:id/person-name` | master/admin/user | rufc |
|  |  |  | 懒加载揭示平台姓名：与 verify 同型的平台认证，成功只回 `{person_name}` 单字段；姓名不落库、不进日志正文，审计 `account.person_name_reveal` 的 detail 只有字段名；user 只能取自己账号（越权 404）；平台失败统一 502 中性信封；限流与 verify/sign 同账号键桶，429 带 `Retry-After`。 |
| GET | `/api/schedule/windows` | master/admin | P4 |
| GET | `/api/schedule/preview` | master/admin | P4 |
| POST | `/api/schedule/plan` | master/admin | P4 |
| POST | `/api/schedule/override` | master/admin | P4 |
| GET | `/api/me/time-pref` | master/admin/user | P4 |
| PUT | `/api/me/time-pref` | master/admin/user | P4 |
|  |  |  | GET 返回本人生效账号当日任务的有效窗口与 5 分钟槽位；只读窗口缓存/覆盖/标准回退，零平台发现、不建全量计划。`pct` 是 10% 档拥挤度，与 `/api/time-prefs/stats` 的 count/cap 同源；槽数按计划实际片数（无 admin 的两小时上限）。`estimated` 读当日台账 `run_at`，无任务行时为 null 且 `estimate_note=待生成`，均非执行承诺。有任务但无缓存/覆盖窗口时 `window` 回退标准窗口并按计划同口径裁剪边距；无生效账号或无启用任务时无计划行，`window` 为标准窗口不裁剪。多任务取最早未了结，全部终态取最早；用户不传也不回 `task_id`。 |
| GET | `/api/me/sign-calendar` | user | pm7 |
|  |  |  | 本人账号的逐日签到计数；按账号当日最近一行去重，软删账号排除；`from`/`to` 必填且含首尾至多 31 天。 |
| GET | `/api/me/sign-calendar/:day` | user | pm7 |
|  |  |  | 本人账号在指定业务日的逐账号结果；他人账号与软删账号不出现；未来日 `future=true` 且 `accounts=[]`；v1 不提供 `reason`/`suggestion`。 |
| POST | `/api/me/accounts/:id/delete` | master/admin/user | 5mx |
|  |  |  | 本人软删自己的账号；`deleted_by` 记本人；审计 `account.self_delete`。已删除再删 400；他人账号 404。 |
| POST | `/api/me/accounts/:id/restore` | master/admin/user | 5mx |
|  |  |  | 撤销本人自删；仅 `deleted_by`＝本人、7 天内（`core/account.SelfRestoreGrace`）、名下无其他生效账号。管理员删除 403；超窗 409；已有生效账号 400；审计 `account.self_restore`。 |
| GET | `/api/me/mail-notify` | master/admin/user | 5mx |
|  |  |  | 本人签到失败邮件开关；缺省 true。 |
| PUT | `/api/me/mail-notify` | master/admin/user | 5mx |
|  |  |  | 写本人开关；审计 `user.mail_notify`，detail 只含字段与 on/off。 |
| GET | `/api/me/notifications` | master/admin | notify A |
|  |  |  | 本人站内信；按 `(created_at DESC, id DESC)` 键集分页，`limit` 缺省 20 上限 100。 |
| GET | `/api/me/notifications/unread-count` | master/admin | notify A |
|  |  |  | 本人未读数。 |
| POST | `/api/me/notifications/read` | master/admin | notify A |
|  |  |  | 标记本人指定 id 已读；越权/不存在 id 静默忽略，幂等。 |
| POST | `/api/me/notifications/read-all` | master/admin | notify A |
|  |  |  | 标记本人全部未读；重复调用幂等。 |
| GET | `/api/capacity` | master/admin | P4 |
| GET | `/api/sign-stats` | master/admin | M8C |
| GET | `/api/time-prefs/stats` | master/admin | M8C |
| POST | `/api/accounts/:id/sign` | master/admin/user | P4 |
| GET | `/api/me/photos` | master/admin/user | P5 |
| POST | `/api/me/photos` | master/admin/user | P5 |
| DELETE | `/api/me/photos/:id` | master/admin/user | P5 |
| GET | `/api/me/photos/:id` | master/admin/user | P5 |
| GET | `/api/me/photo-retention` | master/admin/user | P5 |
| PUT | `/api/me/photo-retention` | master/admin/user | P5 |
| GET | `/api/accounts/:id/location` | user/admin | P5 |
| PUT | `/api/accounts/:id/location` | user/admin | P5 |
| GET | `/api/accounts/:id/photos` | user/admin | P5 |
| GET | `/api/schools/:id/default-photo` | master/admin/user | P5 |
| PUT | `/api/schools/:id/default-photo` | admin | P5 |
| GET | `/api/admin/events` | master/admin | P6 |
| GET | `/api/admin/tasks` | master/admin | UX-3 swh |
| GET | `/api/admin/tasks/export` | master/admin | UX-3 swh |
| GET | `/api/admin/engine` | master/admin | UX-5 aqn |
| GET | `/api/admin/notify` | master/admin | 2ws A |
|  |  |  | 各自管理自己的订阅：返回全部类别×三渠道（in_app/email/webhook）共 12 项的有效状态；缺行按默认（review=in_app+email+webhook；credential_invalid=in_app+webhook；sign_failed_daily=email；capacity_rejected=in_app）。投递已接线：站内信＋邮件账本＋webhook 出口（kqa）。 |
| PUT | `/api/admin/notify` | master/admin | 2ws A |
|  |  |  | 幂等替换本人的覆盖集；未知类别/渠道或重复对 400；未列出的对回默认。 |
| GET | `/api/admin/auto-approve` | master/admin | ykxf |
|  |  |  | 全局自动审批开关；默认 false；行缺失也读 false。 |
| PUT | `/api/admin/auto-approve` | master | ykxf |
|  |  |  | 开/关自动审批；写 `settings.auto_approve` 审计（detail 只含字段与目标状态）。按 registration 先例，设置级写入不要求二次口令：不触碰凭据、不可逆性为零。 |
| GET | `/api/admin/settings` | master/admin | 2ws A |
|  |  |  | 只读配置名册，按组返回；敏感键只给 `{set, masked, source}`，永不回显原文；source=env/default/unknown（file 保留，本服务不产出）。键全集来自编译期嵌入的 `internal/configdata/registry.json`。 |
| POST | `/api/admin/mail/test` | master | c3e |
|  |  |  | 向当前会话邮箱同步发一封测试信；空请求体，收件人不可由客户端指定；邮件投递关闭（SMTP 未启用）与无邮箱均 409（文案不同）、发送失败 502（不含 SMTP 应答/主机/口令）；每次外呼（成功/失败）写 `settings.mail_test` 审计，detail 只含 result 与粗分类；每管理员 60s 冷却（进程内，重启清空）429＋Retry-After。 |
| POST | `/api/admin/webhook/test` | master | kqa |
|  |  |  | 向已配置的 webhook 出口同步发一条测试推送（force：跳过节流与当日额度）；空请求体；渠道未启用（ENABLE 关或 SECRET 空）409；发送失败 502（只含粗分类，不含对端应答/URL/密钥）；每次尝试（成功/失败）写 `settings.webhook_test` 审计；每管理员 60s 冷却（进程内，重启清空）429＋Retry-After。 |
| PUT | `/api/admin/announcement` | master | 2ws A |
|  |  |  | 发布或下线全站公告；去首尾空白后单行、上限 200 字（与旧线一致）；写 `settings.announcement` 审计，detail 不含正文。 |
| GET | `/api/announcement` | master/admin/user | 2ws A |
|  |  |  | 所有已登录角色读同一正文；未设置或已清空回空文本与 null 时间。 |
| GET | `/api/audit-logs` | master/admin | 2ws A |
|  |  |  | 只读审计行：游标分页＋action/actor/from/to 筛选；actor 过滤值按写入同口径掩码；detail 出口过掩码口径；不校验链、不导出。 |
| GET | `/api/admin/audit-status` | master/admin | 2cu |
|  |  |  | 按需巡检审计链：按 id 升序分窗读行（每窗 2000 行，内存 O(窗)），逐行复算 HMAC 并校验 prev_hash，跨窗用上一窗末行哈希作锚点；完好 200 `ok=true`＋链头哈希；断链 200 `ok=false`＋`broken_at`（首个不符行 id）；空链 200 `ok=false,empty=true`（零行没有证明任何事，不是通过）。只回结论、行数与时刻，不回审计正文/actor/detail。 |
| GET | `/api/me/diagnostics` | master/admin/user | P6 |
| GET | `/api/accounts/:id/diagnostics` | user/admin/master | UX-3 (was user/admin) |
| GET | `/api/me/sign-status` | master/admin/user | P6 |
| GET | `/healthz` | public | P7 |

## Enforcement notes

`additionalProperties: false` is declarative only. The Go JSON decoder
ignores unknown fields, so a client typo does not fail the request. Every
schema that declares the keyword is registered in
`internal/entry/http/oapi/contract_gates_test.go`
(`additionalPropertiesExemptions`). A new declaration without a registry
entry fails that test. `UserUpdate` is the request-side shape: a stray
`rolee` key is ignored by the decoder and the visible outcome is a 400 for
the empty body, not a silent no-op.

Nullable response fields are always `required`. The generated Go type
therefore has no `omitempty`: the key is present and carries `null` when no
value exists. `offset_min` in `TimePrefUpdate` is the same shape on the
request side: a missing key is 400 `validation_failed`, while an explicit
`null` clears the preference.

A request body over the entry limit is 413 `validation_failed` on every
endpoint that accepts a body. The status code separates a size failure from
a shape failure.

## Stable codes

`validation_failed`, `unauthorized`, `forbidden`, `not_found`, `conflict`,
`rate_limited`, `capacity_exceeded`, `capacity_shortfall`, `window_closed`,
`already_signed`, `sign_running`, `task_ambiguous`, `photo_missing`,
`photo_invalid`, `photo_rejected`, `location_out_of_range`,
`upstream_unavailable`, `internal_error`.

The list grows by addition only: values are never renamed or removed.
`capacity_shortfall` is retained for wire compatibility but is no longer
returned: a shortfall plan is built against the available capacity and
succeeds; the gap is reported in the response numbers and an administrator
capacity_rejected notification. `task_ambiguous` is retained
for wire compatibility but is no longer returned: when several equivalent
tasks apply, the worker picks one at random.
