/**
 * **功能**
 * 时间片页的文案与徽章色：窗口来源、任务启停。
 *
 * **归属**
 * `features/schedule` 展示层：纯函数，无 React、无网络。
 *
 * **口径**
 * 来源值只取自契约 `ScheduleWindowSource`：`platform | cache | fallback | override`。
 * 契约漂移时原样显示未知值，不静默改写。
 * 启停来自 `ScheduleWindow.enabled`。
 * 标签是全仓唯一一份。时间格式化在 `lib/format`。
 */

import type { Tone } from '@/components/ui/Badge'
import type { ScheduleWindowSource } from '../../lib/api/schedule'

/** 契约来源枚举的运行时副本。类型注解保证它不漂移。 */
const CONTRACT_SOURCES: readonly ScheduleWindowSource[] = [
  'platform',
  'cache',
  'fallback',
  'override',
]

export const SOURCE_LABELS: Record<ScheduleWindowSource, string> = {
  platform: '平台',
  cache: '缓存',
  fallback: '回退',
  override: '覆盖',
}

const SOURCE_TONES: Record<ScheduleWindowSource, Tone> = {
  platform: 'success',
  cache: 'info',
  fallback: 'warning',
  override: 'purple',
}

function knownSource(source: string | null): ScheduleWindowSource | null {
  if (source === null) return null
  return CONTRACT_SOURCES.find((value) => value === source) ?? null
}

/** 未知值原样返回：契约漂移必须可见。 */
export function sourceLabel(source: string | null): string {
  const known = knownSource(source)
  if (known !== null) return SOURCE_LABELS[known]
  return source ?? '—'
}

export function sourceTone(source: string | null): Tone {
  const known = knownSource(source)
  return known === null ? 'info' : SOURCE_TONES[known]
}

export function enabledLabel(enabled: boolean): string {
  return enabled ? '已启用' : '已停用'
}

export function enabledTone(enabled: boolean): Tone {
  return enabled ? 'success' : 'warning'
}
