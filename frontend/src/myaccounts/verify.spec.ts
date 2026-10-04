import { describe, expect, it } from "vitest";
import {
  VERIFY_MAX_POLLS,
  VERIFY_POLL_MS,
  cancelErrorText,
  exhaustedText,
  finishText,
  isTerminal,
  pollDelay,
  progressText,
  queryErrorText,
  queryingText,
} from "./verify";

describe("终态判定与退避节奏", () => {
  it("终态集合与后端 verify_jobs 一一对应", () => {
    expect(isTerminal("done")).toBe(true);
    expect(isTerminal("rejected")).toBe(true);
    expect(isTerminal("cancelled")).toBe(true);
    expect(isTerminal("pending")).toBe(false);
    expect(isTerminal("running")).toBe(false);
  });

  it("退避取第 tries 项并夹到末项（不越界、不无限变长）", () => {
    expect(pollDelay(1)).toBe(VERIFY_POLL_MS[1]);
    expect(pollDelay(3)).toBe(VERIFY_POLL_MS[3]);
    expect(pollDelay(99)).toBe(VERIFY_POLL_MS[VERIFY_POLL_MS.length - 1]);
    expect(pollDelay(0)).toBe(VERIFY_POLL_MS[1]);
  });

  it("轮询上界存在（诚实性约定：不许无限转圈）", () => {
    expect(VERIFY_MAX_POLLS).toBeGreaterThan(0);
    expect(exhaustedText()).toBe("在线校验仍在进行，可稍后刷新本页查看结果。");
  });
});

describe("progressText（pending 可取消、running 不可）", () => {
  it("按状态给文案与可取消性", () => {
    expect(progressText("running")).toEqual({ text: "正在在线校验账号信息…", cancellable: false });
    expect(progressText("pending")).toEqual({ text: "已排上在线校验，等待执行…", cancellable: true });
    expect(progressText("")).toEqual({ text: "已排上在线校验，等待执行…", cancellable: false });
    expect(queryingText()).toBe("正在查询校验状态…");
  });
});

describe("finishText（终态文案；rejected 不编造原因）", () => {
  it("通过：带手机号时括号注明", () => {
    expect(finishText("done", null, "13800138001")).toEqual({
      text: "在线校验通过（13800138001）。等待管理员审核后即可自动签到。",
      bad: false,
    });
    expect(finishText("done", null, "").text).toBe("在线校验通过。等待管理员审核后即可自动签到。");
  });

  it("已取消：交代账号仍在、由管理员核对", () => {
    expect(finishText("cancelled")).toEqual({
      text: "已取消本次在线校验。账号仍已提交，管理员审核时可手动核对。",
      bad: false,
    });
  });

  it("未通过：有原因就照实显示，没原因不编", () => {
    expect(finishText("rejected", "验证码不匹配")).toEqual({
      text: "在线校验未通过：验证码不匹配。可修正账号信息后重新提交。",
      bad: true,
    });
    expect(finishText("rejected", "")).toEqual({
      text: "在线校验未通过。可修正账号信息后重新提交。",
      bad: true,
    });
  });
});

describe("查询失败：如实说明并停止（不把查不到说成已结束）", () => {
  it("按状态码分流", () => {
    expect(queryErrorText(404)).toBe("校验任务已不存在或已过期");
    expect(queryErrorText(403)).toBe("无权查看该校验任务");
    expect(queryErrorText(500)).toBe("校验状态查询失败，请稍后刷新查看");
    expect(queryErrorText(undefined)).toBe("校验状态查询失败，请稍后刷新查看");
  });

  it("取消失败文案带原因；409 由调用方改走重查（常量在此声明）", () => {
    expect(cancelErrorText("任务已开工")).toBe("取消失败：任务已开工");
    expect(cancelErrorText("")).toBe("取消失败：请稍后重试");
  });
});
