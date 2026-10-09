import { afterEach, describe, expect, it, vi } from "vitest";
import {
  confirmDialog,
  dangerousSubmit,
  hasShell,
  openChangePassword,
  ownerEmailVisible,
  passwordHint,
  passwordPolicyOk,
  passwordPolicyOkAdmin,
  setOwnerEmailVisible,
  shellBase,
  toast,
} from "../lib/shell";

type Win = { YB?: unknown };

function installStub(stub: Record<string, unknown>): void {
  (window as unknown as Win).YB = stub;
}

afterEach(() => {
  delete (window as unknown as Win).YB;
});

describe("外壳缺失时的安全默认（组件单测 / 独立运行场景）", () => {
  it("hasShell 为假", () => {
    expect(hasShell()).toBe(false);
  });

  it("确认框一律返回 false——宁可什么都不做，也不误触删除/注销", async () => {
    await expect(confirmDialog({ title: "删除账号" })).resolves.toBe(false);
  });

  it("受门禁提交直接拒绝（不静默成功）", async () => {
    await expect(dangerousSubmit({ method: "PUT", path: "/api/mail-config" })).rejects.toThrow(
      "外壳未就绪",
    );
  });

  it("toast / 弹窗 / 偏好读写均为空操作且不抛错", () => {
    const t = toast();
    expect(() => {
      t("普通提示");
      t.success("成功");
      t.error("失败");
      t.warning("警告");
      t.info("信息");
    }).not.toThrow();
    expect(() => openChangePassword({ builtinAdmin: true })).not.toThrow();
    expect(() => setOwnerEmailVisible(true)).not.toThrow();
    expect(ownerEmailVisible()).toBe(false);
    expect(shellBase()).toBe("");
  });

  it("口令策略保守判否；提示回落为与后端同口径的字面量", () => {
    expect(passwordPolicyOk("Abcd1234!!")).toBe(false);
    expect(passwordPolicyOkAdmin("Abcd1234!!xy")).toBe(false);
    expect(passwordHint(false)).toBe("至少 10 位，且包含大小写字母、数字、符号中的至少两类");
    expect(passwordHint(true)).toBe("至少 12 位，且包含大写字母、小写字母、数字、符号中的至少三类");
  });
});

describe("外壳存在时透传（不在 TS 里重实现策略）", () => {
  it("确认框与门禁提交直接委托", async () => {
    const confirm = vi.fn().mockResolvedValue(true);
    const dangerous = vi.fn().mockResolvedValue({ ok: true });
    installStub({ confirmDialog: confirm, dangerousSubmit: dangerous });

    await expect(confirmDialog({ title: "x" })).resolves.toBe(true);
    expect(confirm).toHaveBeenCalledWith({ title: "x" });
    await dangerousSubmit({ method: "PUT", path: "/p" });
    expect(dangerous).toHaveBeenCalledWith({ method: "PUT", path: "/p" });
    expect(hasShell()).toBe(true);
  });

  it("口令策略走外壳实现（单一源在 core.js；此处只转发）", () => {
    const ok = vi.fn().mockReturnValue(true);
    const okAdmin = vi.fn().mockReturnValue(false);
    installStub({
      passwordPolicyOk: ok,
      passwordPolicyOkAdmin: okAdmin,
      PW_POLICY_HINT: "自定义提示",
      BASE: "/sub",
    });

    expect(passwordPolicyOk("whatever")).toBe(true);
    expect(ok).toHaveBeenCalledWith("whatever");
    expect(passwordPolicyOkAdmin("whatever")).toBe(false);
    expect(passwordHint(false)).toBe("自定义提示");
    expect(shellBase()).toBe("/sub");
  });

  it("偏好读写与改密弹窗转发到外壳", () => {
    const set = vi.fn();
    const open = vi.fn();
    installStub({ prefs: { ownerEmailVisible: () => true, setOwnerEmailVisible: set }, changePassword: { open } });

    expect(ownerEmailVisible()).toBe(true);
    setOwnerEmailVisible(false);
    expect(set).toHaveBeenCalledWith(false);
    openChangePassword({ builtinAdmin: false });
    expect(open).toHaveBeenCalledWith({ builtinAdmin: false });
  });
});
