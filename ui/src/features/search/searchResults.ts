/**
 * **功能**
 * 全局搜索的取数收敛与结果整形：角色门、掩码号直达改写、三类结果行。
 *
 * **归属**
 * `features/search` 纯逻辑：无 React、无 DOM；网络只走 `lib/api`。
 *
 * **口径**
 * 角色门按任务书 06 的数据表：账号与任务归 master/admin，用户只归 master。
 * user 角色无结果组（管理壳 user 面退役见任务 13），不发任何检索请求。
 * 每类只取第一页小值（limit 5）：搜索要「查得到」，不要拖全表。
 * 可见掩码手机号（138****8000 形）服务端按「后 4 位」数字直达，
 * 本层再按掩码前 3 位＋后 4 位过滤展示值，避免同尾号误报。
 * 展示构建（SHOWCASE）读合成夹具，零网络——与其余页面同一口径。
 */

import { listAccounts } from '@/lib/api/account'
import type { Account } from '@/lib/api/account'
import { listTasks } from '@/lib/api/task'
import type { AdminTaskRow } from '@/lib/api/task'
import { listUsers } from '@/lib/api/user'
import type { User } from '@/lib/api/user'
import type { Role } from '@/lib/api/auth'
import { SHOWCASE } from '@/lib/session'
import { showcaseAccounts, showcaseRunPage, showcaseUsers } from '@/lib/showcase'

export type SearchKind = 'account' | 'user' | 'task'

export type SearchResult = {
  kind: SearchKind
  id: string
  title: string
  subtitle: string
  /** 跳转目标：逻辑层不依赖路由实例，由组件落到具体 route。 */
  route: 'accounts' | 'users' | 'run'
  /** 目标页可复现这批结果的检索词（掩码号会被改写为后 4 位）。 */
  query: string
  /** 账号行定位用 id：落地页据此高亮现有精确命中样式。 */
  locateId?: string
}

export type SearchOutcome = {
  results: SearchResult[]
  /** 任一组请求失败的部分失败标记：结果照显示，失败要可见。 */
  partial: boolean
}

/** 每组上限：搜索面板不是列表页。 */
const GROUP_LIMIT = 5

/** 展示形态的手机号掩码：3 位前缀 + 4 位星 + 4 位尾号。 */
const PHONE_MASK = /^\d{3}\*{4}\d{4}$/

export function searchKindLabel(kind: SearchKind): string {
  return kind === 'account' ? '账号' : kind === 'user' ? '用户' : '任务'
}

/** 角色可见的结果组。顺序即面板分组顺序。 */
export function groupsFor(role: Role | null): SearchKind[] {
  if (role === 'master') return ['account', 'user', 'task']
  if (role === 'admin') return ['account', 'task']
  return []
}

/** 账号端点实际发送的检索词：掩码号按后 4 位精确直达。 */
export function accountQueryFor(term: string): string {
  const trimmed = term.trim()
  return PHONE_MASK.test(trimmed) ? trimmed.slice(-4) : trimmed
}

function contains(haystack: string, needle: string): boolean {
  return haystack.toLowerCase().includes(needle)
}

function mapAccounts(items: Account[], term: string): SearchResult[] {
  const mask = PHONE_MASK.test(term.trim()) ? term.trim() : null
  const query = accountQueryFor(term)
  return items
    .filter((item) =>
      mask
        ? item.phone_masked.startsWith(mask.slice(0, 3)) &&
          item.phone_masked.endsWith(mask.slice(-4))
        : true,
    )
    .slice(0, GROUP_LIMIT)
    .map((item) => ({
      kind: 'account' as const,
      id: item.id,
      title: item.display_name || item.phone_masked,
      subtitle: `${item.phone_masked} · ${item.owner_email_masked}`,
      route: 'accounts' as const,
      query,
      locateId: item.id,
    }))
}

function mapUsers(items: User[], term: string): SearchResult[] {
  const query = term.trim()
  return items.slice(0, GROUP_LIMIT).map((item) => ({
    kind: 'user' as const,
    id: item.id,
    title: item.email,
    subtitle: `${item.account_count} 个账号`,
    route: 'users' as const,
    query,
  }))
}

function mapTasks(items: AdminTaskRow[], term: string): SearchResult[] {
  const query = term.trim()
  return items.slice(0, GROUP_LIMIT).map((item) => ({
    kind: 'task' as const,
    id: item.id,
    title: `${item.task_name} · ${item.school_name}`,
    subtitle: `${item.account_phone_masked} · ${item.state}`,
    // 当 02/04 链合入后此路由此处改为 /logs?tab=events&q=
    route: 'run' as const,
    query,
  }))
}

/** 展示构建的本地匹配：可见字段子串；掩码号比对掩码本身。 */
function showcaseAccountRows(term: string): Account[] {
  const lower = term.toLowerCase()
  const mask = PHONE_MASK.test(term) ? term : null
  return showcaseAccounts().items.filter((item) =>
    mask
      ? item.phone_masked.startsWith(mask.slice(0, 3)) &&
        item.phone_masked.endsWith(mask.slice(-4))
      : contains(item.phone_masked, lower) ||
        contains(item.display_name, lower) ||
        contains(item.name, lower) ||
        contains(item.owner_email_masked, lower),
  )
}

function showcaseUserRows(term: string): User[] {
  const lower = term.toLowerCase()
  return showcaseUsers().items.filter(
    (item) => contains(item.email, lower) || contains(item.role, lower),
  )
}

function showcaseTaskRows(term: string): AdminTaskRow[] {
  const lower = term.toLowerCase()
  return showcaseRunPage().items.filter(
    (item) =>
      contains(item.task_name, lower) ||
      contains(item.school_name, lower) ||
      contains(item.account_phone_masked, lower),
  )
}

/**
 * 一次检索：按角色门并发取数，空组不出现。
 * 任何一组失败返回 partial；已成功的组照常展示。
 */
export async function search(term: string, role: Role | null): Promise<SearchOutcome> {
  const trimmed = term.trim()
  const kinds = groupsFor(role)
  if (trimmed === '' || kinds.length === 0) return { results: [], partial: false }

  if (SHOWCASE) {
    const results: SearchResult[] = []
    if (kinds.includes('account')) results.push(...mapAccounts(showcaseAccountRows(trimmed), trimmed))
    if (kinds.includes('user')) results.push(...mapUsers(showcaseUserRows(trimmed), trimmed))
    if (kinds.includes('task')) results.push(...mapTasks(showcaseTaskRows(trimmed), trimmed))
    return { results, partial: false }
  }

  let partial = false
  const results: SearchResult[] = []
  // 每组三态：未启用（null）/成功/失败。失败可见但不吞掉成功组。
  type Guarded<T> = { ok: true; value: T } | { ok: false }
  const guard = async <T>(promise: Promise<T>): Promise<Guarded<T>> => {
    try {
      return { ok: true, value: await promise }
    } catch {
      return { ok: false }
    }
  }
  const skip: Promise<null> = Promise.resolve(null)

  const [accountOutcome, userOutcome, taskOutcome] = await Promise.all([
    kinds.includes('account') ? guard(listAccounts({ q: accountQueryFor(trimmed), limit: GROUP_LIMIT })) : skip,
    kinds.includes('user') ? guard(listUsers({ q: trimmed, limit: GROUP_LIMIT })) : skip,
    kinds.includes('task') ? guard(listTasks({ q: trimmed, limit: GROUP_LIMIT })) : skip,
  ])

  if (accountOutcome) {
    if (accountOutcome.ok) results.push(...mapAccounts(accountOutcome.value.items, trimmed))
    else partial = true
  }
  if (userOutcome) {
    if (userOutcome.ok) results.push(...mapUsers(userOutcome.value.items, trimmed))
    else partial = true
  }
  if (taskOutcome) {
    if (taskOutcome.ok) results.push(...mapTasks(taskOutcome.value.items, trimmed))
    else partial = true
  }
  return { results, partial }
}
