/**
 * **功能**
 * 账号表格：行点击开详情、行内写操作、游标翻页。
 *
 * **归属**
 * `features/accounts` 展示层：数据与回调由 `AccountsView` 给。
 *
 * **口径**
 * 服务端游标分页，没有总数。页脚只报当前位置与本页条数。
 * 行高固定 48px；表格最小宽度由内容决定，窄屏在 `.table-scroll` 内滚动。
 * 无权限与不可评审一律置灰，并给一句可见原因。
 * 置灰判据只取原因：原因非空即可用性为假，二者不会分叉。
 * 展示构建只读时原因统一为 `展示模式只读`，不再追加状态类说明。
 * 行增删与重排走 180ms 过渡；不改行点击、菜单与焦点还原的语义。
 * 过渡库会永久给 tbody 写 `position: relative`：表体文字由次像素抗锯齿变为灰度。
 * 视觉基线按此口径拍，不是颜色或字号变化。
 */

import { useAutoAnimate } from '@formkit/auto-animate/react'
import type { Account } from '@/lib/api/account'
import type { Role } from '@/lib/api/auth'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Icon } from '@/components/ui/Icon'
import { formatDateTime } from '@/lib/format'
import { SHOWCASE } from '@/lib/showcase-mode'
import { isPending, statusLabel, statusTone } from './display'
import { AccountRowMenu } from './AccountRowMenu'
import {
  can,
  denyReason,
  pauseDenyReason,
  pauseDirection,
  type AccountActionKind,
} from './permissions'

type AccountsTableProps = {
  rows: Account[]
  loading: boolean
  error: string | null
  role: Role | null
  pageIndex: number
  hasPrevious: boolean
  hasNext: boolean
  onPrevious: () => void
  onNext: () => void
  onRetry: () => void
  onOpenDetail: (account: Account, opener: HTMLElement | null) => void
  onAction: (account: Account, kind: AccountActionKind, opener: HTMLElement) => void
  onEdit: (account: Account, opener: HTMLElement | null) => void
  onLocation: (account: Account, opener: HTMLElement | null) => void
  onDelete: (account: Account, opener: HTMLElement | null) => void
}

export function AccountsTable({
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
  onAction,
  onEdit,
  onLocation,
  onDelete,
}: AccountsTableProps) {
  // 列表过渡：180ms 与 vendor 过渡同量级；尊重系统「减少动态效果」偏好。
  const [tbodyRef] = useAutoAnimate<HTMLTableSectionElement>({
    duration: 180,
    disrespectUserMotionPreference: false,
  })
  return (
    <>
      <div className="table-scroll">
        <table className="data-table app-row-table">
          <caption className="app-visually-hidden">账号列表</caption>
          <thead>
            <tr>
              <th scope="col">手机号</th>
              <th scope="col">学校 ID</th>
              <th scope="col">状态</th>
              <th scope="col">归属用户</th>
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
                  <div>没有匹配的账号</div>
                  <div className="app-muted">调整筛选条件后重试。</div>
                </td>
              </tr>
            ) : null}

            {rows.map((account) => {
              const pauseKind: AccountActionKind = pauseDirection(account.status)
              const pauseLabel = pauseKind === 'pause' ? '暂停' : '恢复'
              const pauseReason = pauseDenyReason(role, account.status)
              const reviewBlockedByStatus = can(role, 'review') && !isPending(account.status)

              const notes = new Set<string>()
              for (const action of ['sign', 'verify', 'review'] as const) {
                const reason = denyReason(role, action)
                if (reason) notes.add(reason)
              }
              if (pauseReason) notes.add(pauseReason)
              // 展示构建只读时状态类说明是噪声：写动作都不可用。
              if (!SHOWCASE && reviewBlockedByStatus) notes.add('仅待审核账号可评审')

              const signReason = denyReason(role, 'sign')
              const verifyReason = denyReason(role, 'verify')
              const reviewReason = denyReason(role, 'review') ?? (isPending(account.status) ? null : '仅待审核账号可评审')

              return (
                <tr
                  key={account.id}
                  className="app-account-row"
                  tabIndex={0}
                  onClick={(event) => onOpenDetail(account, event.currentTarget)}
                  onKeyDown={(event) => {
                    if (event.target !== event.currentTarget) return
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      onOpenDetail(account, event.currentTarget)
                    }
                  }}
                >
                  <td>
                    <button
                      type="button"
                      className="app-cell-link data-cell-mono"
                      aria-label={`查看 ${account.phone_masked}`}
                      onClick={(event) => {
                        event.stopPropagation()
                        onOpenDetail(account, event.currentTarget)
                      }}
                    >
                      {account.phone_masked}
                    </button>
                  </td>
                  <td className="data-cell-mono">{account.school_id}</td>
                  <td>
                    <Badge tone={statusTone(account.status)} dot>
                      {statusLabel(account.status)}
                    </Badge>
                  </td>
                  <td className="data-cell-mono">{account.owner_user_id}</td>
                  <td className="data-cell-mono">{formatDateTime(account.updated_at)}</td>
                  <td>
                    <div className="data-cell-actions app-account-actions">
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`${pauseLabel} ${account.phone_masked}`}
                        title={pauseReason ?? undefined}
                        disabled={pauseReason !== null}
                        onClick={(event) => {
                          event.stopPropagation()
                          onAction(account, pauseKind, event.currentTarget)
                        }}
                      >
                        {pauseLabel}
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`签到 ${account.phone_masked}`}
                        title={signReason ?? undefined}
                        disabled={signReason !== null}
                        onClick={(event) => {
                          event.stopPropagation()
                          onAction(account, 'sign', event.currentTarget)
                        }}
                      >
                        签到
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`验证 ${account.phone_masked}`}
                        title={verifyReason ?? undefined}
                        disabled={verifyReason !== null}
                        onClick={(event) => {
                          event.stopPropagation()
                          onAction(account, 'verify', event.currentTarget)
                        }}
                      >
                        验证
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`评审 ${account.phone_masked}`}
                        title={reviewReason ?? undefined}
                        disabled={reviewReason !== null}
                        onClick={(event) => {
                          event.stopPropagation()
                          onAction(account, 'review', event.currentTarget)
                        }}
                      >
                        评审
                      </Button>
                      <AccountRowMenu
                        account={account}
                        role={role}
                        onEdit={onEdit}
                        onLocation={onLocation}
                        onDelete={onDelete}
                      />
                      {notes.size > 0 ? (
                        <span className="app-deny-note">{[...notes].join('；')}</span>
                      ) : null}
                    </div>
                  </td>
                </tr>
              )
            })}
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
