/**
 * 列表分页的纯函数（Vitest 单测）。
 *
 * 三个列表页（账号 / 用户 / 日志）共用同一份「切片 + 夹取」口径，避免各页各写一份、
 * 在越界与空表上分叉。控件本身走 Element Plus 的 `el-pagination`（不是原生
 * `<select>`/`<input type=range>`：原生控件的弹出层由 UA 渲染、样式不可控，属既有禁令）。
 *
 * 口径（测不变式，不测编舞）：
 *   · `pageSlice` 任一页的切片都不越界、不与相邻页重叠；把所有页拼起来 = 原数组；
 *   · `clampPage` 把页码夹到 `[1, pageCount]`；空表（total=0）仍返回第 1 页；
 *   · `pageCount` 为空表返回 0，`clampPage` 因此把页码夹到 1（空表显示第 1 页而不是 0 页）。
 */

export interface PagerState {
  page: number;
  size: number;
}

/** 每页条数可选值。上限 100 与后端分页的 `PAGE_LIMIT_MAX`(200) 之内，且远小于 5k。 */
export const PAGE_SIZES = [20, 50, 100] as const;
export const DEFAULT_PAGE_SIZE = 20;

/** 总页数。空表返回 0（调用方据此决定是否显示分页控件）。 */
export function pageCount(total: number, size: number): number {
  const n = Math.max(0, Math.floor(Number(total) || 0));
  const s = Math.max(1, Math.floor(Number(size) || 1));
  return Math.ceil(n / s);
}

/** 把页码夹到 `[1, pageCount]`；非法值（NaN / <1）一律回到第 1 页。 */
export function clampPage(page: number, total: number, size: number): number {
  const p = Math.floor(Number(page));
  if (!Number.isFinite(p) || p < 1) return 1;
  return Math.min(p, Math.max(1, pageCount(total, size)));
}

/**
 * 取第 `page` 页（1 基）的切片。页码越界时先夹取再切，故任一页都不越界。
 * `rows` 为只读，函数不修改入参。
 */
export function pageSlice<T>(rows: readonly T[], page: number, size: number): T[] {
  const s = Math.max(1, Math.floor(Number(size) || 1));
  const p = clampPage(page, rows.length, s);
  const start = (p - 1) * s;
  return rows.slice(start, start + s);
}
