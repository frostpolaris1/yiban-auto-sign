import { adminTest as test, expect } from "./base";

// 窄屏表单与审计列表的结构护栏。
//
// 这两条测的是"任何实现下都不该被违反"的性质，不是某一串调用：
// 1. 账号表单弹窗必须完整落在视口内，且 footer（取消/保存）在视口内可见——
//    560px 固定宽时代实测 390 视口下弹窗右缘越界、按钮被裁。
// 2. 审计记录在窄屏必须用不依赖横向滚动的形态，且时间/动作/对象/详情都能读到——
//    900px 固定列宽表格在 390 只能看到前两列，且 hover tooltip 在触屏不可达。
const ADMIN_USER = "admin";
const ADMIN_PASS = "TestPass1234!";

// 本文件 7 条管理员用例的会话由 playwright.config.ts 的 admin project 注入（.auth/admin.json）。
// 用户端那条用例仍自己登录一次（它需要换身份）。

test("窄屏：账号表单弹窗不溢出，审计记录用卡片列表", async ({ page }) => {
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

  // 同一条用例顺带测审计卡片：两者都是窄屏形态护栏，合并可省一次 API 登录。
  // e2e 的登录限速按客户端 IP 计（60 秒 10 次），整套的登录次数是要省着用的资源。
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

  // 工具条单排：检索框与「显示全部日志 / 导出日志」同一行；按钮只剩图标，全名在 aria-label。
  const actions = page.locator(".logs-head--log .logs-actions > *");
  await expect(actions).toHaveCount(2);
  const searchBox = await page.locator("#logs-search-input").boundingBox();
  expect(searchBox, "取不到检索输入框位置").not.toBeNull();
  for (const box of await actions.all()) {
    const b = await box.boundingBox();
    expect(b, "取不到工具按钮位置").not.toBeNull();
    expect(Math.abs(b!.y - searchBox!.y), "工具按钮掉到第二排").toBeLessThanOrEqual(2);
    expect(await box.getAttribute("aria-label"), "图标按钮缺少全名").toBeTruthy();
  }
  const toolsH = await page
    .locator(".logs-head--log .logs-tools")
    .evaluate((el) => el.getBoundingClientRect().height);
  expect(toolsH, "工具条仍占两排高度").toBeLessThanOrEqual(40);
  await expect(page.locator(".logs-head--log .logs-act-text").first()).toBeHidden();

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

// ---------------------------------------------------------------------------
// 同排控件同高（形状守卫）
//
// 用户点名过两次"各处控件高度不一致"（工具条输入框/日期框 40 与 32 参差、日期框与文本框
// 不同高）。这里钉的不是某页某控件，而是那条**任何实现下都不该被违反**的性质：
// **同一视觉行内的可见控件，高度差不得超过 2px**。做法是把可见控件按中心 y 聚类成行，
// 再逐行比最大最小高——换实现、加页面都不会让它假红，而"某处悄悄长出一档高度"会。
//
// 排除面：`/work/pilot` 是"旧栈 .btn vs EP el-button"对照页，同排混放两套按钮是它的**目的**，
// 不是参差。除它之外，全站页面都在覆盖内。
// ---------------------------------------------------------------------------
const ROW_PAGES_ADMIN = [
  "/data/dashboard",
  "/data/audit",
  "/data/logs",
  "/work/accounts",
  "/work/users",
  "/work/settings",
];
const ROW_PAGES_USER = ["/my/calendar", "/my/account"];

// 只取**控件本体**，不取 EP 的内部 input（.el-input__inner / .el-select__input /
// .el-range-input 都是 wrapper 的子孙，高度天然小于 wrapper，混进来会造出假参差）。
const CONTROL_SEL = [
  "button.btn",
  "button.el-button",
  ".el-select__wrapper",
  ".el-date-editor",
  "input.input",
].join(",");

type RowMismatch = { y: number; min: number; max: number; what: string };

async function rowMismatches(page: import("@playwright/test").Page): Promise<RowMismatch[]> {
  return page.evaluate((sel) => {
    const items: { y: number; h: number; what: string }[] = [];
    for (const el of Array.from(document.querySelectorAll<HTMLElement>(sel))) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === "hidden" || cs.display === "none") continue;
      const what = (el.getAttribute("aria-label") || el.textContent || el.className || "")
        .toString()
        .trim()
        .slice(0, 16);
      items.push({ y: Math.round(r.y + r.height / 2), h: Math.round(r.height * 10) / 10, what });
    }
    items.sort((a, b) => a.y - b.y);
    const rows: { y: number; items: { y: number; h: number; what: string }[] }[] = [];
    for (const it of items) {
      const last = rows[rows.length - 1];
      if (last && Math.abs(last.y - it.y) <= 3) last.items.push(it);
      else rows.push({ y: it.y, items: [it] });
    }
    return rows
      .map((row) => {
        const hs = row.items.map((i) => i.h);
        const min = Math.min(...hs);
        const max = Math.max(...hs);
        return {
          y: row.y,
          min,
          max,
          what: row.items.map((i) => `${i.h}:${i.what}`).join(" | "),
        };
      })
      .filter((row) => row.max - row.min > 2);
  }, CONTROL_SEL);
}

test("管理端：各页工具行内控件同高（高度差 ≤2px）", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  for (const path of ROW_PAGES_ADMIN) {
    await page.goto(path);
    const bad = await rowMismatches(page);
    expect(bad, `${path} 同排控件参差：\n${bad.map((b) => `  y=${b.y} ${b.min}..${b.max}  ${b.what}`).join("\n")}`)
      .toEqual([]);
  }
});

test("用户端：各页工具行内控件同高（高度差 ≤2px）", async ({ page }) => {
  const resp = await page.request.post("/api/login", {
    data: { username: "e2e-user@example.com", password: "UserPass123!" },
  });
  expect(resp.ok()).toBeTruthy();

  await page.setViewportSize({ width: 1440, height: 900 });
  for (const path of ROW_PAGES_USER) {
    await page.goto(path);
    const bad = await rowMismatches(page);
    expect(bad, `${path} 同排控件参差：\n${bad.map((b) => `  y=${b.y} ${b.min}..${b.max}  ${b.what}`).join("\n")}`)
      .toEqual([]);
  }
});
