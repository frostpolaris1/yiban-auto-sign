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
|  |  |  | 401 同形（clc）：口令错与不存在同码同文案；本人注销、7 天窗内且口令正确时 `details` 增补 `recoverable:true`＋`recover_until`（RFC3339，注销时刻＋宽限）引导走 `/api/me/restore`，不建会话；超窗、管理员删除、口令错不带该字段（与不存在同形，防枚举）；沿用 `unauthorized`，不新增错误码。 |
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
|  |  |  | 本人注销登录账号：`current_password` 二次口令（缺 400/错 403/连败 429＋Retry-After）；软删 `users` 行并连带软删名下全部生效账号；注销全部会话并清 cookie；master 403；7 天宽限（`core/user.SelfRestoreGrace`）；成功 200＋`recover_until`（恢复窗终点，RFC3339），注销当下即告知可恢复期限；宽限内邮箱挡公开重注册，手机号随软删立即释放（保号取舍：释放＝防占号，代价＝可被抢注，见 `docs/ops/cutover.md` §6）；审计 `user.self_delete`，detail 不含邮箱/手机号。 |
| POST | `/api/me/restore` | public | lac2 |
|  |  |  | 未登录可调：邮箱＋登录口令；仅恢复 7 天内本人软删的登录账号及注销时生效的那一行账号；口令错、账号不存在、超窗、管理员删除一律同一条 401，不泄露存在性；口令验对后撞活跃行唯一索引（手机号被抢注、邮箱被顶号）报 409 conflict，文案不含占用者身份，事务整体回滚；容量满 429；恢复不建立会话；按客户端 IP 与归一邮箱限流，429 带 `Retry-After`；审计 `user.self_restore`，detail 不含邮箱/手机号。 |
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
|  |  |  | `status=disabled` 挡新不挡旧：该校启用任务不进日切计划、活跃账号不进铺点、手动签到不提供新候选；停校前已铺的 pending 行照常认领执行。管理读面与窗口发现仍可用，既有数据仍可读。 |
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
|  |  |  | 只读当日窗口缓存：GET 不触发平台发现（`refresh` 查询参数保留仅为线兼容并忽略）；强制刷新走 `POST /api/schedule/windows/refresh`。 |
| POST | `/api/schedule/windows/refresh` | master/admin | abs R1 |
|  |  |  | 强制发现全部任务的平台窗口并落缓存；响应体与 GET 同 schema。 |
| GET | `/api/schedule/preview` | master/admin | P4 |
|  |  |  | 只读缓存/覆盖/回退窗口并返回与 Build 相同的计划行；GET 不触发平台发现；强制刷新走 `POST /api/schedule/preview/refresh`。 |
| POST | `/api/schedule/preview/refresh` | master/admin | abs R1 |
|  |  |  | 先发现平台窗口再预览；响应体与 GET 同 schema，仍不写库。 |
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
|  |  |  | 只读缓存/覆盖/回退窗口的容量报告：GET 不触发平台发现（`refresh` 查询参数保留仅为线兼容并忽略）；强制刷新走 `POST /api/capacity/refresh`。 |
| POST | `/api/capacity/refresh` | master/admin | abs R1 |
|  |  |  | 先发现平台窗口并落缓存，再返回容量报告；响应体与 GET 同 schema。 |
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
|  |  |  | 单进程执行监视快照；响应含 `paused`（全局暂停开关，settings 行 engine.paused，缺行=false，DB 持久跨重启）。 |
| PUT | `/api/admin/engine/pause` | master/admin | 3a 28l |
|  |  |  | 写全局暂停开关（settings 行 engine.paused，DB 持久、热生效、跨重启）：按请求目标值分闸——`paused=true`（暂停）master/admin 皆可，`paused=false`（恢复）仅 master（旧线急停先例）；两字段（paused、current_password）必填，缺 400、口令错 403，均零写入零审计；连错沿用全局再认证节流 429；settings 查询故障上抛不静默放行；写入与恰一行 `settings.engine_paused` 审计同事务，detail 只含目标态（操作者在审计行主体列）；幂等重放允许且每次恰一行；恢复写 false 不删行（缺行也读 false，本端点无删行语义）；200 回显 `{"paused": bool}`。 |
| GET | `/api/admin/notify` | master/admin | 2ws A |
|  |  |  | 各自管理自己的订阅：返回全部类别×三渠道（in_app/email/webhook）共 12 项的有效状态；缺行按默认（review=in_app+email+webhook；credential_invalid=in_app+webhook；sign_failed_daily=email；capacity_rejected=in_app）。投递已接线：站内信＋邮件账本＋webhook 出口（kqa）。 |
| PUT | `/api/admin/notify` | master/admin | 2ws A |
|  |  |  | 幂等替换本人的覆盖集；未知类别/渠道或重复对 400；未列出的对回默认。 |
| GET | `/api/admin/auto-approve` | master/admin | ykxf |
|  |  |  | 全局自动审批开关；默认 false；行缺失也读 false。 |
| PUT | `/api/admin/auto-approve` | master | ykxf |
|  |  |  | 开/关自动审批；写 `settings.auto_approve` 审计（detail 只含字段与目标状态）。按 registration 先例，设置级写入不要求二次口令：不触碰凭据、不可逆性为零。 |
| GET | `/api/admin/settings` | master/admin | 2ws A |
|  |  |  | 只读配置名册，按组返回；敏感键只给 `{set, masked, source}`，永不回显原文；source=env/default/unknown（file 保留，本服务不产出）。可写回的调度三键（schedule.edge_front_sec、schedule.edge_back_sec、schedule.allow_time_pref）额外读 settings 覆盖行：有效行存在时报 source=db 与行值，DB 覆盖 env（sett2）。键全集来自编译期嵌入的 `internal/configdata/registry.json`。 |
| PUT | `/api/admin/schedule-settings` | master | sett2 |
|  |  |  | 部分补丁写回调度键：缺字段=不动该键、显式 null=删覆盖行回缺省、值=upsert；sett2 三键（edge 两键收 0–300 整数秒、allow_time_pref 收 true/false 字面）＋批 3b 四枚 DB-only 键（sunday_sign/saturday_sign 周末门布尔，缺行=false=当日不铺行，行为对齐旧线周末缺省不签；default_sign_start_min/end_min 收 0–1439 分钟，成对生效：单侧写入与生效值组合后仍需 start<end 否则 400，读面半对/非法整对回退内置 06:30/07:50）＋批 3c 的 sign_dist（收小写 front|uniform：front 顺序装满前段且为缺行默认档（既有行为），uniform 全窗分层；大写/未知词 400）。越域、JSON 类型错、未知字段 400 且零写入零审计；写入与 `settings.schedule_writeback` 审计同事务，detail 只含变更键旧→新。edge、窗口与分布档改值次日构建生效（当日已铺行不回改）；allow_time_pref 次请求生效；五枚机制键不出现在名册响应。200 回全量名册（同 GET 形态）。 |
| GET | `/api/admin/quota-settings` | master/admin | r4r |
|  |  |  | 读两枚 DB-only 配额键（quota.max_users/quota.max_accounts）的生效值与来源：缺行=1（master 自用紧档），0=不限哨兵；source=db（有效覆盖行）/default（缺行或坏行，坏行每键 Warn 一次）。配额键不进 GET /api/admin/settings 名册；存储故障 500，不报成缺省。 |
| PUT | `/api/admin/quota-settings` | master | r4r |
|  |  |  | 部分补丁写回配额两键：缺字段=不动该键、显式 null=删覆盖行回缺省、值=upsert；两键收 0–100000 整数（0=不限：计数检查整体跳过，一人一号规则独立不动）。越域、JSON 类型错、未知字段、空体 400 且零写入零审计；写入与恰一行 `settings.quota_writeback` 审计同事务，detail 只含变更键旧→新。写回热生效（下一次注册/建号/恢复即用新值，无需重启）；删行不回收存量行，只拒新写。200 回生效配额报告。 |
| GET | `/api/admin/mail-settings` | master/admin | smtp4 |
|  |  |  | 读六枚 DB-only SMTP 键（mail.smtp_host/mail.smtp_port/mail.tls_mode/mail.from/mail.user/mail.pass）的生效值与来源：缺行=env 值（source=env）或 registry 缺省（source=default）；host/port/tls_mode 回真实值，from/user 只回 `{set, masked:"****", source}`，pass 只回 `{set, source}`（无值、无掩码、无密文）。坏行（空主机/越域端口/词表外 TLS/无法解密的密文）回退 env 并每键 Warn 一次；存储故障 500，不报成缺省。六键不进 GET /api/admin/settings 名册。 |
| PUT | `/api/admin/mail-settings` | master | smtp4 |
|  |  |  | 部分补丁写回六键：缺字段=不动该键、显式 null=删覆盖行回 env、值=upsert；host 收合法主机名/IP，port 收 1–65535 整数，tls_mode 收小写 auto|implicit|starttls，from 收可投递地址，user/pass 收非空串。**任一字段变更都要求 current_password**：缺失/空 400、口令错 403，均零写入零审计；pass 写入前用部署凭据钥 AES-GCM 加密（复用 account cipher），密文 base64 落 settings 行，明文不进日志/审计/响应；加密失败 500 零写入。越域、JSON 类型错、未知字段、空体 400 且零写入零审计；写入与恰一行 `settings.mail_writeback` 审计同事务，detail 的 from/user/pass 只记 set/cleared，host/port/tls_mode 记字面旧→新。写回热生效（下一次 SMTP 外呼即用新值，无需重启）。200 回生效设置报告。 |
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
| GET | `/api/changelog` | public | v1.0.0 |
|  |  |  | `CHANGELOG.md` 全文，进程内首读缓存；`YIBAN_DOCS_DIR` 未设或读不到时 200＋`text="暂无更新日志"`。 |
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
