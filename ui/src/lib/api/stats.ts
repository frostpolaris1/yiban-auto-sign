/**
 * **功能**
 * 看板聚合 API：签到终态统计与自选时间片人数。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径与查询串，不含展示逻辑。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`，不手工重写契约形状。
 * 三个端点都只给 master/admin；user 的今日状态走 `me/sign-status`。
 * `days` 取 1..90；窗口终点由服务端按部署时区定为今天。
 * 失败明细按「失败结果词表」分桶，桶内 result 为空时服务端归为 `unknown`，词表外的值原样返回。
 */

import type { components } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type SignStatsReport = Schemas['SignStatsReport']
export type SignStatsDay = Schemas['SignStatsDay']
export type SignStatsCounts = Schemas['SignStatsCounts']
export type PrefStatsReport = Schemas['PrefStatsReport']
export type PrefStatsSlot = Schemas['PrefStatsSlot']
export type SignFailuresReport = Schemas['SignFailuresReport']
export type SignFailuresScope = Schemas['SignFailuresScope']
export type SignFailureBucket = Schemas['SignFailureBucket']

export const STATS_PATHS = {
  sign: '/api/sign-stats',
  pref: '/api/time-prefs/stats',
  failures: '/api/sign-stats/failures',
} as const

/** 签到终态统计。缺省窗口 30 天。 */
export function getSignStats(days = 30): Promise<SignStatsReport> {
  const params = new URLSearchParams({ days: String(days) })
  return apiRequest(`${STATS_PATHS.sign}?${params.toString()}`)
}

export type PrefStatsQuery = {
  schoolId: string
  taskId: string
  day: string
}

export type SignFailuresQuery = {
  /** 业务日 YYYY-MM-DD；缺省由服务端取部署时区的今天。 */
  day?: string
  /** 只统计某校；admin 只能查自己绑定的学校，跨校 404。 */
  schoolId?: string
  /** 与 schoolId 成对；单独给或与学校不匹配 400。 */
  taskId?: string
}

/** 今日失败明细：把 failed 桶按失败结果拆开。查询串只带非空值。 */
export function getSignFailures(query: SignFailuresQuery = {}): Promise<SignFailuresReport> {
  const params = new URLSearchParams()
  if (query.day) params.set('day', query.day)
  if (query.schoolId) params.set('school_id', query.schoolId)
  if (query.taskId) params.set('task_id', query.taskId)
  const search = params.toString()
  return apiRequest(`${STATS_PATHS.failures}${search ? `?${search}` : ''}`)
}

/** 自选时间片人数。school/task 与容量卡的选择器同源。 */
export function getPrefStats(query: PrefStatsQuery): Promise<PrefStatsReport> {
  const params = new URLSearchParams({
    school_id: query.schoolId,
    task_id: query.taskId,
    day: query.day,
  })
  return apiRequest(`${STATS_PATHS.pref}?${params.toString()}`)
}
