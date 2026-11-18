/**
 * **功能**
 * 站内通知收件箱纯函数展示映射：类别元数据、相对时间格式化、角标计数与缓存修补。
 *
 * **归属**
 * `features/notify` 展示层：无 React、无网络请求，供抽屉组件与单元测试共用。
 *
 * **口径**
 * 类别词表闭合于契约 `NotifyCategory`（四类）；未知值退回中性信息态。
 * 时间比较固定按 `Asia/Shanghai` 墙钟计算，跨机器不漂移。
 * 缓存修补只把 `read_at === null` 的目标行补上首读时间，保留既有已读时间戳。
 */

import type { InfiniteData } from '@tanstack/react-query'
import type { Tone } from '@/components/ui/Badge'
import type { IconName } from '@/components/ui/Icon'
import type { Notification, NotificationPage } from '@/lib/api/notify'
import { dayOf, formatClock } from '@/lib/format'

export const NOTIFY_UNREAD_POLL_MS = 30_000
export const NOTIFICATIONS_UNREAD_QUERY_KEY = ['notifications-unread'] as const
export const NOTIFICATIONS_INBOX_QUERY_KEY = ['notifications', 'inbox'] as const

export type CategoryMeta = {
  label: string
  icon: IconName
  tone: Tone
}

const CATEGORY_META: Record<string, CategoryMeta> = {
  credential_invalid: {
    label: '凭据失效',
    icon: 'keyRound',
    tone: 'danger',
  },
  review_pending: {
    label: '账号待审',
    icon: 'userCog',
    tone: 'primary',
  },
  sign_failed_daily: {
    label: '失败摘要',
    icon: 'alertCircle',
    tone: 'warning',
  },
  capacity_rejected: {
    label: '容量不足',
    icon: 'gauge',
    tone: 'danger',
  },
}

export function categoryMeta(category: string): CategoryMeta {
  return CATEGORY_META[category] ?? { label: '系统通知', icon: 'info', tone: 'info' }
}

/** 角标数字收窄：0 或非法值隐藏（null）；超过 99 截断为 `99+`。 */
export function formatUnreadBadge(count: number | null | undefined): string | null {
  if (typeof count !== 'number' || !Number.isFinite(count) || count <= 0) return null
  const floored = Math.floor(count)
  if (floored <= 0) return null
  return floored > 99 ? '99+' : String(floored)
}

function shanghaiYesterday(now: Date): string {
  const [year, month, day] = dayOf(now).split('-').map(Number)
  // 取前一天 UTC 04:00（对应上海 12:00），避开跨月跨年与零点边界。
  return dayOf(new Date(Date.UTC(year, month - 1, day - 1, 4, 0, 0)))
}

/**
 * 收件箱时间显示：`刚刚` / `x 分钟前` / `x 小时前` / `昨天 HH:mm` / `MM-DD HH:mm`。
 * 跨年补齐四位年份；无效文本原样或返回 `—`。
 */
export function formatNotificationTime(
  value: string | null | undefined,
  now: Date = new Date(),
): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value

  const diffMs = now.getTime() - date.getTime()
  const targetDay = dayOf(date)
  const todayDay = dayOf(now)
  const clock = formatClock(value)

  if (diffMs < -60_000) {
    return `${targetDay} ${clock}`
  }
  if (diffMs < 60_000) {
    return '刚刚'
  }
  const diffMinutes = Math.floor(diffMs / 60_000)
  if (diffMinutes < 60) {
    return `${diffMinutes} 分钟前`
  }
  if (targetDay === todayDay) {
    const diffHours = Math.max(1, Math.floor(diffMs / 3_600_000))
    return `${diffHours} 小时前`
  }
  if (targetDay === shanghaiYesterday(now)) {
    return `昨天 ${clock}`
  }
  if (targetDay.slice(0, 4) === todayDay.slice(0, 4)) {
    return `${targetDay.slice(5)} ${clock}`
  }
  return `${targetDay} ${clock}`
}

/** 展平分页结果并按 id 去重，避免新通知到达后翻页边界重复渲染。 */
export function flattenUniqueNotifications(
  pages: readonly NotificationPage[] | undefined,
): Notification[] {
  if (!pages || pages.length === 0) return []
  const seen = new Set<string>()
  const out: Notification[] = []
  for (const page of pages) {
    for (const item of page.items) {
      if (seen.has(item.id)) continue
      seen.add(item.id)
      out.push(item)
    }
  }
  return out
}

/** 仅给未读的目标 id 补上 read_at，已读行保持原首次已读时刻不变。 */
export function patchPagesRead(
  data: InfiniteData<NotificationPage>,
  ids: readonly string[],
  readAt: string,
): InfiniteData<NotificationPage> {
  const idSet = new Set(ids)
  return {
    ...data,
    pages: data.pages.map((page) => ({
      ...page,
      items: page.items.map((item) =>
        idSet.has(item.id) && item.read_at === null ? { ...item, read_at: readAt } : item,
      ),
    })),
  }
}

/** 全部已读本地同步：补齐所有未读行，保留既有页面结构与游标。 */
export function patchAllPagesRead(
  data: InfiniteData<NotificationPage>,
  readAt: string,
): InfiniteData<NotificationPage> {
  return {
    ...data,
    pages: data.pages.map((page) => ({
      ...page,
      items: page.items.map((item) =>
        item.read_at === null ? { ...item, read_at: readAt } : item,
      ),
    })),
  }
}
