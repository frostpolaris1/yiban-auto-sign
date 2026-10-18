import { expect, test } from "@playwright/test";

// 窄屏表单与审计列表的结构护栏。
//
// 这两条测的是"任何实现下都不该被违反"的性质，不是某一串调用：
// 1. 账号表单弹窗必须完整落在视口内，且 footer（取消/保存）在视口内可见——
//    560px 固定宽时代实测 390 视口下弹窗右缘越界、按钮被裁。
// 2. 审计记录在窄屏必须用不依赖横向滚动的形态，且时间/动作/对象/详情都能读到——
//    900px 固定列宽表格在 390 只能看到前两列，且 hover tooltip 在触屏不可达。
const ADMIN_USER = "admin";
const ADMIN_PASS = "TestPass1234!";

test("窄屏：账号表单弹窗不溢出，footer 常驻视口", async ({ page }) => {
  const resp = await page.request.post("/api/login", {
    data: { username: ADMIN_USER, password: ADMIN_PASS },
  });
  expect(resp.ok()).toBeTruthy();

  for (const width of [390, 360]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/work/accounts");
    await page.locator("[data-add-account]").first().click();

    const dialog = page.locator(".app-form-dialog");
    await expect(dialog).toBeVisible();
    const box = await dialog.boundingBox();
    expect(box, `${width}px 取不到弹窗位置`).not.toBeNull();
    expect(box!.x, `${width}px 弹窗左缘越界`).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width, `${width}px 弹窗右缘越界`).toBeLessThanOrEqual(width + 1);

    const footer = dialog.locator(".el-dialog__footer");
    await expect(footer).toBeVisible();
    const fb = await footer.boundingBox();
    expect(fb, `${width}px 取不到 footer 位置`).not.toBeNull();
    expect(fb!.y + fb!.height, `${width}px footer 落在视口外`).toBeLessThanOrEqual(844 + 1);

    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  }
});

test("窄屏：审计记录用卡片列表，关键字段不依赖横向滚动", async ({ page }) => {
  const resp = await page.request.post("/api/login", {
    data: { username: ADMIN_USER, password: ADMIN_PASS },
  });
  expect(resp.ok()).toBeTruthy();

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/data/audit");

  await expect(page.locator(".audit-cards")).toBeVisible();
  await expect(page.locator(".audit-table-wrap")).toBeHidden();
  const card = page.locator(".audit-card").first();
  await expect(card).toBeVisible();
  await expect(card.locator(".audit-card-time")).not.toBeEmpty();
  await expect(card.locator(".audit-card-action")).not.toBeEmpty();
  await expect(card.locator(".audit-card-line").first()).not.toBeEmpty();

  const over = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(over, "审计页在 390px 出现横向滚动").toBeLessThanOrEqual(1);
});

test("竖屏 dashboard：KPI 一行两个，首图不被推出首屏", async ({ page }) => {
  const resp = await page.request.post("/api/login", {
    data: { username: ADMIN_USER, password: ADMIN_PASS },
  });
  expect(resp.ok()).toBeTruthy();

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/data/dashboard");

  const grid = page.locator(".kpi-grid");
  await expect(grid).toBeVisible();
  const cols = await grid.evaluate(
    (el) => getComputedStyle(el).gridTemplateColumns.trim().split(/\s+/).length,
  );
  expect(cols, "390px KPI 不是两列").toBe(2);

  const boxes = await Promise.all(
    [0, 1, 2, 3].map((i) => page.locator(".kpi-card").nth(i).boundingBox()),
  );
  expect(boxes.every(Boolean)).toBe(true);
  expect(Math.abs(boxes[0]!.y - boxes[1]!.y), "第一行两张卡不在同一行").toBeLessThanOrEqual(1);
  expect(Math.abs(boxes[2]!.y - boxes[3]!.y), "第二行两张卡不在同一行").toBeLessThanOrEqual(1);
  expect(boxes[2]!.y, "第三张卡没有落到第二行").toBeGreaterThan(boxes[0]!.y + 1);

  const firstChart = await page.locator(".grid").first().boundingBox();
  expect(firstChart, "取不到第一张图的位置").not.toBeNull();
  expect(firstChart!.y, "首图仍被 KPI 推到 700px 之后").toBeLessThanOrEqual(700);

  const over = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(over, "看板在 390px 出现横向滚动").toBeLessThanOrEqual(1);
});

test("平板图标栏：待处理徽标、服务器时钟与分组分隔线仍可见", async ({ page }) => {
  const resp = await page.request.post("/api/login", {
    data: { username: ADMIN_USER, password: ADMIN_PASS },
  });
  expect(resp.ok()).toBeTruthy();

  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto("/data/dashboard");

  const badge = page.locator('[data-nav-badge="work-accounts"]');
  await expect(badge).toBeVisible();
  await expect(badge).toHaveText("2");

  const clock = page.locator("#sidebar-clock");
  await expect(clock).toBeVisible();
  await expect(clock.locator("[data-clock-text]")).toHaveText(/\d{2}:\d{2}:\d{2}/);
  const shownClock = await clock
    .locator("[data-clock-text]")
    .evaluate((el) => getComputedStyle(el, "::after").content);
  expect(shownClock, "图标栏时钟只显示图标、没有时分秒").toMatch(/\d{2}:\d{2}:\d{2}/);

  const sep = await page
    .locator(".nav-section + .nav-section")
    .first()
    .evaluate((el) => getComputedStyle(el, "::before").width);
  expect(parseFloat(sep), "图标栏缺少分组分隔线").toBeGreaterThan(0);
});

test("自研页签：切换后滑动指示条与 active 页签对齐", async ({ page }) => {
  const resp = await page.request.post("/api/login", {
    data: { username: ADMIN_USER, password: ADMIN_PASS },
  });
  expect(resp.ok()).toBeTruthy();

  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto("/work/accounts");
  await page.locator('[data-acct-tab="active"]').click();

  const indicator = page.locator(".tabs-indicator");
  await expect
    .poll(() => indicator.evaluate((el) => getComputedStyle(el).opacity))
    .toBe("1");
  // 指示条有 200ms 的滑动过渡；等它落到 active 页签上再量几何。
  await expect
    .poll(async () => {
      const ind = await indicator.boundingBox();
      const act = await page.locator(".tabs--indicator .tab.is-active").boundingBox();
      if (!ind || !act) return Number.POSITIVE_INFINITY;
      return Math.max(Math.abs(ind.x - act.x), Math.abs(ind.width - act.width));
    })
    .toBeLessThanOrEqual(1);



});

test("竖屏签到日志：页签不裁切、控件铺满、事件/巡检表改卡片", async ({ page }) => {
  const resp = await page.request.post("/api/login", {
    data: { username: ADMIN_USER, password: ADMIN_PASS },
  });
  expect(resp.ok()).toBeTruthy();

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/data/logs");

  const tabs = page.locator(".el-tabs__item");
  await expect(tabs).toHaveCount(4);
  const lastTab = await tabs.nth(3).boundingBox();
  expect(lastTab, "取不到第 4 个页签位置").not.toBeNull();
  expect(lastTab!.x + lastTab!.width, "第 4 个页签被裁在视口外").toBeLessThanOrEqual(390 + 1);

  const card = await page.locator(".logs .card").first().boundingBox();
  const date = await page.locator(".logs-date").boundingBox();
  expect(card && date).toBeTruthy();
  expect(date!.width, "日期框没有铺满卡片内容宽").toBeGreaterThan(card!.width - 80);

  const over = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(over, "日志页在 390px 出现横向滚动").toBeLessThanOrEqual(1);

  await page.getByRole("tab", { name: /签到事件/ }).click();
  await expect(page.locator(".logs-table-wrap").first()).toBeHidden();
  const cards = page.locator(".el-tab-pane:visible .logs-event-cards");
  await expect(cards).toBeVisible();
  const event = cards.locator(".logs-event-card").first();
  await expect(event.locator(".logs-event-time")).not.toBeEmpty();
  await expect(event.locator(".logs-event-phone")).not.toBeEmpty();
  await expect(event.locator(".logs-event-msg")).not.toBeEmpty();

  await page.getByRole("tab", { name: "运行巡检" }).click();
  await expect(page.locator("#run-summary .run-table-wrap")).toBeHidden();
  await expect(page.locator("#run-summary .run-mobile-cards")).toBeVisible();
  await expect(page.locator("#run-timeline .run-mobile-cards")).toBeVisible();
});

test("竖屏账号表：无横向滚动，手机号与审核状态行内可见", async ({ page }) => {
  const resp = await page.request.post("/api/login", {
    data: { username: ADMIN_USER, password: ADMIN_PASS },
  });
  expect(resp.ok()).toBeTruthy();

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/work/accounts");
  await page.locator('[data-acct-tab="pending"]').click();

  const over = await page.locator("#acct-panel-pending .acct-table").evaluate(
    (el) => el.scrollWidth - el.clientWidth,
  );
  expect(over, "账号表在 390px 仍需横向滚动").toBeLessThanOrEqual(1);
  await expect(page.locator(".acct-phone-inline").first()).toBeVisible();
  await expect(page.locator(".acct-audit-inline").first()).toBeVisible();
  await expect(page.locator(".acct-cell-phone").first()).toBeHidden();
});
