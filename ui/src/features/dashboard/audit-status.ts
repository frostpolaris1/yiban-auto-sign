/**
 * **功能**
 * 审计链巡检结论的兼容转出。
 *
 * **归属**
 * 纯判定已下沉到 `lib/audit-status.ts`（审计页与看板共用，跨功能目录不互相 import）。
 * 本文件保留原有导出面，看板卡片与单测的导入路径不变。
 */

export {
  AUDIT_VERDICT_LABELS,
  auditBrokenText,
  auditVerdict,
  auditVerdictTone,
  shortHash,
  healthForcedKey,
  resolveHealthOpen,
  type AuditVerdict,
  type AuditVerdictTone,
} from '@/lib/audit-status'
