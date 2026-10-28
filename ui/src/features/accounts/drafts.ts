/**
 * **功能**
 * 编辑与位置表单的纯逻辑：草稿校验与请求体构造。
 *
 * **归属**
 * `features/accounts` 纯逻辑：无 React、无网络、无 DOM。
 *
 * **口径**
 * PATCH 四字段全 write-only：留空＝不修改，只带填写的字段。
 * 除密码外首尾空格一律去掉；密码原样透传（服务端与登录表单都不动空格）。
 * phone 必须匹配契约 pattern（11 位、1 开头）。
 * location auto 不带经纬度与理由；fixed 必填且过 WGS84 范围；理由只在不空时携带。
 * 校验不过时构造函数抛错；例外＝PATCH 空草稿返回 `{}`，
 * 由组件层的「至少填一项」把关（契约 minProperties: 1 在 UI 入口兑现）。
 */

import type { AccountLocationUpdate, AccountUpdate } from '@/lib/api/account'

export type AccountUpdateDraft = {
  phone?: string
  password?: string
  device_code?: string
  phone_model?: string
}

const PHONE_PATTERN = /^1[0-9]{10}$/

function clean(value: string | undefined): string {
  return (value ?? '').trim()
}

/** 留空合法；填写了 phone 时必须匹配契约 pattern。 */
export function accountUpdateError(draft: AccountUpdateDraft): string | null {
  const phone = clean(draft.phone)
  if (phone !== '' && !PHONE_PATTERN.test(phone)) {
    return '手机号需 11 位、1 开头'
  }
  return null
}

export function buildAccountUpdate(draft: AccountUpdateDraft): AccountUpdate {
  const error = accountUpdateError(draft)
  if (error !== null) {
    throw new Error(error)
  }
  const body: AccountUpdate = {}
  const phone = clean(draft.phone)
  if (phone !== '') body.phone = phone
  // 密码不 trim：服务端与登录表单都不动空格，trim 会静默改写用户输入的凭据。
  const password = draft.password
  if (password !== undefined && password !== '') body.password = password
  const deviceCode = clean(draft.device_code)
  if (deviceCode !== '') body.device_code = deviceCode
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
