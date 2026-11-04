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

- Pagination: `?cursor=&limit=` (default 50, max 200).
- Cross-scope object: **404**. Action forbidden: **403**.
- Rate limits: the inbound limiter answers **429** with `Retry-After` in
  seconds. Login, reset password, change password, verify and sign are limited
  per client IP and per account key; forgot password is limited per client IP
  only; register is limited per client IP and per normalized email. Account
  create and update are limited per caller. The admin test-email endpoint is
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
| POST | `/api/auth/forgot-password` | public | P1 |
| POST | `/api/auth/reset-password` | public | P1 |
| POST | `/api/auth/change-password` | master/admin/user | P1 |
| GET | `/api/auth/registration` | public | S05 |
| PUT | `/api/auth/registration` | master | S05 |
| POST | `/api/auth/register` | public | S05 |
| GET | `/api/users` | master | P1 |
| POST | `/api/users` | master | P1 |
| PATCH | `/api/users/:id` | master | UX-1 |
| POST | `/api/users/:id/password-reset` | master | UX-1 |
| DELETE | `/api/users/:id` | master | UX-1 |
| POST | `/api/users/:id/restore` | master | UX-1 |
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
| GET | `/api/accounts/summary` | master/admin | N190 |
|  |  |  | 只数未软删账号：`pending` 与 `rejected` 两计数，供看板待处理 KPI 与账号页提示条。 |
| GET | `/api/accounts/:id` | master/admin/user | P1 |
| PATCH | `/api/accounts/:id` | master/admin/user | P1 |
| POST | `/api/accounts/:id/review` | master/admin | P1 |
| POST | `/api/accounts/:id/pause` | master/admin/user | P1 |
| DELETE | `/api/accounts/:id` | master/admin/user | P1 |
| POST | `/api/accounts/:id/device-refresh` | master/admin/user | P4 |
| POST | `/api/accounts/:id/restore` | master/admin | P4 |
| POST | `/api/accounts/:id/verify` | master/admin | P3 |
| GET | `/api/schedule/windows` | master/admin | P4 |
| GET | `/api/schedule/preview` | master/admin | P4 |
| POST | `/api/schedule/plan` | master/admin | P4 |
| POST | `/api/schedule/override` | master/admin | P4 |
| GET | `/api/me/time-pref` | master/admin/user | P4 |
| PUT | `/api/me/time-pref` | master/admin/user | P4 |
| POST | `/api/me/accounts/:id/delete` | master/admin/user | 5mx |
|  |  |  | 本人软删自己的账号；`deleted_by` 记本人；审计 `account.self_delete`。已删除再删 400；他人账号 404。 |
| POST | `/api/me/accounts/:id/restore` | master/admin/user | 5mx |
|  |  |  | 撤销本人自删；仅 `deleted_by`＝本人、7 天内（`core/account.SelfRestoreGrace`）、名下无其他生效账号。管理员删除 403；超窗 409；已有生效账号 400；审计 `account.self_restore`。 |
| GET | `/api/me/mail-notify` | master/admin/user | 5mx |
|  |  |  | 本人签到失败邮件开关；缺省 true。 |
| PUT | `/api/me/mail-notify` | master/admin/user | 5mx |
|  |  |  | 写本人开关；审计 `user.mail_notify`，detail 只含字段与 on/off。 |
| GET | `/api/capacity` | master/admin | P4 |
| GET | `/api/sign-stats` | master/admin | M8C |
| GET | `/api/time-prefs/stats` | master/admin | M8C |
| POST | `/api/accounts/:id/sign` | master/admin/user | P4 |
| GET | `/api/me/photos` | master/admin/user | P5 |
| POST | `/api/me/photos` | master/admin/user | P5 |
| DELETE | `/api/me/photos/:id` | master/admin/user | P5 |
| GET | `/api/me/photos/:id` | master/admin/user | P5 |
| PUT | `/api/me/photo-retention` | master/admin/user | P5 |
| PUT | `/api/accounts/:id/location` | user/admin | P5 |
| GET | `/api/accounts/:id/photos` | user/admin | P5 |
| GET | `/api/schools/:id/default-photo` | master/admin/user | P5 |
| PUT | `/api/schools/:id/default-photo` | admin | P5 |
| GET | `/api/admin/events` | master/admin | P6 |
| GET | `/api/admin/tasks` | master/admin | UX-3 swh |
| GET | `/api/admin/tasks/export` | master/admin | UX-3 swh |
| GET | `/api/admin/engine` | master/admin | UX-5 aqn |
| GET | `/api/admin/notify` | master/admin | 2ws A |
|  |  |  | 各自管理自己的订阅：返回全部类别×渠道的有效状态；缺行按默认（review=in_app+email；credential_invalid=in_app；sign_failed_daily=email；capacity_rejected=in_app）。本阶段只存取，不投递。 |
| PUT | `/api/admin/notify` | master/admin | 2ws A |
|  |  |  | 幂等替换本人的覆盖集；未知类别/渠道或重复对 400；未列出的对回默认。 |
| GET | `/api/admin/settings` | master/admin | 2ws A |
|  |  |  | 只读配置名册，按组返回；敏感键只给 `{set, masked, source}`，永不回显原文；source=env/default/unknown（file 保留，本服务不产出）。键全集来自编译期嵌入的 `internal/configdata/registry.json`。 |
| POST | `/api/admin/mail/test` | master | c3e |
|  |  |  | 向当前会话邮箱同步发一封测试信；空请求体，收件人不可由客户端指定；邮件投递关闭（SMTP 未启用）与无邮箱均 409（文案不同）、发送失败 502（不含 SMTP 应答/主机/口令）；每次外呼（成功/失败）写 `settings.mail_test` 审计，detail 只含 result 与粗分类；每管理员 60s 冷却（进程内，重启清空）429＋Retry-After。 |
| PUT | `/api/admin/announcement` | master | 2ws A |
|  |  |  | 发布或下线全站公告；去首尾空白后单行、上限 200 字（与旧线一致）；写 `settings.announcement` 审计，detail 不含正文。 |
| GET | `/api/announcement` | master/admin/user | 2ws A |
|  |  |  | 所有已登录角色读同一正文；未设置或已清空回空文本与 null 时间。 |
| GET | `/api/audit-logs` | master/admin | 2ws A |
|  |  |  | 只读审计行：游标分页＋action/actor/from/to 筛选；actor 过滤值按写入同口径掩码；detail 出口过掩码口径；不校验链、不导出。 |
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
`capacity_shortfall` is a plan-build refusal (409, details carry demand,
capacity, shortfall, binding and, when raising concurrency within the
64-channel cap fits the demand, suggested_workers); `task_ambiguous` means two
enabled tasks apply to the same account and the server will not guess which one
a manual sign means.
