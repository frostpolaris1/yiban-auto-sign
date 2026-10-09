import { describe, expect, it } from "vitest";
import {
  INITIAL_STATE,
  canLoadMore,
  dedupeByRowId,
  mergePage,
  nextPageNumber,
  resetState,
  type AuditPageResponse,
  type AuditRow,
} from "./paging";

function row(id: string): AuditRow {
  return { row_id: id, ts: "2026-10-02 12:00:00", actor: "a***@x.com", action: "login", target: "t", detail: "d" };
}

function resp(over: Partial<AuditPageResponse>): AuditPageResponse {
  return { rows: [], page: 1, page_size: 50, total: 0, has_more: false, ...over };
}

describe("mergePage", () => {
  it("append=false（改条件后的首页）整表替换", () => {
    const prev = mergePage(INITIAL_STATE, resp({ rows: [row("r1"), row("r2")], total: 2 }), false);
    const next = mergePage(prev, resp({ rows: [row("r9")], total: 1 }), false);
    expect(next.rows.map((r) => r.row_id)).toEqual(["r9"]);
    expect(next.total).toBe(1);
  });

  it("append=true 追加并按 row_id 去重（并发下两页可能重叠）", () => {
    const prev = mergePage(INITIAL_STATE, resp({ rows: [row("r1"), row("r2")], total: 3 }), false);
    const next = mergePage(prev, resp({ rows: [row("r2"), row("r3")], page: 2, total: 3 }), true);
    expect(next.rows.map((r) => r.row_id)).toEqual(["r1", "r2", "r3"]);
    expect(next.page).toBe(2);
  });

  it("page/total/has_more 一律取服务端回显值", () => {
    const next = mergePage(INITIAL_STATE, resp({ rows: [row("r1")], page: 4, page_size: 100, total: 400, has_more: true }), false);
    expect(next.page).toBe(4);
    expect(next.total).toBe(400);
    expect(next.hasMore).toBe(true);
  });
});

describe("翻页终止判据（契约第 2 条：只看 has_more）", () => {
  it("末页恰好满页但 has_more=false → 不可再翻（不得用行数推断）", () => {
    const full = Array.from({ length: 50 }, (_, i) => row(`r${i}`));
    const state = mergePage(INITIAL_STATE, resp({ rows: full, page: 2, page_size: 50, total: 100, has_more: false }), true);
    expect(state.rows).toHaveLength(50);
    expect(canLoadMore(state)).toBe(false);
  });

  it("未满页但 has_more=true → 仍可继续（服务端说了算）", () => {
    const state = mergePage(INITIAL_STATE, resp({ rows: [row("r1")], has_more: true }), false);
    expect(canLoadMore(state)).toBe(true);
  });

  it("空表不可翻，下一页号由已加载页推导", () => {
    expect(canLoadMore(INITIAL_STATE)).toBe(false);
    const state = mergePage(INITIAL_STATE, resp({ rows: [row("r1")], page: 3, has_more: true }), false);
    expect(nextPageNumber(state)).toBe(4);
  });
});

describe("dedupeByRowId / resetState", () => {
  it("保序去重", () => {
    expect(dedupeByRowId([row("a"), row("b"), row("a")]).map((r) => r.row_id)).toEqual(["a", "b"]);
  });

  it("resetState 回到初始态且不共享引用", () => {
    const s1 = resetState();
    const s2 = resetState();
    expect(s1).toEqual(INITIAL_STATE);
    expect(s1).not.toBe(s2);
    expect(s1.rows).not.toBe(s2.rows);
  });
});
