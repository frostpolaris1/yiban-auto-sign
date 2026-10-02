import { expect, test } from "@playwright/test";

// 日志页真浏览器端到端（P1b）：三分区切换 + 日志正文渲染 + **端到端脱敏**。
//
// 数据由 e2e/server.py 预置：当天的假 sign.log（三行，其中含**完整手机号**
// 13800138001）、审计种子行。签到/探针事件表在该实例里为空（不种 DB 事件），
// 故这两个分区只验证"能切换 + 空态可见"。
const ADMIN_USER = "admin";
const ADMIN_PASS = "TestPass1234!";

test("登录 → 日志页：正文渲染、完整手机号不出现在 DOM、三分区可切换", async ({ page }) => {
  await page.goto("/login");
  await page.fill("#username", ADMIN_USER);
  await page.fill("#password", ADMIN_PASS);
  await page.click("#login-btn");
  await expect(page).toHaveURL(/\/(data\/dashboard)?$/);

  await page.goto("/data/logs");

  // 1) 日志正文：信息栏报行数，正文 pre 渲染出种子行
  await expect(page.locator(".logs-info")).toContainText("行");
  const box = page.locator(".log-box");
  await expect(box).toBeVisible();
  await expect(box).toContainText("签到成功");

  // 2) 端到端脱敏：脱敏形态出现，完整号码**绝不出现**（服务端单出口 + 前端只插值）
  const text = await box.innerText();
  expect(text).toContain("138****8001");
  expect(text).not.toContain("13800138001");

  // 3) 三分区切换（el-tabs）；事件表在本实例为空 → 空态可见。
  //    定位收窄到**可见分区**：EP 把所有 pane 都留在 DOM 里（未激活的 display:none），
  //    裸 `.el-table` 会命中两个而触发 Playwright 严格模式报错。
  await page.getByRole("tab", { name: /签到事件/ }).click();
  const signPane = page.locator(".el-tab-pane:visible");
  await expect(signPane.locator(".el-table")).toBeVisible();
  await expect(signPane).toContainText("该日无签到事件");

  await page.getByRole("tab", { name: /探针记录/ }).click();
  const probePane = page.locator(".el-tab-pane:visible");
  await expect(probePane.locator(".el-table")).toBeVisible();
  await expect(probePane).toContainText("该日无探针记录");

  // 4) 分区深链：切到探针后 URL 带 ?tab=probe（与旧页 URL 契约一致，可直接分享）
  await expect(page).toHaveURL(/[?&]tab=probe/);

  // 5) 回到日志分区仍在（切 tab 不重载页面、正文保留）
  await page.getByRole("tab", { name: "日志" }).click();
  await expect(page.locator(".log-box")).toContainText("签到成功");
});
