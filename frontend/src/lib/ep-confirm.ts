import { ElMessageBox } from "element-plus/es";

/**
 * 确认族弹窗的 EP 实现（uz7w「手搓弹窗换 EP 自带件」）。
 *
 * 出口与外壳 core.js 的 `confirmDialog` **逐字同形**：`(opts) => Promise<boolean>`，
 * 确认 resolve(true)，取消 / Esc / 关闭一律 resolve(false)。调用方（accounts/users/
 * settings 的 ops.js 经 `YB.confirmDialog`，MyAccounts 等经 `shell.confirmDialog`）
 * 零改动——core.js 的 confirmDialog 先探 `window.YB_EP_CONFIRM`（本模块在装配时注册），
 * 命中即走本实现；未命中（无 EP 页面 / EP 装配前）落回外壳旧实现。
 *
 * 样式不进本模块：`element-plus/es/.../style/css` 在 vitest 的原生 ESM 路径下不可解析
 * （`Unknown file extension ".css"`）。样式由页面装配入口 `ep-mount.ts` 引入——那是
 * Vite 的构建图，vitest 的单测不需要 CSS。
 *
 * 为什么文案必须显式给中文：ElMessageBox 缺省按钮文案是英文（"OK"/"Cancel"），
 * 本站不整体 `app.use(ElementPlus)`（见 ep-mount.ts 的按需装配说明），message-box
 * 服务不走 ElConfigProvider 的 locale 注入，只能在本层逐字给。
 *
 * 安全口径与外壳旧实现一致：破坏性确认（danger）的初始焦点落在**取消**侧，
 * 回车不会直发破坏性动作（旧实现 initialFocus 落「取消」的语义照搬）。
 */
export interface EpConfirmOptions {
  title?: string;
  body?: string;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
}

export function epConfirm(opts: EpConfirmOptions): Promise<boolean> {
  const o = opts ?? {};
  const p = ElMessageBox.confirm(o.body ?? "", o.title ?? "请确认", {
    confirmButtonText: o.confirmText ?? "确定",
    cancelButtonText: o.cancelText ?? "取消",
    confirmButtonClass: o.danger ? "el-button--danger" : "el-button--primary",
    autofocus: false,
  }).then(
    () => true,
    () => false,
  );
  // autofocus:false 关掉 EP 默认的「确认键抢焦点」；把焦点送到取消键（安全侧）。
  // 盒子由 EP 在自己的帧里挂到 body 级 overlay，故按帧轮询若干次而不是只探一帧
  // （首帧可能还没挂载，只探一帧会让安全侧焦点静默丢失）。
  let tries = 0;
  const focusCancel = (): void => {
    if (tries++ > 10) return;
    const box = document.querySelector<HTMLElement>(".el-message-box");
    const cancel = box?.querySelector<HTMLElement>(".el-message-box__btns .el-button:first-child");
    if (cancel) {
      cancel.focus();
      return;
    }
    requestAnimationFrame(focusCancel);
  };
  requestAnimationFrame(focusCancel);
  return p;
}

/** 装配点：把 EP 确认实现注册到外壳探测面（幂等；由 ep-mount.ts 在每页装配时调用）。 */
export function registerEpConfirm(): void {
  (window as { YB_EP_CONFIRM?: unknown }).YB_EP_CONFIRM = epConfirm;
}
