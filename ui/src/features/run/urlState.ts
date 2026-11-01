/**
 * **功能**
 * 运行页的 URL 状态：业务日、状态、学校、检索词与游标互转。
 *
 * **归属**
 * `features/run` 纯逻辑：无 React、无网络、无 DOM。
 * `app/router.tsx` 的 `validateSearch` 与 `RunView` 每次渲染都经过这里，
 * 单元测试直接覆盖。
 *
 * **口径**
 * URL 是唯一事实源：`?day&state&school&q&cursor`。
 * 空值与缺省值都不产生键，保证同状态同 URL。
 * `day` 只收 `YYYY-MM-DD` 真日历日（`lib/format.parseDay`）；
 * `state` 只收五个任务状态词；其余值在解析时丢弃（校验即净化）。
 * 游标是服务端的不透明串，原样透传。
 * 筛选变化必须丢游标：`withRunFilter` 统一清，调用方不再各写一份。
 */

import type { TaskQuery } from '@/lib/api/task'
import { parseDay } from '@/lib/format'
import { TASK_STATES } from '@/lib/identity'

/** 状态筛选取值域：契约生成的查询枚举，不另写一份。 */
export type RunStateFilter = NonNullable<TaskQuery['state']>

export type RunURLState = {
  day?: string
  state?: RunStateFilter
  school?: string
  q?: string
  cursor?: string
}

/** 从路由 search 记录读状态。未知键忽略，值非法时按缺省处理。 */
export function parseRunSearchRecord(search: Record<string, unknown>): RunURLState {
  const state: RunURLState = {}
  const day = parseDay(search.day)
  if (day) state.day = day
  const taskState = search.state
  if (typeof taskState === 'string' && TASK_STATES.find((value) => value === taskState)) {
    state.state = taskState as RunStateFilter
  }
  const school = search.school
  if (typeof school === 'string' && school !== '') state.school = school
  const q = search.q
  if (typeof q === 'string' && q !== '') state.q = q
  const cursor = search.cursor
  if (typeof cursor === 'string' && cursor !== '') state.cursor = cursor
  return state
}

/** 把状态写成路由 search 记录。空值与缺省值不产生键。 */
export function runSearchRecord(state: RunURLState): Record<string, string> {
  const record: Record<string, string> = {}
  if (state.day) record.day = state.day
  if (state.state) record.state = state.state
  if (state.school) record.school = state.school
  if (state.q) record.q = state.q
  if (state.cursor) record.cursor = state.cursor
  return record
}

/** 换筛选：统一回到第一页。游标只在显式翻页时存在。 */
export function withRunFilter(
  state: RunURLState,
  patch: Partial<Omit<RunURLState, 'cursor'>>,
): RunURLState {
  const next: RunURLState = { ...state, ...patch }
  delete next.cursor
  if (!next.day) delete next.day
  if (!next.state) delete next.state
  if (!next.school) delete next.school
  if (!next.q) delete next.q
  return next
}
