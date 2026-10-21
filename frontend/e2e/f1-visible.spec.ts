import { adminTest as test, expect } from "./base";

// F1 可见缺陷批（用户 2026-10-21 裁决）：
//  1) EP 表头/表体统一 48px 行高；账号表名称格在窄屏多行时留出上下呼吸位；
//  2) 「说明 + 开关」行控件与左侧文案垂直居中；
//  3) 公告不再用横幅，每个新会话首次进入自动弹一次「我知道了」，本会话不再弹；
//  4) 重置密码弹窗补「本站登录口令 ≠ 易班签到凭证」一句。
//
// 公告接口用 page.route 伪造：e2e/server.py 不种公告，避免为了一个前端行为去改
// 全站共享的临时 .env（发布公告还会写审计、发告警，且失败后会污染后续用例）。
const ADMIN_USER = "admin";
const ADMIN_PASS = "TestPass1234!";
const ANNOUNCE_KEY = "yiban-announce-dismissed";

// 会话由 base.ts 的 adminTest 夹具在每条用例内登录（每账号只保留最新 sid）。
test("F1：表头行高 / 账号多行呼吸位 / 开关居中 / 公告每会话一次 / 重置密码口径", async ({ page }) => {

  // ① EP 表：表头与表体同高 48px（当前表头仍是 EP small 的 32px）。
  await page.goto("/data/audit");
  const header = page.locator(".el-table__header th").first();
  const bodyRow = page.locator(".el-table__body tr").first();
  await expect(header).toBeVisible();
  await expect(bodyRow).toBeVisible();
  const headerBox = await header.boundingBox();
  const bodyBox = await bodyRow.boundingBox();
  expect(headerBox?.height, `表头高 ${headerBox?.height}，应为 48`).toBeGreaterThanOrEqual(47);
  expect(headerBox?.height).toBeLessThanOrEqual(49);
  expect(bodyBox?.height, `表体行高 ${bodyBox?.height}，应为 48`).toBeGreaterThanOrEqual(47);
  expect(bodyBox?.height).toBeLessThanOrEqual(49);

  // ② 账号表名称格：窄屏一行变三/四行时，上下各留 8px（当前只剩约 4px）。
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/work/accounts");
  await page.getByRole("tab", { name: /正常账号/ }).click();
  const nameCell = page.locator(".acct-cell-name:visible").filter({ hasText: "138" }).first();
  await expect(nameCell).toBeVisible();
  const pad = await nameCell.evaluate((td) => {
    const cs = getComputedStyle(td);
    return { top: parseFloat(cs.paddingTop), bottom: parseFloat(cs.paddingBottom) };
  });
  expect(pad.top, "名称格上内距").toBeGreaterThanOrEqual(7.5);
  expect(pad.bottom, "名称格下内距").toBeGreaterThanOrEqual(7.5);

  // ③ 开关行：控件中心与左侧文案中心偏差 ≤1.5px（当前单行偏下 6px、两行偏上 5px）。
  await page.setViewportSize({ width: 1280, height: 900 });
  for (const tab of ["schedule", "notify"]) {
    await page.goto(`/work/settings?tab=${tab}`);
    const deltas = await page.evaluate(() =>
      Array.from(document.querySelectorAll(".tab-panel.is-active .set-row")).flatMap((row) => {
        const sw = row.querySelector(".el-switch, .switch");
        const txt = row.querySelector(".set-row-text");
        if (!sw || !txt) return [];
        const sr = sw.getBoundingClientRect();
        const tr = txt.getBoundingClientRect();
        if (!sr.height || !tr.height) return [];
        return [{
          label: (txt.textContent || "").trim().slice(0, 24),
          delta: Math.round(((sr.y + sr.height / 2) - (tr.y + tr.height / 2)) * 10) / 10,
        }];
      }),
    );
    expect(deltas.length, `${tab} 页未找到开关行`).toBeGreaterThan(0);
    const bad = deltas.filter((d) => Math.abs(d.delta) > 1.5);
    expect(bad, `${tab} 页开关未与文案居中：${JSON.stringify(bad)}`).toEqual([]);
  }

  // ④ 公告：整页加载后自动弹一次；点「我知道了」；重载不再弹；换文本再弹。
  //    整页重载会重建 page 上下文里的 sessionStorage 读取路径，但不能清掉它，
  //    这正是"同一会话"的判据。
  let announceText = "e2e 公告：请在本周五前完成信息核对。";
  // 前面几个页面已经把空公告写进 apiCached（60s TTL）；不清掉就会命中缓存，
  // 永远看不到 route 伪造的文本。只清这一条，保留同一会话里的其它状态。
  await page.evaluate(() => sessionStorage.removeItem("yiban-cache:announcement"));
  await page.route("**/api/announcement", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, text: announceText }),
    });
  });
  await page.goto("/data/dashboard");
  const dialog = page.locator(".pm-panel", { hasText: "公告" });
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(".pm-announce")).toHaveText(announceText);
  await expect(dialog.getByRole("button", { name: "我知道了" })).toBeVisible();
  await page.getByRole("button", { name: "我知道了" }).click();
  await expect(dialog).toBeHidden();
  expect(await page.evaluate((k) => sessionStorage.getItem(k), ANNOUNCE_KEY)).toBe(announceText);
  // 右上角铃铛仍在（公告只收回入口，不删入口）。
  await expect(page.locator("#announcementBtn")).toBeVisible();

  await page.reload();
  await expect(page.locator(".pm-panel", { hasText: "公告" })).toHaveCount(0);
  // 换公告文本：同一会话也应重新弹（避免"永久静音"掉新通知）。
  announceText = "e2e 公告：签到时间临时调整。";
  await page.evaluate(() => sessionStorage.removeItem("yiban-cache:announcement"));
  await page.reload();
  await expect(page.locator(".pm-panel", { hasText: "公告" })).toBeVisible();
  await expect(page.locator(".pm-announce")).toHaveText(announceText);

  // 明确要求：不留横幅。管理端与登录页都不应有 [data-announcement-bar]。
  expect(await page.locator("[data-announcement-bar]").count()).toBe(0);
  // 登录页不弹公告（未登录页没有通知中心，也不该被公告弹窗打断登录）。
  // 显式清空 storageState：project 默认注入的管理员 Cookie 会让 /login 直接跳走，
  // 这里要的是真正的匿名登录页。
  const authCtx = await page.context().browser()!.newContext({
    viewport: { width: 1280, height: 900 },
    storageState: { cookies: [], origins: [] },
  });
  const authPage = await authCtx.newPage();
  await authPage.route("**/api/announcement", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, text: "登录页不应弹的公告" }),
    });
  });
  await authPage.goto("/login");
  await expect(authPage.locator(".pm-panel", { hasText: "公告" })).toHaveCount(0);
  expect(await authPage.locator("[data-announcement-bar]").count()).toBe(0);
  await authCtx.close();

  // ⑤ 重置密码弹窗：必须说清改的是本站登录口令，不是易班签到凭证。
  await page.goto("/work/users?tab=normal");
  const row = page.locator('tbody tr:not(.skel-row):not(.usr-row-master)').first();
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: /更多操作/ }).click();
  await page.locator(".el-dropdown-menu:visible").getByText("重置密码").click();
  const pwModal = page.locator(".pm-panel", { hasText: "重置密码" });
  await expect(pwModal).toBeVisible();
  await expect(pwModal.locator(".pm-confirm-text")).toContainText("不是易班签到凭证");
});
