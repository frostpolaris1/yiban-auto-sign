import { describe, expect, it, vi } from "vitest";
import { createVerifyJob, type VerifyJobScheduler } from "./useVerifyJob";
import { VERIFY_MAX_POLLS, VERIFY_POLL_MS } from "./verify";

/** 手动推进的假调度器：记录每个待触发任务与其延时，测试里显式触发。 */
function fakeScheduler() {
  let seq = 0;
  const queued = new Map<number, { fn: () => void; ms: number }>();
  const sched: VerifyJobScheduler = {
    schedule: (fn, ms) => {
      const h = ++seq;
      queued.set(h, { fn, ms });
      return h;
    },
    cancel: (h) => {
      queued.delete(h);
    },
  };
  return {
    sched,
    queued,
    delays: () => [...queued.values()].map((v) => v.ms),
    /** 触发当前所有已排队的定时任务（清空后再跑，模拟"到点了"） */
    fire(): void {
      const items = [...queued.entries()];
      queued.clear();
      for (const [, v] of items) v.fn();
    },
    count: () => queued.size,
  };
}

/** 冲掉微任务队列（脚本化 api 返回的是已 resolve 的 promise）。 */
async function flush(): Promise<void> {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
}

/** 按脚本依次返回响应的假 api；记录调用以便断言。 */
function scriptedApi(responses: Array<unknown | { reject: unknown }>) {
  const calls: Array<{ method: string; path: string }> = [];
  let i = 0;
  const api = vi.fn(async (method: string, path: string) => {
    calls.push({ method, path });
    const r = responses[Math.min(i, responses.length - 1)];
    i += 1;
    if (r && typeof r === "object" && "reject" in (r as object)) throw (r as { reject: unknown }).reject;
    return r;
  });
  return { api, calls };
}

async function make(responses: Array<unknown | { reject: unknown }>) {
  const s = fakeScheduler();
  const { api, calls } = scriptedApi(responses);
  const finishes: Array<{ status: string; error: string; phone: string }> = [];
  const ctl = createVerifyJob({ api, scheduler: s.sched, onFinish: (r) => finishes.push(r) });
  return { ctl, s, calls, finishes, api };
}

describe("初始与进度态", () => {
  it("start 立即显示查询中文案并马上发一次请求", async () => {
    const { ctl, calls } = await make([{ job: { status: "pending" } }]);
    ctl.start("job-1");
    expect(ctl.state.value).toEqual({ visible: true, text: "正在查询校验状态…", bad: false, cancellable: false });
    await flush();
    expect(calls).toEqual([{ method: "GET", path: "/api/verify-jobs/job-1" }]);
  });

  it("空 jobId 不启动（提交未排校验时静默）", async () => {
    const { ctl, calls } = await make([{}]);
    ctl.start("");
    await flush();
    expect(calls).toHaveLength(0);
    expect(ctl.state.value.visible).toBe(false);
  });

  it("pending 可取消、running 不可；延时按退避序列", async () => {
    const { ctl, s } = await make([
      { job: { status: "pending" } },
      { job: { status: "running" } },
    ]);
    ctl.start("j");
    await flush();
    expect(ctl.state.value).toMatchObject({ text: "已排上在线校验，等待执行…", cancellable: true });
    expect(s.delays()).toEqual([VERIFY_POLL_MS[1]]);

    s.fire();
    await flush();
    expect(ctl.state.value).toMatchObject({ text: "正在在线校验账号信息…", cancellable: false });
  });

  it("任务 id 进 URL 前做编码（防路径注入）", async () => {
    const { ctl, calls } = await make([{ job: { status: "done" } }]);
    ctl.start("a/b c");
    await flush();
    expect(calls[0].path).toBe("/api/verify-jobs/a%2Fb%20c");
  });
});

describe("终态：立即停止轮询并回调", () => {
  it("done 带手机号", async () => {
    const { ctl, s, finishes } = await make([{ job: { status: "done", phone: "13800138001" } }]);
    ctl.start("j");
    await flush();
    expect(ctl.state.value.text).toBe("在线校验通过（13800138001）。等待管理员审核后即可自动签到。");
    expect(ctl.state.value.bad).toBe(false);
    expect(s.queued.size).toBe(0);
    expect(finishes).toEqual([{ status: "done", error: "", phone: "13800138001" }]);
  });

  it("rejected 无原因时不编造原因，且标为告警态", async () => {
    const { ctl } = await make([{ job: { status: "rejected" } }]);
    ctl.start("j");
    await flush();
    expect(ctl.state.value.text).toBe("在线校验未通过。可修正账号信息后重新提交。");
    expect(ctl.state.value.bad).toBe(true);
  });

  it("cancelled 终态文案", async () => {
    const { ctl } = await make([{ job: { status: "cancelled" } }]);
    ctl.start("j");
    await flush();
    expect(ctl.state.value.text).toBe("已取消本次在线校验。账号仍已提交，管理员审核时可手动核对。");
  });
});

describe("诚实性：轮询上界与失败即停", () => {
  it("到上界后停止轮询并交还用户（不无限转圈）", async () => {
    const { ctl, s } = await make([{ job: { status: "running" } }]); // 永远 running
    ctl.start("j");
    for (let i = 0; i < VERIFY_MAX_POLLS + 5; i += 1) {
      await flush();
      s.fire();
    }
    await flush();
    expect(ctl.state.value.text).toBe("在线校验仍在进行，可稍后刷新本页查看结果。");
    expect(ctl.state.value.cancellable).toBe(false);
    expect(s.queued.size).toBe(0); // 不再排队
    expect(ctl.state.value.visible).toBe(true); // 文案仍在，不是消失
  });

  it("查询失败按状态码如实说明并停止", async () => {
    for (const [status, expected] of [
      [404, "校验任务已不存在或已过期"],
      [403, "无权查看该校验任务"],
      [500, "校验状态查询失败，请稍后刷新查看"],
    ] as Array<[number, string]>) {
      const err = new Error("boom") as Error & { status: number };
      err.status = status;
      const { ctl, s } = await make([{ reject: err }]);
      ctl.start("j");
      await flush();
      expect(ctl.state.value.text).toBe(expected);
      expect(ctl.state.value.bad).toBe(true);
      expect(s.queued.size).toBe(0);
    }
  });
});

describe("取消：仅 pending；409 改查真实状态；失败不谎报", () => {
  it("pending 时取消成功走终态", async () => {
    const { ctl, calls, finishes } = await make([
      { job: { status: "pending" } },
      { job: { status: "cancelled" } },
    ]);
    ctl.start("j");
    await flush();
    ctl.cancel();
    await flush();
    expect(calls[1]).toEqual({ method: "DELETE", path: "/api/verify-jobs/j" });
    expect(ctl.state.value.text).toContain("已取消本次在线校验");
    expect(finishes[0].status).toBe("cancelled");
  });

  it("非 pending（running）时点取消应被忽略", async () => {
    const { ctl, calls } = await make([{ job: { status: "running" } }]);
    ctl.start("j");
    await flush();
    ctl.cancel();
    await flush();
    expect(calls.filter((c) => c.method === "DELETE")).toHaveLength(0);
  });

  it("取消失败 409 → 改查一次真实状态（不停在'可取消'）", async () => {
    const conflict = new Error("已被后台抢走") as Error & { status: number };
    conflict.status = 409;
    const { ctl, calls } = await make([
      { job: { status: "pending" } },
      { reject: conflict },
      { job: { status: "done" } },
    ]);
    ctl.start("j");
    await flush();
    ctl.cancel();
    await flush();
    expect(calls.map((c) => c.method)).toEqual(["GET", "DELETE", "GET"]);
    expect(ctl.state.value.text).toContain("在线校验通过");
  });

  it("取消失败（其它错误）→ 显示失败原因且仍可重试", async () => {
    const boom = new Error("网络错误") as Error & { status: number };
    boom.status = 500;
    const { ctl } = await make([{ job: { status: "pending" } }, { reject: boom }]);
    ctl.start("j");
    await flush();
    ctl.cancel();
    await flush();
    expect(ctl.state.value.text).toBe("取消失败：网络错误");
    expect(ctl.state.value.bad).toBe(true);
    expect(ctl.state.value.cancellable).toBe(true); // 仍 pending，可再试
  });
});

describe("并发与生命周期", () => {
  it("重复 start 会收掉上一个任务（同一时刻只跟一个）", async () => {
    const { ctl, s } = await make([{ job: { status: "running" } }]);
    ctl.start("j1");
    await flush();
    expect(s.queued.size).toBe(1);
    ctl.start("j2");
    await flush();
    // 旧任务已停：其定时器被取消，只剩新任务的一个
    expect(s.queued.size).toBe(1);
    expect(ctl.state.value.text).toBe("正在在线校验账号信息…");
  });

  it("stop 之后不再改状态（陈旧的响应被丢弃）", async () => {
    let resolveLater: ((v: unknown) => void) | null = null;
    const api = vi.fn(
      () =>
        new Promise<unknown>((res) => {
          resolveLater = res;
        }),
    );
    const s = fakeScheduler();
    const ctl = createVerifyJob({ api, scheduler: s.sched });
    ctl.start("j");
    const before = ctl.state.value;
    ctl.stop();
    resolveLater?.({ job: { status: "done" } });
    await flush();
    expect(ctl.state.value).toEqual(before); // 未因晚到的响应改写
  });
});
