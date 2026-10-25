/**
 * **功能**
 * 状态徽章（`badge`）与状态标签（`tag`）。
 *
 * **归属**
 * `components` 通用件：只映射 Adminator 类名。
 *
 * **口径**
 * `Tone` 是徽章色，`TagTone` 是标签色。两套枚举不混用。
 */

import type { ReactNode } from 'react'
import { cx } from '@/lib/cx'

export type Tone = 'primary' | 'success' | 'warning' | 'danger' | 'info' | 'purple'

type BadgeProps = {
  tone?: Tone
  dot?: boolean
  solid?: boolean
  children: ReactNode
}

export function Badge({ tone, dot, solid, children }: BadgeProps) {
  return <span className={cx('badge', tone, dot && 'dot', solid && 'solid')}>{children}</span>
}

export type TagTone = 't-new' | 't-used' | 't-old' | 't-unavail' | 't-active' | 't-info'

export function Tag({ tone, children }: { tone: TagTone; children: ReactNode }) {
  return <span className={cx('tag', tone)}>{children}</span>
}
