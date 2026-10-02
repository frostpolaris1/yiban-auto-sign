import { expect, test } from "@playwright/test";

// 用户端账号页真浏览器端到端（P2 整页迁移后）。
// 数据由 e2e/server.py 预置：普通用户 e2e-user@example.com 名下有一个生效账号
// （e2e-user-acct / 13800138001）。会话走 API 登录（表单登录由 logs.spec 覆盖一次即可，
// 避免反复访问 /login 撞上服务端的登录循环守卫）。
const USER_EMAIL = "e2e-user@example.com";
const USER_PASS = "UserPass123!";

async function loginUser(page: import("@playwright/test").Page): Promise<void> {
  const resp = await page.request.post("/api/login", { data: { username: USER_EMAIL, password: USER_PASS } });
  expect(resp.ok()).toBeTruthy();
}

test("用户端账号页：账号行/调度提示/自选时段/邮件开关/改密入口，且无管理端专有卡", async ({ page }) => {
  await loginUser(page);
  await page.goto("/user/account");

  // 页头 + 调度提示条（由 /api/me 填充）
  await expect(page.locator("h1.page-title")).toHaveText("账号与设置");
  await expect(page.locator(".schedule-banner")).toContainText("签到方式");

  // 账号行：名称 / 状态徽标 / **完整手机号**（自己看自己的账号按既有设计显示完整号）
  const card = page.locator(".account-card").first();
  await expect(card.locator(".account-name")).toHaveText("e2e-user-acct");
  await expect(card.locator(".account-title-row .badge")).toHaveText("已生效");
  await expect(card.locator(".account-meta")).toContainText("13800138001");

  // 生效账号的四个动作（日历链接 / 暂停 / 编辑 / 删除）
  const actions = card.locator(".account-actions");
  await expect(actions.getByRole("link", { name: "签到日历" })).toHaveAttribute("href", /\/user\/calendar$/);
  await expect(actions.getByRole("button", { name: "暂停签到" })).toBeVisible();
  await expect(actions.getByRole("button", { name: "编辑" })).toBeVisible();
  await expect(actions.getByRole("button", { name: "删除" })).toBeVisible();

  // 自选签到时间：默认收起 → 展开后槽位网格可见
  await expect(page.locator(".slot-grid")).toBeHidden();
  await page.getByRole("button", { name: /展开配置|收起/ }).click();
  await expect(page.locator(".slot-grid")).toBeVisible();
  await expect(page.locator(".slot-grid .slot").first()).toBeVisible();

  // 邮件提醒卡与改密入口存在
  await expect(page.locator(".card", { hasText: "邮件提醒" }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: /修改密码/ })).toBeVisible();

  // 侧栏账号块只显示邮箱本地部（legacy renderSidebarEmail 口径）；完整邮箱进 title
  await expect(page.locator("[data-account-email]").first()).toHaveText("e2e-user");
  await expect(page.locator("[data-account-email]").first()).toHaveAttribute("title", USER_EMAIL);

  // 用户端专有：注销账号；管理端专有：显示偏好（**不应出现**）
  await expect(page.getByRole("button", { name: /注销账号/ })).toBeVisible();
  await expect(page.locator("h2.panel-title", { hasText: "显示偏好" })).toHaveCount(0);
});

test("用户端账号页：暂停/恢复往返、编辑保存、改密弹窗、注销确认（取消）", async ({ page }) => {
  await loginUser(page);
  await page.goto("/user/account");
  const card = page.locator(".account-card").first();
  const badge = card.locator(".account-title-row .badge");

  // ① 暂停：确认框 → 就地状态行 + 徽标变「已取消」
  await card.getByRole("button", { name: "暂停签到" }).click();
  await page.locator(".modal-foot .btn:not(.btn--ghost)").click();
  await expect(card.locator(".state-line--flash")).toContainText("已暂停签到");
  await expect(badge).toHaveText("已取消");

  // ② 恢复：无需确认框（legacy 语义），徽标回到「已生效」
  await card.getByRole("button", { name: "恢复签到" }).click();
  await expect(card.locator(".state-line--flash")).toContainText("已恢复签到");
  await expect(badge).toHaveText("已生效");

  // ③ 编辑：改名称 → 保存 → 行内名称更新（覆盖表单口径 + PUT + 重新拉取）
  await card.getByRole("button", { name: "编辑" }).click();
  const dialog = page.locator(".el-dialog");
  await expect(dialog).toBeVisible();
  const nameInput = dialog.locator('input[placeholder="如：我的易班账号"]');
  await nameInput.fill("e2e-改名后");
  await dialog.locator(".el-dialog__footer .btn--primary").click();
  await expect(dialog).toBeHidden();
  await expect(card.locator(".account-name")).toHaveText("e2e-改名后");

  // ④ 改密入口：复用 core.js 的共享弹窗（本页显式加载了 change-password.js）
  await page.getByRole("button", { name: /修改密码/ }).click();
  await expect(page.locator(".pm-backdrop")).toBeVisible();
  await expect(page.locator(".pm-backdrop")).toContainText("新密码");
  await page.keyboard.press("Escape");

  // ⑤ 注销账号：确认框出现（此处只验证入口与文案，不真的注销——那会销毁会话）
  await page.getByRole("button", { name: /注销账号/ }).click();
  await expect(page.locator(".pm-backdrop")).toContainText("7 天内可撤销恢复");
  await expect(page.locator(".modal-foot .btn:not(.btn--ghost)")).toHaveText("继续注销");
  await page.keyboard.press("Escape");
});
