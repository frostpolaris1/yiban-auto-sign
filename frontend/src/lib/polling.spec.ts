import { effectScope, ref } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePolling } from "./polling";

/**
 * 共享轮询机制的行为钉（退役账「手写轮询收编」）。
 *
 * 这些性质收编前在 4 个页面各写一遍、且**没有任何用例**：
 *   · 到点就跑；
 *   · 标签页隐藏时不跑，切回可见立刻补跑一次（收编前日志页缺这条补拉）；
 *   · `gate` 为假只跳过本次，不停表；
 *   · `active` 为假**停表**（冷却倒计时/首屏未就绪时不留空转定时器）；
 *   · scope 销毁后停表并摘掉 visibilitychange 监听（收编前少一处清理即静默泄漏）。
 */

function setVisibility(state: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
}

describe("usePolling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setVisibility("visible");
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("到点就跑，节拍由调用方给定", () => {
    const run = vi.fn();
    const scope = effectScope();
    scope.run(() => usePolling(run, { intervalMs: 10000 }));
    expect(run).not.toHaveBeenCalled();
    vi.advanceTimersByTime(30000);
    expect(run).toHaveBeenCalledTimes(3);
    scope.stop();
  });

  it("标签页隐藏时不跑；切回可见立刻补跑一次", () => {
    const run = vi.fn();
    const scope = effectScope();
    scope.run(() => usePolling(run, { intervalMs: 10000 }));
    setVisibility("hidden");
    vi.advanceTimersByTime(30000);
    expect(run, "隐藏期间不得轮询").not.toHaveBeenCalled();
    setVisibility("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(run, "切回可见必须补拉一次").toHaveBeenCalledTimes(1);
    scope.stop();
  });

  it("gate 为假只跳过本次，不停表", () => {
    let open = true;
    const run = vi.fn();
    const scope = effectScope();
    scope.run(() => usePolling(run, { intervalMs: 10000, gate: () => open }));
    vi.advanceTimersByTime(10000);
    expect(run).toHaveBeenCalledTimes(1);
    open = false;
    vi.advanceTimersByTime(20000);
    expect(run, "闸门为假期间不跑").toHaveBeenCalledTimes(1);
    open = true;
    vi.advanceTimersByTime(10000);
    expect(run, "闸门放开后照常跑（表没停）").toHaveBeenCalledTimes(2);
    scope.stop();
  });

  it("active 为假时停表（不留空转定时器）", () => {
    const on = ref(false);
    const run = vi.fn();
    const scope = effectScope();
    scope.run(() => usePolling(run, { intervalMs: 1000, active: () => on.value }));
    vi.advanceTimersByTime(5000);
    expect(run, "未激活不得起表").not.toHaveBeenCalled();
    on.value = true;
    vi.advanceTimersByTime(3000);
    expect(run).toHaveBeenCalledTimes(3);
    on.value = false;
    vi.advanceTimersByTime(5000);
    expect(run, "停表后不得再跑").toHaveBeenCalledTimes(3);
    scope.stop();
  });

  it("scope 销毁后停表并摘掉 visibilitychange 监听", () => {
    const run = vi.fn();
    const addSpy = vi.spyOn(document, "addEventListener");
    const removeSpy = vi.spyOn(document, "removeEventListener");
    const scope = effectScope();
    scope.run(() => usePolling(run, { intervalMs: 10000 }));
    const handler = addSpy.mock.calls.find(([type]) => type === "visibilitychange")?.[1];
    expect(handler, "必须监听 visibilitychange（补拉）").toBeTruthy();
    scope.stop();
    vi.advanceTimersByTime(30000);
    expect(run, "销毁后不得再跑").not.toHaveBeenCalled();
    expect(
      removeSpy.mock.calls.some(([type, fn]) => type === "visibilitychange" && fn === handler),
      "销毁必须摘掉 visibilitychange 监听",
    ).toBe(true);
  });
});
