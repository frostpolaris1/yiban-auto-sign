import { expect, test } from "@playwright/test";

// F4 首次启动引导（真实用户路径）：
//   空 .env（无 YIBAN_ADMIN_*）→ 首次打开站点 → 自动进 /setup → 填表创建 →
//   落到管理端首页 → 退出 → 用刚创建的管理员登录。
//   已配置管理员的实例 → /setup 与 /api/setup/admin 都必须关闭。
//
// 空实例由 e2e/setup_server.py 另起 127.0.0.1:8766（见 playwright.config.ts 的
// 第二个 webServer）：主实例必须预置管理员，两种状态不能共用一个进程。

const SETUP = "http://127.0.0.1:8766";
const CONFIGURED = "http://127.0.0.1:8765";

test.describe.serial("F4 首次启动引导", () => {
  test("空实例：弱口令被服务端拒绝", async ({ request }) => {
    const resp = await request.post(`${SETUP}/api/setup/admin`, {
      data: { username: "admin", password: "short" },
    });
    expect(resp.status(), "弱口令必须 400").toBe(400);
    const body = await resp.json();
    expect(body.error).toContain("至少 12 位");
  });

  test("空实例：首次打开自动进 /setup，创建后落管理端，退出后可用新口令登录", async ({ page }) => {
    await page.goto(SETUP + "/");
    await expect(page).toHaveURL(/\/setup$/);
    await expect(page.locator("#setup-title")).toContainText("创建管理员");

    // 客户端先校验两次输入一致；这里测真实表单路径，不绕过页面。
    await page.fill("#setup-username", "admin");
    await page.fill("#setup-password", "SetupPass123!");
    await page.fill("#setup-password2", "SetupPass999!");
    await page.click("#setup-btn");
    await expect(page.locator("#setup-error")).toContainText("两次输入的密码不一致");

    await page.fill("#setup-password2", "SetupPass123!");
    await page.click("#setup-btn");
    await page.waitForURL(/\/data\/dashboard$/);
    await expect(page.locator("h1.page-title")).toHaveText("数据总览");

    // 新建一个干净浏览器上下文（而不是 clearCookies）回到未登录态：证明落盘的是
    // 可验证的 scrypt 哈希，另一台设备/另一个浏览器也能用刚创建的口令登录。
    // 不用 /api/logout：该接口要 CSRF 头，page.request 没有；退出路径另有 e2e 覆盖。
    const fresh = await page.context().browser()!.newContext();
    try {
      const p2 = await fresh.newPage();
      await p2.goto(SETUP + "/login");
      await p2.fill("#username", "admin");
      await p2.fill("#password", "SetupPass123!");
      await p2.click("#login-btn");
      await p2.waitForURL(/\/data\/dashboard$/);
      await expect(p2.locator("h1.page-title")).toHaveText("数据总览");
    } finally {
      await fresh.close();
    }
  });

  test("已配置实例：/setup 转登录页，创建接口拒绝", async ({ page, request }) => {
    await page.goto(CONFIGURED + "/setup");
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.locator("#login-form")).toBeVisible();

    const resp = await request.post(`${CONFIGURED}/api/setup/admin`, {
      data: { username: "attacker", password: "AttackerPass123!" },
    });
    expect(resp.status(), "已配置后创建接口必须关闭").toBe(409);
    const body = await resp.json();
    expect(body.error).toContain("已配置");
  });
});