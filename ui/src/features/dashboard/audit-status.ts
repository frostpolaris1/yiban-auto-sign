/**
 * **功能**
 * 审计链巡检卡的文案与色：三态（完好 / 异常 / 暂无行）、定位行与短哈希。
 *
 * **归属**
 * `features/dashboard` 展示层：纯函数，无 React、无网络。
 *
 * **口径**
 * 三态由契约字段派生：锚点不符/错误优先判异常，其次 `empty`，最后 `ok`。
 * 空链不是通过（服务端口径），文案明说「暂无审计行」而不是「异常」；
 * 但空链带锚点不符时锚点优先，必须显示异常。
 * 不展示审计正文：断链只给定位行 id，不给行内容、actor 或 detail。
 * 链头只显示前 12 位十六进制，帮助运维与外部锚点比对。
 */

import type { Tone } from '@/components/ui/Badge'
import type { AuditStatusReport } from '@/lib/api/audit'

export type AuditVerdict = 'ok' | 'broken' | 'empty'

/** 锚点不符或读不出时优先判异常：空链不能把锚点告警洗成「暂无行」。 */
function anchorBroken(report: AuditStatusReport): boolean {
  return report.anchor_status === 'mismatch' || report.anchor_status === 'error'
}

/** 三态判定：锚点异常优先，其次空链，最后 ok。 */
export function auditVerdict(report: AuditStatusReport): AuditVerdict {
  if (anchorBroken(report)) return 'broken'
  if (report.empty) return 'empty'
  return report.ok ? 'ok' : 'broken'
}

export const AUDIT_VERDICT_LABELS: Record<AuditVerdict, string> = {
  ok: '链完好',
  broken: '链异常',
  empty: '暂无审计行',
}

const AUDIT_VERDICT_TONES: Record<AuditVerdict, Tone> = {
  ok: 'success',
  broken: 'danger',
  empty: 'info',
}

export function auditVerdictTone(verdict: AuditVerdict): Tone {
  return AUDIT_VERDICT_TONES[verdict]
}

/** 断链或锚点异常的说明；无异常时返回 null。 */
export function auditBrokenText(report: AuditStatusReport): string | null {
  if (anchorBroken(report)) {
    return report.anchor_reason || '库外锚点比对失败，链头与锚点记录不一致。'
  }
  if (report.broken_at === null) return null
  return `在校验第 ${report.broken_at} 行时发现哈希或链接不符。`
}

/** 链头短哈希；空前缀返回空串，不造占位。 */
export function shortHash(hash: string): string {
  if (hash === '') return ''
  return hash.length > 12 ? `${hash.slice(0, 12)}…` : hash
}
