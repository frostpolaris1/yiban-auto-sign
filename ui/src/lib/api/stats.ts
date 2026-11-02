/**
 * **功能**
 * 看板聚合 API：签到终态统计与自选时间片人数。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径与查询串，不含展示逻辑。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`，不手工重写契约形状。
 * 两个端点都只给 master/admin；user 的今日状态走 `me/sign-status`。
 * `days` 取 1..90；窗口终点由服务端按部署时区定为今天。
 */

import type { components } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type SignStatsReport = Schemas['SignStatsReport']
export type SignStatsDay = Schemas['SignStatsDay']
export type SignStatsCounts = Schemas['SignStatsCounts']
export type PrefStatsReport = Schemas['PrefStatsReport']
export type PrefStatsSlot = Schemas['PrefStatsSlot']

export const STATS_PATHS = {
  sign: '/api/sign-stats',
  pref: '/api/time-prefs/stats',
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

/** 自选时间片人数。school/task 与容量卡的选择器同源。 */
export function getPrefStats(query: PrefStatsQuery): Promise<PrefStatsReport> {
  const params = new URLSearchParams({
    school_id: query.schoolId,
    task_id: query.taskId,
    day: query.day,
  })
  return apiRequest(`${STATS_PATHS.pref}?${params.toString()}`)
}
