/**
 * **功能**
 * 顶栏：面包屑、抽屉开关、通知入口插槽、主题切换、账号菜单。
 *
 * **归属**
 * `components/shell`：显示层，不改路由状态，不反向导入 `features`。
 *
 * **口径**
 * 面包屑取自 `nav.ts`。与侧栏共用一张表，故两处不会分叉。
 * 账号菜单读真实会话：邮箱与角色来自 `GET /api/auth/me`。
 * 通知收件箱由装配层经 `inbox` 插槽注入，遵守单向依赖。
 */

import { Fragment, type ReactNode } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { Icon } from '@/components/ui/Icon'
import { Menu, MenuDivider, MenuItem } from '@/components/ui/Menu'
import type { SessionUser } from '@/lib/api/auth'
import { initialsFor, roleLabel } from '@/lib/identity'
import { SHOWCASE } from '@/lib/session'
import { originFromEvent, useTheme } from '@/lib/theme'

type TopbarProps = {
  me: SessionUser
  crumbs: string[]
  drawerOpen: boolean
  onOpenDrawer: () => void
  onLogout: () => void
  loggingOut: boolean
  inbox?: ReactNode
}

export function Topbar({
  me,
  crumbs,
  drawerOpen,
  onOpenDrawer,
  onLogout,
  loggingOut,
  inbox,
}: TopbarProps) {
  const { theme, toggleTheme } = useTheme()
  const navigate = useNavigate()

  return (
    <header className="d-topbar">
      <div className="crumbs">
        <button
          type="button"
          className="hamburger"
          data-drawer-open
          aria-label="打开导航"
          aria-expanded={drawerOpen}
          onClick={onOpenDrawer}
        >
          <Icon name="menu" />
        </button>
        {crumbs.map((crumb, index) => (
          <Fragment key={`${crumb}-${index}`}>
            {index > 0 ? <Icon name="chevronRight" className="sep" size={10} /> : null}
            <span className={index === crumbs.length - 1 ? 'current' : undefined}>{crumb}</span>
          </Fragment>
        ))}
      </div>

      <div className="topbar-actions">
        <button
          type="button"
          className="cmd"
          disabled
          title="命令面板在数据层批次接入"
        >
          <Icon name="search" />
          <span>搜索…</span>
          <kbd className="kbd">Ctrl K</kbd>
        </button>

        {inbox}

        <button
          type="button"
          className="icon-btn"
          id="themeToggle"
          aria-label="切换明暗主题"
          onClick={(event) => toggleTheme(originFromEvent(event))}
        >
          <Icon name={theme === 'dark' ? 'sun' : 'moon'} />
        </button>

        <Menu
          profile
          trigger={
            <button type="button" className="avatar" aria-label="账号菜单">
              {initialsFor(me.email)}
            </button>
          }
        >
          <div className="dd-profile-head">
            <div className="dd-profile-name">{roleLabel(me.role)}</div>
            <div className="dd-profile-email">{me.email}</div>
          </div>
          <MenuItem onSelect={() => void navigate({ to: '/settings' })}>
            <Icon name="settings" />
            账号设置
          </MenuItem>
          {!SHOWCASE && (
            <>
              <MenuDivider />
              <MenuItem danger disabled={loggingOut} onSelect={onLogout}>
                <Icon name="logout" />
                {loggingOut ? '正在退出…' : '退出登录'}
              </MenuItem>
            </>
          )}
        </Menu>
      </div>
    </header>
  )
}
