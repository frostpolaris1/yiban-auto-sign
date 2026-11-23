/**
 * **功能**
 * 用户表格：列表、已删徽标、行操作与游标翻页。
 *
 * **归属**
 * `features/user` 展示层：数据与回调由 `UsersView` 给。
 *
 * **口径**
 * 服务端游标分页，没有总数。页脚只报当前位置与本页条数。
 * 行高固定 48px；表格最小宽度由内容决定，窄屏在 `.table-scroll` 内滚动。
 * 加载、错误与空态由 `AsyncState` 在容器层处理；本组件只渲染数据态。
 * 读屏播报点在容器层（`UsersView`）常驻：本组件的数据态是整块挂载的，
 * 播报点随数据一起出现时读屏会漏读，故不在这里再挂一份。
 * 已删行（`deleted_at` 非空）在状态格叠加「已删除」徽标。
 * 行操作「管理」开右侧抽屉；写动作的在途态都在抽屉与弹窗内。
 * 已删行的操作格多一枚「彻底删除」：只能硬删已软删除的行，需二次口令确认。
 * 上次登录为空时显示破折号，由 `lib/format` 统一给出。
 */

import type { User } from '@/lib/api/user'
import { Badge } from '@/components/ui/Badge'
import { BusyButton } from '@/components/ui/BusyButton'
import { Icon } from '@/components/ui/Icon'
import { formatDateTime } from '@/lib/format'
import { roleLabel } from '@/lib/identity'
import { USER_DELETED_LABEL, userStatusLabel, userStatusTone } from './display'

type UsersTableProps = {
  rows: User[]
  pageIndex: number
  hasPrevious: boolean
  hasNext: boolean
  onPrevious: () => void
  onNext: () => void
  /** 批量选择：只有 master 渲染选择列。 */
  selectable: boolean
  selectedIds: ReadonlySet<string>
  onToggleRow: (id: string) => void
  onToggleAll: (ids: string[], checked: boolean) => void
  onManage: (user: User, opener: HTMLElement | null) => void
  /** 彻底删除：只对已软删除的行出现，且只归 master。 */
  canPurge: boolean
  onPurge: (user: User, opener: HTMLElement | null) => void
}

export function UsersTable({
  rows,
  pageIndex,
  hasPrevious,
  hasNext,
  onPrevious,
  onNext,
  selectable,
  selectedIds,
  onToggleRow,
  onToggleAll,
  onManage,
  canPurge,
  onPurge,
}: UsersTableProps) {
  const visibleIds = rows.map((row) => row.id)
  const allSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedIds.has(id))
  const someSelected = visibleIds.some((id) => selectedIds.has(id))
  return (
    <>
      <div className="table-scroll">
        <table className="data-table app-row-table app-mobile-cards app-users-table">
          <caption className="app-visually-hidden">用户列表</caption>
          <thead>
            <tr>
              {selectable ? (
                <th scope="col" className="app-selection-cell">
                  <input
                    type="checkbox"
                    aria-label="全选本页用户"
                    checked={allSelected}
                    ref={(element) => {
                      if (element) element.indeterminate = someSelected && !allSelected
                    }}
                    onChange={(event) => onToggleAll(visibleIds, event.target.checked)}
                  />
                </th>
              ) : null}
              <th scope="col">邮箱</th>
              <th scope="col">角色</th>
              <th scope="col">状态</th>
              <th scope="col">账号</th>
              <th scope="col">需要改密</th>
              <th scope="col">创建时间</th>
              <th scope="col">上次登录</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((user) => (
              <tr key={user.id}>
                {selectable ? (
                  <td className="app-selection-cell">
                    <input
                      type="checkbox"
                      aria-label={`选择 ${user.email}`}
                      checked={selectedIds.has(user.id)}
                      onChange={() => onToggleRow(user.id)}
                    />
                  </td>
                ) : null}
                <td className="data-cell-mono">{user.email}</td>
                <td>{roleLabel(user.role)}</td>
                <td>
                  <Badge tone={userStatusTone(user.status)} dot>
                    {userStatusLabel(user.status)}
                  </Badge>{' '}
                  {user.deleted_at ? <Badge tone="danger">{USER_DELETED_LABEL}</Badge> : null}
                </td>
                <td>
                  <div className="app-name-cell">
                    <span className="data-cell-mono">{user.account_count}</span>
                    {user.review_count > 0 ? (
                      <Badge tone="warning">待处理 {user.review_count}</Badge>
                    ) : null}
                  </div>
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
                <td>
                  <div className="data-cell-actions app-row-actions">
                    <BusyButton
                      size="sm"
                      variant="ghost"
                      aria-label={`管理 ${user.email}`}
                      onClick={(event) => onManage(user, event.currentTarget)}
                    >
                      管理
                    </BusyButton>
                    {canPurge && user.deleted_at !== null ? (
                      <BusyButton
                        size="sm"
                        variant="outline-danger"
                        aria-label={`彻底删除 ${user.email}`}
                        title="不可恢复，审计记录保留"
                        onClick={(event) => onPurge(user, event.currentTarget)}
                      >
                        彻底删除
                      </BusyButton>
                    ) : null}
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
