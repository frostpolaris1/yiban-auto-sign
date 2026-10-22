import { adminTest as test, expect } from "./base";

// 账号列表 5k 规模的分页验收（工单 p37f）：首屏 DOM 受控、翻页不整页重拉、徽标走 count_only。
//
// 规模数据由 e2e/server.py 按 `YB_E2E_BULK_ACCOUNTS` 灌入（默认 0 ⇒ 本用例 skip，其余 spec 不受影响）。
// 跑法（Windows，需显式给解释器）：
//   YB_E2E_PYTHON=D:/code/yiban-auto-sign/.venv/Scripts/python.exe \
//   YB_E2E_BULK_ACCOUNTS=5000 npx playwright test e2e/paging.spec.ts
//
// 断言的是**不变式**：DOM 节点数与页大小同阶（不随 5k 线性增长）；5k 下响应仍是单页大小
// （服务端分组分页，limit=20）；翻页只重拉目标页，不重拉整表；徽标只取计数。
// 不钉某段调用顺序。
//
// 注：5k 下服务端只取未解密快照做分组/统计，当前页再解密；首屏响应应在秒级。
// 仍显式等待首屏分页响应（超时放宽），不靠隐式 10s。

const ADMIN_USER = "admin";
const ADMIN_PASS = "TestPass1234!";
const BULK = Number(process.env.YB_E2E_BULK_ACCOUNTS ?? 0);

test("账号列表 5k：首屏 DOM 受控、翻页不整页重拉、徽标走 count_only", async ({ page }) => {
  test.skip(BULK < 1000, "需 YB_E2E_BULK_ACCOUNTS>=1000：本用例量的是 5k 规模");
  test.setTimeout(180_000);

  // 管理员会话由 admin project 注入（.auth/admin.json）。
  // 只用**响应监听**量字节（不改写响应体：route.fulfill 会破坏 gzip/内容长度，页面解析失败）。
  const reqs: { url: string; bytes: number }[] = [];
  const pending: Promise<void>[] = [];
  page.on("response", (r) => {
    const u = r.url();
    if (u.includes("/api/accounts")) {
      pending.push(
        r.body().then((b) => void reqs.push({ url: u, bytes: b.length })).catch(() => undefined),
      );
    }
  });

  const badgeResp = page.waitForResponse(
    (r) => r.url().includes("/api/accounts") && r.url().includes("count_only=1"),
    { timeout: 120_000 },
  );
  const firstPage = page.waitForResponse(
    (r) => r.url().includes("/api/accounts") && !r.url().includes("count_only=1"),
    { timeout: 120_000 },
  );
  const activeFirst = page.waitForResponse(
    (r) => r.url().includes("/api/accounts") && r.url().includes("group=active")
      && r.url().includes("offset=0"),
    { timeout: 120_000 },
  );
  await page.goto("/work/accounts");
  const firstResp = await firstPage;
  const badgeBytes = (await (await badgeResp).body()).length;
  const firstBytes = (await firstResp.body()).length;
  const firstUrl = new URL(firstResp.url());
  expect(firstUrl.searchParams.get("limit"), "首屏必须带服务端分页上限").toBe("20");
  expect(firstBytes, "5k 下首屏分页响应必须 ≤400KiB").toBeLessThan(400 * 1024);

  await page.locator('[data-acct-tab="active"]').click();
  const activeResp = await activeFirst;
  const activeBytes = (await activeResp.body()).length;
  expect(new URL(activeResp.url()).searchParams.get("limit"), "active 首屏也要带分页上限").toBe("20");
  expect(activeBytes, "5k 下 active 首屏分页响应必须 ≤400KiB").toBeLessThan(400 * 1024);
  const rows = page.locator("#accounts-tbody tr");
  await expect(rows).toHaveCount(20, { timeout: 30_000 });

  // ① 首屏 DOM 与页大小同阶：5k 账号下不许出现 11 万节点。
  const nodes = await page.evaluate(() => document.querySelectorAll("*").length);
  expect(nodes, `5k 行下首屏 DOM 节点数=${nodes}，必须与页大小同阶`).toBeLessThan(4000);

  await Promise.all(pending);
  const pages = reqs.filter((r) => !r.url.includes("count_only=1"));
  expect(pages.length, "首屏必须走服务端分页").toBeGreaterThan(0);

  // ② 徽标只取计数：count_only 响应远小于分页响应。
  expect(badgeBytes, "导航徽标必须走 count_only 通路（工单 p37f）").toBeGreaterThan(0);
  expect(badgeBytes, "count_only 响应必须是计数级（<2KiB）").toBeLessThan(2000);
  expect(badgeBytes).toBeLessThan(pages[0].bytes);
  console.log(`[p37f] 首屏分页字节=${firstBytes} 徽标字节=${badgeBytes} 首屏DOM=${nodes}`);

  // ③ 翻页只重拉目标页：点下一页恰好请求 offset=20&limit=20，不重拉整表。
  const firstIdxPage1 = await rows.first().getAttribute("data-acct-idx");
  const nextPage = page.waitForResponse((r) => {
    const u = new URL(r.url());
    return u.pathname.includes("/api/accounts") && u.searchParams.get("offset") === "20";
  });
  await page.locator('[data-acct-pager="active"] .el-pager li').nth(1).click();
  const nextResp = await nextPage;
  await expect(rows).toHaveCount(20);
  const firstIdxPage2 = await rows.first().getAttribute("data-acct-idx");
  expect(firstIdxPage2, "第 2 页首行必须不同于第 1 页（真翻了页）").not.toBe(firstIdxPage1);
  const nextUrl = new URL(nextResp.url());
  expect(nextUrl.searchParams.get("limit"), "翻页请求必须带单页上限").toBe("20");
  expect(nextUrl.searchParams.get("offset"), "翻页请求必须指向目标页").toBe("20");
  expect((await nextResp.body()).length, "翻页响应不得超过单页上限").toBeLessThan(400 * 1024);
  expect(
    pages.every((r) => new URL(r.url).searchParams.get("limit") === "20"),
    "5k 规模下不得出现无分页上限的整表请求",
  ).toBe(true);

  const nodes2 = await page.evaluate(() => document.querySelectorAll("*").length);
  expect(nodes2).toBeLessThan(4000);
});

// 翻页竞态：先点第 2 页（人为延迟），再点第 3 页。旧页响应后到时不得覆盖新页。
// 这条钉的是组件内的 per-group 请求序号；检索竞态由 loadToken 在 race.spec.ts 钉。
test("账号翻页：慢的旧页响应不得覆盖新页", async ({ page }) => {
  test.skip(BULK < 1000, "需 YB_E2E_BULK_ACCOUNTS>=1000：要有多页才触发翻页竞态");
  test.setTimeout(120_000);

  await page.goto("/work/accounts");
  await page.locator('[data-acct-tab="active"]').click();
  const rows = page.locator("#accounts-tbody tr");
  await expect(rows).toHaveCount(20, { timeout: 30_000 });

  let delayed = false;
  await page.route("**/api/accounts?*", async (route) => {
    const url = new URL(route.request().url());
    if (!delayed && url.searchParams.get("group") === "active" && url.searchParams.get("offset") === "20") {
      delayed = true;
      await new Promise((resolve) => setTimeout(resolve, 900));
    }
    await route.continue();
  });

  const pager = page.locator('[data-acct-pager="active"] .el-pager li');
  const slowPage2 = page.waitForRequest((req) => {
    const url = new URL(req.url());
    return url.pathname === "/api/accounts" && url.searchParams.get("offset") === "20";
  });
  await pager.nth(1).click();
  await slowPage2;

  const page3 = page.waitForResponse((resp) => {
    const url = new URL(resp.url());
    return url.pathname === "/api/accounts" && url.searchParams.get("offset") === "40" && resp.status() === 200;
  });
  await pager.nth(2).click();
  await page3;

  // 等旧页响应真正回来：守卫必须丢弃它，页面仍停在第 3 页。
  await page.waitForTimeout(1200);
  await expect(page.locator('[data-acct-pager="active"] .el-pager li.is-active')).toHaveText("3");
  const firstIdx = await rows.first().getAttribute("data-acct-idx");
  expect(Number(firstIdx), "第 3 页首行索引必须 ≥40；若被慢的第 2 页覆盖会回到 20").toBeGreaterThanOrEqual(40);
});