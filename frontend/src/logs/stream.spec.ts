/**
 * SSE 进度流状态机单测（Vitest）：`frontend/src/logs/stream.ts`。
 *
 * 契约来源：`web/routes/stream_api.py` 的 5 类事件与 `yiban/store/run_events.snapshot`。
 * 只测纯函数状态机；EventSource 接线由 Playwright e2e 钉住。
 */
import { describe, expect, it } from "vitest";

import {
  RECENT_MAX,
  applyProgress,
  endReasonText,
  emptyStreamState,
  execRowNote,
  isTerminalEnd,
  recentText,
  stateFromSnapshot,
  streamStatusText,
  windowLine,
  type StreamProgress,
  type StreamSnapshot,
} from "./stream";

function snap(partial: Partial<StreamSnapshot> = {}): StreamSnapshot {
  return {
    day: "2026-10-10",
    window: { start_day: "2026-09-27", end_day: "2026-10-10", in_window: true, retention_days: 14 },
    totals: { claim: 3, start: 2, success: 1, fail: 1, pause: 0, finalize: 1 },
    by_executor: [
      {
        executor: "single",
        executor_label: "单执行体",
        claim: 3,
        start: 2,
        success: 1,
        fail: 1,
        pause: 0,
        finalize: 1,
        unexecuted: 1,
        first_ts: "2026-10-10 06:40:00",
        last_ts: "2026-10-10 06:40:21",
        duration_sec: 21,
      },
    ],
    node_order: ["claim", "start", "success", "fail", "pause", "finalize"],
    node_labels: {
      claim: "领取",
      start: "开始",
      success: "成功",
      fail: "失败",
      pause: "暂停",
      finalize: "收尾",
    },
    cursor: 8,
    generated_ts: "2026-10-10 06:41:00",
    truncated: false,
    ...partial,
  };
}

function ev(partial: Partial<StreamProgress> = {}): StreamProgress {
  return {
    id: 9,
    ts: "2026-10-10 06:41:00",
    day: "2026-10-10",
    node: "success",
    node_label: "成功",
    executor: "single",
    executor_label: "单执行体",
    message: "",
    ...partial,
  };
}

describe("stateFromSnapshot", () => {
  it("复制 totals 与 by_executor，最近事件清空", () => {
    const s = stateFromSnapshot(snap());
    expect(s.totals).toEqual({ claim: 3, start: 2, success: 1, fail: 1, pause: 0, finalize: 1 });
    expect(s.execs).toHaveLength(1);
    expect(s.execs[0].executor_label).toBe("单执行体");
    expect(s.recent).toEqual([]);
    // 不共享引用：改快照对象不影响状态
    expect(s.totals).not.toBe(snap().totals);
  });

  it("承载服务端下发的 day / window / node_order / node_labels（前端不另抄节点表）", () => {
    const s = stateFromSnapshot(snap());
    expect(s.day).toBe("2026-10-10");
    expect(s.window?.retention_days).toBe(14);
    expect(s.nodeOrder).toEqual(["claim", "start", "success", "fail", "pause", "finalize"]);
    expect(s.nodeLabels.success).toBe("成功");
    // 深拷贝：改状态不改快照
    s.nodeOrder.push("x");
    expect(snap().node_order).toHaveLength(6);
  });
});

describe("windowLine / isTerminalEnd", () => {
  it("无快照时窗口行回空", () => {
    expect(windowLine(emptyStreamState())).toBe("");
  });

  it("窗口行含业务日、保留窗口两端与窗口内外", () => {
    expect(windowLine(stateFromSnapshot(snap()))).toContain("2026-09-27 至 2026-10-10");
    expect(windowLine(stateFromSnapshot(snap()))).toContain("窗口内");
    const out = stateFromSnapshot(snap({ window: { start_day: "2026-09-27", end_day: "2026-09-30", in_window: false, retention_days: 14 } }));
    expect(windowLine(out)).toContain("不在保留窗口内");
  });

  it("终止与可重连的 end 原因分开", () => {
    expect(isTerminalEnd("session_expired")).toBe(true);
    expect(isTerminalEnd("disabled")).toBe(true);
    expect(isTerminalEnd("too_many_connections")).toBe(true);
    expect(isTerminalEnd("server_busy")).toBe(true);
    expect(isTerminalEnd("max_duration")).toBe(false);
    expect(isTerminalEnd("idle_timeout")).toBe(false);
  });

  it("applyProgress 保留快照承载的节点表与窗口", () => {
    const s = applyProgress(stateFromSnapshot(snap()), ev());
    expect(s.nodeOrder).toHaveLength(6);
    expect(s.day).toBe("2026-10-10");
  });
});

describe("applyProgress", () => {
  it("按事件行累加对应节点计数（不去重）", () => {
    const s0 = stateFromSnapshot(snap());
    const s1 = applyProgress(s0, ev({ node: "success" }));
    expect(s1.totals.success).toBe(2);
    expect(s0.totals.success).toBe(1); // 不可变：原状态不变
    const s2 = applyProgress(s1, ev({ node: "finalize", node_label: "收尾", id: 10 }));
    expect(s2.totals.finalize).toBe(2);
    expect(s2.totals.success).toBe(2);
  });

  it("finalize 只累加自己那一列，不并入 success/fail", () => {
    const s = applyProgress(stateFromSnapshot(snap()), ev({ node: "finalize" }));
    expect(s.totals.finalize).toBe(2);
    expect(s.totals.success).toBe(1);
    expect(s.totals.fail).toBe(1);
  });

  it("执行体行不存在时新建，并记录最近时刻", () => {
    const s = applyProgress(
      stateFromSnapshot(snap()),
      ev({ node: "start", executor: "worker-2", executor_label: "并行执行体 #2", ts: "2026-10-10 06:45:00" }),
    );
    const row = s.execs.find((r) => r.executor === "worker-2");
    expect(row).toBeDefined();
    expect(row?.start).toBe(1);
    expect(row?.last_ts).toBe("2026-10-10 06:45:00");
    // 活跃执行体排到最前（按 last_ts 降序）
    expect(s.execs[0].executor).toBe("worker-2");
  });

  it("最近事件带封顶 RECENT_MAX，最新在前", () => {
    let s = stateFromSnapshot(snap());
    for (let i = 0; i < RECENT_MAX + 5; i++) {
      s = applyProgress(s, ev({ id: 100 + i, ts: `2026-10-10 06:4${i}:00` }));
    }
    expect(s.recent).toHaveLength(RECENT_MAX);
    expect(s.recent[0].id).toBe(100 + RECENT_MAX + 4);
  });
});

describe("文案", () => {
  it("连接状态文案", () => {
    expect(streamStatusText("open")).toContain("流已连接");
    expect(streamStatusText("connecting")).toContain("正在重连");
    expect(streamStatusText("closed", "too_many_connections")).toContain("连接数超限");
  });

  it("end 原因映射（未知原因原样回）", () => {
    expect(endReasonText("disabled")).toContain("关闭");
    expect(endReasonText("自己编的")).toBe("自己编的");
  });

  it("最近事件带只含节点与执行体（公开标签），不含账号", () => {
    const text = recentText([
      ev({ ts: "2026-10-10 06:41:23", executor: "worker-0", executor_label: "并行执行体 #0", node_label: "成功" }),
      ev({ ts: "2026-10-10 06:41:22", executor: "worker-1", executor_label: "并行执行体 #1", node_label: "开始" }),
    ]);
    expect(text).toContain("06:41:23");
    expect(text).toContain("成功");
    expect(text).toContain("worker-0");
  });

  it("空状态文案", () => {
    expect(recentText([])).toBe("—");
    expect(streamStatusText("closed")).toBe("— 已关闭");
  });

  it("执行体角标是派生标签，不新造事件", () => {
    const base = stateFromSnapshot(snap()).execs[0];
    expect(execRowNote(base)).toBe("有活动");
    expect(execRowNote({ ...base, executor: "fallback", claim: 0 })).toBe("已让位/静默");
  });
});

describe("emptyStreamState", () => {
  it("六节点全 0", () => {
    expect(emptyStreamState().totals).toEqual({
      claim: 0,
      start: 0,
      success: 0,
      fail: 0,
      pause: 0,
      finalize: 0,
    });
  });
});
