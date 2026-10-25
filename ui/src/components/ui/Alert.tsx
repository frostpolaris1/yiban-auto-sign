import { useState, type ReactNode } from 'react'
import { cx } from '@/lib/cx'
import { Icon, type IconName } from '@/components/ui/Icon'
import type { Tone } from './Badge'

const TONE_ICON: Record<Tone, IconName> = {
  primary: 'info',
  success: 'check',
  warning: 'alertCircle',
  danger: 'alertCircle',
  info: 'info',
  purple: 'info',
}

type AlertProps = {
  tone?: Tone
  title?: string
  dismissible?: boolean
  onDismiss?: () => void
  children: ReactNode
}

/** Adminator `.alert`. The close button has no vendor behavior, so React owns it. */
export function Alert({ tone = 'info', title, dismissible, onDismiss, children }: AlertProps) {
  const [visible, setVisible] = useState(true)
  if (!visible) return null

  return (
    <div className={cx('alert', tone)} role={tone === 'danger' ? 'alert' : 'status'}>
      <span className="ico">
        <Icon name={TONE_ICON[tone]} />
      </span>
      <div className="body">
        {title ? <div className="title">{title}</div> : null}
        {children}
      </div>
      {dismissible ? (
        <button
          type="button"
          className="close"
          aria-label="关闭提示"
          onClick={() => {
            setVisible(false)
            onDismiss?.()
          }}
        >
          <Icon name="close" />
        </button>
      ) : null}
    </div>
  )
}
