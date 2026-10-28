import { onScopeDispose, watch } from "vue";

/**
 * 数据轮询的**唯一实现**（退役账「手写轮询收编」）。
 *
 * 收编前同一套形状在 4 个页面各写一遍（账号列表 / 用户列表 / 总览 KPI / 日志页），
 * 每份都是「`setInterval` ＋ `visibilitychange` 补拉 ＋ 闸门谓词 ＋ 卸载清理」，
 * 其中账号页与用户页近乎逐字相同。形状各写一遍的代价不是行数，而是**语义漂移**：
 * 少一处 visibilitychange 补拉、少一处卸载清理，都是静默的（实测日志页就少了补拉）。
 *
 * 分工（这是本模块存在的意义）：
 * · **机制**归本模块：节拍、可见性、卸载清理、回到可见时补跑一次；
 * · **策略**留在各页：闸门谓词（弹窗打开 / 在途请求 / 未跟随 / 自动刷新关闭）由调用方给。
 *
 * 节拍来源：一律 `pollMs()`（core.js 的 `POLL_MS`，全站唯一一份节拍定义）。
 * 页面**不得**再写数字节拍 —— 同一件事两个数就是两处漂移（见
 * `tests/test_review_refresh_cadence_single_source.py`）。
 */
export interface PollingOptions {
  /** 节拍（毫秒）。数据轮询一律传 `pollMs()`。 */
  intervalMs: number;
  /** 附加闸门：返回 false 时**本次不跑**（表继续走）。省略＝每次都跑。 */
  gate?: () => boolean;
  /** 只在这些时候计时：返回 false 时**停表**（不是跳过）。省略＝一直计时。 */
  active?: () => boolean;
  /** 标签页回到可见时立刻补跑一次（默认 true）。 */
  runOnVisible?: boolean;
}

/** 起一个轮询；组件卸载（或 effect scope 销毁）时自动停表并摘监听。 */
export function usePolling(run: () => void, opts: PollingOptions): void {
  let timer: ReturnType<typeof setInterval> | null = null;

  const visible = (): boolean => document.visibilityState === "visible";
  const allowed = (): boolean => visible() && (opts.gate ? opts.gate() : true);
  const tick = (): void => {
    if (!allowed()) return;
    run();
  };
  const start = (): void => {
    if (timer !== null) return;
    timer = setInterval(tick, opts.intervalMs);
  };
  const stop = (): void => {
    if (timer === null) return;
    clearInterval(timer);
    timer = null;
  };

  if (opts.active) {
    // flush:"sync"：起/停表是**机制**，不该等渲染 flush —— 否则"冷却刚开始"与
    // "首屏取数完成"到真正起表之间会隔一次调度，行为取决于组件渲染时序。
    watch(opts.active, (on) => (on ? start() : stop()), { immediate: true, flush: "sync" });
  } else {
    start();
  }

  const onVisible = (): void => {
    if (visible()) tick();
  };
  if (opts.runOnVisible !== false) {
    document.addEventListener("visibilitychange", onVisible);
  }

  onScopeDispose(() => {
    stop();
    document.removeEventListener("visibilitychange", onVisible);
  });
}
