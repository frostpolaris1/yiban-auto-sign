/**
 * **功能**
 * 时间片域的纯逻辑：请求体构造、窗口覆盖草稿校验、直方图换算、时间偏好选项与错误文案。
 *
 * **归属**
 * `features/schedule` 纯逻辑：无 React、无网络、无 DOM。
 *
 * **口径**
 * 覆盖请求体只含契约六字段：day、school_id、task_id、start、end、reason。
 * reason 去首尾空格后必须 1..200 字；end 必须严格晚于 start。
 * 时间输入按上海墙钟解释，输出 RFC3339 且固定 `+08:00`。
 * 时间偏好偏移是 5 的倍数，范围 0..120；请求体只含 offset_min。
 * 409 一律读作「任务已停用」；429 与 403 的文案是定稿。
 */

import type {
  ScheduleOverrideRequest,
  SchedulePlan,
  SchedulePlanRequest,
} from '../../lib/api/schedule'
import { ApiError, describeApiError } from '../../lib/api/client'
import { isoToShanghaiInput, isValidShanghaiInput, shanghaiInputToRfc3339 } from '../../lib/format'

export const OVERRIDE_REASON_MAX = 200
export const TIME_PREF_MIN = 0
export const TIME_PREF_MAX = 120
export const TIME_PREF_STEP = 5

export type OverrideDraft = {
  /** `<input type="datetime-local">` 的值，上海墙钟。 */
  start: string
  end: string
  reason: string
}

export const EMPTY_OVERRIDE_DRAFT: OverrideDraft = { start: '', end: '', reason: '' }

/**
 * 覆盖草稿的第一条问题；无问题时返回 null。
 * 时间值都是定宽 `YYYY-MM-DDTHH:mm`，可直接按字符串比较先后。
 * 非法时间形状（被手工输入改坏）也在此报错：
 * buildOverrideBody 不得在渲染期抛错。
 */
export function overrideDraftError(draft: OverrideDraft): string | null {
  if (draft.start === '') return '开始时间必填'
  if (draft.end === '') return '结束时间必填'
  if (!isValidShanghaiInput(draft.start) || !isValidShanghaiInput(draft.end)) {
    return '时间格式必须是 YYYY-MM-DDTHH:mm'
  }
  if (draft.end <= draft.start) return '结束时间必须晚于开始时间'
  const reason = draft.reason.trim()
  if (reason === '') return '修改原因必填'
  if (reason.length > OVERRIDE_REASON_MAX) return `修改原因最多 ${OVERRIDE_REASON_MAX} 字`
  return null
}

/** 校验不过时抛错：调用方只在校验通过后调用。 */
export function buildOverrideBody(input: {
  day: string
  schoolId: string
  taskId: string
  draft: OverrideDraft
}): ScheduleOverrideRequest {
  const error = overrideDraftError(input.draft)
  if (error !== null) throw new Error(error)
  return {
    day: input.day,
    school_id: input.schoolId,
    task_id: input.taskId,
    start: shanghaiInputToRfc3339(input.draft.start),
    end: shanghaiInputToRfc3339(input.draft.end),
    reason: input.draft.reason.trim(),
  }
}

export function buildPlanBody(input: {
  day: string
  schoolId: string
  taskId: string
}): SchedulePlanRequest {
  return { day: input.day, school_id: input.schoolId, task_id: input.taskId }
}

/** 覆盖表单预填：当前有效窗口换成上海墙钟，原因留空。 */
export function overrideDraftFromPlan(plan: SchedulePlan): OverrideDraft {
  return {
    start: isoToShanghaiInput(plan.window_start),
    end: isoToShanghaiInput(plan.window_end),
    reason: '',
  }
}

export type HistogramBar = { slice: number; count: number; heightPct: number }

/**
 * 直方图换算：按切片号数字升序；柱高是 count 与峰值的百分比。
 * 非数字键丢弃；峰值为 0 时所有柱高为 0。
 */
export function histogramBars(histogram: Record<string, number>): HistogramBar[] {
  const entries = Object.entries(histogram)
    .map(([key, count]) => ({ slice: Number(key), count }))
    .filter((entry) => Number.isInteger(entry.slice) && entry.slice >= 0 && Number.isFinite(entry.count))
    .sort((left, right) => left.slice - right.slice)
  const peak = entries.reduce((max, entry) => Math.max(max, entry.count), 0)
  return entries.map((entry) => ({
    ...entry,
    heightPct: peak > 0 ? Math.round((entry.count / peak) * 100) : 0,
  }))
}

/** 偏移选项：0 到 120，步进 5。值用字符串，直接喂给 `<select>`。 */
export function timePrefOptions(): Array<{ value: string; label: string }> {
  const options: Array<{ value: string; label: string }> = []
  for (let offset = TIME_PREF_MIN; offset <= TIME_PREF_MAX; offset += TIME_PREF_STEP) {
    options.push({ value: String(offset), label: timePrefLabel(offset) })
  }
  return options
}

export function timePrefLabel(offset: number | null): string {
  if (offset === null) return '未设置'
  return `窗口开始 + ${offset} 分钟`
}

/** 偏移控件的第一条问题；无问题时返回 null。 */
export function timePrefOffsetError(value: string): string | null {
  if (value === '') return '偏移分钟必填'
  const offset = Number(value)
  if (!Number.isInteger(offset)) return '偏移分钟必须是整数'
  if (offset < TIME_PREF_MIN || offset > TIME_PREF_MAX) {
    return `偏移分钟需在 ${TIME_PREF_MIN}–${TIME_PREF_MAX} 之间`
  }
  if (offset % TIME_PREF_STEP !== 0) return `偏移分钟必须是 ${TIME_PREF_STEP} 的倍数`
  return null
}

export function timePrefErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429) return '修改太频繁，请稍后再试'
    if (error.status === 403) return '此部署未开放时间偏好'
  }
  return describeApiError(error)
}

export function scheduleErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return '任务已停用'
    if (error.status === 403) return '当前角色无权执行此操作'
  }
  return describeApiError(error)
}
