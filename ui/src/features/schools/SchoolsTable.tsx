/**
 * **功能**
 * 学校表格：行点击开详情、行内「更多」菜单、游标翻页。
 *
 * **归属**
 * `features/schools` 展示层：数据与回调由 `SchoolsView` 给。
 *
 * **口径**
 * 服务端游标分页，没有总数。页脚只报当前位置与本页条数。
 * 行高固定 48px；表格最小宽度由内容决定，窄屏在 `.table-scroll` 内滚动。
 * 合同没有删除端点，操作列只有「更多」。
 * 无权限的菜单项置灰，不隐藏。
 * 行增删与重排走 180ms 过渡；不改行点击、菜单与焦点还原的语义。
 * 过渡库会永久给 tbody 写 `position: relative`：表体文字由次像素抗锯齿变为灰度。
 * 视觉基线按此口径拍，不是颜色或字号变化。
 */

import { useAutoAnimate } from '@formkit/auto-animate/react'
import type { School } from '@/lib/api/school'
import type { Role } from '@/lib/api/auth'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Icon } from '@/components/ui/Icon'
import { formatDateTime } from '@/lib/format'
import { schoolStatusLabel, schoolStatusTone } from './display'
import { SchoolRowMenu } from './SchoolRowMenu'

type SchoolsTableProps = {
  rows: School[]
  loading: boolean
  error: string | null
  role: Role | null
  pageIndex: number
  hasPrevious: boolean
  hasNext: boolean
  onPrevious: () => void
  onNext: () => void
  onRetry: () => void
  onOpenDetail: (school: School, opener: HTMLElement | null) => void
  onEdit: (school: School, opener: HTMLElement | null) => void
  onAdmins: (school: School, opener: HTMLElement | null) => void
}

export function SchoolsTable({
  rows,
  loading,
  error,
  role,
  pageIndex,
  hasPrevious,
  hasNext,
  onPrevious,
  onNext,
  onRetry,
  onOpenDetail,
  onEdit,
  onAdmins,
}: SchoolsTableProps) {
  // 列表过渡：180ms 与 vendor 过渡同量级；尊重系统「减少动态效果」偏好。
  const [tbodyRef] = useAutoAnimate<HTMLTableSectionElement>({
    duration: 180,
    disrespectUserMotionPreference: false,
  })
  return (
    <>
      <div className="table-scroll">
        <table className="data-table app-row-table">
          <caption className="app-visually-hidden">学校列表</caption>
          <thead>
            <tr>
              <th scope="col">学校</th>
              <th scope="col">代码</th>
              <th scope="col">状态</th>
              <th scope="col">时区</th>
              <th scope="col">更新时间</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody ref={tbodyRef}>
            {error ? (
              <tr>
                <td colSpan={6}>
                  <span className="app-muted">{error}</span>{' '}
                  <Button variant="ghost" size="sm" onClick={onRetry}>
                    重试
                  </Button>
                </td>
              </tr>
            ) : null}

            {!error && loading && rows.length === 0 ? (
              <tr>
                <td colSpan={6} className="app-muted">
                  加载中…
                </td>
              </tr>
            ) : null}

            {!error && !loading && rows.length === 0 ? (
              <tr>
                <td colSpan={6}>
                  <div>没有学校</div>
                  <div className="app-muted">master 可新建第一所学校。</div>
                </td>
              </tr>
            ) : null}

            {rows.map((school) => (
              <tr
                key={school.id}
                className="app-school-row"
                tabIndex={0}
                onClick={(event) => onOpenDetail(school, event.currentTarget)}
                onKeyDown={(event) => {
                  if (event.target !== event.currentTarget) return
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    onOpenDetail(school, event.currentTarget)
                  }
                }}
              >
                <td>
                  <button
                    type="button"
                    className="app-cell-link"
                    aria-label={`查看 ${school.name}`}
                    onClick={(event) => {
                      event.stopPropagation()
                      onOpenDetail(school, event.currentTarget)
                    }}
                  >
                    {school.name}
                  </button>
                </td>
                <td className="data-cell-mono">{school.code}</td>
                <td>
                  <Badge tone={schoolStatusTone(school.status)} dot>
                    {schoolStatusLabel(school.status)}
                  </Badge>
                </td>
                <td className="data-cell-mono">{school.timezone}</td>
                <td className="data-cell-mono">{formatDateTime(school.updated_at)}</td>
                <td>
                  <div className="data-cell-actions app-row-actions">
                    <SchoolRowMenu
                      school={school}
                      role={role}
                      onEdit={onEdit}
                      onAdmins={onAdmins}
                    />
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="data-foot">
        <div className="data-foot-info">
          <span>
            第 <strong>{pageIndex + 1}</strong> 页
          </span>
          <span className="app-toolbar-note">本页 {rows.length} 条</span>
        </div>
        <div className="pager">
          <button
            type="button"
            className="pager-btn"
            aria-label="上一页"
            disabled={!hasPrevious}
            onClick={onPrevious}
          >
            <Icon name="chevronLeft" />
          </button>
          <button
            type="button"
            className="pager-btn"
            aria-label="下一页"
            disabled={!hasNext}
            onClick={onNext}
          >
            <Icon name="chevronRight" />
          </button>
        </div>
      </div>
    </>
  )
}
