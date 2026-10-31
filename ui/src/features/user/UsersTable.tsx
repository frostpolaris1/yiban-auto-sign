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
 * 已删行（`deleted_at` 非空）在状态格叠加「已删除」徽标。
 * 行操作「管理」开右侧抽屉；写动作的在途态都在抽屉与弹窗内。
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
  onManage: (user: User, opener: HTMLElement | null) => void
}

export function UsersTable({
  rows,
  pageIndex,
  hasPrevious,
  hasNext,
  onPrevious,
  onNext,
  onManage,
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
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((user) => (
              <tr key={user.id}>
                <td className="data-cell-mono">{user.email}</td>
                <td>{roleLabel(user.role)}</td>
                <td>
                  <Badge tone={userStatusTone(user.status)} dot>
                    {userStatusLabel(user.status)}
                  </Badge>{' '}
                  {user.deleted_at ? <Badge tone="danger">{USER_DELETED_LABEL}</Badge> : null}
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
                  <BusyButton
                    size="sm"
                    variant="ghost"
                    aria-label={`管理 ${user.email}`}
                    onClick={(event) => onManage(user, event.currentTarget)}
                  >
                    管理
                  </BusyButton>
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
