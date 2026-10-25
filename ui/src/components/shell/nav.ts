import type { IconName } from '@/components/ui/Icon'

/**
 * Navigation source of truth.
 *
 * This replaces `NAV` in Adminator `Shell.js`. A new page is one entry here.
 * Every `to` value must exist in the router. The shell derives breadcrumbs
 * from the same table, so the two cannot drift.
 */

export type NavBadgeKind = 'new' | 'pro' | 'hot'

export type NavBadge = {
  kind: NavBadgeKind
  text: string
}

export type NavLeaf = {
  key: string
  text: string
  to: string
  icon: IconName
  badge?: NavBadge
  crumbs: string[]
}

export type NavGroup = {
  key: string
  text: string
  icon: IconName
  children: NavLeaf[]
}

export type NavItem = NavLeaf | NavGroup

export type NavSection = {
  label: string
  items: NavItem[]
}

export function isNavGroup(item: NavItem): item is NavGroup {
  return 'children' in item
}

export const NAV: NavSection[] = [
  {
    label: '工作区',
    items: [
      {
        key: 'dashboard',
        text: '仪表盘',
        to: '/',
        icon: 'dashboard',
        crumbs: ['工作区', '仪表盘'],
      },
    ],
  },
  {
    label: '系统',
    items: [
      {
        key: 'design',
        text: '设计系统',
        icon: 'design',
        children: [
          {
            key: 'design-gallery',
            text: '组件画廊',
            to: '/design',
            icon: 'design',
            badge: { kind: 'new', text: 'NEW' },
            crumbs: ['系统', '设计系统', '组件画廊'],
          },
          {
            key: 'login',
            text: '登录页',
            to: '/login',
            icon: 'shield',
            crumbs: ['系统', '设计系统', '登录页'],
          },
        ],
      },
    ],
  },
]

/** Flat list of navigable leaves. Used by breadcrumb lookup. */
export const NAV_LEAVES: NavLeaf[] = NAV.flatMap((section) =>
  section.items.flatMap((item) => (isNavGroup(item) ? item.children : [item])),
)

export function crumbsFor(pathname: string): string[] {
  const leaf = NAV_LEAVES.find((item) => item.to === pathname)
  return leaf?.crumbs ?? ['工作区', '未命名页面']
}

export function documentTitleFor(pathname: string): string {
  const leaf = NAV_LEAVES.find((item) => item.to === pathname)
  return leaf ? `${leaf.text} · 易班自动签到` : '易班自动签到'
}
