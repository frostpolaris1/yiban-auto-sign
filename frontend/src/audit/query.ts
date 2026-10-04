/**
 * 审计查询参数构造（纯函数，便于 Vitest 单测）。
 *
 * 契约来源：`web/routes/audit_api.py` 模块 docstring（七约束）——
 *   · 过滤键白名单：action / actor / target / from_ts / to_ts，出现其它键后端 400；
 *   · page ≥ 1，page_size ∈ 1..200（后端默认 50，两者在响应里回显）；
 *   · `ts` 列以 `"YYYY-MM-DD HH:MM:SS"` 文本存储、闭区间比较
 *     （`yiban/store/audit_chain.py` 写入侧 `strftime`，读取侧 `ts >= ?` / `ts <= ?`），
 *     故日期选择器给出的 `YYYY-MM-DD` 必须补全为当日 00:00:00 / 23:59:59——
 *     只传日期会让 `to_ts` 漏掉当天（"2026-10-02" < "2026-10-02 00:00:01"）。
 */
export const DEFAULT_PAGE_SIZE = 50;
export const MAX_PAGE_SIZE = 200;

export interface AuditFilters {
  action: string;
  actor: string;
  target: string;
  /** `YYYY-MM-DD`（原生 date 输入的值形态） */
  fromDate: string;
  toDate: string;
}

export const EMPTY_FILTERS: AuditFilters = {
  action: "",
  actor: "",
  target: "",
  fromDate: "",
  toDate: "",
};

export function clampPageSize(n: number): number {
  if (!Number.isFinite(n)) return DEFAULT_PAGE_SIZE;
  const i = Math.floor(n);
  if (i < 1) return 1;
  if (i > MAX_PAGE_SIZE) return MAX_PAGE_SIZE;
  return i;
}

/** `2026-10-02` → `2026-10-02 00:00:00`；非日期形态原样返回空串（不构造非法过滤值）。 */
export function toDayStart(date: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(date) ? `${date} 00:00:00` : "";
}

/** `2026-10-02` → `2026-10-02 23:59:59`（闭区间上界，见模块注释）。 */
export function toDayEnd(date: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(date) ? `${date} 23:59:59` : "";
}

/**
 * 组装查询串。空过滤值不下发（后端对空串按“该键存在但为空”处理成 None，仍会命中白名单，
 * 但不下发更省一次无意义比较）；键序固定，便于测试与日志比对。
 */
export function buildAuditQuery(filters: AuditFilters, page: number, pageSize: number): string {
  const params = new URLSearchParams();
  params.set("page", String(Math.max(1, Math.floor(page) || 1)));
  params.set("page_size", String(clampPageSize(pageSize)));

  const action = filters.action.trim();
  const actor = filters.actor.trim();
  const target = filters.target.trim();
  if (action) params.set("action", action);
  if (actor) params.set("actor", actor);
  if (target) params.set("target", target);

  const from = toDayStart(filters.fromDate);
  const to = toDayEnd(filters.toDate);
  if (from) params.set("from_ts", from);
  if (to) params.set("to_ts", to);

  return `/api/audit-logs?${params.toString()}`;
}
