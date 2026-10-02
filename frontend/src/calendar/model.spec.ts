import { describe, expect, it } from "vitest";
import type { CalendarCell, CalendarCtx } from "./types";
import {
  BLANK_CLS,
  CAL_API,
  CELLS,
  LOG_API,
  ROWS,
  SKELETON_CLS,
  WEEK,
  dayCell,
  gridCells,
  logEmptyText,
  logSkipText,
  monthDirection,
  monthIndex,
  monthKey,
  monthLabel,
  offDayOf,
  shiftMonth,
  shouldAnimate,
  stateEntry,
  statusLine,
  unionTones,
} from "./model.js";

/* 口径层单测。**分工**：`statusLine` / `stateEntry` / `dayCell` 三个函数另由
   tests/test_calendar_state_visibility.py 在 Node 里真跑（它按字面量从 model.js 抽段，
   见该文件头部的约束说明）——这里只补它够不到的：格组合、换月方向、图例并集、周末门、
   日志空态。刻意**不**重复它已覆盖的断言集，避免两处维护同一份期望值。 */

function ctxFixture(over: Partial<CalendarCtx> = {}): CalendarCtx {
  return {
    by_code: {
      pending: { symbol: "⏳", text: "待签到", tone: "warn" },
      success: { symbol: "✅", text: "签到成功", tone: "ok" },
      failed: { symbol: "❌", text: "签到失败", tone: "bad" },
      retrying: { symbol: "🔄", text: "正在签到", tone: "busy" },
    },
    by_symbol: {
      "✅": { label: "已签到", tone: "ok" },
      "❌": { label: "签到失败", tone: "bad" },
      "🔄": { label: "正在签到", tone: "busy" },
    },
    day_off: null,
    ...over,
  };
}

describe("常量与月份口径", () => {
  it("星期表头周一起始，顺序即列序", () => {
    expect(WEEK).toEqual(["一", "二", "三", "四", "五", "六", "日"]);
  });

  it("固定 6 行 42 格，骨架格与空位格类名是唯一出处", () => {
    expect(ROWS).toBe(6);
    expect(CELLS).toBe(42);
    expect(SKELETON_CLS).toBe("sc-cell sc-cell--skeleton");
    expect(BLANK_CLS).toBe("sc-blank");
    expect(CAL_API).toBe("/api/my-calendar");
    expect(LOG_API).toBe("/api/my-logs");
  });

  it("月份标题/键/序号三处同口径", () => {
    expect(monthLabel(2026, 9)).toBe("2026年9月");
    expect(monthKey(2026, 9)).toBe("2026-09");
    expect(monthIndex(2026, 1)).toBe(2026 * 12 + 1);
  });

  it("换月跨年双向归位", () => {
    expect(shiftMonth({ year: 2026, month: 12 }, 1)).toEqual({ year: 2027, month: 1 });
    expect(shiftMonth({ year: 2026, month: 1 }, -1)).toEqual({ year: 2025, month: 12 });
    expect(shiftMonth({ year: 2026, month: 5 }, 0)).toEqual({ year: 2026, month: 5 });
  });

  it("方向取「本次月份 − 上次已渲染月份」的符号，未渲染过为 0", () => {
    expect(monthDirection("", 2026, 9)).toBe(0);
    expect(monthDirection("2026-09", 2026, 9)).toBe(0);
    expect(monthDirection("2026-09", 2026, 10)).toBe(1);
    expect(monthDirection("2026-09", 2026, 8)).toBe(-1);
    // 跨年：2025-12 → 2026-01 是往后一月，不能按字符串/月号比较
    expect(monthDirection("2025-12", 2026, 1)).toBe(1);
    expect(monthDirection("2026-01", 2025, 12)).toBe(-1);
    // 快速连点：月份已 +2 而上次渲染仍是起点，方向照旧累计为 +1
    expect(monthDirection("2026-09", 2026, 11)).toBe(1);
  });

  it("月份键非法时方向按未换月处理（不抛）", () => {
    expect(monthDirection("bogus", 2026, 9)).toBe(0);
  });
});

describe("周末门（与服务端同一份 .env 解析结果）", () => {
  // 2026-09-26 周六、2026-09-27 周日、2026-09-23 周三
  it("开关关闭时周末判停签，工作日不受影响", () => {
    expect(offDayOf("2026-09-26", { saturday: false, sunday: false })).toBe("六");
    expect(offDayOf("2026-09-27", { saturday: false, sunday: false })).toBe("日");
    expect(offDayOf("2026-09-23", { saturday: false, sunday: false })).toBe("");
  });

  it("开关开启后周末照常显示（不再置灰）", () => {
    expect(offDayOf("2026-09-26", { saturday: true, sunday: true })).toBe("");
    expect(offDayOf("2026-09-27", { saturday: true, sunday: true })).toBe("");
    // 两个开关互相独立：只开周六不影响周日
    expect(offDayOf("2026-09-26", { saturday: true, sunday: false })).toBe("");
    expect(offDayOf("2026-09-27", { saturday: true, sunday: false })).toBe("日");
  });

  it("停签日不查日志，直接给跳过说明（两种形态文案固定）", () => {
    expect(logSkipText("2026-09-26", { saturday: false, sunday: false })).toBe("周六无需签到");
    expect(logSkipText("2026-09-27", { saturday: false, sunday: false })).toBe("周日无需签到");
    expect(logSkipText("2026-09-23", { saturday: false, sunday: false })).toBe("");
    expect(logSkipText("2026-09-26", { saturday: true, sunday: true })).toBe("");
  });

  it("无日志空态带日期，与「还没选日期」的引导态区分", () => {
    expect(logEmptyText("2026-09-12")).toBe("2026-09-12 暂无签到记录");
  });
});

describe("月历格组合", () => {
  const flags = { saturday: true, sunday: true }; // 全周可签，隔离周末门的影响

  function build(over: Record<string, unknown> = {}) {
    const days: Record<string, Record<string, string>> = {};
    days["2026-09-12"] = { "13800138001": "✅" };
    days["2026-09-13"] = { "13800138001": "❌" };
    days["2026-09-14"] = { "13800138001": "🔄" };
    return gridCells({
      data: { days },
      phone: "13800138001",
      year: 2026,
      month: 9,
      selected: "",
      today: "2026-09-12",
      flags,
      ctx: ctxFixture(),
      ...over,
    });
  }

  it("恒为 42 格：前置空位 + 当月天数 + 尾部补齐（空格是 null）", () => {
    const { cells } = build();
    expect(cells.length).toBe(42);
    // 2026-09-01 是周二 → 前置 1 个空位（周一起始）
    expect(cells[0]).toBeNull();
    expect(cells[1]?.d).toBe(1);
    expect(cells[1]?.date).toBe("2026-09-01");
    expect(cells[30]?.d).toBe(30);
    expect(cells[31]).toBeNull();
    expect(cells[41]).toBeNull();
  });

  it("月初落在周日时前置 6 格（周一起始的边界）", () => {
    // 2026-02-01 是周日 → 前置 6 格
    const { cells } = gridCells({
      data: { days: {} },
      phone: "p",
      year: 2026,
      month: 2,
      selected: "",
      today: "",
      flags,
      ctx: ctxFixture(),
    });
    expect(cells[5]).toBeNull();
    expect(cells[6]?.d).toBe(1);
    expect(cells[6]?.date).toBe("2026-02-01");
    expect(cells.length).toBe(42);
  });

  it("状态符号反查语气档落到格底类名，符号本身不进界面", () => {
    const { cells } = build();
    // 前置 1 格后 cells[d] 即当月第 d 日
    expect(cells[12]?.cls).toContain("sc-cell--ok");
    expect(cells[13]?.cls).toContain("sc-cell--bad");
    expect(cells[14]?.cls).toContain("sc-cell--busy");
    // 无记录的日子走 --none，不给状态底色
    expect(cells[15]?.cls).toContain("sc-cell--none");
    for (const c of [cells[12], cells[13], cells[14], cells[15]]) {
      expect(c?.label).not.toContain("✅");
      expect(c?.label).not.toContain("❌");
      expect(c?.cls).not.toContain("sc-sym");
      expect(c?.cls).not.toContain("sc-dot");
    }
  });

  it("今天与选中是两条独立通道，标签都会说明", () => {
    const { cells } = build({ selected: "2026-09-14" });
    expect(cells[12]?.cls).toContain("sc-cell--today");
    expect(cells[12]?.label).toContain("今天");
    expect(cells[14]?.cls).toContain("is-selected");
    expect(cells[14]?.pressed).toBe(true);
    expect(cells[12]?.pressed).toBe(false);
    // 选中不改底色：状态底色与选中态同格共存
    expect(cells[14]?.cls).toContain("sc-cell--busy");
  });

  it("停签格走中性底 + 「休」角标，不叠状态底色，也不进图例统计", () => {
    const { cells, used } = build({
      flags: { saturday: false, sunday: false },
      today: "2026-09-12",
    });
    // 2026-09-12 是周六且未开周六签到 → 该格为停签格（状态符号被角标取代）
    expect(cells[12]?.cls).toContain("sc-cell--off");
    expect(cells[12]?.cls).not.toContain("sc-cell--ok");
    expect(cells[12]?.offBadge).toBe("六");
    expect(cells[12]?.label).toContain("（周六不签到）");
    expect(used).not.toContain("ok");
    // 周日（09-13）同样停签：角标是「日」
    expect(cells[13]?.offBadge).toBe("日");
    // 周三（09-16）无记录、非停签 → 仍是普通空格
    expect(cells[16]?.cls).toContain("sc-cell--none");
    // 周一（09-14，busy）照常计入图例；两个周末格的状态被「休」取代，不计入
    expect(used).toEqual(["busy"]);
  });

  it("图例收敛键只收真正显示出来的语气档", () => {
    const { used } = build();
    expect(used).toEqual(["ok", "bad", "busy"]);
  });

  it("接口缺 days / 缺该账号时全月为空格（不抛）", () => {
    for (const data of [{}, { days: {} }, { days: { "2026-09-12": {} } }]) {
      const { cells, used } = gridCells({
        data, phone: "p", year: 2026, month: 9, selected: "",
        today: "", flags, ctx: ctxFixture(),
      });
      expect(cells.length).toBe(42);
      expect(used).toEqual([]);
      expect(cells[0]).toBeNull();
    }
  });
});

describe("图例并集", () => {
  it("多卡并集驱动显隐，空输入安全", () => {
    const used = unionTones({ 0: ["ok", "warn"], 1: ["warn", "bad"], 2: [] });
    expect(Object.keys(used).sort()).toEqual(["bad", "ok", "warn"]);
    expect(unionTones({})).toEqual({});
    expect(unionTones()).toEqual({});
  });
});

describe("慢请求才播动效", () => {
  it("阈值 80ms：更短的请求不播（加载越短越不该动）", () => {
    expect(shouldAnimate(0)).toBe(false);
    expect(shouldAnimate(80)).toBe(false);
    expect(shouldAnimate(81)).toBe(true);
  });
});

describe("薄封装（口径与 Python 对拍测试的接缝）", () => {
  it("stateEntry 对未知符号/缺上下文返回 null，不抛", () => {
    expect(stateEntry("✅", ctxFixture())?.tone).toBe("ok");
    expect(stateEntry("🆕", ctxFixture())).toBeNull();
    expect(stateEntry("✅", undefined)).toBeNull();
    expect(stateEntry("✅", {} as CalendarCtx)).toBeNull();
  });

  it("dayCell 返回数据（类名/读屏名/角标），不返回 HTML 串", () => {
    const cell: CalendarCell = dayCell(
      { d: 12, date: "2026-09-12", state: "✅", off: false, offDay: "", isToday: false, selected: false },
      ctxFixture(),
    );
    expect(cell.cls).toBe("sc-cell sc-cell--ok");
    expect(cell.label).toBe("2026-09-12，已签到");
    expect(cell.offBadge).toBe("");
    expect(cell.pressed).toBe(false);
  });

  it("statusLine 走服务端状态表（急停/无点位不再冒充排队）", () => {
    const ctx = ctxFixture({
      by_code: {
        ...ctxFixture().by_code,
        global_paused: { symbol: "⛔", text: "全局暂停（急停）", tone: "warn" },
      },
    });
    expect(statusLine({ state_status: "global_paused", queue_ahead: 1 }, ctx).text).toContain("急停");
    expect(statusLine({ state_status: "global_paused", queue_ahead: 1 }, ctx).text).not.toContain("排队");
    // 未知码如实报告未知，不回落成"排队待签"
    const unknown = statusLine({ state_status: "wat", queue_ahead: 0 }, ctx);
    expect(unknown.text).not.toContain("排队");
    expect(unknown.text).toContain("wat");
  });
});
