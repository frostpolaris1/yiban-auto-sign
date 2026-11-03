/**
 * **功能**
 * 编辑与位置表单的纯逻辑：草稿校验与请求体构造。
 *
 * **归属**
 * `features/accounts` 纯逻辑：无 React、无网络、无 DOM。
 *
 * **口径**
 * 凭据字段全 write-only：留空＝不修改，只带填写的字段。
 * 备注名是唯一回填字段：与打开模态时的原值相同则不发，空串表示清空。
 * 除密码外首尾空格一律去掉；密码原样透传（服务端与登录表单都不动空格）。
 * phone 必须匹配契约 pattern（11 位、1 开头）；备注名最多 50 个码点。
 * 校验判据单一来源在 `schema.ts`（S2 起委托 zod，不再各写一套）。
 * location auto 不带经纬度与理由；fixed 必填且过 WGS84 范围；理由只在不空时携带。
 * 校验不过时构造函数抛错；例外＝PATCH 空草稿返回 `{}`，
 * 由组件层的「至少填一项」把关（契约 minProperties: 1 在 UI 入口兑现）。
 * `clear_device_code` 是唯一允许发送空设备码的路径，且优先于输入值：
 * 其他情况下留空都表示不修改。
 */

import type { AccountLocationUpdate, AccountUpdate } from '@/lib/api/account'
import { accountUpdateSchema } from './schema'

export type AccountUpdateDraft = {
  phone?: string
  name?: string
  password?: string
  device_code?: string
  phone_model?: string
  /** 清除哨兵：勾选后 PATCH 体带 device_code: ''，优先于输入值。 */
  clear_device_code?: boolean
}

function clean(value: string | undefined): string {
  return (value ?? '').trim()
}

/** 留空合法；填写了 phone 时必须匹配契约 pattern；备注名不超过 50 字。 */
export function accountUpdateError(draft: AccountUpdateDraft): string | null {
  const parsed = accountUpdateSchema.safeParse(draft)
  if (parsed.success) return null
  return parsed.error.issues[0]?.message ?? null
}

/**
 * 构造 PATCH 体。
 * originalName 是模态打开时的备注名：相同则不发，避免「只改口令」顺带
 * 把一个未变的备注名写进审计；清空时 originalName 非空、name 为空串，照常发送。
 */
export function buildAccountUpdate(draft: AccountUpdateDraft, originalName = ''): AccountUpdate {
  const error = accountUpdateError(draft)
  if (error !== null) {
    throw new Error(error)
  }
  const body: AccountUpdate = {}
  const name = clean(draft.name)
  if (draft.name !== undefined && name !== clean(originalName)) {
    body.name = name
  }
  const phone = clean(draft.phone)
  if (phone !== '') body.phone = phone
  // 密码不 trim：服务端与登录表单都不动空格，trim 会静默改写用户输入的凭据。
  const password = draft.password
  if (password !== undefined && password !== '') body.password = password
  // 清除哨兵优先：勾选后输入框禁用且草稿里的旧值不再发送。
  const deviceCode = clean(draft.device_code)
  if (draft.clear_device_code === true) {
    body.device_code = ''
  } else if (deviceCode !== '') {
    body.device_code = deviceCode
  }
  const phoneModel = clean(draft.phone_model)
  if (phoneModel !== '') body.phone_model = phoneModel
  return body
}

export type LocationDraft = {
  mode: 'auto' | 'fixed'
  lng: string
  lat: string
  reason: string
}

/** fixed 必填经纬度并落在 WGS84 范围；理由最多 200 字。 */
export function locationError(draft: LocationDraft): string | null {
  if (draft.mode === 'auto') return null
  const lng = draft.lng.trim()
  const lat = draft.lat.trim()
  if (lng === '' || lat === '') return '经纬度必填'
  const lngNumber = Number(lng)
  if (!Number.isFinite(lngNumber) || lngNumber < -180 || lngNumber > 180) {
    return '经度需在 -180..180'
  }
  const latNumber = Number(lat)
  if (!Number.isFinite(latNumber) || latNumber < -90 || latNumber > 90) {
    return '纬度需在 -90..90'
  }
  if (clean(draft.reason).length > 200) return '理由最多 200 字'
  return null
}

export function buildLocationUpdate(draft: LocationDraft): AccountLocationUpdate {
  const error = locationError(draft)
  if (error !== null) {
    throw new Error(error)
  }
  if (draft.mode === 'auto') {
    return { mode: 'auto' }
  }
  const body: AccountLocationUpdate = {
    mode: 'fixed',
    lng: Number(draft.lng.trim()),
    lat: Number(draft.lat.trim()),
  }
  const reason = draft.reason.trim()
  if (reason !== '') body.reason = reason
  return body
}
