import { describe, expect, it } from "vitest";
import {
  ageText,
  cardName,
  execHealthQuery,
  healthSummary,
  heartbeatText,
  stateText,
  stateTone,
  throughputText,
  unconfiguredNote,
  type ExecHealthCard,
  type ExecState,
} from "./exec-health";

function card(over: Partial<ExecHealthCard> = {}): ExecHealthCard {
  return {
    identity: "worker-0",
    identity_source: "heartbeat_file",
    heartbeat_file: "worker-alive-0.json",
    role: "worker",
    index: 0,
    label: "并行执行体 #1",
    name: null,
    configured: true,
    state: "running",
    heartbeat_at: "2026-10-13 06:40:00",
    age_sec: 12,
    fresh: true,
    last_seen_at: "2026-10-13 06:40:00",
    throughput: { done: 12, claimed: 1, failed: 0, total: 13, window_sec: 3600, per_min: 0.2 },
    ...over,
  };
}

describe("execHealthQuery", () => {
  it("无查询参数（端点只回全量卡片）", () => {
    expect(execHealthQuery()).toBe("/api/admin/executor-health");
  });
});

describe("卡片显示名与名册归属", () => {
  it("用户设的行名优先，否则用后端标签", () => {
    expect(cardName(card())).toBe("并行执行体 #1");
    expect(cardName(card({ name: "机房 A 出口" }))).toBe("机房 A 出口");
  });

  it("清单外的执行体必须标出来", () => {
    expect(unconfiguredNote(card({ configured: false }))).toBe("清单外");
    expect(unconfiguredNote(card())).toBe("");
  });
});

describe("四态", () => {
  const states: ExecState[] = ["running", "stale", "idle", "finished"];

  it("四态互斥且各有独立标签与色调", () => {
    const labels = states.map(stateText);
    expect(new Set(labels).size).toBe(4);
    const tones = states.map(stateTone);
    expect(stateTone("running")).toBe("ok");
    expect(stateTone("stale")).toBe("bad");
    expect(tones.filter((t) => t === "ok")).toHaveLength(1);
    expect(tones.filter((t) => t === "bad")).toHaveLength(1);
  });
});

describe("heartbeatText：过期与无记录都显式写「无心跳」", () => {
  it("运行时给时刻与年龄", () => {
    expect(heartbeatText(card())).toContain("心跳");
    expect(heartbeatText(card())).toContain("12 秒前");
  });

  it("过期：出现「无心跳」并带上最后心跳时刻", () => {
    const text = heartbeatText(card({
      state: "stale", heartbeat_at: "2026-10-13 06:30:00", age_sec: 600, fresh: false,
    }));
    expect(text).toContain("无心跳");
    expect(text).toContain("06:30:00");
  });

  it("无记录：出现「无心跳」，不留白、不显示 0", () => {
    const text = heartbeatText(card({
      state: "idle", heartbeat_at: null, age_sec: null, fresh: null, last_seen_at: null,
    }));
    expect(text).toBe("无心跳");
    expect(text).not.toContain("0 秒前");
  });

  it("收尾态照给收尾时刻", () => {
    expect(heartbeatText(card({ state: "finished" }))).toContain("已收尾");
  });

  it("任一状态的文案都非空（不留白）", () => {
    for (const state of ["running", "stale", "idle", "finished"] as ExecState[]) {
      expect(heartbeatText(card({ state })).length).toBeGreaterThan(0);
    }
  });
});

describe("throughputText：分子/分母/换算值一起给出", () => {
  it("窗口长度参与文案", () => {
    expect(throughputText(card())).toBe("完成 12 个 ÷ 60 分 = 0.20 个/分");
  });

  it("窗口为 0 时不编 0，写「窗口不可用」", () => {
    const text = throughputText(card({
      throughput: { done: 0, claimed: 0, failed: 0, total: 0, window_sec: 0, per_min: null },
    }));
    expect(text).toContain("窗口不可用");
    expect(text).toContain("--");
  });
});

describe("ageText / healthSummary", () => {
  it("年龄分档", () => {
    expect(ageText(12)).toBe("12 秒前");
    expect(ageText(600)).toBe("10 分钟前");
    expect(ageText(7200)).toBe("2 小时前");
    expect(ageText(null)).toBe("");
    expect(ageText(-5)).toBe("0 秒前");
  });

  it("概览各态计数，空列表也说得出", () => {
    const list = [
      card(), card({ identity: "worker-1" }),
      card({ identity: "fallback", state: "stale" }),
      card({ identity: "single", state: "idle" }),
    ];
    expect(healthSummary(list)).toBe("共 4 个执行体：运行 2 · 可疑中断 1 · 未运行 1 · 已跑完 0");
    expect(healthSummary([])).toContain("没有执行体");
  });
});
