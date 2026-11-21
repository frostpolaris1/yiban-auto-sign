/**
 * **功能**
 * 通知订阅矩阵的纯逻辑：展示映射、按契约响应序组表、全量替换体与错误文案。
 *
 * **归属**
 * `features/settings` 域逻辑：无 React、无网络、无 DOM。
 *
 * **口径**
 * 类别与渠道列表完全以契约 `GET /api/admin/notify` 返回顺序为准，不在前端猜缺省行。
 * 未知类别或渠道降级显示原始 id，确保新枚举下发时不丢格。
 * 写操作发送全量覆盖集（`NotifyPrefsUpdate`），且不原地变更入参。
 */

import type {
  NotifyCategory,
  NotifyChannel,
  NotifyPref,
  NotifyPrefsUpdate,
} from '../../lib/api/adminNotify'
import { ApiError, describeApiError } from '../../lib/api/client'

export const ADMIN_NOTIFY_QUERY_KEY = ['admin-notify'] as const

export const NOTIFY_SAVED_TOAST = '通知订阅已保存'
export const NOTIFY_NETWORK_ERROR = '网络异常，请稍后重试'
export const NOTIFY_READ_ERROR = '通知订阅读取失败，请稍后重试'

export type NotifyCategoryMeta = {
  label: string
  description: string
}

const CATEGORY_META: Record<string, NotifyCategoryMeta> = {
  credential_invalid: {
    label: '凭据失效',
    description: '账号凭据失效或登录异常，需重新授权处理',
  },
  review_pending: {
    label: '账号待审',
    description: '有新提交或修改资料后待审核的签到账号',
  },
  sign_failed_daily: {
    label: '每日失败摘要',
    description: '每个业务日定时汇总前一日签到失败情况',
  },
  capacity_rejected: {
    label: '容量驳回',
    description: '任务窗口容量不足导致账号无法排入',
  },
}

const CHANNEL_LABELS: Record<string, string> = {
  in_app: '站内信',
  email: '邮件',
  webhook: 'Webhook',
}

export type NotifyChannelInfo = {
  id: NotifyChannel
  label: string
}

export type NotifyMatrixRow = {
  category: NotifyCategory
  label: string
  description: string
  cells: Partial<Record<NotifyChannel, boolean>>
}

export type NotifyMatrix = {
  channels: NotifyChannelInfo[]
  rows: NotifyMatrixRow[]
}

export function notifyCategoryMeta(category: string): NotifyCategoryMeta {
  return CATEGORY_META[category] ?? { label: category, description: '' }
}

export function notifyChannelLabel(channel: string): string {
  return CHANNEL_LABELS[channel] ?? channel
}

/** 按契约响应中的首次出现顺序提取渠道列与类别行，组装 2D 矩阵。 */
export function buildNotifyMatrix(items: readonly NotifyPref[]): NotifyMatrix {
  const channelOrder: NotifyChannel[] = []
  const seenChannels = new Set<NotifyChannel>()
  const rowsByCategory = new Map<NotifyCategory, NotifyMatrixRow>()

  for (const item of items) {
    if (!seenChannels.has(item.channel)) {
      seenChannels.add(item.channel)
      channelOrder.push(item.channel)
    }
    let row = rowsByCategory.get(item.category)
    if (!row) {
      const meta = notifyCategoryMeta(item.category)
      row = {
        category: item.category,
        label: meta.label,
        description: meta.description,
        cells: {},
      }
      rowsByCategory.set(item.category, row)
    }
    row.cells[item.channel] = item.enabled
  }

  return {
    channels: channelOrder.map((id) => ({ id, label: notifyChannelLabel(id) })),
    rows: [...rowsByCategory.values()],
  }
}

/** 生成全量替换请求体：保留全部既有配对，只翻转目标格的 `enabled`。 */
export function buildNotifyUpdateBody(
  items: readonly NotifyPref[],
  targetCategory: NotifyCategory,
  targetChannel: NotifyChannel,
  nextEnabled: boolean,
): NotifyPrefsUpdate {
  return {
    items: items.map((item) =>
      item.category === targetCategory && item.channel === targetChannel
        ? { category: item.category, channel: item.channel, enabled: nextEnabled }
        : { category: item.category, channel: item.channel, enabled: item.enabled },
    ),
  }
}

export function notifySaveErrorMessage(error: unknown): string {
  if (error instanceof ApiError) return describeApiError(error)
  return NOTIFY_NETWORK_ERROR
}

export function notifyReadErrorMessage(error: unknown): string {
  if (error instanceof ApiError) return describeApiError(error)
  return NOTIFY_READ_ERROR
}
