/**
 * **功能**
 * 审计链巡检结论的文案与色：三态（完好 / 异常 / 暂无行）、定位行与短哈希。
 *
 * **归属**
 * `lib` 纯逻辑：看板与审计页共用同一份三态口径。无 React、无网络、无组件依赖
 * （色调用本文件收窄的字面量联合，调用方按 `Badge` 的 `Tone` 消费）。
 *
 * **口径**
 * 三态由契约字段派生：锚点不符/错误优先判异常，其次 `empty`，最后 `ok`。
 * 空链不是通过（服务端口径），文案明说「暂无审计行」而不是「异常」；
 * 但空链带锚点不符时锚点优先，必须显示异常。
 * 链头只显示前 12 位十六进制，帮助运维与外部锚点比对。
 */

import type { AuditStatusReport } from './api/audit'

export type AuditVerdict = 'ok' | 'broken' | 'empty'

/** 语义色联合：`success`/`danger`/`info` 都是 `Badge` 色调的子集。 */
export type AuditVerdictTone = 'success' | 'danger' | 'info'

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

const AUDIT_VERDICT_TONES: Record<AuditVerdict, AuditVerdictTone> = {
  ok: 'success',
  broken: 'danger',
  empty: 'info',
}

export function auditVerdictTone(verdict: AuditVerdict): AuditVerdictTone {
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
