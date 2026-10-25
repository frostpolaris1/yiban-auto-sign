# Host API v1 — frozen shape

P0 freezes the shape. The OpenAPI 3.1 YAML is generated in P1 from this file.

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

## Roles

| Role | Scope |
|---|---|
| master | all schools, admins, settings, audit, egress |
| admin | bound schools only |
| user | own accounts |

## Endpoints

| Method | Path | Roles | Phase |
|---|---|---|---|
| POST | `/api/auth/login` | public | P1 |
| POST | `/api/auth/logout` | any | P1 |
| GET | `/api/auth/me` | any | P1 |
| POST | `/api/auth/forgot-password` | public | P1 |
| POST | `/api/auth/reset-password` | public | P1 |
| POST | `/api/users` | master | P1 |
| GET | `/api/schools` | master | P1 |
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
| POST | `/api/accounts/:id/verify` | master/admin/user | P3 |
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
| GET | `/api/admin/events` | master/admin (SSE) | P6 |
| GET | `/api/me/diagnostics` | user/admin | P6 |
| GET | `/api/accounts/:id/diagnostics` | user/admin | P6 |
| GET | `/api/me/sign-status` | user (polling) | P6 |
| GET | `/healthz` | public | P7 |

## Stable codes

`validation_failed`, `unauthorized`, `forbidden`, `not_found`, `conflict`,
`rate_limited`, `capacity_exceeded`, `window_closed`, `already_signed`,
`sign_running`, `photo_missing`, `photo_invalid`, `photo_rejected`,
`location_out_of_range`, `upstream_unavailable`, `internal_error`.
