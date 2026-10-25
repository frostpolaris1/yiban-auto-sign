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
