import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { epConfirm } from "./ep-confirm";

/**
 * 确认族弹窗换 EP（uz7w）的判据。
 *
 * 不变式：
 *   1. 确认路径 resolve(true)，取消 / Esc / 关闭一律 resolve(false)——与外壳旧
 *      confirmDialog 的 Promise<boolean> 出口逐字同形，调用方（ops.js / shell.ts）零改动。
 *   2. 按钮文案：确认侧取 opts.confirmText（缺省「确定」）、取消侧取 opts.cancelText
 *      （缺省「取消」）——EP 缺省是英文，中文文案必须由本层显式给。
 *   3. danger 确认键必须带危险档样式类；焦点落在**取消**侧（破坏性动作不因回车直发，
 *      与外壳旧实现同一安全口径）。
 *   4. 外壳 core.js 的 confirmDialog 必须**先**探 EP 注册面再落旧实现（EP 页面换装、
 *      无 EP 面兜底），出口 YB.confirmDialog 形状不变。
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "..", "..", "..");
const CORE_JS = join(REPO, "web", "static", "js", "core.js");

function box(): HTMLElement {
  const el = document.querySelector<HTMLElement>(".el-message-box");
  if (!el) throw new Error("ElMessageBox 未挂载 .el-message-box");
  return el;
}

async function waitForBox(): Promise<HTMLElement> {
  await vi.waitFor(() => {
    if (!document.querySelector(".el-message-box")) throw new Error("盒子未挂载");
  });
  return box();
}

function buttonIn(root: HTMLElement, cls: string): HTMLButtonElement {
  const b = root.querySelector<HTMLButtonElement>(`.el-button${cls}`);
  if (!b) throw new Error(`按钮未找到：${cls}`);
  return b;
}

afterEach(() => {
  // 盒子是全局单例挂 body：每个用例后清场，避免互相污染。
  document.querySelectorAll(".el-overlay, .el-message-box").forEach((n) => n.remove());
  const host = document.querySelector(".el-popper-container") ?? document.body;
  void host;
});

describe("epConfirm：EP 确认框路径", () => {
  it("点确认 resolve(true)，文案取 opts 与中文缺省", async () => {
    const p = epConfirm({ title: "删除账号", body: "确定删除该账号？", confirmText: "删除" });
    const el = await waitForBox();
    expect(box().querySelector<HTMLElement>(".el-message-box__title")?.textContent).toContain("删除账号");
    expect(box().textContent).toContain("确定删除该账号？");
    expect(buttonIn(el, "--primary").textContent).toBe("删除");
    expect(buttonIn(el, ":not(.el-button--primary)").textContent).toBe("取消");
    buttonIn(el, "--primary").click();
    await expect(p).resolves.toBe(true);
  });

  it("点取消 resolve(false)；未传文案时用中文缺省", async () => {
    const p = epConfirm({ title: "请确认", body: "继续吗？" });
    const el = await waitForBox();
    expect(buttonIn(el, "--primary").textContent).toBe("确定");
    expect(buttonIn(el, ":not(.el-button--primary)").textContent).toBe("取消");
    buttonIn(el, ":not(.el-button--primary)").click();
    await expect(p).resolves.toBe(false);
  });

  it("danger 确认键带危险档样式；初始焦点落在取消侧", async () => {
    const p = epConfirm({ title: "不可逆", body: "此操作不可撤销", danger: true });
    const el = await waitForBox();
    expect(buttonIn(el, "--danger")).toBeTruthy();
    // 焦点由实现按帧送达（EP 自行决定挂载帧），等它落定再断言。
    await vi.waitFor(() => {
      if (document.activeElement !== buttonIn(el, ":not(.el-button--danger)")) {
        throw new Error("焦点尚未落到取消侧");
      }
    });
    buttonIn(el, "--danger").click();
    await expect(p).resolves.toBe(true);
  });
});

describe("外壳 confirmDialog 的 EP 探测", () => {
  it("core.js 先探 YB_EP_CONFIRM 再落旧实现；出口 Promise<boolean> 不变", () => {
    const src = readFileSync(CORE_JS, "utf8");
    const head = src.indexOf("function confirmDialog(");
    expect(head, "core.js 缺 confirmDialog（出口被删？）").toBeGreaterThanOrEqual(0);
    const body = src.slice(head, src.indexOf("function promptDialog("));
    expect(body).toContain("YB_EP_CONFIRM");
    expect(body).toContain("legacyConfirmDialog");
  });
});
