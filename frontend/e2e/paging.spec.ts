import { adminTest as test, expect } from "./base";

// 账号列表 5k 规模的分页验收（工单 p37f）：首屏 DOM 受控、翻页不整页重拉、徽标走 count_only。
//
// 规模数据由 e2e/server.py 按 `YB_E2E_BULK_ACCOUNTS` 灌入（默认 0 ⇒ 本用例 skip，其余 spec 不受影响）。
// 跑法（Windows，需显式给解释器）：
//   YB_E2E_PYTHON=D:/code/yiban-auto-sign/.venv/Scripts/python.exe \
//   YB_E2E_BULK_ACCOUNTS=5000 npx playwright test e2e/paging.spec.ts
//
// 断言的是**不变式**：DOM 节点数与页大小同阶（不随 5k 线性增长）；翻页是客户端切片，
// 不发整表请求；徽标只取计数。不钉某段调用顺序。
//
// 注：5k 下 `/api/accounts` 服务端派生（状态/计划/上次执行体）耗时数秒且与其它请求串行，
// 故本用例显式等待首个整表响应（超时放宽），不靠隐式 10s。

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

  const firstFull = page.waitForResponse(
    (r) => r.url().includes("/api/accounts") && !r.url().includes("count_only=1"),
    { timeout: 120_000 },
  );
  await page.goto("/work/accounts");
  await firstFull;

  await page.locator('[data-acct-tab="active"]').click();
  const rows = page.locator("#accounts-tbody tr");
  await expect(rows).toHaveCount(20, { timeout: 30_000 });

  // ① 首屏 DOM 与页大小同阶：5k 账号下不许出现 11 万节点。
  const nodes = await page.evaluate(() => document.querySelectorAll("*").length);
  expect(nodes, `5k 行下首屏 DOM 节点数=${nodes}，必须与页大小同阶`).toBeLessThan(4000);

  await Promise.all(pending);
  const full = reqs.filter((r) => !r.url.includes("count_only=1"));
  expect(full.length, "首屏必须真拉过一次整表（客户端分组需要它）").toBeGreaterThan(0);

  // ② 徽标只取计数：count_only 响应远小于整表。
  const badges = reqs.filter((r) => r.url.includes("count_only=1"));
  expect(badges.length, "导航徽标必须走 count_only 通路（工单 p37f）").toBeGreaterThan(0);
  expect(badges[0].bytes, "count_only 响应必须是计数级（<2KiB）").toBeLessThan(2000);
  expect(badges[0].bytes).toBeLessThan(full[0].bytes);
  console.log(`[p37f] 整表字节=${full[0].bytes} 徽标字节=${badges[0].bytes} 首屏DOM=${nodes}`);

  // ③ 翻页不整页重拉：客户端分页，点下一页不发 /api/accounts 请求。
  const firstIdxPage1 = await rows.first().getAttribute("data-acct-idx");
  const before = reqs.length;
  await page.locator('[data-acct-pager="active"] .el-pager li').nth(1).click();
  await expect(rows).toHaveCount(20);
  const firstIdxPage2 = await rows.first().getAttribute("data-acct-idx");
  expect(firstIdxPage2, "第 2 页首行必须不同于第 1 页（真翻了页）").not.toBe(firstIdxPage1);
  expect(reqs.length, "翻页不得重拉整表（0 字节网络）").toBe(before);

  const nodes2 = await page.evaluate(() => document.querySelectorAll("*").length);
  expect(nodes2).toBeLessThan(4000);
});
