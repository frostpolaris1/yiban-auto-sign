/**
 * **功能**
 * 看板的文案与派生计数：状态标签、建议标签、KPI 计数、容量选项。
 *
 * **归属**
 * `features/dashboard` 展示层：纯函数，无 React、无网络。
 *
 * **口径**
 * 计数只由契约响应派生，不另造统计端点。
 * `state` 是契约文档词汇而非枚举：未知值原样显示，契约漂移必须可见。
 * `null` 状态计入「无任务」，不进已签、进行中/待签、异常三项。
 * `suggestion` 是契约枚举：中文标签四值穷尽，`Record` 在类型层保证。
 * 容量选项只取「启用且起止时间都非 null」的行。
 * 来源标签与时间片页逐字相同；结构门禁止 `features` 互导，故各留一份，
 * 由单测钉住两份不漂移。
 */

import type { Tone } from '@/components/ui/Badge'
import type { IconName } from '@/components/ui/Icon'
import type { SignStatusItem } from '@/lib/api/me'
import type { DiagnosticsSuggestion } from '@/lib/api/me'
import type { ScheduleWindow, ScheduleWindowSource } from '@/lib/api/schedule'

/** 契约文档给出的五个状态词。运行时副本，类型注解保证不漂移。 */
export const SIGN_STATES = ['pending', 'running', 'success', 'failed', 'skipped'] as const
export type SignState = (typeof SIGN_STATES)[number]

export const STATE_LABELS: Record<SignState, string> = {
  pending: '待签',
  running: '进行中',
  success: '已签',
  failed: '失败',
  skipped: '跳过',
}

const STATE_TONES: Record<SignState, Tone> = {
  pending: 'info',
  running: 'primary',
  success: 'success',
  failed: 'danger',
  skipped: 'warning',
}

export const NO_TASK_LABEL = '无任务'

function knownState(state: string | null): SignState | null {
  if (state === null) return null
  return SIGN_STATES.find((value) => value === state) ?? null
}

/** `null` 是「今日无任务行」；未知值原样返回。 */
export function stateLabel(state: string | null): string {
  const known = knownState(state)
  if (known !== null) return STATE_LABELS[known]
  return state ?? NO_TASK_LABEL
}

export function stateTone(state: string | null): Tone {
  const known = knownState(state)
  return known === null ? 'info' : STATE_TONES[known]
}

export const SUGGESTION_LABELS: Record<DiagnosticsSuggestion, string> = {
  wait: '等待',
  retry_now: '即将重试',
  re_arm: '需重新排程',
  contact_admin: '联系管理员',
}

export const SUGGESTION_TONES: Record<DiagnosticsSuggestion, Tone> = {
  wait: 'info',
  retry_now: 'warning',
  re_arm: 'purple',
  contact_admin: 'danger',
}

export type SignCounts = {
  total: number
  success: number
  active: number
  failed: number
  noTask: number
}

/**
 * 四桶派生。已知状态各落一桶；未知状态只计入 `total`，让漂移在合计差上露头。
 */
export function signCounts(items: readonly SignStatusItem[]): SignCounts {
  const counts: SignCounts = { total: items.length, success: 0, active: 0, failed: 0, noTask: 0 }
  for (const item of items) {
    const known = knownState(item.state)
    if (known === null) {
      if (item.state === null) counts.noTask += 1
      continue
    }
    if (known === 'success') counts.success += 1
    else if (known === 'pending' || known === 'running') counts.active += 1
    else counts.failed += 1
  }
  return counts
}

export type WindowCounts = {
  total: number
  reported: number
  disabled: number
  usable: number
}

/** 窗口派生：已报＝起止都非 null；可用＝启用且已报。 */
export function windowCounts(windows: readonly ScheduleWindow[]): WindowCounts {
  let reported = 0
  let disabled = 0
  let usable = 0
  for (const window of windows) {
    const hasWindow = window.start !== null && window.end !== null
    if (hasWindow) reported += 1
    if (!window.enabled) disabled += 1
    if (window.enabled && hasWindow) usable += 1
  }
  return { total: windows.length, reported, disabled, usable }
}

/** 与 `features/schedule/display.ts` 的 `SOURCE_LABELS` 逐字相同。 */
export const CAPACITY_SOURCE_LABELS: Record<ScheduleWindowSource, string> = {
  platform: '平台',
  cache: '缓存',
  fallback: '回退',
  override: '覆盖',
}

const CAPACITY_SOURCE_TONES: Record<ScheduleWindowSource, Tone> = {
  platform: 'success',
  cache: 'info',
  fallback: 'warning',
  override: 'purple',
}

export function capacitySourceLabel(source: ScheduleWindowSource): string {
  return CAPACITY_SOURCE_LABELS[source]
}

export function capacitySourceTone(source: ScheduleWindowSource): Tone {
  return CAPACITY_SOURCE_TONES[source]
}

export type CapacityOption = {
  /** `<select>` 的值：`school_id:task_id`。 */
  value: string
  label: string
  schoolId: string
  taskId: string
}

/** 选择器选项：label 是 `学校名 · 任务名`。顺序与窗口列表一致。 */
export function capacityOptions(windows: readonly ScheduleWindow[]): CapacityOption[] {
  const options: CapacityOption[] = []
  for (const window of windows) {
    if (!window.enabled) continue
    if (window.start === null || window.end === null) continue
    options.push({
      value: `${window.school_id}:${window.task_id}`,
      label: `${window.school_name} · ${window.task_name}`,
      schoolId: window.school_id,
      taskId: window.task_id,
    })
  }
  return options
}

export type KpiCard = {
  tone: 'success' | 'danger' | 'purple' | 'primary'
  icon: IconName
  label: string
  value: number | null
  unit: string
  note: string
}

/** `counts` 为 null 时值是 `—`：没有数据就不编数字。 */
export function signKpiCards(counts: SignCounts | null): KpiCard[] {
  return [
    {
      tone: 'purple',
      icon: 'accounts',
      label: '账号总数',
      value: counts?.total ?? null,
      unit: '个',
      note: counts === null ? '等待读取' : `含无任务 ${counts.noTask} 个`,
    },
    {
      tone: 'success',
      icon: 'check',
      label: '已签',
      value: counts?.success ?? null,
      unit: '个',
      note: '已完成签到',
    },
    {
      tone: 'primary',
      icon: 'calendar',
      label: '进行中/待签',
      value: counts?.active ?? null,
      unit: '个',
      note: '等待执行或执行中',
    },
    {
      tone: 'danger',
      icon: 'alertCircle',
      label: '异常',
      value: counts?.failed ?? null,
      unit: '个',
      note: '失败或跳过',
    },
  ]
}

export function windowKpiCards(counts: WindowCounts | null): KpiCard[] {
  return [
    {
      tone: 'purple',
      icon: 'calendar',
      label: '任务总数',
      value: counts?.total ?? null,
      unit: '项',
      note: '今日任务',
    },
    {
      tone: 'success',
      icon: 'check',
      label: '窗口已报',
      value: counts?.reported ?? null,
      unit: '项',
      note: '起止时间已报',
    },
    {
      tone: 'danger',
      icon: 'close',
      label: '停用任务',
      value: counts?.disabled ?? null,
      unit: '项',
      note: '不参与调度',
    },
    {
      tone: 'primary',
      icon: 'filter',
      label: '可用任务',
      value: counts?.usable ?? null,
      unit: '项',
      note: '启用且有窗口',
    },
  ]
}

export const EMPTY_SIGN_TEXT = '今日暂无签到任务'
export const EMPTY_WINDOW_TEXT = '今日无可用窗口'
export const STALE_TEXT = '数据已过期'
