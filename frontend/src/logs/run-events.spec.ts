import { describe, expect, it } from "vitest";
import {
  buildRunEventsQuery,
  emptyRoundsText,
  eventTime,
  findRound,
  formatDuration,
  needsExecutorFallback,
  nextSelectedKey,
  outOfWindowHint,
  roundKey,
  roundsTruncatedText,
  windowText,
  type RunEventsWindow,
  type RunRound,
} from "./run-events";

const WIN: RunEventsWindow = {
  start_day: "2026-09-25",
  end_day: "2026-10-08",
  day: "2026-10-08",
  in_window: true,
  min_day: "2026-09-26",
  max_day: "2026-10-08",
  has_data: true,
};

function round(over: Partial<RunRound> = {}): RunRound {
  return {
    day: "2026-10-08",
    executor: "single",
    executor_label: "单执行体",
    claim: 3,
    start: 2,
    success: 1,
    fail: 1,
    pause: 0,
    unexecuted: 1,
    duration_sec: 21,
    first_ts: "2026-10-08 06:40:00",
    last_ts: "2026-10-08 06:40:21",
    ...over,
  };
}

describe("buildRunEventsQuery", () => {
  it("day 与 executor 仅在给定时下发（缺省由服务端取最新）", () => {
    expect(buildRunEventsQuery("", "")).toBe("/api/admin/run-events");
    expect(buildRunEventsQuery("2026-10-08", "")).toBe("/api/admin/run-events?day=2026-10-08");
    expect(buildRunEventsQuery("2026-10-08", "worker-2")).toBe(
      "/api/admin/run-events?day=2026-10-08&executor=worker-2",
    );
  });
});

describe("roundKey / findRound", () => {
  it("同一业务日同一执行体即同一轮", () => {
    expect(roundKey(round())).toBe("2026-10-08|single");
    expect(findRound([round(), round({ executor: "worker-2" })], "2026-10-08|worker-2")?.executor).toBe("worker-2");
  });

  it("找不到回 null（页面据此回落首轮，不崩）", () => {
    expect(findRound([round()], "2026-10-07|single")).toBeNull();
    expect(findRound([], "x")).toBeNull();
  });
});

describe("nextSelectedKey（轮询沿用在途选择）", () => {
  const newest = round(); // 2026-10-08|single（最新）
  const older = round({ executor: "worker-2", last_ts: "2026-10-08 06:20:00" });

  it("保留在途选择：当前选中仍在新数据里就留（10 秒轮询不得改回最新）", () => {
    expect(nextSelectedKey([newest, older], "2026-10-08", "", "2026-10-08|worker-2", true)).toBe(
      "2026-10-08|worker-2",
    );
  });

  it("保留态下选中项不在新数据里，才回落该日最新一轮", () => {
    expect(nextSelectedKey([newest, older], "2026-10-08", "", "2026-10-07|x", true)).toBe(
      "2026-10-08|single",
    );
    expect(nextSelectedKey([], "2026-10-08", "", "2026-10-08|single", true)).toBe("");
  });

  it("导航态：显式指定执行体就选它，否则选该日最新一轮", () => {
    expect(nextSelectedKey([newest, older], "2026-10-08", "worker-2", "", false)).toBe(
      "2026-10-08|worker-2",
    );
    expect(nextSelectedKey([newest, older], "2026-10-08", "", "", false)).toBe("2026-10-08|single");
    expect(nextSelectedKey([], "2026-10-08", "", "", false)).toBe("");
  });
});

describe("needsExecutorFallback（R2：选中态与时间线同源）", () => {
  const newest = round(); // 2026-10-08|single

  it("不带 executor 时不重取（缺省口径本就同源）", () => {
    expect(needsExecutorFallback([newest], "2026-10-08", "")).toBe(false);
    expect(needsExecutorFallback([], "2026-10-08", "")).toBe(false);
  });

  it("响应含该执行体分组时不重取", () => {
    expect(
      needsExecutorFallback([newest, round({ executor: "worker-2" })], "2026-10-08", "worker-2"),
    ).toBe(false);
  });

  it("带了 executor 但响应不含该分组时重取一次（含 rounds 被截断/当日无该执行体）", () => {
    expect(needsExecutorFallback([newest], "2026-10-08", "worker-9")).toBe(true);
    expect(needsExecutorFallback([], "2026-10-08", "worker-9")).toBe(true);
  });

  it("键含业务日：别的业务日有同名执行体不算命中", () => {
    expect(needsExecutorFallback([newest], "2026-10-07", "single")).toBe(true);
  });
});

describe("formatDuration", () => {
  it("null / undefined 回 --（时刻解析不出时不假装是 0 秒）", () => {
    expect(formatDuration(null)).toBe("--");
    expect(formatDuration(undefined)).toBe("--");
  });

  it("小于一分钟给秒，否则给分与秒", () => {
    expect(formatDuration(0)).toBe("0 秒");
    expect(formatDuration(21)).toBe("21 秒");
    expect(formatDuration(59)).toBe("59 秒");
    expect(formatDuration(60)).toBe("1 分");
    expect(formatDuration(192)).toBe("3 分 12 秒");
  });
});

describe("eventTime", () => {
  it("从完整时刻里取时分秒", () => {
    expect(eventTime("2026-10-08 06:40:21")).toBe("06:40:21");
  });

  it("形状不符时原样回，空值给占位", () => {
    expect(eventTime("06:40:21")).toBe("06:40:21");
    expect(eventTime("")).toBe("--:--:--");
  });
});

describe("可见窗口文案", () => {
  it("写明保留期与库内实际边界", () => {
    expect(windowText(WIN, 14)).toBe(
      "进度事件只保留最近 14 天（2026-09-25 ~ 2026-10-08）；库内实际数据 2026-09-26 ~ 2026-10-08",
    );
  });

  it("库内无数据时如实说无数据（不写一个假的边界）", () => {
    const empty = { ...WIN, min_day: null, max_day: null, has_data: false };
    expect(windowText(empty, 14)).toContain("库内暂无数据");
  });
});

describe("窗口外指引与空态", () => {
  it("窗口内不给指引", () => {
    expect(outOfWindowHint(WIN)).toBe("");
    expect(emptyRoundsText(WIN)).toBe("2026-10-08 没有运行进度记录（该日在保留窗口内）。");
  });

  it("窗口外给「跨月回溯请走审计日志页」的指引，空态也说清原因", () => {
    const out: RunEventsWindow = { ...WIN, day: "2026-08-01", in_window: false, has_data: false };
    expect(outOfWindowHint(out)).toContain("审计日志");
    expect(emptyRoundsText(out)).toContain("超出");
  });
});

describe("轮级摘要截断文案（F1：不许静默丢轮）", () => {
  it("未截断时不给文案", () => {
    expect(roundsTruncatedText({ rounds_truncated: false, rounds_limit: 200 })).toBe("");
  });

  it("截断时写明已截断与该日上限", () => {
    expect(roundsTruncatedText({ rounds_truncated: true, rounds_limit: 200 })).toContain("已截断");
    expect(roundsTruncatedText({ rounds_truncated: true, rounds_limit: 200 })).toContain("200");
  });
});
