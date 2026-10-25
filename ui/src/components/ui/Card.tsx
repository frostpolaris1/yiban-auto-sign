/**
 * **功能**
 * Adminator 卡片，可带 `.card-head` 头块。
 *
 * **归属**
 * `components` 通用件：只排版，不取数。
 *
 * **口径**
 * eyebrow、title、action 任一存在才渲染头块。
 * 三者全无时不输出空头块。
 */

import type { ReactNode } from 'react'
import { cx } from '@/lib/cx'

type CardProps = {
  id?: string
  eyebrow?: string
  title?: ReactNode
  action?: ReactNode
  className?: string
  children: ReactNode
}

/** 卡片容器。头块缺失时 DOM 少一层，不留空白。 */
export function Card({ id, eyebrow, title, action, className, children }: CardProps) {
  const hasHead = eyebrow !== undefined || title !== undefined || action !== undefined
  return (
    <section id={id} className={cx('card', className)}>
      {hasHead ? (
        <div className="card-head">
          <div className="card-title-wrap">
            {eyebrow ? <span className="eyebrow">{eyebrow}</span> : null}
            {title ? <h2 className="card-title">{title}</h2> : null}
          </div>
          {action ? <div>{action}</div> : null}
        </div>
      ) : null}
      {children}
    </section>
  )
}
