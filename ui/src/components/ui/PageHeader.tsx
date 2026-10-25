/**
 * **功能**
 * 页头：眉标、主标题、副标题、右侧动作。
 *
 * **归属**
 * `components` 通用件：只排版，不取数。
 *
 * **口径**
 * 每页一个 `<h1>`。主标题放在这里。
 * 页面内不再另起一级标题。
 */

import type { ReactNode } from 'react'

type PageHeaderProps = {
  eyebrow: string
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
}

export function PageHeader({ eyebrow, title, subtitle, actions }: PageHeaderProps) {
  return (
    <div className="hero">
      <div className="hero-text">
        <span className="eyebrow">{eyebrow}</span>
        <h1 className="hero-title">{title}</h1>
        {subtitle ? <p className="hero-sub">{subtitle}</p> : null}
      </div>
      {actions ? <div className="hero-actions">{actions}</div> : null}
    </div>
  )
}
