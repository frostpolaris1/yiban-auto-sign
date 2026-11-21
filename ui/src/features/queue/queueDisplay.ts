/**
 * **功能**
 * 待办页纯函数展示映射：业务日、窗口摘要、状态徽章、诊断建议与驳回预设。
 *
 * **归属**
 * `features/queue` 展示层：无 React、无网络请求，供容器与单元测试共用。
 *
 * **口径**
 * 任务状态文案取自 `lib/identity` 共用词表，不重复定义。
 * 账号状态与建议词表与后端契约 `AccountStatus` / `AccountDiagnostics.suggestion` 对齐。
 */

import type { Tone } from '@/components/ui/Badge'
import type { AccountStatus } from '@/lib/api/account'
import type { ScheduleWindow } from '@/lib/api/schedule'
import type { School } from '@/lib/api/school'
import type { AccountDiagnostics } from '@/lib/api/task'
import { formatClock } from '@/lib/format'
import { taskStatusLabel } from '@/lib/identity'

export const QUEUE_PAGE_SIZE = 10
export const REJECT_REASON_MAX = 200

export type ReviewSegment = 'pending' | 'rejected'
export type UnsignedSegment = 'pending' | 'running'

export const REJECT_REASON_PRESETS: readonly string[] = [
  '手机号码有误',
  '所属学校不匹配',
  '未按要求上传照片',
  '重复提交账号',
]

const WEEKDAY_FORMATTER = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  weekday: 'short',
})

/** 将 `YYYY-MM-DD` 格式化为 `2026-10-31 · 周六`。非法日期退回原文。 */
export function formatBusinessDayLabel(day: string): { date: string; weekday: string } {
  const date = new Date(`${day}T12:00:00+08:00`)
  if (Number.isNaN(date.getTime())) {
    return { date: day, weekday: '' }
  }
  return { date: day, weekday: WEEKDAY_FORMATTER.format(date) }
}

export type WindowSummaryView = {
  label: string
  detail: string
  closed: boolean
}

/** 汇总当日多个学校/任务的窗口时段（压缩技术文本，突出时段核心信息）。 */
export function formatWindowSummary(windows: readonly ScheduleWindow[]): WindowSummaryView {
  if (windows.length === 0) {
    return { label: '未配置', detail: '暂无可用窗口', closed: false }
  }

  const active = windows.filter(
    (w): w is ScheduleWindow & { start: string; end: string } =>
      w.enabled && typeof w.start === 'string' && typeof w.end === 'string',
  )

  if (active.length === 0) {
    const weekendClosed = windows.some((w) => w.source === 'weekend_closed')
    return weekendClosed
      ? { label: '今日休窗', detail: '周末不签到', closed: true }
      : { label: '未启用', detail: '请检查签到计划', closed: true }
  }

  const sortedStarts = [...active].sort(
    (a, b) => new Date(a.start).getTime() - new Date(b.start).getTime(),
  )
  const sortedEnds = [...active].sort(
    (a, b) => new Date(a.end).getTime() - new Date(b.end).getTime(),
  )
  const earliest = formatClock(sortedStarts[0].start)
  const latest = formatClock(sortedEnds[sortedEnds.length - 1].end)

  if (active.length === 1) {
    return {
      label: `${earliest} — ${latest}`,
      detail: `${active[0].school_name} · ${active[0].task_name}`,
      closed: false,
    }
  }

  return {
    label: `${earliest} — ${latest}`,
    detail: `${active.length} 个时段`,
    closed: false,
  }
}

const ACCOUNT_STATUS_META: Record<AccountStatus, { label: string; tone: Tone }> = {
  pending: { label: '待审核', tone: 'warning' },
  active: { label: '正常', tone: 'success' },
  rejected: { label: '已驳回', tone: 'danger' },
  paused_admin: { label: '管理员暂停', tone: 'info' },
  paused_user: { label: '用户暂停', tone: 'info' },
  credential_error: { label: '凭据异常', tone: 'danger' },
}

export function accountStatusMeta(status: AccountStatus): { label: string; tone: Tone } {
  return ACCOUNT_STATUS_META[status]
}

const TASK_STATE_TONES: Record<string, Tone> = {
  pending: 'info',
  running: 'primary',
  success: 'success',
  failed: 'danger',
  skipped: 'warning',
}

export function taskStateMeta(state: string): { label: string; tone: Tone } {
  return {
    label: taskStatusLabel(state),
    tone: TASK_STATE_TONES[state] ?? 'info',
  }
}

const SUGGESTION_META: Record<
  AccountDiagnostics['suggestion'],
  { label: string; tone: Tone; description: string }
> = {
  retry_now: {
    label: '可以立即重试',
    tone: 'primary',
    description: '当前处于可触发条件，建议直接执行重试。',
  },
  wait: {
    label: '等待下次调度',
    tone: 'info',
    description: '系统将在到达计划时刻或重试窗口后自动执行。',
  },
  re_arm: {
    label: '需要重新布点',
    tone: 'warning',
    description: '定位或设备环境需要重新校准后再试。',
  },
  contact_admin: {
    label: '联系管理员排查',
    tone: 'danger',
    description: '存在凭据或平台异常，需人工核查账号配置。',
  },
}

export function diagnosticsSuggestionMeta(suggestion: AccountDiagnostics['suggestion']): {
  label: string
  tone: Tone
  description: string
} {
  return SUGGESTION_META[suggestion]
}

export function schoolNameMap(schools: readonly School[]): Map<string, string> {
  const map = new Map<string, string>()
  for (const school of schools) {
    map.set(school.id, school.name)
  }
  return map
}

export type TaskResultGuide = {
  label: string
  hint: string
}

/**
 * 只登记「会落到 failed 的任务结果码」。
 * 权威词表是 `server/internal/signing/contract/contract.go` 的 `Outcome`；
 * 落 failed 的路径见 `server/internal/app/worker/worker.go` 的 `settleOutcome`：
 * 确定性失败 rejected／photo_rejected／blocked、凭据错 credential_invalid，
 * 以及重试预算耗尽的 transient／pending_upstream／unknown。
 * 后端新增词表时先改后端，再补这里；单元测试会拿契约词表校验本表。
 */
const TASK_RESULT_GUIDES: Record<string, TaskResultGuide> = {
  credential_invalid: {
    label: '凭据无效',
    hint: '需用户更新账号密码或设备信息，盲目重试通常仍失败',
  },
  rejected: {
    label: '平台拒绝请求',
    hint: '平台判定本次签到无效，先核对账号与签到点位',
  },
  photo_rejected: {
    label: '照片未通过',
    hint: '平台驳回上传照片，需更换照片后重新提交',
  },
  blocked: {
    label: '请求被拦截',
    hint: '命中平台风控或 WAF 拦截，先核查出口与账号状态',
  },
  transient: {
    label: '临时网络波动',
    hint: '重试预算已用尽，可手动重签',
  },
  pending_upstream: {
    label: '平台处理未完成',
    hint: '平台上仍在处理，稍后再重签',
  },
  unknown: {
    label: '平台返回未知结果',
    hint: '结果不明，打开诊断确认后再决定是否重试',
  },
}

/** 将服务端任务结果码映射为中文短句与处置建议，保留原码排查能力。 */
export function describeTaskResult(code: string): TaskResultGuide {
  const normalized = code.trim()
  if (!normalized) {
    return { label: '执行失败', hint: '请打开诊断查看详细信息' }
  }
  const hit = TASK_RESULT_GUIDES[normalized]
  if (hit) return hit
  return { label: '平台返回异常', hint: '可打开诊断确认是否值得重试' }
}

/** 供单元测试校验：本表登记的码必须已在契约词表里。 */
export function taskResultGuideCodes(): string[] {
  return Object.keys(TASK_RESULT_GUIDES)
}

/** 分页页脚诚实摘要，不拿末页条数冒充全量总数。 */
export function formatPagedSummary(pageIndex: number, count: number, hasNext: boolean): string {
  const base = `第 ${pageIndex + 1} 页 · 本页 ${count} 条`
  return hasNext ? `${base}（还有更多）` : base
}

/**
 * 驳回理由预设标签点选：芯片的选中态就代表「理由里含这条」，点击即切换。
 * - 空输入 → 填入该预设；
 * - 已含该预设 → 从理由里移除（分隔符一并收干净）；
 * - 手写内容 + 未含该预设 → 末尾追加，超 200 字则不追加。
 */
export function applyRejectPreset(current: string, preset: string): string {
  const trimmed = current.trim()
  if (trimmed === '') return preset
  const parts = trimmed
    .split('；')
    .map((part) => part.trim())
    .filter(Boolean)
  if (parts.includes(preset)) {
    return parts.filter((part) => part !== preset).join('；')
  }
  const combined = `${trimmed}；${preset}`
  return combined.length <= REJECT_REASON_MAX ? combined : current
}
