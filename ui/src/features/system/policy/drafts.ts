/**
 * **功能**
 * 运行策略区（#policy）纯校验与写回请求体构造。
 *
 * **归属**
 * `features/system/policy` 纯逻辑层：零 React / DOM 依赖，直接跑 Node 单测。
 *
 * **口径**
 * - 边界缩进域 `[0, 300]` 整数秒；配额域 `[0, 100000]` 整数（0 = 不限，默认 = 1）。
 * - 默认签到窗口接受 `HH:MM` 墙钟格式且要求 `start < end`（两键成对提交）。
 * - 未改动字段不写入补丁；若无任何改动，返回 `null` 避免发出空 JSON `{}` 触发 400。
 */

import { ApiError } from '@/lib/api/client'
import type {
  AdminQuotaSettingsUpdate,
  AdminScheduleSettingsUpdate,
  QuotaReport,
  ScheduleSettingsReport,
  ScheduleSettingSource,
  ScheduleSignDist,
} from '@/lib/api/adminSettings'

export const EDGE_SEC_MIN = 0
export const EDGE_SEC_MAX = 300
export const QUOTA_MIN = 0
export const QUOTA_MAX = 100000

export const POLICY_READ_ONLY_REASON = '仅主管理员可修改全站运行策略'

export const SOURCE_LABELS: Record<ScheduleSettingSource, string> = {
  db: '数据库覆盖',
  env: '环境变量',
  default: '默认',
}

export const SIGN_DIST_OPTIONS: ReadonlyArray<{
  value: ScheduleSignDist
  label: string
  detail: string
}> = [
  {
    value: 'front',
    label: '前段顺序填充（front）',
    detail: '按时间片自前向后填充（默认策略）',
  },
  {
    value: 'uniform',
    label: '全窗均匀分散（uniform）',
    detail: '将计划行分散至整个签到窗口',
  },
]

export function settingSourceLabel(source: ScheduleSettingSource): string {
  return SOURCE_LABELS[source] ?? source
}

/** 把日内分钟数 [0, 1439] 转成 `HH:MM`。非法值退回 `'00:00'`。 */
export function minutesToClockInput(minutes: number): string {
  if (!Number.isInteger(minutes) || minutes < 0 || minutes > 1439) {
    return '00:00'
  }
  const h = String(Math.floor(minutes / 60)).padStart(2, '0')
  const m = String(minutes % 60).padStart(2, '0')
  return `${h}:${m}`
}

const CLOCK_INPUT_RE = /^([01]\d|2[0-3]):([0-5]\d)$/

/** 把 `HH:MM` 解析为日内分钟数 [0, 1439]；格式错误返回 null。 */
export function parseClockInputToMinutes(raw: string): number | null {
  const trimmed = raw.trim()
  const match = CLOCK_INPUT_RE.exec(trimmed)
  if (!match) return null
  const hour = Number(match[1])
  const minute = Number(match[2])
  return hour * 60 + minute
}

function parseBoundedInt(raw: string, min: number, max: number): number | null {
  const trimmed = raw.trim()
  if (!/^\d+$/.test(trimmed)) return null
  const value = Number(trimmed)
  if (!Number.isSafeInteger(value) || value < min || value > max) return null
  return value
}

export type SchedulePolicyDraft = {
  edgeFrontSec: string
  edgeBackSec: string
  allowTimePref: boolean
  saturdaySign: boolean
  sundaySign: boolean
  defaultSignStartClock: string
  defaultSignEndClock: string
  signDist: ScheduleSignDist
}

export type SchedulePolicyErrors = {
  edgeFrontSec?: string
  edgeBackSec?: string
  defaultSignStartClock?: string
  defaultSignEndClock?: string
}

export function createSchedulePolicyDraft(report: ScheduleSettingsReport): SchedulePolicyDraft {
  return {
    edgeFrontSec: String(report.edge_front_sec.value),
    edgeBackSec: String(report.edge_back_sec.value),
    allowTimePref: report.allow_time_pref.value,
    saturdaySign: report.saturday_sign.value,
    sundaySign: report.sunday_sign.value,
    defaultSignStartClock: minutesToClockInput(report.default_sign_start_min.value),
    defaultSignEndClock: minutesToClockInput(report.default_sign_end_min.value),
    signDist: report.sign_dist.value,
  }
}

export function validateSchedulePolicyDraft(draft: SchedulePolicyDraft): SchedulePolicyErrors {
  const errors: SchedulePolicyErrors = {}
  if (parseBoundedInt(draft.edgeFrontSec, EDGE_SEC_MIN, EDGE_SEC_MAX) === null) {
    errors.edgeFrontSec = `请输入 ${EDGE_SEC_MIN} 至 ${EDGE_SEC_MAX} 之间的整数秒`
  }
  if (parseBoundedInt(draft.edgeBackSec, EDGE_SEC_MIN, EDGE_SEC_MAX) === null) {
    errors.edgeBackSec = `请输入 ${EDGE_SEC_MIN} 至 ${EDGE_SEC_MAX} 之间的整数秒`
  }
  const startMin = parseClockInputToMinutes(draft.defaultSignStartClock)
  const endMin = parseClockInputToMinutes(draft.defaultSignEndClock)
  if (startMin === null) {
    errors.defaultSignStartClock = '请填写有效的开始时间（HH:MM）'
  }
  if (endMin === null) {
    errors.defaultSignEndClock = '请填写有效的结束时间（HH:MM）'
  }
  if (startMin !== null && endMin !== null && startMin >= endMin) {
    errors.defaultSignEndClock = '结束时间必须晚于开始时间'
  }
  return errors
}

/** 构造调度写回补丁；若无任何字段变化则返回 null，防止空对象触发服务端 400。 */
export function buildScheduleSettingsPatch(
  draft: SchedulePolicyDraft,
  initial: ScheduleSettingsReport,
): AdminScheduleSettingsUpdate | null {
  const patch: AdminScheduleSettingsUpdate = {}
  const front = parseBoundedInt(draft.edgeFrontSec, EDGE_SEC_MIN, EDGE_SEC_MAX)
  const back = parseBoundedInt(draft.edgeBackSec, EDGE_SEC_MIN, EDGE_SEC_MAX)
  const startMin = parseClockInputToMinutes(draft.defaultSignStartClock)
  const endMin = parseClockInputToMinutes(draft.defaultSignEndClock)
  if (front === null || back === null || startMin === null || endMin === null || startMin >= endMin) {
    return null
  }
  if (front !== initial.edge_front_sec.value) patch.edge_front_sec = front
  if (back !== initial.edge_back_sec.value) patch.edge_back_sec = back
  if (draft.allowTimePref !== initial.allow_time_pref.value) {
    patch.allow_time_pref = draft.allowTimePref
  }
  if (draft.saturdaySign !== initial.saturday_sign.value) {
    patch.saturday_sign = draft.saturdaySign
  }
  if (draft.sundaySign !== initial.sunday_sign.value) {
    patch.sunday_sign = draft.sundaySign
  }
  if (
    startMin !== initial.default_sign_start_min.value ||
    endMin !== initial.default_sign_end_min.value
  ) {
    patch.default_sign_start_min = startMin
    patch.default_sign_end_min = endMin
  }
  if (draft.signDist !== initial.sign_dist.value) {
    patch.sign_dist = draft.signDist
  }
  return Object.keys(patch).length > 0 ? patch : null
}

/** 显式 null 清除全部 8 个调度键的数据库覆盖行，使其回到环境/内置缺省。 */
export function buildScheduleResetPatch(): AdminScheduleSettingsUpdate {
  return {
    edge_front_sec: null,
    edge_back_sec: null,
    allow_time_pref: null,
    sunday_sign: null,
    saturday_sign: null,
    default_sign_start_min: null,
    default_sign_end_min: null,
    sign_dist: null,
  }
}

export type QuotaPolicyDraft = {
  maxUsers: string
  maxAccounts: string
}

export type QuotaPolicyErrors = {
  maxUsers?: string
  maxAccounts?: string
}

export function createQuotaPolicyDraft(report: QuotaReport): QuotaPolicyDraft {
  return {
    maxUsers: String(report.max_users.value),
    maxAccounts: String(report.max_accounts.value),
  }
}

export function validateQuotaPolicyDraft(draft: QuotaPolicyDraft): QuotaPolicyErrors {
  const errors: QuotaPolicyErrors = {}
  if (parseBoundedInt(draft.maxUsers, QUOTA_MIN, QUOTA_MAX) === null) {
    errors.maxUsers = `请输入 ${QUOTA_MIN} 至 ${QUOTA_MAX} 之间的整数（0 表示不限）`
  }
  if (parseBoundedInt(draft.maxAccounts, QUOTA_MIN, QUOTA_MAX) === null) {
    errors.maxAccounts = `请输入 ${QUOTA_MIN} 至 ${QUOTA_MAX} 之间的整数（0 表示不限）`
  }
  return errors
}

/** 构造配额写回补丁；若两项数值均未变化则返回 null，防止空对象触发 400。 */
export function buildQuotaSettingsPatch(
  draft: QuotaPolicyDraft,
  initial: QuotaReport,
): AdminQuotaSettingsUpdate | null {
  const users = parseBoundedInt(draft.maxUsers, QUOTA_MIN, QUOTA_MAX)
  const accounts = parseBoundedInt(draft.maxAccounts, QUOTA_MIN, QUOTA_MAX)
  if (users === null || accounts === null) return null
  const patch: AdminQuotaSettingsUpdate = {}
  if (users !== initial.max_users.value) patch.max_users = users
  if (accounts !== initial.max_accounts.value) patch.max_accounts = accounts
  return Object.keys(patch).length > 0 ? patch : null
}

/** 显式 null 清除两项配额覆盖行，使其恢复默认紧档 1。 */
export function buildQuotaResetPatch(): AdminQuotaSettingsUpdate {
  return { max_users: null, max_accounts: null }
}

/**
 * 把系统运行策略接口的失败收拢成可读中文说明。
 * 后端 `code · message` 原文不外泄：已知状态给对应话术，其余状态兜底中文。
 */
export function formatSystemWriteError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return '登录已过期，请重新登录后再试'
    }
    if (error.status === 403) {
      return `权限不足：${POLICY_READ_ONLY_REASON}`
    }
    if (error.status === 400) {
      return '参数不符合要求，请核对输入范围与格式'
    }
    if (error.status === 429) {
      const wait = error.retryAfterSec ? `，请约 ${error.retryAfterSec} 秒后再试` : ''
      return `请求过于频繁${wait || '，请稍后重试'}`
    }
    return '保存失败，请稍后重试'
  }
  return '网络异常或服务不可达，请稍后重试'
}
