/**
 * **功能**
 * 时间片域 API：类型别名、路径表、窗口一览、计划预览、落库、覆盖与用户时间偏好。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径与查询串，不含展示逻辑。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`，不手工重写契约形状。
 * `refresh` 只在重新发现时为 `true`；缺省不出现该参数。
 * 预览只读；写操作只有 `POST plan` 与 `POST override` 两个端点。
 * 时间偏好只对 user 开放：`GET/PUT /api/me/time-pref`，master/admin 收到 403。
 */

import type { components } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type ScheduleWindow = Schemas['ScheduleWindow']
export type ScheduleWindowSource = Schemas['ScheduleWindowSource']
export type ScheduleWindowList = Schemas['ScheduleWindowList']
export type SchedulePlan = Schemas['SchedulePlan']
export type SchedulePlanRequest = Schemas['SchedulePlanRequest']
export type ScheduleRow = Schemas['ScheduleRow']
export type ScheduleOverrideRequest = Schemas['ScheduleOverrideRequest']
export type TimePref = Schemas['TimePref']
export type TimePrefUpdate = Schemas['TimePrefUpdate']
export type CapacityReport = Schemas['CapacityReport']

export const SCHEDULE_PATHS = {
  windows: '/api/schedule/windows',
  preview: '/api/schedule/preview',
  plan: '/api/schedule/plan',
  override: '/api/schedule/override',
  capacity: '/api/capacity',
  timePref: '/api/me/time-pref',
} as const

export type WindowsQuery = { day: string; refresh?: boolean }
export type PreviewQuery = { day: string; schoolId: string; taskId: string }
export type CapacityQuery = { day: string; schoolId: string; taskId: string; refresh?: boolean }

/** 窗口一览的查询串。`refresh` 缺省不写，服务端按缓存读。 */
export function scheduleWindowsSearch(query: WindowsQuery): string {
  const params = new URLSearchParams({ day: query.day })
  if (query.refresh) params.set('refresh', 'true')
  return `?${params.toString()}`
}

/** 预览的查询串。三个参数都是定位一行窗口的坐标。 */
export function schedulePreviewSearch(query: PreviewQuery): string {
  const params = new URLSearchParams({
    day: query.day,
    school_id: query.schoolId,
    task_id: query.taskId,
  })
  return `?${params.toString()}`
}

/**
 * 容量查询串。`school_id` 与 `task_id` 是契约必填；`day` 由调用方给，
 * 使看板与窗口列表读同一个业务日。`refresh` 缺省不写。
 */
export function capacitySearch(query: CapacityQuery): string {
  const params = new URLSearchParams({
    school_id: query.schoolId,
    task_id: query.taskId,
    day: query.day,
  })
  if (query.refresh) params.set('refresh', 'true')
  return `?${params.toString()}`
}

export function listScheduleWindows(query: WindowsQuery): Promise<ScheduleWindowList> {
  return apiRequest<ScheduleWindowList>(
    `${SCHEDULE_PATHS.windows}${scheduleWindowsSearch(query)}`,
  )
}

/** 容量体检：同一窗口解析与同一容量公式。`shortfall>0` 表示需求超容量。 */
export function getCapacity(query: CapacityQuery): Promise<CapacityReport> {
  return apiRequest<CapacityReport>(`${SCHEDULE_PATHS.capacity}${capacitySearch(query)}`)
}

/** 只读预览：不写库，`version` 在纯预览时为 0。 */
export function previewSchedule(query: PreviewQuery): Promise<SchedulePlan> {
  return apiRequest<SchedulePlan>(`${SCHEDULE_PATHS.preview}${schedulePreviewSearch(query)}`)
}

/** 落库：写窗口并重排 pending 行。停用任务返回 409。 */
export function buildSchedulePlan(body: SchedulePlanRequest): Promise<SchedulePlan> {
  return apiRequest<SchedulePlan>(SCHEDULE_PATHS.plan, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** 覆盖：写入当日窗口，`reason` 供日后审查。停用任务返回 409。 */
export function overrideScheduleWindow(body: ScheduleOverrideRequest): Promise<SchedulePlan> {
  return apiRequest<SchedulePlan>(SCHEDULE_PATHS.override, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function getTimePref(): Promise<TimePref> {
  return apiRequest<TimePref>(SCHEDULE_PATHS.timePref)
}

/** `offset_min: null` 清除偏好。冷却窗口内返回 429。 */
export function setTimePref(body: TimePrefUpdate): Promise<TimePref> {
  return apiRequest<TimePref>(SCHEDULE_PATHS.timePref, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}
