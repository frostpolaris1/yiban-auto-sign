import { adminTest, expect } from "./base";

// 竞态回归：连续两次检索，慢的旧响应后到，不得覆盖新结果。
// 真实用户动作：在检索框里很快改词；网络抖动时旧请求可能晚回。
adminTest("用户检索：慢的旧响应不得覆盖新结果", async ({ page }) => {
  await page.goto("/work/users");
  await page.locator("#usr-tab-normal").click();
  await expect(page.locator("#usr-normal-tbody tr").first()).toBeVisible();

  let delayed = false;
  await page.route("**/api/users?group=normal*", async (route) => {
    const url = new URL(route.request().url());
    if (!delayed && url.searchParams.get("q") === "e2e-user") {
      delayed = true;
      await new Promise((resolve) => setTimeout(resolve, 900));
    }
    await route.continue();
  });

  const staleRequest = page.waitForRequest((req) => {
    const url = new URL(req.url());
    return url.pathname === "/api/users" && url.searchParams.get("q") === "e2e-user";
  });
  await page.locator("#usr-normal-search").fill("e2e-user");
  await staleRequest;

  const freshResponse = page.waitForResponse((resp) => {
    const url = new URL(resp.url());
    return url.pathname === "/api/users" && url.searchParams.get("q") === "zzz" && resp.status() === 200;
  });
  await page.locator("#usr-normal-search").fill("zzz");
  await freshResponse;

  // 等旧响应真正回来：守卫必须丢弃它，页面仍停在新检索的空结果。
  await page.waitForTimeout(1200);
  await expect(page.locator("#usr-normal-tbody")).not.toContainText("e2e-user");
  await expect(page.locator("#usr-normal-count")).toHaveText("（0 人匹配 / 共 2 人）");
});
