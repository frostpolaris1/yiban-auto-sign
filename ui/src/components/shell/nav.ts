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
 * `/login` 不在表内：壳内路由需要会话，登录页只对未登录可见。
 * 整页无权限＝导航项不渲染（2026-10-28 用户裁决）；`roles` 是唯一判据。
 * 页内动作级无权限仍走各 feature 的「置灰不隐藏」。
 */

import type { IconName } from '@/components/ui/Icon'
import type { Role } from '@/lib/api/auth'
import { SHOWCASE } from '@/lib/showcase-mode'
import {
  RUN_VIEW_ROLES,
  SCHOOL_VIEW_ROLES,
  SETTINGS_VIEW_ROLES,
  SYSTEM_VIEW_ROLES,
  USER_VIEW_ROLES,
  roleAllowed,
} from '../../lib/roles'

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
  /** 缺省＝全体可见。给出时只对这些角色渲染。 */
  roles?: readonly Role[]
  /** 入口合并期：这些路径也点亮本项（如 /queue 临时落到 /run）。 */
  alsoActive?: readonly string[]
  /** 页面未上线：渲染为不可点条目，title 说明原因。 */
  pendingNote?: string
  /** 仅开发或展示构建渲染，生产侧栏隐藏（路由仍保留直达）。 */
  devOrShowcaseOnly?: boolean
}

export type NavGroup = {
  key: string
  text: string
  icon: IconName
  children: NavLeaf[]
  /** 仅开发或展示构建渲染，生产侧栏隐藏（路由仍保留直达）。 */
  devOrShowcaseOnly?: boolean
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
    label: '今日',
    items: [
      {
        key: 'dashboard',
        text: '总览',
        to: '/',
        icon: 'dashboard',
        crumbs: ['今日', '总览'],
      },
      {
        key: 'queue',
        text: '待办',
        to: '/queue',
        icon: 'listTodo',
        roles: RUN_VIEW_ROLES,
        crumbs: ['今日', '待办'],
      },
      {
        key: 'logs',
        text: '签到台账',
        to: '/logs',
        icon: 'scrollText',
        roles: RUN_VIEW_ROLES,
        crumbs: ['今日', '签到台账'],
      },
    ],
  },
  {
    label: '管理',
    items: [
      {
        key: 'accounts',
        text: '账号',
        to: '/accounts',
        icon: 'accounts',
        crumbs: ['管理', '账号'],
      },
      {
        key: 'users',
        text: '用户',
        to: '/users',
        icon: 'userCog',
        roles: USER_VIEW_ROLES,
        crumbs: ['管理', '用户'],
      },
      {
        key: 'schools',
        text: '学校',
        to: '/schools',
        icon: 'school',
        roles: SCHOOL_VIEW_ROLES,
        crumbs: ['管理', '学校'],
      },
    ],
  },
  {
    label: '系统',
    items: [
      {
        key: 'audit',
        text: '审计日志',
        to: '/audit',
        icon: 'shield',
        roles: RUN_VIEW_ROLES,
        crumbs: ['系统', '审计日志'],
      },
      {
        key: 'settings',
        text: '账号设置',
        to: '/settings',
        icon: 'settings',
        roles: SETTINGS_VIEW_ROLES,
        crumbs: ['系统', '账号设置'],
      },
      {
        key: 'system',
        text: '系统设置',
        to: '/system',
        icon: 'sliders',
        roles: SYSTEM_VIEW_ROLES,
        crumbs: ['系统', '系统设置'],
      },
      {
        key: 'design',
        text: '设计系统',
        icon: 'design',
        devOrShowcaseOnly: true,
        children: [
          {
            key: 'design-gallery',
            text: '组件画廊',
            to: '/design',
            icon: 'design',
            badge: { kind: 'new', text: 'NEW' },
            crumbs: ['系统', '设计系统', '组件画廊'],
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

/** 设计系统导航仅在展示构建或开发模式渲染。 */
export function isDesignNavEnabled(): boolean {
  return SHOWCASE || Boolean(import.meta.env?.DEV)
}

function navLeafVisible(
  leaf: NavLeaf,
  role: Role | null | undefined,
  includeDesign: boolean,
): boolean {
  if (leaf.devOrShowcaseOnly && !includeDesign) return false
  return leaf.roles === undefined || roleAllowed(role, leaf.roles)
}

/**
 * 按角色过滤后的导航。整页无权限的叶子不渲染；子项全被过滤的分组一起消失。
 * 面包屑仍用 `NAV_LEAVES`：直接访问无权限页或直达 `/design` 时标题与面包屑照常可解析。
 */
export function visibleNav(
  role: Role | null | undefined,
  options?: { includeDesign?: boolean },
): NavSection[] {
  const includeDesign = options?.includeDesign ?? isDesignNavEnabled()
  return NAV.flatMap((section): NavSection[] => {
    const items = section.items.flatMap((item): NavItem[] => {
      if (isNavGroup(item)) {
        if (item.devOrShowcaseOnly && !includeDesign) return []
        const children = item.children.filter((child) =>
          navLeafVisible(child, role, includeDesign),
        )
        return children.length > 0 ? [{ ...item, children }] : []
      }
      return navLeafVisible(item, role, includeDesign) ? [item] : []
    })
    return items.length > 0 ? [{ ...section, items }] : []
  })
}

export function crumbsFor(pathname: string): string[] {
  const leaf = NAV_LEAVES.find((item) => item.to === pathname)
  return leaf?.crumbs ?? ['系统', '未命名页面']
}

export function documentTitleFor(pathname: string): string {
  const leaf = NAV_LEAVES.find((item) => item.to === pathname)
  const title = leaf?.text
  return title ? `${title} · 易班自动签到` : '易班自动签到'
}
