/**
 * **功能**
 * 今日失败明细的纯展示逻辑：失败结果码的中文名与处置建议、桶排序与占比文案。
 *
 * **归属**
 * `features/dashboard` 纯逻辑层；卡片组件只负责呈现。
 *
 * **口径**
 * - 数据来自 `GET /api/sign-stats/failures`（按失败结果分桶的已有端点），
 *   不重算 `/api/sign-stats` 的计数，也不臆造逐任务明细。
 * - 服务端已按 count 降序再按 result 升序排好；本层不再重排，只补中文名。
 * - 结果码的权威词表在服务端 `signing/contract` 的 `Outcome`；空串由服务端
 *   归为 `unknown`，词表外的值原样展示，不猜含义。
 * - `features` 之间禁止互导，故这里自带一份词表；`features/queue` 的
 *   `TASK_RESULT_GUIDES` 是同一批码的另一份呈现，两边都由契约词表钉住。
 * - 今日无失败不等于数据缺失：空态必须把这句话说清楚。
 */

import type { SignFailuresReport } from '@/lib/api/stats'

/** 失败结果码的中文名与处置建议。只登记会落 failed 的码。 */
const FAILURE_GUIDES: Record<string, { label: string; hint: string }> = {
  credential_invalid: {
    label: '凭据无效',
    hint: '需用户更新账号密码或设备信息，盲目重试通常仍失败',
  },
  rejected: { label: '平台拒绝请求', hint: '平台判定本次签到无效，先核对账号与签到点位' },
  photo_rejected: { label: '照片未通过', hint: '平台驳回上传照片，需更换照片后重新提交' },
  blocked: { label: '请求被拦截', hint: '命中平台风控或 WAF 拦截，先核查出口与账号状态' },
  transient: { label: '临时网络波动', hint: '重试预算已用尽，可手动重签' },
  pending_upstream: { label: '平台处理未完成', hint: '平台上仍在处理，稍后再重签' },
  unknown: { label: '平台返回未知结果', hint: '结果不明，打开诊断确认后再决定是否重试' },
}

export type FailureBucketRow = {
  /** 服务端原样的结果码，用于排查。 */
  code: string
  label: string
  hint: string
  count: number
  /** 占失败总数的百分比文案，例如 62.5%。 */
  share: string
}

/** 结果码 → 中文名；空串归 unknown，未登记的原样回显码本身。 */
export function describeFailureResult(code: string): { label: string; hint: string } {
  const normalized = code.trim()
  if (normalized === '') {
    return { label: '未提供结果码', hint: '服务端未记录具体失败原因，可打开诊断确认' }
  }
  const hit = FAILURE_GUIDES[normalized]
  if (hit) return hit
  return { label: '未登记的结果码', hint: '后端新增了结果码，前端词表尚未登记' }
}

/** 供单元测试与排查确认：本表登记了哪些码。 */
export function failureGuideCodes(): string[] {
  return Object.keys(FAILURE_GUIDES)
}

/** 把报告摊成可直接渲染的行；顺序保持服务端给的顺序。 */
export function failureBucketRows(report: SignFailuresReport): FailureBucketRow[] {
  const total = report.total
  return report.items.map((bucket) => {
    const guide = describeFailureResult(bucket.result)
    const share = total > 0 ? `${((bucket.count / total) * 100).toFixed(1)}%` : '—'
    return {
      code: bucket.result,
      label: guide.label,
      hint: guide.hint,
      count: bucket.count,
      share,
    }
  })
}

/** 今日无失败时的空态文案：明确「没有失败」与「没有数据」不是一回事。 */
export const FAILURE_EMPTY_TEXT = '今日暂无签到失败'
export const FAILURE_EMPTY_HINT =
  '这是今日任务结果的统计结论，不是读取失败：今天还没有落到失败终态的任务。'
