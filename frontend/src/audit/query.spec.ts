import { describe, expect, it } from "vitest";
import {
  DEFAULT_PAGE_SIZE,
  EMPTY_FILTERS,
  MAX_PAGE_SIZE,
  buildAuditQuery,
  clampPageSize,
  toDayEnd,
  toDayStart,
} from "./query";

/** 解析 buildAuditQuery 的产物为可断言结构（路径 + 解码后的参数）。 */
function parse(query: string): { path: string; params: URLSearchParams } {
  const [path, qs] = query.split("?");
  return { path, params: new URLSearchParams(qs ?? "") };
}

describe("clampPageSize", () => {
  it("把越界值收进 1..200，非法值回默认", () => {
    expect(clampPageSize(0)).toBe(1);
    expect(clampPageSize(-5)).toBe(1);
    expect(clampPageSize(500)).toBe(MAX_PAGE_SIZE);
    expect(clampPageSize(Number.NaN)).toBe(DEFAULT_PAGE_SIZE);
    expect(clampPageSize(75.9)).toBe(75);
  });
});

describe("日期 → ts 边界（契约：ts 为 'YYYY-MM-DD HH:MM:SS' 闭区间）", () => {
  it("日期补全为当日首尾，避免 to_ts 漏掉当天", () => {
    expect(toDayStart("2026-10-02")).toBe("2026-10-02 00:00:00");
    expect(toDayEnd("2026-10-02")).toBe("2026-10-02 23:59:59");
  });

  it("非日期形态返回空串（不构造非法过滤值）", () => {
    expect(toDayStart("")).toBe("");
    expect(toDayStart("2026/10/02")).toBe("");
    expect(toDayEnd("20261002")).toBe("");
  });
});

describe("buildAuditQuery", () => {
  it("始终带 page 与 page_size，且页码下界为 1", () => {
    const { path, params } = parse(buildAuditQuery(EMPTY_FILTERS, 0, 50));
    expect(path).toBe("/api/audit-logs");
    expect(params.get("page")).toBe("1");
    expect(params.get("page_size")).toBe("50");
  });

  it("空过滤值不下发（白名单键只在有值时才出现）", () => {
    const { params } = parse(buildAuditQuery(EMPTY_FILTERS, 1, 50));
    for (const key of ["action", "actor", "target", "from_ts", "to_ts"]) {
      expect(params.has(key)).toBe(false);
    }
  });

  it("五个白名单过滤键按预期下发，且日期映射为当日闭区间", () => {
    const { params } = parse(
      buildAuditQuery(
        { action: "login_success", actor: "138****8000", target: "acct-1", fromDate: "2026-10-01", toDate: "2026-10-02" },
        3,
        100,
      ),
    );
    expect(params.get("page")).toBe("3");
    expect(params.get("page_size")).toBe("100");
    expect(params.get("action")).toBe("login_success");
    expect(params.get("actor")).toBe("138****8000");
    expect(params.get("target")).toBe("acct-1");
    expect(params.get("from_ts")).toBe("2026-10-01 00:00:00");
    expect(params.get("to_ts")).toBe("2026-10-02 23:59:59");
  });

  it("过滤值两端空白被裁掉（避免把空格当等值条件）", () => {
    const { params } = parse(buildAuditQuery({ ...EMPTY_FILTERS, action: "  login_success  " }, 1, 50));
    expect(params.get("action")).toBe("login_success");
  });

  it("只出现白名单键——不得混入任何其它查询键（契约第 3 条，后端对未知键 400）", () => {
    const { params } = parse(
      buildAuditQuery({ action: "a", actor: "b", target: "c", fromDate: "2026-01-01", toDate: "2026-01-02" }, 2, 50),
    );
    const allowed = new Set(["page", "page_size", "action", "actor", "target", "from_ts", "to_ts"]);
    for (const key of params.keys()) expect(allowed.has(key), `未知键 ${key}`).toBe(true);
  });

  it("page_size 越界时按 clamp 后的值下发", () => {
    expect(parse(buildAuditQuery(EMPTY_FILTERS, 1, 9999)).params.get("page_size")).toBe("200");
  });
});
