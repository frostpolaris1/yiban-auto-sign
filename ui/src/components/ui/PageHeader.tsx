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
