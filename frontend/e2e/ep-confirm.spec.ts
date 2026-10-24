import { adminTest, expect } from "./base";

/* 确认族弹窗的载体判据（uz7w「手搓弹窗换 EP 自带件」）。

 * 为什么钉载体而不只钉文案：换件的价值就是「不再维护自研模态的焦点/Esc/遮罩」。
 * 若只断言「出现了确认文案」，自研实现也能满足，这条门就抓不到回退。
 * 故断言三件事：
 *   ① 页面里的确认框是 EP 的 `.el-message-box`（且不再有自研 `.pm-backdrop` 参与）；
 *   ② 两个按钮是中文缺省文案（EP 缺省是英文，中文由 ep-confirm 逐字给）；
 *   ③ 「取消」可关闭且不留残层——用户可见的结果，不是内部调用顺序。

 * 出口形状不变由 frontend/src/lib/ep-confirm.spec.ts 的 vitest 钉住；本用例只钉
 * 浏览器里真实可见的那一面。 */

adminTest("账号页「通过」确认框走 EP：中文按钮、可取消、无自研残层", async ({ page }) => {
  await page.goto("/work/accounts");
  await page.waitForSelector("tbody tr[data-acct-idx]");

  const row = page.locator("tbody tr[data-acct-idx]").first();
  await row.getByRole("button", { name: "通过", exact: true }).click();

  const box = page.locator(".el-message-box");
  await expect(box, "确认框必须是 EP 的 ElMessageBox").toBeVisible();
  await expect(box.locator(".el-message-box__title")).toContainText("通过账号");
  await expect(box.locator(".el-message-box__message")).toContainText("确定通过");

  // ① 中文缺省文案（EP 自带是 OK / Cancel）
  const buttons = box.locator(".el-message-box__btns .el-button");
  await expect(buttons.filter({ hasText: "取消" })).toHaveCount(1);
  await expect(buttons.filter({ hasText: /^(通过|确定)$/ })).toHaveCount(1);

  // ② 自研模态不参与：这一层不许出现 .pm-backdrop
  await expect(page.locator(".pm-backdrop")).toHaveCount(0);

  // ③ 取消关闭：用户可见结果是盒子消失、表格行数不变（没有误发写请求）
  const rowsBefore = await page.locator("tbody tr[data-acct-idx]").count();
  await buttons.filter({ hasText: "取消" }).click();
  await expect(box).toBeHidden();
  expect(await page.locator("tbody tr[data-acct-idx]").count()).toBe(rowsBefore);
});
