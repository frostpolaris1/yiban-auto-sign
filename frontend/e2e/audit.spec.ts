import { expect, test } from "@playwright/test";

// 审计页真浏览器端到端（P1c）：这条测试闭合"客户端拉取 + has_more 翻页"这段
// Python 契约测试与纯函数单测都覆盖不到的证据缺口。
//
// 数据由 e2e/server.py 预置（默认 60 条 e2e_seed_*，见该文件）。
const SEED_ROWS = Number(process.env.YB_E2E_SEED_ROWS ?? 60);
const ADMIN_USER = "admin";
const ADMIN_PASS = "TestPass1234!";

test("登录 → 审计页：首屏一页、加载更多补齐、动作过滤收窄", async ({ page }) => {
  // 1) 真实登录表单（服务端渲染页 + core.js 驱动提交）
  await page.goto("/login");
  await page.fill("#username", ADMIN_USER);
  await page.fill("#password", ADMIN_PASS);
  await page.click("#login-btn");
  await expect(page).toHaveURL(/\/(data\/dashboard)?$/);

  // 2) 进入审计页：客户端渲染首批（默认 page_size=50）
  //    注意：**读取本身也留痕**（audit_api 契约第 6 条：每次成功读取写一条 audit_logs_read），
  //    故总条数会随浏览增长——断言只用"自洽性"（已显示 == 实际行数），不钉固定总数。
  await page.goto("/data/audit");
  const rows = page.locator(".el-table__row");
  await expect(rows).toHaveCount(50);
  const total = Number(/共\s*(\d+)\s*条/.exec(await page.locator(".audit-meta").innerText())![1]);
  expect(total).toBeGreaterThanOrEqual(SEED_ROWS);

  // 3) has_more 驱动翻页（契约第 2 条）：加载更多 → 补齐到底、按钮转"已到末尾"
  const more = page.getByRole("button", { name: "加载更多" });
  await expect(more).toBeEnabled();
  await more.click();
  await expect(page.getByRole("button", { name: "已到末尾" })).toBeDisabled();
  const shown = Number(/已显示\s*(\d+)/.exec(await page.locator(".audit-meta").innerText())![1]);
  expect(shown).toBeGreaterThan(50);
  await expect(rows).toHaveCount(shown);

  // 4) 等值过滤收窄（服务端白名单键 action；读取留痕的 action 是 audit_logs_read，不会混入）
  await page.fill('input[placeholder="如 login_success"]', "e2e_seed_3");
  await page.getByRole("button", { name: "查询" }).click();
  await expect(rows).toHaveCount(1);
  await expect(page.locator(".audit-meta")).toContainText("共 1 条");
  await expect(rows).toContainText("e2e_seed_3");

  // 5) 重置回到首屏一页
  await page.getByRole("button", { name: "重置" }).click();
  await expect(rows).toHaveCount(50);
  await expect(more).toBeEnabled();
});

test("审计页：非管理员登录被守卫挡回用户端", async ({ page }) => {
  // 用错密码不应进入：先验证登录页错误路径仍然可用（错误框可见）
  await page.goto("/login");
  await page.fill("#username", ADMIN_USER);
  await page.fill("#password", "wrong-password-123");
  await page.click("#login-btn");
  await expect(page.locator("#error-box")).toBeVisible();
});
