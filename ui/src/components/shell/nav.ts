/**
 * **功能**
 * 导航数据源：段、项、分组、面包屑、页面标题。
 *
 * **归属**
 * `components/shell`：壳的配置，不渲染。
 *
 * **口径**
 * 每个 `to` 必须在 `router.tsx` 里有对应路由。
 * 面包屑与标题都从这里派生，不另写一份。
 */

import type { IconName } from '@/components/ui/Icon'

/**
 * 导航唯一来源。取代 Adminator `Shell.js` 里的 `NAV`。
 * 新增页面只在此加一条。
 * 路径写错时侧栏出死链，页面标题退回产品名。
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

/** 可导航叶子的平铺表。面包屑查找用。 */
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
