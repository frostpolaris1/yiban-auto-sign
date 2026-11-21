/**
 * **功能**
 * 单条站内通知行：类别图标、标题、两行正文预览、相对时间与单条标为已读操作。
 *
 * **归属**
 * `features/notify` 展示层：只接收条数据与回调，不持有查询状态。
 *
 * **口径**
 * `read_at === null` 渲染未读底色与圆点；已读后移除未读态，焦点留在本行不丢。
 * 展示模式只读：禁用已读按钮并给出 `READ_ONLY_REASON`。
 */

import { useEffect, useRef } from 'react'
import { Badge } from '@/components/ui/Badge'
import { BusyButton } from '@/components/ui/BusyButton'
import { Icon } from '@/components/ui/Icon'
import type { Notification } from '@/lib/api/notify'
import { cx } from '@/lib/cx'
import { formatDateTime } from '@/lib/format'
import { READ_ONLY_REASON } from '@/lib/showcase-mode'
import { categoryMeta, formatNotificationTime } from './display'

type InboxItemProps = {
  item: Notification
  marking: boolean
  readOnly: boolean
  onMarkRead: (id: string) => void
}

export function InboxItem({ item, marking, readOnly, onMarkRead }: InboxItemProps) {
  const unread = item.read_at === null
  const meta = categoryMeta(item.category)
  const itemRef = useRef<HTMLLIElement | null>(null)
  const activatedReadRef = useRef(false)

  useEffect(() => {
    if (!unread && activatedReadRef.current) {
      activatedReadRef.current = false
      itemRef.current?.focus({ preventScroll: true })
    } else if (unread && !marking) {
      activatedReadRef.current = false
    }
  }, [unread, marking])

  return (
    <li
      ref={itemRef}
      tabIndex={-1}
      className={cx('app-inbox-item', unread && 'is-unread')}
      data-unread={unread ? 'true' : 'false'}
    >
      <article className="app-inbox-article">
        <div className={cx('app-inbox-icon', `is-${meta.tone}`)} aria-hidden="true">
          <Icon name={meta.icon} size={17} />
        </div>

        <div className="app-inbox-content">
          <div className="app-inbox-head">
            <div className="app-inbox-title-wrap">
              {unread ? <span className="app-inbox-dot" aria-hidden="true" /> : null}
              <strong className="app-inbox-title">{item.title}</strong>
            </div>
            <time
              className="dd-time"
              dateTime={item.created_at}
              title={formatDateTime(item.created_at)}
            >
              {formatNotificationTime(item.created_at)}
            </time>
          </div>

          <p className="app-inbox-preview">{item.body}</p>

          <div className="app-inbox-meta">
            <div className="app-inbox-tags">
              <Badge tone={meta.tone}>{meta.label}</Badge>
              {item.school_id === null ? <span className="app-inbox-scope">全站</span> : null}
            </div>
            {unread ? (
              <BusyButton
                variant="ghost"
                size="sm"
                className="app-inbox-read-btn"
                busy={marking}
                busyLabel="处理中…"
                disabled={readOnly}
                title={readOnly ? READ_ONLY_REASON : undefined}
                aria-label={`标为已读：${item.title}`}
                onClick={() => {
                  activatedReadRef.current = true
                  onMarkRead(item.id)
                }}
              >
                标为已读
              </BusyButton>
            ) : (
              <span className="app-inbox-read-state">已读</span>
            )}
          </div>
        </div>
      </article>
    </li>
  )
}
