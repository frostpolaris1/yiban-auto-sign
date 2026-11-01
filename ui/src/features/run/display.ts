/**
 * **功能**
 * 运行页的展示构建：任务状态文案与徽章色调的引用表。
 *
 * **归属**
 * `features/run` 展示层：只做映射，不取数、不判角色。
 *
 * **口径**
 * 一词一义：任务状态文案只取 `lib/identity` 的共用词表，
 * 本文件不得再写字面量（单元测试扫描钉住）。
 * 徽章色调是本页自己的呈现决定，不算词表。
 * 「在飞」＝running，「待执行」＝pending：页面文案用操作词，状态徽章用共用词。
 */

import type { Tone } from '@/components/ui/Badge'
import { TASK_STATES, TASK_STATE_LABELS, type TaskState } from '@/lib/identity'

const STATE_TONES: Record<TaskState, Tone> = {
  pending: 'info',
  running: 'primary',
  success: 'success',
  failed: 'danger',
  skipped: 'warning',
}

/** 任务状态徽章的共用文案。 */
export function runStateLabel(state: string): string {
  return taskStateWord(state)
}

/** 任务状态徽章色调。未知值按 info 处理。 */
export function runStateTone(state: string): Tone {
  const known = TASK_STATES.find((value) => value === state)
  return known ? STATE_TONES[known] : 'info'
}

function taskStateWord(state: string): string {
  const known = TASK_STATES.find((value) => value === state)
  return known ? TASK_STATE_LABELS[known] : state
}
