/**
 * 在线校验任务控制器（Vue composable；**可注入依赖以便单测**）。
 *
 * **语义逐条对齐 legacy `web/static/js/components/verify-job.js`**，但把"定时器 + DOM 查询"
 * 换成"注入式调度器 + 响应式状态"——legacy 那版把 `setTimeout` 与 `#my-verify-job` 写死，
 * 于是 24 次轮询上界、取消竞态、失败即停这些**诚实性**路径一条都测不了。这里用注入换来
 * 可测试性：
 *   · `api`       —— 请求层（生产传 `shell.api`，测试传脚本化假实现）
 *   · `scheduler` —— 定时（生产传 setTimeout/clearTimeout，测试传手动推进的假调度器）
 *
 * 三条约定（与 verify.ts 的文案口径配套）：
 *   1. 轮询有上界：到顶显示"仍在进行，可稍后刷新"，**不无限转圈**；
 *   2. 任何查询失败都如实说失败并**停止**，不把"查不到"说成"已结束"；
 *   3. 全程不阻断账号提交（账号在提交那刻已落库，校验只影响审核判断）。
 */

import { ref, type Ref } from "vue";
import { errorMessage, errorStatus, isCanceled } from "../lib/shell";
import {
  VERIFY_CANCEL_CONFLICT,
  VERIFY_MAX_POLLS,
  cancelErrorText,
  exhaustedText,
  finishText,
  isTerminal,
  pollDelay,
  progressText,
  queryErrorText,
  queryingText,
} from "./verify";

export interface VerifyJobApi {
  (method: string, path: string, body?: unknown): Promise<unknown>;
}

export interface VerifyJobScheduler {
  schedule(fn: () => void, ms: number): number;
  cancel(handle: number): void;
}

export interface VerifyFinish {
  status: string;
  error: string;
  phone: string;
}

export interface VerifyJobState {
  /** 提示条是否显示（文案为空时不显示） */
  visible: boolean;
  text: string;
  /** 失败/告警态（页面据此用告警样式） */
  bad: boolean;
  /** 当前是否可取消（仅 pending 阶段可取消） */
  cancellable: boolean;
}

export interface VerifyJobController {
  state: Ref<VerifyJobState>;
  start(jobId: string): void;
  cancel(): void;
  stop(): void;
}

interface JobShape {
  status?: string;
  error?: string;
  phone?: string;
}

function jobOf(data: unknown): JobShape {
  const j = (data as { job?: JobShape } | null)?.job;
  return j && typeof j === "object" ? j : {};
}

export function createVerifyJob(deps: {
  api: VerifyJobApi;
  scheduler: VerifyJobScheduler;
  onFinish?: (r: VerifyFinish) => void;
}): VerifyJobController {
  const state = ref<VerifyJobState>({ visible: false, text: "", bad: false, cancellable: false });

  let jobId = "";
  let timer: number | null = null;
  let tries = 0;
  let stopped = true;
  /** 最近一次服务端确认的任务状态（取消时据此判断可否点） */
  let job: JobShape = {};

  function clearTimer(): void {
    if (timer !== null) {
      deps.scheduler.cancel(timer);
      timer = null;
    }
  }

  function paint(text: string, bad: boolean, cancellable: boolean): void {
    state.value = { visible: !!text, text, bad, cancellable };
  }

  function finish(status: string, err?: string, phone?: string): void {
    clearTimer();
    const { text, bad } = finishText(status, err, phone);
    paint(text, bad, false);
    if (deps.onFinish) deps.onFinish({ status, error: err || "", phone: phone || "" });
  }

  function poll(): void {
    if (stopped) return;
    deps.api("GET", `/api/verify-jobs/${encodeURIComponent(jobId)}`).then(
      (data) => {
        if (stopped) return;
        job = jobOf(data);
        const st = job.status || "";
        tries += 1;
        if (isTerminal(st)) {
          finish(st, job.error, job.phone);
          return;
        }
        const { text, cancellable } = progressText(st);
        paint(text, false, cancellable);
        if (tries >= VERIFY_MAX_POLLS) {
          clearTimer();
          paint(exhaustedText(), false, false);
          return;
        }
        timer = deps.scheduler.schedule(poll, pollDelay(tries));
      },
      (e) => {
        if (stopped) return;
        clearTimer();
        // 如实说明并停止：不把"查不到"说成"已结束"
        paint(queryErrorText(errorStatus(e)), true, false);
      },
    );
  }

  function cancel(): void {
    // 仅 pending 可取消（后端也只允许取消 pending）
    if (stopped || job.status !== "pending") return;
    paint(state.value.text, state.value.bad, false);
    deps.api("DELETE", `/api/verify-jobs/${encodeURIComponent(jobId)}`).then(
      (data) => {
        if (stopped) return;
        const j = jobOf(data);
        finish(j.status || "cancelled", j.error, j.phone);
      },
      (e) => {
        if (stopped) return;
        if (isCanceled(e)) return;
        if (errorStatus(e) === VERIFY_CANCEL_CONFLICT) {
          // 已被后台线程抢走开工：取消不再成立 → 改查一次真实状态，别停在"可取消"
          poll();
          return;
        }
        paint(cancelErrorText(errorMessage(e, "")), true, job.status === "pending");
      },
    );
  }

  function start(nextJobId: string): void {
    if (!nextJobId) return;
    stop(); // 同一时刻只跟一个任务，避免两个提示互相覆盖
    jobId = nextJobId;
    tries = 0;
    job = {};
    stopped = false;
    paint(queryingText(), false, false);
    poll();
  }

  function stop(): void {
    stopped = true;
    clearTimer();
  }

  return { state, start, cancel, stop };
}

/** 生产用调度器（浏览器 setTimeout）。 */
export const browserScheduler: VerifyJobScheduler = {
  schedule: (fn, ms) => setTimeout(fn, ms) as unknown as number,
  cancel: (handle) => clearTimeout(handle),
};
