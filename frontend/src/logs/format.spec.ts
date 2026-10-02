import { describe, expect, it } from "vitest";
import {
  buildExportUrl,
  buildLogsQuery,
  eventCountText,
  eventMessage,
  infoText,
  paginate,
  shouldPoll,
  sortEvents,
  statusLabel,
  type LogEvent,
} from "./format";

function ev(over: Partial<LogEvent>): LogEvent {
  return { time: "08:00:00", phone: "138****8000", status: "success", message: "m", ...over };
}

describe("infoText（三分支：检索态 / 截断态 / 总数）", () => {
  it("无检索无截断时只报总数", () => {
    expect(infoText({ truncated: false, returned: 12, total_lines: 12, dropped_lines: 0, q: "" })).toBe("共 12 行");
  });

  it("截断时报「已截断：显示 X / 共 Y 行」", () => {
    expect(infoText({ truncated: true, returned: 80, total_lines: 500, dropped_lines: 0, q: "" })).toBe(
      "已截断：显示 80 / 共 500 行",
    );
  });

  it("**检索态**报「X 行匹配 / 共 Y 行」（判据取服务端回显的 q）", () => {
    expect(infoText({ truncated: false, returned: 1, total_lines: 1, dropped_lines: 0, q: "签到" })).toBe(
      "1 行匹配 / 共 1 行",
    );
    // 检索态下截断标志不再参与（匹配数即本页全部匹配）
    expect(infoText({ truncated: true, returned: 3, total_lines: 3, dropped_lines: 0, q: "k" })).toBe(
      "3 行匹配 / 共 3 行",
    );
  });

  it("有被丢弃的行时追加提示（dropped_lines 是切行模型丢掉的，不是截断）", () => {
    expect(infoText({ truncated: true, returned: 80, total_lines: 500, dropped_lines: 3, q: "" })).toBe(
      "已截断：显示 80 / 共 500 行（另有 3 行未计入）",
    );
  });
});

describe("buildLogsQuery", () => {
  it("date 必带；q 与 all 仅在生效时下发", () => {
    expect(buildLogsQuery("2026-10-03", "", false)).toBe("/api/logs?date=2026-10-03");
    expect(buildLogsQuery("2026-10-03", "  timeout  ", false)).toBe("/api/logs?date=2026-10-03&q=timeout");
    expect(buildLogsQuery("2026-10-03", "", true)).toBe("/api/logs?date=2026-10-03&all=1");
  });

  it("date 为空时不下发该键（由服务端解析最近有日志的一天）", () => {
    expect(buildLogsQuery("", "", false)).toBe("/api/logs");
  });
});

describe("buildExportUrl", () => {
  it("日期进 query（文件名仅含日期，不带路径）", () => {
    expect(buildExportUrl("2026-10-03")).toBe("/api/logs/export?date=2026-10-03");
  });
});

describe("shouldPoll（与 legacy pollTick 逐条一致）", () => {
  const base = {
    visibility: "visible" as DocumentVisibilityState,
    autoRefresh: true,
    following: true,
    busy: false,
    overlayOpen: false,
  };

  it("五个条件全满足才轮询", () => {
    expect(shouldPoll(base)).toBe(true);
  });

  it("任一条件不满足即不轮询", () => {
    expect(shouldPoll({ ...base, visibility: "hidden" })).toBe(false);
    expect(shouldPoll({ ...base, autoRefresh: false })).toBe(false);
    expect(shouldPoll({ ...base, busy: true })).toBe(false);
    expect(shouldPoll({ ...base, overlayOpen: true })).toBe(false);
  });

  it("pin 住某一天（非跟随）时不轮询——即使那天就是今天", () => {
    expect(shouldPoll({ ...base, following: false })).toBe(false);
  });
});

describe("sortEvents", () => {
  const rows = [
    ev({ time: "09:00:00", status: "failed", attempt: 2 }),
    ev({ time: "07:30:00", status: "success", attempt: 1 }),
    ev({ time: "08:15:00", status: "skipped_window", attempt: 1 }),
  ];

  it("按 time 文本排序（HH:MM:SS 字典序即时间序）", () => {
    expect(sortEvents(rows, "time", "ascending").map((r) => r.time)).toEqual(["07:30:00", "08:15:00", "09:00:00"]);
    expect(sortEvents(rows, "time", "descending").map((r) => r.time)).toEqual(["09:00:00", "08:15:00", "07:30:00"]);
  });

  it("attempt 按数值排序（不是字符串）", () => {
    const r = sortEvents([ev({ attempt: 10, time: "01:00:00" }), ev({ attempt: 2, time: "02:00:00" })], "attempt", "ascending");
    expect(r.map((x) => x.attempt)).toEqual([2, 10]);
  });

  it("不改原数组（返回副本）", () => {
    const before = rows.map((r) => r.time);
    sortEvents(rows, "time", "descending");
    expect(rows.map((r) => r.time)).toEqual(before);
  });
});

describe("paginate", () => {
  const rows = Array.from({ length: 25 }, (_, i) => i);
  it("按页切片，页码下界为 1", () => {
    expect(paginate(rows, 1, 20)).toEqual(rows.slice(0, 20));
    expect(paginate(rows, 2, 20)).toEqual(rows.slice(20));
    expect(paginate(rows, 0, 20)).toEqual(rows.slice(0, 20));
    expect(paginate(rows, 3, 20)).toEqual([]);
  });
});

describe("statusLabel（中文标签 + 色调；原始码保留）", () => {
  it("签到状态全覆盖", () => {
    expect(statusLabel("sign", "success")).toEqual({ label: "成功", tone: "ok", raw: "success" });
    expect(statusLabel("sign", "already")).toEqual({ label: "已签到", tone: "ok", raw: "already" });
    expect(statusLabel("sign", "no_task")).toEqual({ label: "无需签到", tone: "muted", raw: "no_task" });
    expect(statusLabel("sign", "failed")).toEqual({ label: "失败", tone: "bad", raw: "failed" });
    expect(statusLabel("sign", "retrying")).toEqual({ label: "重试中", tone: "warn", raw: "retrying" });
    expect(statusLabel("sign", "pending")).toEqual({ label: "待签", tone: "info", raw: "pending" });
    expect(statusLabel("sign", "skipped_window")).toEqual({ label: "超出时段", tone: "warn", raw: "skipped_window" });
    expect(statusLabel("sign", "skipped_norange")).toEqual({ label: "不在范围", tone: "warn", raw: "skipped_norange" });
    expect(statusLabel("sign", "paused")).toEqual({ label: "已暂停", tone: "muted", raw: "paused" });
    expect(statusLabel("sign", "user_cancelled")).toEqual({ label: "已取消", tone: "muted", raw: "user_cancelled" });
  });

  it("未知签到码回落**原始码** + muted（信息不丢，不假装认识）", () => {
    expect(statusLabel("sign", "brand_new_code")).toEqual({ label: "brand_new_code", tone: "muted", raw: "brand_new_code" });
    expect(statusLabel("sign", "")).toEqual({ label: "未知", tone: "muted", raw: "" });
  });

  it("探针只有 异常/正常 两态（非 failed 一律正常），原始码保留", () => {
    expect(statusLabel("probe", "failed")).toEqual({ label: "异常", tone: "bad", raw: "failed" });
    expect(statusLabel("probe", "ok")).toEqual({ label: "正常", tone: "ok", raw: "ok" });
    expect(statusLabel("probe", "some_future_state")).toEqual({ label: "正常", tone: "ok", raw: "some_future_state" });
  });
});

describe("eventMessage（尝试号只在消息里没有它时补）", () => {
  it("签到事件且 attempt>1：追加「（第 N 次）」", () => {
    expect(eventMessage(ev({ status: "failed", attempt: 3, message: "密码错误" }), true)).toBe("密码错误（第 3 次）");
  });

  it("retrying 不追加（引擎已在消息里带号，否则出现双份）", () => {
    expect(eventMessage(ev({ status: "retrying", attempt: 3, message: "待重试（已 2 次）: x" }), true)).toBe(
      "待重试（已 2 次）: x",
    );
  });

  it("attempt<=1 不追加；探针事件从不追加", () => {
    expect(eventMessage(ev({ status: "failed", attempt: 1, message: "m" }), true)).toBe("m");
    expect(eventMessage(ev({ status: "failed", attempt: 5, message: "m" }), false)).toBe("m");
  });

  it("空消息但有尝试号时只给号", () => {
    expect(eventMessage(ev({ status: "failed", attempt: 2, message: "" }), true)).toBe("（第 2 次）");
  });
});

describe("eventCountText", () => {
  it("条数文案", () => {
    expect(eventCountText(0)).toBe("共 0 条");
    expect(eventCountText(7)).toBe("共 7 条");
  });
});
