/**
 * **功能**
 * 侧栏：品牌、导航段、分组、底部工作区块。
 *
 * **归属**
 * `components/shell`：只渲染 `nav.ts` 的表。
 *
 * **口径**
 * 导航项全部来自 `NAV`。本文件不硬编码路径。
 * 分组手动开合优先。未手动时跟随当前路由。
 * 底部工作区块读真实会话：头像、显示名、角色都来自 `GET /api/auth/me`。
 * 「账号」计数只对 master/admin 取数，与账号页/看板共用 `['account-summary']`；
 * user 零请求、零徽标。0 不渲染，写操作失效后随同键刷新。
 * 展示构建读同一份合成 summary，静态显示、不发请求。
 */

import { useState } from 'react'
import { Link } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { cx } from '@/lib/cx'
import { getAccountSummary } from '@/lib/api/account'
import type { SessionUser } from '@/lib/api/auth'
import { displayNameFor, initialsFor, roleLabel } from '@/lib/identity'
import { SHOWCASE } from '@/lib/session'
import { showcaseAccountSummary } from '@/lib/showcase'
import { BrandMark } from '@/components/ui/BrandMark'
import { Icon } from '@/components/ui/Icon'
import { isNavGroup, visibleNav, type NavGroup, type NavLeaf } from './nav'

/** 待处理账号计数的复核间隔。summary 便宜，但 10 秒的列表轮询不必同步给它。 */
const SUMMARY_POLL_MS = 30_000

type SidebarProps = {
  me: SessionUser
  pathname: string
  onNavigate: () => void
}

export function Sidebar({ me, pathname, onNavigate }: SidebarProps) {
  const [manualOpen, setManualOpen] = useState<Record<string, boolean>>({})
  // 整页无权限的导航项不渲染：判据来自 nav 的 roles 元数据。
  const sections = visibleNav(me.role)

  // 账号待处理计数：与账号页提示条、看板第 5 卡共用缓存键。
  // 只对 master/admin 取数；展示构建读合成夹具，不走网络。
  const isManager = me.role === 'master' || me.role === 'admin'
  const summaryQuery = useQuery({
    queryKey: ['account-summary'],
    queryFn: () =>
      SHOWCASE ? Promise.resolve(showcaseAccountSummary()) : getAccountSummary(),
    enabled: isManager,
    staleTime: 0,
    retry: false,
    refetchInterval: SHOWCASE ? false : SUMMARY_POLL_MS,
    refetchIntervalInBackground: false,
  })
  const pendingAccounts = summaryQuery.data
    ? summaryQuery.data.pending + summaryQuery.data.rejected
    : 0
  const accountsBadge = isManager && pendingAccounts > 0 ? pendingAccounts : null

  return (
    <aside className="d-sidebar" aria-label="主导航">
      <div className="brand">
        <div className="brand-logo">
          <BrandMark />
        </div>
        <div className="brand-text">
          <div className="brand-name">易班自动签到</div>
          <div className="brand-tag">v0.1.0 · skeleton</div>
        </div>
      </div>

      {sections.map((section) => (
        <nav className="nav-section" key={section.label} aria-label={section.label}>
          <div className="nav-label">{section.label}</div>
          {section.items.map((item) => {
            if (!isNavGroup(item)) {
              return (
                <NavLeafView
                  key={item.key}
                  item={item}
                  badge={item.key === 'accounts' ? accountsBadge : null}
                  onNavigate={onNavigate}
                />
              )
            }
            const activeGroup = item.children.some((child) => child.to === pathname)
            // 手动状态优先。未手动时按当前路由自动展开。
            const open = manualOpen[item.key] ?? activeGroup
            return (
              <NavGroupView
                key={item.key}
                item={item}
                open={open}
                onToggle={() =>
                  setManualOpen((previous) => ({ ...previous, [item.key]: !open }))
                }
                onNavigate={onNavigate}
              />
            )
          })}
        </nav>
      ))}

      <div className="sidebar-footer">
        <div className="workspace">
          <div className="workspace-avatar">{initialsFor(me.email)}</div>
          <div className="workspace-text">
            <div className="workspace-name">{displayNameFor(me.email)}</div>
            <div className="workspace-role">{roleLabel(me.role)}</div>
          </div>
          <Icon name="chevronDown" className="workspace-chev" size={14} />
        </div>
      </div>
    </aside>
  )
}

function NavLeafView({
  item,
  badge,
  onNavigate,
}: {
  item: NavLeaf
  /** 动态计数：0 已收敛为 null，不渲染。 */
  badge: number | null
  onNavigate: () => void
}) {
  return (
    <Link
      to={item.to}
      className="nav-link"
      aria-label={badge !== null ? `${item.text}，待处理账号 ${badge}` : undefined}
      activeProps={{ className: 'nav-link is-active' }}
      activeOptions={{ exact: true }}
      onClick={onNavigate}
    >
      <Icon name={item.icon} />
      <span>{item.text}</span>
      {item.badge ? <span className={cx('nav-badge', item.badge.kind)}>{item.badge.text}</span> : null}
      {badge !== null ? (
        <span className="nav-badge nav-badge--count" aria-hidden="true">
          {badge}
        </span>
      ) : null}
    </Link>
  )
}

type NavGroupViewProps = {
  item: NavGroup
  open: boolean
  onToggle: () => void
  onNavigate: () => void
}

/**
 * 分组视图。子项激活时父级默认展开。
 * 用户点过一次后，手动值一直压过自动判定。
 */
function NavGroupView({ item, open, onToggle, onNavigate }: NavGroupViewProps) {
  return (
    <div className={cx('nav-item-group', open && 'is-open')} data-nav-group>
      <button
        type="button"
        className="nav-link"
        data-nav-toggle
        aria-expanded={open}
        onClick={onToggle}
      >
        <Icon name={item.icon} />
        <span>{item.text}</span>
        <Icon name="chevronRight" className="chev" />
      </button>
      <div className="nav-submenu">
        {item.children.map((child) => (
          <Link
            key={child.key}
            to={child.to}
            activeProps={{ className: 'is-active' }}
            activeOptions={{ exact: true }}
            onClick={onNavigate}
          >
            {child.text}
            {child.badge ? (
              <span className={cx('nav-badge', child.badge.kind)}>{child.badge.text}</span>
            ) : null}
          </Link>
        ))}
      </div>
    </div>
  )
}
