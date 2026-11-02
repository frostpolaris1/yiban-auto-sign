/**
 * **功能**
 * 账号搜索的精确命中判定：完整手机号、完整邮箱，或完整备注名。
 *
 * **归属**
 * `features/accounts` 纯逻辑：无 React、无网络、无 DOM。
 *
 * **口径**
 * 完整手机号与完整邮箱由服务端精确直达，返回的每一行都是命中行：
 * 页面据此整页高亮，管理员一眼确认是谁。
 * 其余输入按子串发现；只有备注名（display_name）与输入全等时才高亮，
 * 部分子串即使只剩一行也不高亮。
 * 手机号归一与服务端同一口径：去非数字，13 位且以 86 开头时去国家码。
 * 含 *** 的邮箱是可见掩码，不是完整邮箱，不触发精确高亮。
 */

import type { Account } from '@/lib/api/account'

/** 完整手机号返回 11 位号码；否则返回 null。 */
export function completePhoneQuery(value: string | undefined): string | null {
  const digits = (value ?? '').replace(/\D/g, '')
  const normalized = digits.length === 13 && digits.startsWith('86') ? digits.slice(2) : digits
  return normalized.length === 11 ? normalized : null
}

/** 与 users.email 的 CHECK 同形：恰好一个 @、两侧非空、无空白、无 *。 */
export function isCompleteEmailQuery(value: string | undefined): boolean {
  const term = (value ?? '').trim()
  if (term.split('@').length !== 2 || term.includes('*')) return false
  const [local, domain] = term.split('@')
  return local !== '' && domain !== '' && !/\s/.test(term)
}

/** 完整标识走服务端精确直达：返回的全部行都是命中行。 */
export function isExactIdentifier(value: string | undefined): boolean {
  return completePhoneQuery(value) !== null || isCompleteEmailQuery(value)
}

/**
 * 命中行 id 集合。空查询不返回任何 id。
 * 完整手机号/邮箱：服务端只返回命中行，整页高亮；
 * 其余查询：只有 display_name 与输入全等（忽略大小写与首尾空白）才高亮。
 */
export function exactMatchIds(
  rows: Array<Pick<Account, 'id' | 'display_name'>>,
  query: string | undefined,
): Set<string> {
  const term = (query ?? '').trim()
  if (term === '') return new Set()
  if (isExactIdentifier(term)) return new Set(rows.map((row) => row.id))
  const lower = term.toLowerCase()
  return new Set(
    rows.filter((row) => row.display_name.trim().toLowerCase() === lower).map((row) => row.id),
  )
}
