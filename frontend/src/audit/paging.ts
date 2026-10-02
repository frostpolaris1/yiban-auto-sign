/**
 * 审计翻页状态机（纯函数，便于 Vitest 单测）。
 *
 * 契约第 2 条（`web/routes/audit_api.py`）明确：**终止判据只看 `has_more`**，
 * 不得用 `len(rows) == page_size` 自行推断——末页恰好满页时会多翻一次空页。
 * 响应信封：`{rows, page, page_size, total, has_more}`；`total` 是当前过滤条件下的
 * 命中总数（忽略分页）。
 */

export interface AuditRow {
  /** 不透明行定位符（HMAC 前 16 hex），仅用作列表 key —— 后端不返回自增 id */
  row_id: string;
  ts: string;
  actor: string;
  action: string;
  target: string;
  detail: string;
}

export interface AuditPageResponse {
  rows: AuditRow[];
  page: number;
  page_size: number;
  total: number;
  has_more: boolean;
}

export interface AuditState {
  rows: AuditRow[];
  /** 已加载到的页码；0 = 尚未加载 */
  page: number;
  total: number;
  hasMore: boolean;
}

export const INITIAL_STATE: AuditState = { rows: [], page: 0, total: 0, hasMore: false };

/**
 * 并入一页结果。`append=false`（改过滤条件后的首页）整表替换并回到起点语义；
 * `append=true`（加载更多）按 row_id 去重后追加——并发期间数据变化可能让两页有重叠行。
 */
export function mergePage(prev: AuditState, resp: AuditPageResponse, append: boolean): AuditState {
  const rows = append ? dedupeByRowId([...prev.rows, ...resp.rows]) : resp.rows.slice();
  return {
    rows,
    page: resp.page,
    total: resp.total,
    hasMore: resp.has_more === true,
  };
}

export function dedupeByRowId(rows: AuditRow[]): AuditRow[] {
  const seen = new Set<string>();
  const out: AuditRow[] = [];
  for (const row of rows) {
    if (seen.has(row.row_id)) continue;
    seen.add(row.row_id);
    out.push(row);
  }
  return out;
}

export function canLoadMore(state: AuditState): boolean {
  return state.hasMore && state.rows.length > 0;
}

export function nextPageNumber(state: AuditState): number {
  return state.page + 1;
}

/**
 * 复位。**必须返回全新对象与全新数组**：不能写成 `{ ...INITIAL_STATE }`——那样
 * `rows` 会与模块级常量共享同一数组引用，调用方一旦原地改动就污染 `INITIAL_STATE`
 * （由 paging.spec.ts 的引用不等断言钉住）。
 */
export function resetState(): AuditState {
  return { rows: [], page: 0, total: 0, hasMore: false };
}
