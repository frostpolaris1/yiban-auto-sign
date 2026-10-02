import { expect, test } from "@playwright/test";

// 签到日历页真浏览器端到端（P2 日历对迁移后）。
// 数据由 e2e/server.py 预置：普通用户 e2e-user@example.com 名下有一个生效账号
// （13800138001），另有一个"取样工作日"带 ✅ 状态与当天日志（见 _probe_day）。
// 会话走 API 登录（表单登录由 logs.spec 覆盖一次即可，避免反复访问 /login 撞登录循环守卫）。
//
// 断言刻意**不**依赖账号名称：myaccounts.spec 会把账号改名，而 spec 文件顺序不是契约。
// 日期一律按"运行时当天"推导（今天/当月天数/取样日由服务端同一规则选出），故整周任意一天
// 跑都成立——只有"今天恰好是周末"这一分支要单独处理（停签日不查日志）。
const USER_EMAIL = "e2e-user@example.com";
const USER_PASS = "UserPass123!";

async function loginUser(page: import("@playwright/test").Page): Promise<void> {
  const resp = await page.request.post("/api/login", { data: { username: USER_EMAIL, password: USER_PASS } });
  expect(resp.ok()).toBeTruthy();
}

/** 与服务端 `_probe_day()` 同规则：当月第一个"非今天、非三天前"的工作日。 */
async function probeDay(page: import("@playwright/test").Page): Promise<string> {
  return page.evaluate(() => {
    const pad = (n: number) => String(n).padStart(2, "0");
    const fmt = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const today = new Date();
    const older = new Date(today.getTime() - 3 * 86400000);
    const days = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
    for (let d = 1; d <= days; d++) {
      const cand = new Date(today.getFullYear(), today.getMonth(), d);
      const wd = cand.getDay();
      if (wd >= 1 && wd <= 5 && fmt(cand) !== fmt(today) && fmt(cand) !== fmt(older)) return fmt(cand);
    }
    return "";
  });
}

test("签到日历：月历骨架/今日选中/语气档底色/图例收敛，且无 legacy 日历脚本", async ({ page }) => {
  await loginUser(page);
  await page.goto("/user/calendar");

  // 卡头：账号手机号（用户看自己的账号按既有设计显示完整号）
  await expect(page.locator(".cal-card").first().locator(".panel-sub").first()).toContainText("13800138001");

  // 固定 6 行 42 格：空位格 + 当月日期格恒为 42
  const cells = page.locator(".sc-grid button[data-sc-date]");
  const blanks = page.locator(".sc-grid .sc-blank");
  const daysInMonth = await page.evaluate(
    () => new Date(new Date().getFullYear(), new Date().getMonth() + 1, 0).getDate(),
  );
  await expect(cells).toHaveCount(daysInMonth);
  await expect(blanks).toHaveCount(42 - daysInMonth);

  // 月份标题 = 当月
  const monthLabel = await page.evaluate(() => {
    const d = new Date();
    return `${d.getFullYear()}年${d.getMonth() + 1}月`;
  });
  await expect(page.locator(".sc-month")).toHaveText(monthLabel);

  // 默认选中今天（提交后只看当天结果的用户进页即可见结果），且是可读屏的选中态
  const today = await page.evaluate(() => {
    const d = new Date();
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  });
  const todayCell = page.locator(`.sc-grid button[data-sc-date="${today}"]`);
  await expect(todayCell).toHaveClass(/is-selected/);
  await expect(todayCell).toHaveAttribute("aria-pressed", "true");
  await expect(todayCell).toHaveClass(/sc-cell--today/);
  await expect(page.locator("[data-sc-log-date]")).toHaveText(today);

  // 取样工作日：✅ → 绿档底色（色彩即语义），且该档位进图例；符号本身不上界面
  const probe = await probeDay(page);
  expect(probe).not.toBe("");
  const probeCell = page.locator(`.sc-grid button[data-sc-date="${probe}"]`);
  await expect(probeCell).toHaveClass(/sc-cell--ok/);
  await expect(probeCell).not.toContainText("✅");
  await expect(probeCell).toHaveAttribute("aria-label", /已签到/);
  await expect(page.locator('.sc-legend li[data-tone="ok"]')).toBeVisible();
  await expect(page.locator('.sc-legend li[data-tone="bad"]')).toBeHidden(); // 该档本页没出现 → 收敛掉

  // 图例的常驻两项与语义标签
  await expect(page.locator(".sc-legend")).toContainText("周末停签");
  await expect(page.locator(".sc-legend")).toContainText("今天");

  // legacy 日历脚本必须彻底退役（"连引用一起删"）
  for (const legacy of ["/static/js/calendar.js", "sign-calendar-view.js", "pages/user_calendar.js"]) {
    await expect(page.locator(`script[src*="${legacy}"]`)).toHaveCount(0);
  }
});

test("签到日历：点取样工作日 → 面板拉当天日志（端到端脱敏）；换月/今天往返", async ({ page }) => {
  await loginUser(page);
  await page.goto("/user/calendar");
  const probe = await probeDay(page);

  // 点取样工作日：面板标题切到该日，内容是该日日志——**手机号必须已脱敏**
  await page.locator(`.sc-grid button[data-sc-date="${probe}"]`).click();
  await expect(page.locator("[data-sc-log-date]")).toHaveText(probe);
  const log = page.locator("[data-sc-log]");
  await expect(log).toContainText("签到成功");
  await expect(log).toContainText("138****8001");
  await expect(log).not.toContainText("13800138001");
  // 点选后该格成为唯一选中态
  await expect(page.locator(".sc-grid button.is-selected")).toHaveCount(1);

  // 换月：月份标题变、网格仍是 42 格；「今天」按钮回到当月并重新选中今天
  const thisMonth = await page.evaluate(() => {
    const d = new Date();
    return { y: d.getFullYear(), m: d.getMonth() + 1 };
  });
  await page.locator('[data-sc-shift="1"]').click();
  const nextLabel = `${thisMonth.m === 12 ? thisMonth.y + 1 : thisMonth.y}年${thisMonth.m === 12 ? 1 : thisMonth.m + 1}月`;
  await expect(page.locator(".sc-month")).toHaveText(nextLabel);
  await expect(page.locator(".sc-grid button[data-sc-date]")).toHaveCount(
    await page.evaluate(() => {
      const d = new Date();
      const y = d.getMonth() + 2 > 12 ? d.getFullYear() + 1 : d.getFullYear();
      const m = d.getMonth() + 2 > 12 ? 1 : d.getMonth() + 2;
      return new Date(y, m, 0).getDate();
    }),
  );

  await page.getByRole("button", { name: "今天" }).click();
  await expect(page.locator(".sc-month")).toHaveText(`${thisMonth.y}年${thisMonth.m}月`);
  const today = await page.evaluate(() => {
    const d = new Date();
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  });
  await expect(page.locator(`.sc-grid button[data-sc-date="${today}"]`)).toHaveClass(/is-selected/);
  await expect(page.locator("[data-sc-log-date]")).toHaveText(today);
});
