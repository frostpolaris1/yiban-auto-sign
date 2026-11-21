/**
 * **功能**
 * 审计行详情抽屉：只读展示时间、动作、操作者、对象与 `detail` 原文。
 *
 * **归属**
 * `features/audit` 展示层：不取数，行对象由容器传入。
 *
 * **口径**
 * 详情原文不解析、不改写；长文在抽屉内换行并限制最大高度、内部滚动，不撑破栅格。
 * 焦点在关闭后回到触发行（由 `Drawer` 的 `returnFocusTo` 负责）。
 */

import { Button } from '@/components/ui/Button'
import { Drawer } from '@/components/ui/Drawer'
import type { AuditLogItem } from '@/lib/api/audit'
import { formatDateTime } from '@/lib/format'

type AuditDetailProps = {
  item: AuditLogItem | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
}

export function AuditDetail({ item, returnFocusTo, onClose }: AuditDetailProps) {
  return (
    <Drawer
      open={item !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title="审计详情"
      description={item ? `#${item.id} · ${item.action}` : undefined}
      returnFocusTo={returnFocusTo}
      footer={
        <Button variant="ghost" onClick={onClose}>
          关闭
        </Button>
      }
    >
      {item ? (
        <div className="a-detail">
          <dl className="a-detail-dl">
            <div className="a-detail-item">
              <dt>时间</dt>
              <dd>{formatDateTime(item.at)}</dd>
            </div>
            <div className="a-detail-item">
              <dt>动作</dt>
              <dd className="a-mono">{item.action}</dd>
            </div>
            <div className="a-detail-item">
              <dt>操作者</dt>
              <dd>{item.actor === '' ? '—' : item.actor}</dd>
            </div>
            <div className="a-detail-item">
              <dt>对象</dt>
              <dd className="a-mono">{item.target === '' ? '—' : item.target}</dd>
            </div>
            <div className="a-detail-item">
              <dt>行号</dt>
              <dd className="a-mono">#{item.id}</dd>
            </div>
          </dl>

          <div className="a-detail-block">
            <span className="a-detail-label">详情原文</span>
            <pre className="a-detail-text" tabIndex={0} aria-label="详情原文">
              {item.detail === '' ? '—' : item.detail}
            </pre>
          </div>
        </div>
      ) : null}
    </Drawer>
  )
}

