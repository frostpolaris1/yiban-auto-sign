/**
 * **功能**
 * 用户表格：只读列表与游标翻页。
 *
 * **归属**
 * `features/user` 展示层：数据与回调由 `UsersView` 给。
 *
 * **口径**
 * 服务端游标分页，没有总数。页脚只报当前位置与本页条数。
 * 行高固定 48px；表格最小宽度由内容决定，窄屏在 `.table-scroll` 内滚动。
 * 本页只读：不出操作列，也不加行过渡（行只随整页替换，不做增删重排）。
 * 上次登录为空时显示破折号，由 `lib/format` 统一给出。
 */

import type { User } from '@/lib/api/user'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Icon } from '@/components/ui/Icon'
import { formatDateTime } from '@/lib/format'
import { roleLabel } from '@/lib/identity'
import { userStatusLabel, userStatusTone } from './display'

type UsersTableProps = {
  rows: User[]
  loading: boolean
  error: string | null
  pageIndex: number
  hasPrevious: boolean
  hasNext: boolean
  onPrevious: () => void
  onNext: () => void
  onRetry: () => void
}

const COLUMNS = 6

export function UsersTable({
  rows,
  loading,
  error,
  pageIndex,
  hasPrevious,
  hasNext,
  onPrevious,
  onNext,
  onRetry,
}: UsersTableProps) {
  return (
    <>
      <div className="table-scroll">
        <table className="data-table app-row-table">
          <caption className="app-visually-hidden">用户列表</caption>
          <thead>
            <tr>
              <th scope="col">邮箱</th>
              <th scope="col">角色</th>
              <th scope="col">状态</th>
              <th scope="col">需要改密</th>
              <th scope="col">创建时间</th>
              <th scope="col">上次登录</th>
            </tr>
          </thead>
          <tbody>
            {error ? (
              <tr>
                <td colSpan={COLUMNS}>
                  <span className="app-muted">{error}</span>{' '}
                  <Button variant="ghost" size="sm" onClick={onRetry}>
                    重试
                  </Button>
                </td>
              </tr>
            ) : null}

            {!error && loading && rows.length === 0 ? (
              <tr>
                <td colSpan={COLUMNS} className="app-muted">
                  加载中…
                </td>
              </tr>
            ) : null}

            {!error && !loading && rows.length === 0 ? (
              <tr>
                <td colSpan={COLUMNS}>
                  <div>没有匹配的用户</div>
                  <div className="app-muted">调整筛选条件后重试。</div>
                </td>
              </tr>
            ) : null}

            {rows.map((user) => (
              <tr key={user.id}>
                <td className="data-cell-mono">{user.email}</td>
                <td>{roleLabel(user.role)}</td>
                <td>
                  <Badge tone={userStatusTone(user.status)} dot>
                    {userStatusLabel(user.status)}
                  </Badge>
                </td>
                <td>
                  {user.must_change_password ? (
                    <Badge tone="warning">需要</Badge>
                  ) : (
                    <span className="app-muted">不需要</span>
                  )}
                </td>
                <td className="data-cell-mono">{formatDateTime(user.created_at)}</td>
                <td className="data-cell-mono">{formatDateTime(user.last_login_at)}</td>
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
