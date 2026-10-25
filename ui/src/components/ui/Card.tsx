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

/** Adminator `.card` with the optional `.card-head` block. */
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
