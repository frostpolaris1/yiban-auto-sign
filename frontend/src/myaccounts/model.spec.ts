import { describe, expect, it } from "vitest";
import {
  accountActions,
  accountIcon,
  accountMeta,
  auditAriaText,
  deletedNote,
  passwordAlertText,
  passwordAlertVisible,
  pendingHintText,
  pendingHintVisible,
  rejectedAlertText,
  statusBadge,
  submitButtonVisible,
  todayStateText,
  type MyAccount,
} from "./model";

function acct(over: Partial<MyAccount> = {}): MyAccount {
  return { display_name: "A1", phone: "13800138001", status: "active", ...over };
}

describe("accountIcon（审核状态即图标；白名单缺失回落 clock）", () => {
  it("按状态取图标，删除态优先", () => {
    expect(accountIcon(acct({ status: "pending" }))).toBe("clock");
    expect(accountIcon(acct({ status: "rejected" }))).toBe("circle-x");
    expect(accountIcon(acct({ status: "active" }))).toBe("circle-check");
    expect(accountIcon(acct({ status: "weird" }))).toBe("clock");
    expect(accountIcon(acct({ deleted: true, status: "active" }))).toBe("trash");
  });
});

describe("statusBadge（删除态 > 用户暂停 > 审核状态）", () => {
  it("各态文案与档位", () => {
    expect(statusBadge(acct({ deleted: true }))).toEqual({ text: "已删除", tone: "" });
    expect(statusBadge(acct({ user_paused: true }))).toEqual({ text: "已取消", tone: "bad" });
    expect(statusBadge(acct({ status: "pending" }))).toEqual({ text: "待审核", tone: "warn" });
    expect(statusBadge(acct({ status: "rejected" }))).toEqual({ text: "已拒绝", tone: "bad" });
    expect(statusBadge(acct({ status: "active" }))).toEqual({ text: "已生效", tone: "ok" });
    expect(statusBadge(acct({ status: "other" }))).toEqual({ text: "other", tone: "" });
  });
});

describe("auditAriaText（图标是装饰，语义另有文本出口）", () => {
  it("各态读屏文案", () => {
    expect(auditAriaText(acct({ deleted: true, deleted_by_me: true }))).toBe("状态：已删除（7 天内可撤销）");
    expect(auditAriaText(acct({ deleted: true, deleted_by_me: false }))).toBe("状态：已被管理员删除");
    expect(auditAriaText(acct({ user_paused: true }))).toBe("状态：已取消（可恢复签到）");
    expect(auditAriaText(acct({ status: "pending" }))).toBe("状态：待审核");
    expect(auditAriaText(acct({ status: "zzz" }))).toBe("状态：未知");
  });
});

describe("accountMeta", () => {
  it("有机型时用 · 连接，无则只有手机号", () => {
    expect(accountMeta(acct({ phone: "13800138001", phone_model: "Pixel" }))).toBe("13800138001 · Pixel");
    expect(accountMeta(acct({ phone: "13800138001", phone_model: "" }))).toBe("13800138001");
  });
});

describe("todayStateText（仅管理端；口径与测试钉住）", () => {
  it("已完成态是固定字面量（与 yiban.status.DISPLAY 同源，改动须两边同步）", () => {
    expect(todayStateText(acct({ state_status: "success" }))).toBe("今日已完成签到");
    expect(todayStateText(acct({ state_status: "already" }))).toBe("今日已完成签到");
  });

  it("未完成时报排队人数；无排队数则不出行", () => {
    expect(todayStateText(acct({ state_status: "pending", queue_ahead: 3 }))).toBe("前方排队 3 人");
    expect(todayStateText(acct({ state_status: "pending", queue_ahead: 0 }))).toBe("前方排队 0 人");
    expect(todayStateText(acct({ state_status: "pending", queue_ahead: null }))).toBe("");
  });

  it("非生效态、已删除、已暂停均不出行", () => {
    expect(todayStateText(acct({ status: "pending", queue_ahead: 2 }))).toBe("");
    expect(todayStateText(acct({ deleted: true, state_status: "success" }))).toBe("");
    expect(todayStateText(acct({ state_status: "paused", queue_ahead: 2 }))).toBe("");
  });
});

describe("提示条与说明行", () => {
  it("删除态说明分本人/管理员两种", () => {
    expect(deletedNote(acct({ deleted: true, deleted_by_me: true }))).toBe("你已删除此账号，7 天内可撤销恢复，超期自动清除");
    expect(deletedNote(acct({ deleted: true, deleted_by_me: false }))).toBe("已被管理员删除，待管理员处理");
    expect(deletedNote(acct())).toBe("");
  });

  it("被拒提示：reason 为空时不编造原因", () => {
    expect(rejectedAlertText(acct({ status: "rejected", reject_reason: "手机号不存在" }))).toBe(
      "账号已被拒绝：手机号不存在。修改后点「修改并重新提交」。",
    );
    expect(rejectedAlertText(acct({ status: "rejected" }))).toBe("账号已被拒绝。修改后点「修改并重新提交」。");
    expect(rejectedAlertText(acct())).toBe("");
  });

  it("凭据异常提示只在生效且 paused 时出现", () => {
    expect(passwordAlertVisible(acct({ state_status: "paused" }))).toBe(true);
    expect(passwordAlertVisible(acct({ status: "pending", state_status: "paused" }))).toBe(false);
    expect(passwordAlertVisible(acct({ deleted: true, state_status: "paused" }))).toBe(false);
    expect(passwordAlertText()).toBe("账号密码异常，签到已暂停，请编辑账号更新密码。");
  });

  it("待审核提示", () => {
    expect(pendingHintVisible(acct({ status: "pending" }))).toBe(true);
    expect(pendingHintVisible(acct({ deleted: true, status: "pending" }))).toBe(false);
    expect(pendingHintText()).toBe("审核通过后自动签到，结果见「签到日历」。");
  });
});

describe("accountActions（顺序与 legacy 一致）", () => {
  it("生效态：日历 → 暂停/恢复 → 编辑 → 删除", () => {
    const acts = accountActions(acct(), { calendarHref: "/user/calendar" });
    expect(acts.map((a) => a.kind)).toEqual(["calendar", "pause", "edit", "delete"]);
    expect(acts[1]).toEqual({ kind: "pause", label: "暂停签到" });
    expect(acts[2]).toEqual({ kind: "edit", label: "编辑" });
  });

  it("已暂停时暂停按钮变「恢复签到」", () => {
    const acts = accountActions(acct({ user_paused: true }), { calendarHref: "/user/calendar" });
    expect(acts.find((a) => a.kind === "pause")).toEqual({ kind: "pause", label: "恢复签到" });
  });

  it("pause_forbidden（管理员直属账号）不出现暂停按钮；无 href 或 inline 模式不出现日历链接", () => {
    expect(accountActions(acct({ pause_forbidden: true }), { calendarHref: "/x" }).map((a) => a.kind)).toEqual([
      "calendar",
      "edit",
      "delete",
    ]);
    expect(accountActions(acct(), {}).map((a) => a.kind)).toEqual(["pause", "edit", "delete"]);
    expect(accountActions(acct(), { calendarHref: "/x", inline: true }).map((a) => a.kind)).toEqual([
      "pause",
      "edit",
      "delete",
    ]);
  });

  it("待审核态没有日历与暂停；被拒态编辑按钮改文案", () => {
    expect(accountActions(acct({ status: "pending" }), { calendarHref: "/x" }).map((a) => a.kind)).toEqual([
      "edit",
      "delete",
    ]);
    const rej = accountActions(acct({ status: "rejected" }), { calendarHref: "/x" });
    expect(rej.map((a) => a.kind)).toEqual(["edit", "delete"]);
    expect(rej[0]).toEqual({ kind: "edit", label: "修改并重新提交" });
  });

  it("删除态：本人删的可撤销；管理员删的只提示", () => {
    expect(accountActions(acct({ deleted: true, deleted_by_me: true }))).toEqual([
      { kind: "restore", label: "撤销删除" },
    ]);
    expect(accountActions(acct({ deleted: true, deleted_by_me: false }))).toEqual([
      { kind: "note", text: "待管理员处理" },
    ]);
  });
});

describe("submitButtonVisible（还有未删除账号就隐藏；全删光时保留）", () => {
  it("空列表/全删除 → 显示；有任一生效 → 隐藏", () => {
    expect(submitButtonVisible([])).toBe(true);
    expect(submitButtonVisible([acct({ deleted: true }), acct({ deleted: true })])).toBe(true);
    expect(submitButtonVisible([acct({ deleted: true }), acct()])).toBe(false);
  });
});
