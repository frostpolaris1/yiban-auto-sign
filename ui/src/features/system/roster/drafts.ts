/**
 * **功能**
 * 系统名册区（#roster）的纯展示判据：来源三态文案、非敏感值格式化、
 * 敏感键视图选择与读取错误文案。
 *
 * **归属**
 * `features/system/roster` 纯逻辑层：零 React / DOM 依赖，直接跑 Node 单测。
 *
 * **口径**
 * - 来源三态如实：`env`→环境变量、`db`→数据库覆盖、`default`→默认、
 *   `file`→配置文件、`unknown`→未知（未设置且无缺省）；未知来源原样回显枚举值兜底。
 * - 敏感视图**只读 `sensitive` 对象**：`item.value` 即使被畸形出包带上原值也绝不消费。
 *   掩码固定 `****`（服务端契约），未设置时为空串。
 * - 非敏感 `value` 为 `null` 表示未设置且无缺省，显式说「未设置」，不冒充空值。
 */

import { ApiError } from '@/lib/api/client'
import type { SettingItem, SettingSource } from '@/lib/api/adminSettings'

export const ROSTER_SOURCE_LABELS: Record<SettingSource, string> = {
  env: '环境变量',
  file: '配置文件',
  db: '数据库覆盖',
  default: '默认',
  unknown: '未知',
}

/** 敏感键「已设置」时服务端固定下发的掩码。 */
export const SENSITIVE_MASK = '****'

export function rosterSourceLabel(source: SettingSource): string {
  return ROSTER_SOURCE_LABELS[source] ?? source
}

export function rosterSourceTone(source: SettingSource): 'info' | 'warning' | undefined {
  if (source === 'db') return 'info'
  if (source === 'env') return 'warning'
  return undefined
}

/** 非敏感键的取值视图：字符串/数字/布尔如实回显；null 说「未设置」。 */
export function formatSettingValue(value: SettingItem['value']): string {
  if (value === null || value === undefined) return '未设置'
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'number') return String(value)
  if (value === '') return '空字符串'
  return value
}

/** 敏感键视图：只消费 `item.sensitive`，绝不读取 `item.value`。 */
export function sensitiveSettingView(item: SettingItem): {
  stateLabel: string
  masked: string
  source: SettingSource
} {
  const view = item.sensitive
  if (!view) {
    return { stateLabel: '未提供敏感视图', masked: '', source: item.source }
  }
  return {
    stateLabel: view.set ? '已设置' : '未设置',
    masked: view.set ? SENSITIVE_MASK : '',
    source: view.source,
  }
}

/** 名册读取失败文案：只报类别与状态，不回显底层错误正文。 */
export function formatRosterReadError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return '登录已过期，请重新登录后再查看名册'
    if (error.status === 403) return '权限不足：仅主管理员与普通管理员可查看名册'
    return '名册读取失败，请稍后重试'
  }
  return '网络异常或服务不可达，名册读取失败'
}
