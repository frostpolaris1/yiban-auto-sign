import { adminTest as test, expect } from "./base";

// 条件请求（工单 lcpx 第 1 步）：分页接口带 ETag；同一 URL 二次读取命中 304 时
// 调用方保留上一次数据，不把空体当错误、也不清空表格。
//
// 断言的是协议不变式：200 → 带 ETag；If-None-Match 命中 → 304；304 对页面无副作用。
// 用未参与首屏查询的 `probe=etag` 让本条自己拿到一次 200、再拿到一次 304，
// 不依赖轮询时序。

test("条件请求：分页二次读取命中 304，页面保留上一次数据", async ({ page }) => {
  await page.goto("/work/accounts");
  await page.locator('[data-acct-tab="active"]').click();
  const rows = page.locator("#accounts-tbody tr");
  await expect(rows.first()).toBeVisible();
  // 分组分页后状态表按组下发：切到「正常账号」必须看到该组自己的签到终态，
  // 不能沿用「待处理」那一次请求的空表。种子 13800138001 今日终态 = failed。
  const userRow = rows.filter({ hasText: "e2e-user-acct" }).first();
  await expect(userRow.locator(".acct-state"), "切页后正常账号的状态表串组").toHaveAttribute(
    "aria-label",
    /签到失败/,
  );

  const statuses: number[] = [];
  page.on("response", (r) => {
    if (r.url().includes("/api/accounts?") && r.url().includes("probe=etag")) {
      statuses.push(r.status());
    }
  });

  const url = "/api/accounts?group=active&limit=20&offset=0&probe=etag";
  const first = await page.evaluate(
    async (u) => await (window as unknown as { YB: { api: (m: string, p: string) => Promise<Record<string, unknown>> } }).YB.api("GET", u),
    url,
  );
  expect(first.notModified, "首次读取必须是 200 实体").not.toBe(true);
  expect(Array.isArray(first.accounts) && (first.accounts as unknown[]).length > 0, "首次读取必须带回行").toBe(true);

  const firstIdx = await rows.first().getAttribute("data-acct-idx");
  const second = await page.evaluate(
    async (u) =>
      await (
        window as unknown as {
          YB: { api: (m: string, p: string, b?: unknown, o?: { conditional?: boolean }) => Promise<Record<string, unknown>> };
        }
      ).YB.api("GET", u, null, { conditional: true }),
    url,
  );
  expect(second.notModified, "If-None-Match 命中必须回 notModified 哨兵").toBe(true);
  expect(statuses, "服务端必须实际回过 304").toContain(304);

  // 304 不清表、不重置到别页：行数与首行下标都不变。
  await expect(rows.first()).toBeVisible();
  expect(await rows.first().getAttribute("data-acct-idx")).toBe(firstIdx);

  // 条件请求必须显式 opt-in：徽标计数（count_only=1）不声明 conditional，
  // 同一 URL 反复读取也只能是 200 实体。否则空体 304 会把徽标读成 0。
  const countStatuses: number[] = [];
  page.on("response", (r) => {
    if (r.url().includes("/api/accounts?count_only=1")) countStatuses.push(r.status());
  });
  const countUrl = "/api/accounts?count_only=1";
  const badge = async (): Promise<number> =>
    await page.evaluate(
      async (u) =>
        await (
          window as unknown as { YB: { api: (m: string, p: string) => Promise<Record<string, unknown>> } }
        ).YB.api("GET", u),
      countUrl,
    );
  const b1 = await badge();
  const b2 = await badge();
  expect(typeof b1.pending, "徽标读数必须是实体标量").toBe("number");
  expect(typeof b2.pending, "二次徽标读数仍必须是实体标量").toBe("number");
  expect(countStatuses.length, "两次计数请求都必须被本条监听到").toBeGreaterThanOrEqual(2);
  expect(countStatuses, "未声明 conditional 的计数请求不得收到 304").not.toContain(304);
});