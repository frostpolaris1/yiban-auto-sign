import { describe, expect, it } from "vitest";
import { DEFAULT_PAGE_SIZE, PAGE_SIZES, clampPage, pageCount, pageSlice } from "./pager";

/* 分页纯函数的不变式（见 pager.ts 模块头）。测性质，不测某段调用顺序。 */
describe("pager", () => {
  it("pageCount：空表 0 页；整除与不整除", () => {
    expect(pageCount(0, 20)).toBe(0);
    expect(pageCount(20, 20)).toBe(1);
    expect(pageCount(21, 20)).toBe(2);
    expect(pageCount(5000, 20)).toBe(250);
  });

  it("clampPage：夹到 [1, pageCount]，空表回 1，非法值回 1", () => {
    expect(clampPage(0, 100, 20)).toBe(1);
    expect(clampPage(-3, 100, 20)).toBe(1);
    expect(clampPage(999, 100, 20)).toBe(5);
    expect(clampPage(3, 100, 20)).toBe(3);
    expect(clampPage(4, 0, 20)).toBe(1);
    expect(clampPage(NaN, 100, 20)).toBe(1);
  });

  it("pageSlice：拼接所有页 == 原数组，且相邻页不重叠", () => {
    const rows = Array.from({ length: 103 }, (_, i) => i);
    const size = 20;
    const out: number[] = [];
    for (let p = 1; p <= pageCount(rows.length, size); p++) {
      const slice = pageSlice(rows, p, size);
      // 不重叠：本页首元素 == 上一页末元素 + 1
      if (out.length) expect(slice[0]).toBe(out[out.length - 1] + 1);
      out.push(...slice);
    }
    expect(out).toEqual(rows);
  });

  it("pageSlice：任一页长度 <= size，越界页夹取后不返回空以外的错行", () => {
    const rows = Array.from({ length: 45 }, (_, i) => i);
    expect(pageSlice(rows, 1, 20)).toHaveLength(20);
    expect(pageSlice(rows, 3, 20)).toEqual([40, 41, 42, 43, 44]);
    // 越界页夹到末页，不返回 undefined 空洞
    expect(pageSlice(rows, 99, 20)).toEqual([40, 41, 42, 43, 44]);
  });

  it("pageSlice：空表任一页都是空数组", () => {
    expect(pageSlice([], 1, 20)).toEqual([]);
    expect(pageSlice([], 5, 20)).toEqual([]);
  });

  it("默认页大小与可选值合法（>=1，且默认在可选值内）", () => {
    expect(DEFAULT_PAGE_SIZE).toBeGreaterThanOrEqual(1);
    expect(PAGE_SIZES).toContain(DEFAULT_PAGE_SIZE);
  });
});
