/**
 * **功能**
 * 审计日志页的展示口径：每页条数、页脚摘要、日期边界与详情摘要。
 *
 * **归属**
 * `features/audit` 展示层：纯函数，无 React、无网络。
 *
 * **口径**
 * 页脚只给「第 N 页 · 本页 M 条」：契约的 total 是快照计数，不冒充稳定总页数。
 * 日期筛选按上海业务日解释：起始取当天 00:00:00，结束取当天 23:59:59（闭区间）。
 * 详情摘要只压平空白并截断，不改写原文；完整原文在行详情抽屉里。
 */

import type { AuditLogItem } from '@/lib/api/audit'
import { ApiError } from '@/lib/api/client'

export const AUDIT_PAGE_SIZE = 20

/** 详情摘要的最大字符数：列表只扫读，完整原文在抽屉。 */
const DETAIL_PREVIEW_LIMIT = 96

/** 页脚务实摘要：不显示总页数。 */
export function formatAuditPager(pageIndex: number, rowCount: number): string {
  return `第 ${pageIndex + 1} 页 · 本页 ${rowCount} 条`
}

/** 业务日起点转 RFC3339（上海固定 +08:00）。空值不产生参数。 */
export function auditDayStart(day: string): string | undefined {
  return day === '' ? undefined : `${day}T00:00:00+08:00`
}

/** 业务日终点转 RFC3339（闭区间，上海固定 +08:00）。空值不产生参数。 */
export function auditDayEnd(day: string): string | undefined {
  return day === '' ? undefined : `${day}T23:59:59+08:00`
}

/** 行详情摘要：压平空白、截断；空详情给破折号。 */
export function detailPreview(detail: string, limit = DETAIL_PREVIEW_LIMIT): string {
  const flat = detail.replace(/\s+/g, ' ').trim()
  if (flat === '') return '—'
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat
}

/** 行键：契约 id 是 int64，React key 用十进制串。 */
export function auditRowKey(item: AuditLogItem): string {
  return String(item.id)
}

/** 错误展示拆分：界面只露 message，内部 code 只进 title。 */
export function auditErrorDetail(error: unknown): {
  message: string
  code: string | null
} {
  if (error instanceof ApiError) return { message: error.message, code: error.code }
  if (error instanceof Error) return { message: error.message, code: null }
  return { message: String(error), code: null }
}
