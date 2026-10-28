/**
 * **功能**
 * 学校只读详情抽屉：展示契约 `School` 的全部字段。
 *
 * **归属**
 * `features/schools` 展示层：不发请求，数据来自列表行。
 *
 * **口径**
 * 契约没有单校详情端点，`School` 全字段可读，故直接用行数据。
 * 列表刷新后同 ID 的抽屉数据由 `SchoolsView` 同步替换。
 */

import type { School } from '@/lib/api/school'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Drawer } from '@/components/ui/Drawer'
import { formatDateTime } from '@/lib/format'
import { schoolStatusLabel, schoolStatusTone } from './display'

type SchoolDetailDrawerProps = {
  school: School | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
}

export function SchoolDetailDrawer({ school, returnFocusTo, onClose }: SchoolDetailDrawerProps) {
  return (
    <Drawer
      open={school !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title="学校详情"
      description={school ? `学校 ${school.name}` : undefined}
      returnFocusTo={returnFocusTo}
      footer={
        <Button variant="ghost" onClick={onClose}>
          关闭
        </Button>
      }
    >
      {school ? (
        <dl className="app-detail-list">
          <dt>学校 ID</dt>
          <dd className="data-cell-mono">{school.id}</dd>
          <dt>代码</dt>
          <dd className="data-cell-mono">{school.code}</dd>
          <dt>名称</dt>
          <dd>{school.name}</dd>
          <dt>状态</dt>
          <dd>
            <Badge tone={schoolStatusTone(school.status)} dot>
              {schoolStatusLabel(school.status)}
            </Badge>
          </dd>
          <dt>时区</dt>
          <dd className="data-cell-mono">{school.timezone}</dd>
          <dt>创建时间</dt>
          <dd className="data-cell-mono">{formatDateTime(school.created_at)}</dd>
          <dt>更新时间</dt>
          <dd className="data-cell-mono">{formatDateTime(school.updated_at)}</dd>
        </dl>
      ) : null}
    </Drawer>
  )
}
