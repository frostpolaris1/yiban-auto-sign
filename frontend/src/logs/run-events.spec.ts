import { describe, expect, it } from "vitest";
import {
  buildRunEventsQuery,
  emptyRoundsText,
  eventTime,
  findRound,
  formatDuration,
  outOfWindowHint,
  roundKey,
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
