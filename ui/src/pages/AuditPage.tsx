/**
 * **功能**
 * 审计日志页：链状态结论、筛选、游标台账与行详情。
 *
 * **归属**
 * `pages` 路由页：只负责挂载 `AuditView`，域逻辑留在 `features/audit`。
 */

import { AuditView } from '@/features/audit/AuditView'

export function AuditPage() {
  return <AuditView />
}
