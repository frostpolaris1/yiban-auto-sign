/**
 * **功能**
 * 管理员本人通知订阅矩阵 API：读取与全量替换生效订阅。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径与调请求，不含展示逻辑。
 *
 * **口径**
 * 类型全部取自生成的 `schema.d.ts`（`NotifyPref`、`NotifyPrefPage`、`NotifyPrefsUpdate`）。
 * 契约：仅 `master`/`admin` 可读写自己的生效订阅；`user` 一律 403。
 * `GET` 返回 4 类 × 3 渠道共 12 项生效态；`PUT` 全量替换调用者覆盖集并回传新态。
 */

import { apiRequest } from './client'
import type { components } from './schema'

type Schemas = components['schemas']

export type NotifyCategory = Schemas['NotifyCategory']
export type NotifyChannel = Schemas['NotifyChannel']
export type NotifyPref = Schemas['NotifyPref']
export type NotifyPrefPage = Schemas['NotifyPrefPage']
export type NotifyPrefsUpdate = Schemas['NotifyPrefsUpdate']

export const ADMIN_NOTIFY_PATH = '/api/admin/notify'

export function getAdminNotify(): Promise<NotifyPrefPage> {
  return apiRequest<NotifyPrefPage>(ADMIN_NOTIFY_PATH)
}

export function setAdminNotify(body: NotifyPrefsUpdate): Promise<NotifyPrefPage> {
  return apiRequest<NotifyPrefPage>(ADMIN_NOTIFY_PATH, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}
