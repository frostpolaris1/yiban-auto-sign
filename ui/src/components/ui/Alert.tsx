/**
 * **功能**
 * Adminator 提示条：图标、标题、可关闭。
 *
 * **归属**
 * `components` 通用件：不发请求，不读域状态。
 *
 * **口径**
 * 关闭按钮由 React 控制。Adminator 未给该按钮行为。
 * 危险色用 `alert` 角色，其余用 `status`。
 */

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

/**
 * 关闭状态只在本组件内存里，父组件不传受控值。
 * 关闭后整块从 DOM 移除，不再占位。
 */
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
