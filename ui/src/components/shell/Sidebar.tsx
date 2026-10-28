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
 */

import { useState } from 'react'
import { Link } from '@tanstack/react-router'
import { cx } from '@/lib/cx'
import type { SessionUser } from '@/lib/api/auth'
import { displayNameFor, initialsFor, roleLabel } from '@/lib/identity'
import { BrandMark, Icon } from '@/components/ui/Icon'
import { isNavGroup, NAV, type NavGroup, type NavLeaf } from './nav'

type SidebarProps = {
  me: SessionUser
  pathname: string
  onNavigate: () => void
}

export function Sidebar({ me, pathname, onNavigate }: SidebarProps) {
  const [manualOpen, setManualOpen] = useState<Record<string, boolean>>({})

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

      {NAV.map((section) => (
        <nav className="nav-section" key={section.label} aria-label={section.label}>
          <div className="nav-label">{section.label}</div>
          {section.items.map((item) => {
            if (!isNavGroup(item)) {
              return <NavLeafView key={item.key} item={item} onNavigate={onNavigate} />
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

function NavLeafView({ item, onNavigate }: { item: NavLeaf; onNavigate: () => void }) {
  return (
    <Link
      to={item.to}
      className="nav-link"
      activeProps={{ className: 'nav-link is-active' }}
      activeOptions={{ exact: true }}
      onClick={onNavigate}
    >
      <Icon name={item.icon} />
      <span>{item.text}</span>
      {item.badge ? <span className={cx('nav-badge', item.badge.kind)}>{item.badge.text}</span> : null}
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
