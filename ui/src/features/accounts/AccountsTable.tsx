/**
 * **功能**
 * 账号表格：行点击开详情、行内写操作、游标翻页。
 *
 * **归属**
 * `features/accounts` 展示层：数据与回调由 `AccountsView` 给。
 *
 * **口径**
 * 服务端游标分页，没有总数。页脚只报当前位置与本页条数。
 * 删除时间列只随「含已删除」渲染：已删行给软删时刻，活行该列是「—」；
 * 未勾选时整列不出现，活行保持 8 列不变宽。
 * 机型来自凭据明文，空值（未设置或不可读）显示「—」。
 * 有筛选的空态给「清除筛选」出路；无筛选的空态保持纯文案。
 * 行高固定 48px；桌面端（>640px）操作列收成「主操作 + 更多菜单」，置灰原因移到
 * 行级 `title` 与表下提示条，确保 1440/1280 零横向滚动；640px 以下行卡模式保持现状。
 * 无权限与不可评审一律置灰，并给一句可见原因。
 * 置灰判据只取原因：原因非空即可用性为假，二者不会分叉。
 * 展示构建只读时原因统一为 `展示模式只读`，不再追加状态类说明。
 * 已删行的行菜单多一条「彻底删除」：只能硬删已软删除的行，需二次口令复核。
 * 行增删与重排走 180ms 过渡；不改行点击、菜单与焦点还原的语义。
 * 过渡库会永久给 tbody 写 `position: relative`：表体文字由次像素抗锯齿变为灰度。
 * 视觉基线按此口径拍，不是颜色或字号变化。
 * 读屏播报点：页脚条数带 `aria-live="polite"`，读到数据或翻页时整条重读；
 * 加载行读作「加载中…」但**不加** `role="status"`（页面级 status 计数被 e2e 精确断言）；
 * 自渲染的错误行用 `role="alert"`，与 `Alert` 同口径。
 */

import { useEffect, useState } from 'react'
import { useAutoAnimate } from '@formkit/auto-animate/react'
import type { Account } from '@/lib/api/account'
import type { Role } from '@/lib/api/auth'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Icon } from '@/components/ui/Icon'
import { formatDateTime } from '@/lib/format'
import { SHOWCASE } from '@/lib/showcase-mode'
import { isPending, statusLabel, statusTone } from './display'
import { AccountRowMenu, type SecondaryRowAction } from './AccountRowMenu'
import {
  can,
  denyReason,
  pauseDenyReason,
  pauseDirection,
  primaryRowAction,
  rowActionReason,
  type AccountActionKind,
} from './permissions'

/** 与 app.css 的 `@media (max-width: 640px)` 行卡断点保持同源。 */
const MOBILE_CARDS_QUERY = '(max-width: 640px)'

function useMobileCards(): boolean {
  const [mobile, setMobile] = useState(() =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia(MOBILE_CARDS_QUERY).matches
      : false,
  )
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const media = window.matchMedia(MOBILE_CARDS_QUERY)
    const onChange = (event: MediaQueryListEvent) => setMobile(event.matches)
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [])
  return mobile
}

/** 汇总单条活行的全部置灰原因，文案与展示构建口径与历史一致。 */
function collectRowDenyNotes(role: Role | null, account: Account): string[] {
  const notes = new Set<string>()
  for (const action of ['sign', 'verify', 'review'] as const) {
    const reason = denyReason(role, action)
    if (reason) notes.add(reason)
  }
  const pauseReason = pauseDenyReason(role, account.status)
  if (pauseReason) notes.add(pauseReason)
  // 展示构建只读时状态类说明是噪声：写动作都不可用。
  if (!SHOWCASE && can(role, 'review') && !isPending(account.status)) {
    notes.add('仅待审核账号可评审')
  }
  if (!SHOWCASE && can(role, 'sign') && account.status !== 'active') {
    notes.add('仅正常状态的账号可签到')
  }
  return [...notes]
}

type AccountsTableProps = {
  rows: Account[]
  loading: boolean
  error: string | null
  role: Role | null
  /** 精确命中的行 id：完整手机号/邮箱或完整备注名。 */
  highlightIds: ReadonlySet<string>
  /** 任一筛选生效时为真：空态才给「清除筛选」出路。 */
  hasActiveFilters: boolean
  onClearFilters: () => void
  /** 勾选「含已删除」时才渲染删除时间列：活行保持 8 列不变宽。 */
  includeDeleted: boolean
  pageIndex: number
  hasPrevious: boolean
  hasNext: boolean
  onPrevious: () => void
  onNext: () => void
  onRetry: () => void
  /** 批量选择：仅 master/admin 渲染选择列；其余角色保持 8 列原状。 */
  selectable: boolean
  selectedIds: ReadonlySet<string>
  onToggleRow: (id: string) => void
  onToggleAll: (ids: string[], checked: boolean) => void
  onOpenDetail: (account: Account, opener: HTMLElement | null) => void
  onAction: (account: Account, kind: AccountActionKind, opener: HTMLElement) => void
  onEdit: (account: Account, opener: HTMLElement | null) => void
  onLocation: (account: Account, opener: HTMLElement | null) => void
  onDelete: (account: Account, opener: HTMLElement | null) => void
  onRestore: (account: Account, opener: HTMLElement | null) => void
  onPurge: (account: Account, opener: HTMLElement | null) => void
}

export function AccountsTable({
  rows,
  loading,
  error,
  role,
  highlightIds,
  hasActiveFilters,
  onClearFilters,
  includeDeleted,
  pageIndex,
  hasPrevious,
  hasNext,
  onPrevious,
  onNext,
  onRetry,
  selectable,
  selectedIds,
  onToggleRow,
  onToggleAll,
  onOpenDetail,
  onAction,
  onEdit,
  onLocation,
  onDelete,
  onRestore,
  onPurge,
}: AccountsTableProps) {
  // 列表过渡：180ms 与 vendor 过渡同量级；尊重系统「减少动态效果」偏好。
  const [tbodyRef] = useAutoAnimate<HTMLTableSectionElement>({
    duration: 180,
    disrespectUserMotionPreference: false,
  })
  const mobileCards = useMobileCards()
  // 删除时间列只随「含已删除」出现：占位列会把行菜单挤出窄视口。
  const colSpan = (includeDeleted ? 9 : 8) + (selectable ? 1 : 0)
  const visibleIds = rows.map((row) => row.id)
  const allSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedIds.has(id))
  const someSelected = visibleIds.some((id) => selectedIds.has(id))

  // 桌面端把置灰说明移到表下提示条与行级 title，避免把操作列撑宽出视口。
  const tableNotes = (() => {
    const merged = new Set<string>()
    for (const account of rows) {
      if (account.deleted_at != null) continue
      for (const note of collectRowDenyNotes(role, account)) {
        merged.add(note)
      }
    }
    return [...merged]
  })()

  return (
    <>
      <div className="table-scroll">
        <table className="data-table app-row-table app-mobile-cards app-accounts-table">
          <caption className="app-visually-hidden">账号列表</caption>
          <thead>
            <tr>
              {selectable ? (
                <th scope="col" className="app-selection-cell">
                  <input
                    type="checkbox"
                    aria-label="全选本页账号"
                    checked={allSelected}
                    ref={(element) => {
                      if (element) element.indeterminate = someSelected && !allSelected
                    }}
                    onChange={(event) => onToggleAll(visibleIds, event.target.checked)}
                  />
                </th>
              ) : null}
              <th scope="col">备注名</th>
              <th scope="col">手机号</th>
              <th scope="col">机型</th>
              <th scope="col">学校 ID</th>
              <th scope="col">状态</th>
              <th scope="col">归属邮箱</th>
              <th scope="col">更新时间</th>
              {includeDeleted ? <th scope="col">删除时间</th> : null}
              <th scope="col" className="app-col-action">
                操作
              </th>
            </tr>
          </thead>
          <tbody ref={tbodyRef}>
            {error ? (
              <tr>
                <td colSpan={colSpan}>
                  <span className="app-muted" role="alert">
                    {error}
                  </span>{' '}
                  <Button variant="ghost" size="sm" onClick={onRetry}>
                    重试
                  </Button>
                </td>
              </tr>
            ) : null}

            {!error && loading && rows.length === 0 ? (
              <tr>
                <td colSpan={colSpan} className="app-muted">
                  加载中…
                </td>
              </tr>
            ) : null}

            {!error && !loading && rows.length === 0 ? (
              <tr>
                <td colSpan={colSpan}>
                  <div>没有匹配的账号</div>
                  <div className="app-muted">调整筛选条件后重试。</div>
                  {hasActiveFilters ? (
                    <Button variant="ghost" size="sm" onClick={onClearFilters}>
                      清除筛选
                    </Button>
                  ) : null}
                </td>
              </tr>
            ) : null}

            {rows.map((account) => {
              const exact = highlightIds.has(account.id)
              // 软删行只展示徽标与恢复菜单：其余动作对已删行都是 404。
              if (account.deleted_at != null) {
                // 已删行不绑点击也不给「查看」：详情与设备刷新对已删行必 404，
                // 唯一出口是行菜单的恢复。
                return (
                  <tr
                    key={account.id}
                    className={`app-account-row${exact ? ' app-account-row-exact' : ''}`}
                    data-exact-match={exact ? 'true' : undefined}
                  >
                    {selectable ? (
                      <td className="app-selection-cell">
                        <input
                          type="checkbox"
                          aria-label={`选择 ${account.phone_masked}`}
                          checked={selectedIds.has(account.id)}
                          onChange={() => onToggleRow(account.id)}
                          onClick={(event) => event.stopPropagation()}
                        />
                      </td>
                    ) : null}
                    <td className="app-account-col-name">
                      <div className="app-name-cell">
                        <span>{account.display_name}</span>
                        {exact ? <Badge tone="info">精确匹配</Badge> : null}
                      </div>
                    </td>
                    <td>
                      <span className="data-cell-mono">{account.phone_masked}</span>
                    </td>
                    <td className="data-cell-mono app-account-col-wrap">
                      {account.phone_model || '—'}
                    </td>
                    <td className="data-cell-mono app-account-col-wrap">{account.school_id}</td>
                    <td className="app-account-col-status">
                      <Badge tone="danger" dot>
                        已删除
                      </Badge>
                    </td>
                    <td className="data-cell-mono app-account-col-email">
                      {account.owner_email_masked}
                    </td>
                    <td className="data-cell-mono app-account-col-time">
                      {formatDateTime(account.updated_at)}
                    </td>
                    {includeDeleted ? (
                      <td className="data-cell-mono app-account-col-time">
                        {formatDateTime(account.deleted_at)}
                      </td>
                    ) : null}
                    <td className="app-account-col-actions app-col-action">
                      <div className="data-cell-actions app-account-actions">
                        <AccountRowMenu
                          account={account}
                          role={role}
                          onEdit={onEdit}
                          onLocation={onLocation}
                          onDelete={onDelete}
                          onRestore={onRestore}
                          onPurge={onPurge}
                        />
                      </div>
                    </td>
                  </tr>
                )
              }
              const pauseKind: AccountActionKind = pauseDirection(account.status)
              const pauseLabel = pauseKind === 'pause' ? '暂停' : '恢复'
              const rowNotes = collectRowDenyNotes(role, account)
              const rowNoteText = rowNotes.join('；')

              const rowActions: SecondaryRowAction[] = [
                {
                  kind: pauseKind,
                  label: pauseLabel,
                  icon: pauseKind === 'pause' ? 'pause' : 'rotateCcw',
                  reason: rowActionReason(role, account.status, pauseKind),
                },
                {
                  kind: 'sign',
                  label: '签到',
                  icon: 'checkCircle',
                  reason: rowActionReason(role, account.status, 'sign'),
                },
                {
                  kind: 'verify',
                  label: '验证',
                  icon: 'shield',
                  reason: rowActionReason(role, account.status, 'verify'),
                },
                {
                  kind: 'review',
                  label: '评审',
                  icon: 'checkCheck',
                  reason: rowActionReason(role, account.status, 'review'),
                },
              ]

              const primaryKind = primaryRowAction(account.status)
              const primaryAction =
                rowActions.find((item) => item.kind === primaryKind) ?? rowActions[0]
              const secondaryActions = rowActions.filter((item) => item.kind !== primaryAction.kind)

              return (
                <tr
                  key={account.id}
                  className={`app-account-row${exact ? ' app-account-row-exact' : ''}`}
                  data-exact-match={exact ? 'true' : undefined}
                  title={!mobileCards && rowNoteText ? rowNoteText : undefined}
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
                  {selectable ? (
                    <td className="app-selection-cell">
                      <input
                        type="checkbox"
                        aria-label={`选择 ${account.phone_masked}`}
                        checked={selectedIds.has(account.id)}
                        onChange={() => onToggleRow(account.id)}
                        onClick={(event) => event.stopPropagation()}
                      />
                    </td>
                  ) : null}
                  <td className="app-account-col-name">
                    <div className="app-name-cell">
                      <span>{account.display_name}</span>
                      {exact ? <Badge tone="info">精确匹配</Badge> : null}
                    </div>
                  </td>
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
                  <td className="data-cell-mono app-account-col-wrap">
                    {account.phone_model || '—'}
                  </td>
                  <td className="data-cell-mono app-account-col-wrap">{account.school_id}</td>
                  <td className="app-account-col-status">
                    <Badge tone={statusTone(account.status)} dot>
                      {statusLabel(account.status)}
                    </Badge>
                  </td>
                  <td className="data-cell-mono app-account-col-email">
                    {account.owner_email_masked}
                  </td>
                  <td className="data-cell-mono app-account-col-time">
                    {formatDateTime(account.updated_at)}
                  </td>
                  {includeDeleted ? (
                    <td className="data-cell-mono app-account-col-time">—</td>
                  ) : null}
                  <td className="app-account-col-actions app-col-action">
                    <div className="data-cell-actions app-account-actions">
                      {mobileCards ? (
                        <>
                          {rowActions.map((item) => (
                            <Button
                              key={item.kind}
                              variant="ghost"
                              size="sm"
                              aria-label={`${item.label} ${account.phone_masked}`}
                              title={item.reason ?? undefined}
                              disabled={item.reason !== null}
                              onClick={(event) => {
                                event.stopPropagation()
                                onAction(account, item.kind, event.currentTarget)
                              }}
                            >
                              {item.label}
                            </Button>
                          ))}
                          <AccountRowMenu
                            account={account}
                            role={role}
                            onEdit={onEdit}
                            onLocation={onLocation}
                            onDelete={onDelete}
                            onRestore={onRestore}
                            onPurge={onPurge}
                          />
                          {rowNoteText ? <span className="app-deny-note">{rowNoteText}</span> : null}
                        </>
                      ) : (
                        <>
                          <Button
                            variant="ghost"
                            size="sm"
                            aria-label={`${primaryAction.label} ${account.phone_masked}`}
                            title={primaryAction.reason ?? undefined}
                            disabled={primaryAction.reason !== null}
                            onClick={(event) => {
                              event.stopPropagation()
                              onAction(account, primaryAction.kind, event.currentTarget)
                            }}
                          >
                            {primaryAction.label}
                          </Button>
                          <AccountRowMenu
                            account={account}
                            role={role}
                            secondaryActions={secondaryActions}
                            onAction={onAction}
                            onEdit={onEdit}
                            onLocation={onLocation}
                            onDelete={onDelete}
                            onRestore={onRestore}
                            onPurge={onPurge}
                          />
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {!mobileCards && tableNotes.length > 0 ? (
        <div className="app-table-deny-bar">
          <Icon name="info" size={14} />
          <span className="app-deny-note">{tableNotes.join('；')}</span>
        </div>
      ) : null}

      <div className="data-foot">
        <div className="data-foot-info" aria-live="polite" aria-atomic="true">
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
