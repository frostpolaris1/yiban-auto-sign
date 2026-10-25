import { Fragment } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { Icon } from '@/components/ui/Icon'
import { Menu, MenuDivider, MenuItem } from '@/components/ui/Menu'
import { useTheme } from '@/lib/theme'

type TopbarProps = {
  crumbs: string[]
  drawerOpen: boolean
  onOpenDrawer: () => void
}

export function Topbar({ crumbs, drawerOpen, onOpenDrawer }: TopbarProps) {
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

        <Menu
          trigger={
            <button type="button" className="icon-btn" aria-label="通知">
              <Icon name="bell" />
              <span className="count danger">3</span>
            </button>
          }
        >
          <div className="dd-head">
            <Icon name="bell" />
            通知
          </div>
          <div className="dd-list">
            <div className="dd-item">
              <div className="dd-avatar a1">SY</div>
              <div className="dd-body">
                <div className="dd-row-head">
                  <strong>系统</strong>
                  <span className="dd-time">2 分钟前</span>
                </div>
                <div className="dd-preview">P1 前端骨架已就绪。</div>
              </div>
            </div>
            <div className="dd-item">
              <div className="dd-avatar a2">SC</div>
              <div className="dd-body">
                <div className="dd-row-head">
                  <strong>调度</strong>
                  <span className="dd-time">9 分钟前</span>
                </div>
                <div className="dd-preview">窗口发现尚未接入，示例数据。</div>
              </div>
            </div>
            <div className="dd-item">
              <div className="dd-avatar a3">AU</div>
              <div className="dd-body">
                <div className="dd-row-head">
                  <strong>审计</strong>
                  <span className="dd-time">26 分钟前</span>
                </div>
                <div className="dd-preview">审计链页面在后续批次交付。</div>
              </div>
            </div>
          </div>
        </Menu>

        <button
          type="button"
          className="icon-btn"
          id="themeToggle"
          aria-label="切换明暗主题"
          onClick={toggleTheme}
        >
          <Icon name={theme === 'dark' ? 'sun' : 'moon'} />
        </button>

        <Menu
          profile
          trigger={
            <button type="button" className="avatar" aria-label="账号菜单">
              OP
            </button>
          }
        >
          <div className="dd-profile-head">
            <div className="dd-profile-name">Operator</div>
            <div className="dd-profile-email">operator@local</div>
          </div>
          <MenuItem onSelect={() => void navigate({ to: '/design' })}>
            <Icon name="settings" />
            账号设置
          </MenuItem>
          <MenuItem onSelect={() => void navigate({ to: '/design' })}>
            <Icon name="userCog" />
            个人信息
          </MenuItem>
          <MenuDivider />
          <MenuItem danger onSelect={() => void navigate({ to: '/login' })}>
            <Icon name="logout" />
            退出登录
          </MenuItem>
        </Menu>
      </div>
    </header>
  )
}
