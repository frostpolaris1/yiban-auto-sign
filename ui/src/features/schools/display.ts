/**
 * **功能**
 * 学校状态的文案与徽章色。
 *
 * **归属**
 * `features/schools` 展示层：纯函数，无 React、无网络。
 *
 * **口径**
 * 状态值只取自契约 `SchoolStatus`：`active | disabled`。
 * 两个文案引用 `lib/identity` 的共用状态词，与用户状态同源。
 * 时间格式化在 `lib/format`。
 */

import type { Tone } from '@/components/ui/Badge'
import type { SchoolStatus } from '@/lib/api/school'
import { ACTIVE_STATUS_LABEL, DISABLED_STATUS_LABEL } from '@/lib/identity'

const LABELS: Record<SchoolStatus, string> = {
  active: ACTIVE_STATUS_LABEL,
  disabled: DISABLED_STATUS_LABEL,
}

/** 下拉选项与标签同源，避免两处文案漂移。 */
export const SCHOOL_STATUS_OPTIONS: Array<{ value: SchoolStatus; label: string }> = (
  Object.keys(LABELS) as SchoolStatus[]
).map((value) => ({ value, label: LABELS[value] }))

const TONES: Record<SchoolStatus, Tone> = {
  active: 'success',
  disabled: 'warning',
}

export function schoolStatusLabel(status: SchoolStatus): string {
  return LABELS[status]
}

export function schoolStatusTone(status: SchoolStatus): Tone {
  return TONES[status]
}
