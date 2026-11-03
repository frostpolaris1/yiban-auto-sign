/**
 * **功能**
 * 看板的文案与派生计数：状态标签、建议标签、KPI 计数、今日终态口径、容量选项。
 *
 * **归属**
 * `features/dashboard` 展示层：纯函数，无 React、无网络。
 *
 * **口径**
 * 计数只由契约响应派生，不另造统计端点。
 * user 的四卡来自 `me/sign-status` 条目；master/admin 的四卡来自
 * `sign-stats` 报告最后两天（今日终态），窗口计数只作容量卡注脚。
 * 成功率＝成功÷（成功+失败），跳过不计入；口径对齐旧线 `rateOf`。
 * `state` 是契约文档词汇而非枚举：未知值原样显示，契约漂移必须可见。
 * `null` 状态计入「无任务」，不进已签、进行中/待签、异常三项。
 * `suggestion` 是契约枚举：中文标签四值穷尽，`Record` 在类型层保证。
 * 容量选项只取「启用且起止时间都非 null」的行。
 * 来源标签与时间片页逐字相同；结构门禁止 `features` 互导，故各留一份，
 * 由单测钉住两份不漂移。
 */

import type { Tone } from '@/components/ui/Badge'
import type { IconName } from '@/components/ui/Icon'
import type { AccountSummary } from '@/lib/api/account'
import type { SignStatusItem } from '@/lib/api/me'
import type { DiagnosticsSuggestion } from '@/lib/api/me'
import type { ScheduleWindow, ScheduleWindowSource } from '@/lib/api/schedule'
import type { SignStatsReport } from '@/lib/api/stats'

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
  /** 成功率卡的环比 pill；其余卡不设。 */
  pill?: RatePill | null
  /** 整卡链接目标。设置时卡片渲染为站内链接，可访问名称来自卡内文本。 */
  href?: '/accounts'
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

/** 环比 pill：`cls` 对应 `.kpi-pill` 的三档配色。 */
export type RatePill = { text: string; cls: 'up' | 'down' | 'flat' }

export type TodayStats = {
  success: number
  failed: number
  /** 进行中 + 待签（running + pending），仍是当日账号口径。 */
  active: number
  /** 成功率＝成功÷（成功+失败），百分数保留一位小数；分母 0 为 null。 */
  rate: number | null
  pill: RatePill | null
}

type StatsDayBuckets = Pick<SignStatsReport['days'][number], 'pending' | 'running' | 'success' | 'failed' | 'skipped'>

/** 成功率＝成功÷（成功+失败）；跳过是「未定论」，不进分母。 */
function dayRate(day: StatsDayBuckets | undefined): number | null {
  const success = day?.success ?? 0
  const failed = day?.failed ?? 0
  const settled = success + failed
  return settled > 0 ? Math.round((success / settled) * 1000) / 10 : null
}

/**
 * 今日终态口径：取报告 `days` 的最后两项（最后一天是今天）。
 * 日期缺失按 0 桶处理，不编造；报告未到或 `days` 为空返回 null，四卡显「—」。
 * pill 只在今日成功率可算时出现：昨日无终态（成功+失败为 0）给「无昨日对比」。
 */
export function todayStats(report: SignStatsReport | null | undefined): TodayStats | null {
  if (report === null || report === undefined || report.days.length === 0) return null
  const today = report.days.at(-1)
  const yesterday = report.days.at(-2)
  const rate = dayRate(today)
  let pill: RatePill | null = null
  if (rate !== null) {
    const previous = dayRate(yesterday)
    if (previous === null) {
      pill = { text: '无昨日对比', cls: 'flat' }
    } else {
      const diff = Math.round((rate - previous) * 10) / 10
      pill = {
        text: `较昨日 ${diff > 0 ? '+' : ''}${diff}pp`,
        cls: diff > 0 ? 'up' : diff < 0 ? 'down' : 'flat',
      }
    }
  }
  return {
    success: today?.success ?? 0,
    failed: today?.failed ?? 0,
    active: (today?.running ?? 0) + (today?.pending ?? 0),
    rate,
    pill,
  }
}

/** master/admin 四卡：今日成功、今日失败、今日成功率、进行中+待签。 */
export function todayKpiCards(stats: TodayStats | null): KpiCard[] {
  return [
    {
      tone: 'success',
      icon: 'check',
      label: '今日成功',
      value: stats?.success ?? null,
      unit: '个',
      note: '按账号当日终态',
    },
    {
      tone: 'danger',
      icon: 'alertCircle',
      label: '今日失败',
      value: stats?.failed ?? null,
      unit: '个',
      note: '按账号当日终态',
    },
    {
      tone: 'purple',
      icon: 'gauge',
      label: '今日成功率',
      value: stats?.rate ?? null,
      unit: '%',
      note: '成功 ÷（成功 + 失败）',
      pill: stats?.pill ?? null,
    },
    {
      tone: 'primary',
      icon: 'activity',
      label: '进行中/待签',
      value: stats?.active ?? null,
      unit: '个',
      note: '等待执行或执行中',
    },
  ]
}

/**
 * 第 5 卡「待处理账号」：值＝pending+rejected（未软删），note 拆两数。
 * 汇总未到或读取失败都是 null：显示「—」，不编 0。整卡链接到账号页。
 */
export function pendingAccountCard(summary: AccountSummary | null): KpiCard {
  const pending = summary?.pending ?? null
  const rejected = summary?.rejected ?? null
  const value = pending === null || rejected === null ? null : pending + rejected
  return {
    tone: 'purple',
    icon: 'edit',
    label: '待处理账号',
    value,
    unit: '个',
    note:
      pending === null || rejected === null
        ? '等待读取'
        : `待审核 ${pending} · 已拒绝 ${rejected}`,
    href: '/accounts',
  }
}

export const EMPTY_SIGN_TEXT = '今日暂无签到任务'
export const EMPTY_WINDOW_TEXT = '今日无可用窗口'
export const STALE_TEXT = '数据已过期'
