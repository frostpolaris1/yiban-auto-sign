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
- No phone number, credential or upstream raw text in any response.

## Conditional requests

`GET /api/me/photos/{id}` sends `ETag` on both the `200` and the `304`
answer. `If-None-Match` uses the weak comparison function: `W/"x"` matches
`"x"`, comma lists and `*` are honoured. Repeated `If-None-Match` header
lines are merged into one comma-separated value per RFC 9110 section 5.3
before binding, so a well-formed list answers `304` or `200`, never `400`.
A `304` carries `ETag` and carries no `Content-Type`, no `Content-Length`
and no body. The `304` response declares the `ETag` header in the YAML.

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
| POST | `/api/users` | master | P1 |
| GET | `/api/schools` | master/admin | P1 |
| POST | `/api/schools` | master | P1 |
| PATCH | `/api/schools/:id` | master | P1 |
| GET | `/api/schools/:id/admins` | master | P1 |
| POST | `/api/schools/:id/admins` | master | P1 |
| DELETE | `/api/schools/:id/admins/:uid` | master | P1 |
| GET | `/api/accounts` | master/admin/user | P1 |
| POST | `/api/accounts` | master/admin/user | P1 |
| GET | `/api/accounts/:id` | master/admin/user | P1 |
| PATCH | `/api/accounts/:id` | master/admin/user | P1 |
| POST | `/api/accounts/:id/review` | master/admin | P1 |
| POST | `/api/accounts/:id/pause` | master/admin/user | P1 |
| DELETE | `/api/accounts/:id` | master/admin/user | P1 |
| POST | `/api/accounts/:id/verify` | master/admin | P3 |
| GET | `/api/schedule/windows` | master/admin | P4 |
| GET | `/api/schedule/preview` | master/admin | P4 |
| POST | `/api/schedule/plan` | master/admin | P4 |
| POST | `/api/schedule/override` | master/admin | P4 |
| GET | `/api/me/time-pref` | user | P4 |
| PUT | `/api/me/time-pref` | user | P4 |
| GET | `/api/capacity` | master/admin | P4 |
| POST | `/api/accounts/:id/sign` | master/admin/user | P4 |
| GET | `/api/me/photos` | user | P5 |
| POST | `/api/me/photos` | user | P5 |
| DELETE | `/api/me/photos/:id` | user | P5 |
| GET | `/api/me/photos/:id` | user/admin | P5 |
| PUT | `/api/me/photo-retention` | user | P5 |
| PUT | `/api/accounts/:id/location` | user/admin | P5 |
| GET | `/api/accounts/:id/photos` | user/admin | P5 |
| GET | `/api/schools/:id/default-photo` | master/admin/user | P5 |
| PUT | `/api/schools/:id/default-photo` | master/admin | P5 |
| GET | `/api/admin/events` | master/admin | P6 |
| GET | `/api/me/diagnostics` | user/admin | P6 |
| GET | `/api/accounts/:id/diagnostics` | user/admin | P6 |
| GET | `/api/me/sign-status` | user | P6 |
| GET | `/healthz` | public | P7 |

## Enforcement notes

`additionalProperties: false` is declarative only. The Go JSON decoder
ignores unknown fields, so a client typo does not fail the request. Every
schema that declares the keyword is registered in
`internal/entry/http/oapi/contract_gates_test.go`
(`additionalPropertiesExemptions`). A new declaration without a registry
entry fails that test.

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
`rate_limited`, `capacity_exceeded`, `window_closed`, `already_signed`,
`sign_running`, `photo_missing`, `photo_invalid`, `photo_rejected`,
`location_out_of_range`, `upstream_unavailable`, `internal_error`.
