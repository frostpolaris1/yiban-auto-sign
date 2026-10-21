import { test as base, expect, type Page } from "@playwright/test";

// 每个用例用**自己的**会话登录，不共享 Cookie 文件。
//
// 为什么不能整套只登录一次：内置管理员的登录会换发 YIBAN_ADMIN_SID（web/security.py
// _issue_admin_sid），注册用户登录会换发 users.sid——同一个账号第二次登录会把上一次的
// Cookie 立刻作废。任何一条用例再登录同一账号，其它用例的 storageState 就整体失效
// （实测：整套 12 failed，页面全被踢回 /login）。
//
// 登录不计入被审查的页面行为；登录限速由 pytest 覆盖，e2e/server.py 在本临时实例内
// 抬高窗口上限，避免 20+ 次登录撞 60 秒 10 次/IP。

const ADMIN = { username: "admin", password: "TestPass1234!" };
const USER = { username: "e2e-user@example.com", password: "UserPass123!" };

async function loginAs(page: Page, who: { username: string; password: string }): Promise<void> {
  const resp = await page.request.post("/api/login", { data: who });
  if (!resp.ok()) {
    throw new Error("e2e 预登录失败: " + who.username + " -> HTTP " + resp.status());
  }
}

/** 管理端用例：每条用例新建会话并以管理员登录。 */
export const adminTest = base.extend({
  page: async ({ page }, use) => {
    await loginAs(page, ADMIN);
    await use(page);
  },
});

/** 用户端用例：每条用例新建会话并以普通用户登录。 */
export const userTest = base.extend({
  page: async ({ page }, use) => {
    await loginAs(page, USER);
    await use(page);
  },
});

export { expect };
