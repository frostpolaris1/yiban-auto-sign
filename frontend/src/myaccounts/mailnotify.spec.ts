import { describe, expect, it } from "vitest";
import {
  TIP_TTL_MS,
  adminGateDesc,
  adminSaveFailedTip,
  adminTip,
  isBuiltinAdminVariant,
  needsGate,
  userSaveFailedTip,
  userTip,
} from "./mailnotify";

describe("变体判定", () => {
  it("只有内置主管理员走 mail-config 变体", () => {
    expect(isBuiltinAdminVariant("builtin-admin")).toBe(true);
    expect(isBuiltinAdminVariant("user")).toBe(false);
  });
});

describe("提示文案（逐字对齐 legacy）", () => {
  it("普通用户", () => {
    expect(userTip(true)).toBe("已开启：签到失败时将邮件提醒你");
    expect(userTip(false)).toBe("已关闭：不再发送签到失败邮件");
    expect(userSaveFailedTip("网络错误")).toBe("保存失败：网络错误");
    expect(userSaveFailedTip("")).toBe("保存失败：请稍后重试");
  });

  it("主管理员", () => {
    expect(adminTip(true)).toBe("已开启接收邮件提醒");
    expect(adminTip(false)).toBe("已关闭接收邮件提醒");
    expect(adminSaveFailedTip()).toBe("保存失败，请稍后重试");
    expect(TIP_TTL_MS).toBe(3000);
  });
});

describe("门禁方向（只有关闭受门禁；开启是纯开启直发）", () => {
  it("needsGate 仅对关闭为真", () => {
    expect(needsGate(false)).toBe(true);
    expect(needsGate(true)).toBe(false);
  });

  it("门禁说明文案是该行为的固定文案", () => {
    expect(adminGateDesc()).toBe(
      "关闭主管理员告警邮件接收？关闭后你不再收到任何告警邮件。请输入当前管理员密码确认。",
    );
  });
});

describe("PII 纪律（提示一律不含手机号/邮箱）", () => {
  it("所有提示文案不含 @ 与 11 位连续数字", () => {
    const tips = [
      userTip(true),
      userTip(false),
      adminTip(true),
      adminTip(false),
      userSaveFailedTip("x"),
      adminSaveFailedTip(),
      adminGateDesc(),
    ];
    for (const t of tips) {
      expect(t).not.toContain("@");
      expect(/\d{11}/.test(t)).toBe(false);
    }
  });
});
