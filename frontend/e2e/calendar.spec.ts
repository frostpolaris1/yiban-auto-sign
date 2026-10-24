import { userTest as test, expect, USER } from "./base";
import {
  businessDay,
  businessDayParts,
  businessDaysInMonth,
  businessDaysInMonthShift,
  businessMonthLabel,
  businessWeekday,
  zonedDay,
} from "./business-day";

// 签到日历页真浏览器端到端（P2 日历对迁移后）。
// 数据由 e2e/server.py 预置：普通用户 e2e-user@example.com 名下有一个生效账号
// （13800138001），另有一个"取样工作日"带 ✅ 状态与当天日志（见 _probe_day）。
// 会话走 API 登录（表单登录由 logs.spec 覆盖一次即可，避免反复访问 /login 撞登录循环守卫）。
//
// 断言刻意**不**依赖账号名称：myaccounts.spec 会把账号改名，而 spec 文件顺序不是契约。
// 日期一律按"运行时当天"推导（今天/当月天数/取样日由服务端同一规则选出），故整周任意一天
// 跑都成立——只有"今天恰好是周末"这一分支要单独处理（停签日不查日志）。
// 会话由 playwright.config.ts 的 user project 注入（.auth/user.json），不在用例里登录。
/** 与服务端 `_probe_day()` 同规则、同事实源：业务月第一个"非今天、非三天前"的工作日。
 *  日期一律走 business-day（Node 侧，固定 +8）；不读宿主时区，也不依赖浏览器 timezoneId。 */
function probeDay(): string {
  const today = businessDay();
  const older = businessDay(3);
  const { y, m } = businessDayParts();
  const pad = (n: number) => String(n).padStart(2, "0");
  for (let d = 1; d <= businessDaysInMonth(); d++) {
    const cand = `${y}-${pad(m)}-${pad(d)}`;
    const wd = businessWeekday(cand);
    if (wd >= 1 && wd <= 5 && cand !== today && cand !== older) return cand;
  }
  return "";
}

test("签到日历：月历骨架/今日选中/语气档底色/图例收敛，且无 legacy 日历脚本", async ({ page }) => {
  await page.goto("/user/calendar");

  // 卡头：账号手机号（用户看自己的账号按既有设计显示完整号）
  await expect(page.locator(".cal-card").first().locator(".panel-sub").first()).toContainText("13800138001");

  // 固定 6 行 42 格：空位格 + 当月日期格恒为 42
  const cells = page.locator(".sc-grid button[data-sc-date]");
  const blanks = page.locator(".sc-grid .sc-blank");
  const daysInMonth = businessDaysInMonth();
  await expect(cells).toHaveCount(daysInMonth);
  await expect(blanks).toHaveCount(42 - daysInMonth);

  // 月份标题 = 业务月（与服务端同一份北京钟；月份边界在 UTC 主机上不得退一个月）
  const bp = businessDayParts();
  await expect(page.locator(".sc-month")).toHaveText(`${bp.y}年${bp.m}月`);

  // 默认选中今天（提交后只看当天结果的用户进页即可见结果），且是可读屏的选中态
  const today = businessDay();
  const todayCell = page.locator(`.sc-grid button[data-sc-date="${today}"]`);
  await expect(todayCell).toHaveClass(/is-selected/);
  await expect(todayCell).toHaveAttribute("aria-pressed", "true");
  await expect(todayCell).toHaveClass(/sc-cell--today/);
  await expect(page.locator("[data-sc-log-date]")).toHaveText(today);

  // 取样工作日：✅ → 绿档底色（色彩即语义），且该档位进图例；符号本身不上界面
  const probe = probeDay();
  expect(probe).not.toBe("");
  const probeCell = page.locator(`.sc-grid button[data-sc-date="${probe}"]`);
  await expect(probeCell).toHaveClass(/sc-cell--ok/);
  await expect(probeCell).not.toContainText("✅");
  await expect(probeCell).toHaveAttribute("aria-label", /已签到/);
  await expect(page.locator('.sc-legend li[data-tone="ok"]')).toBeVisible();
  // 月历只读 sign-daily 文件（web/routes/my_api.py:728-748），今日终态不影响日历：
  // 今天没有 daily 档 ⇒ 红档不进图例，图例随档位收敛。
  // 今日格子的语气档由"今天是否签到日"决定（model.js:136-138）：停签日打 sc-cell--off，
  // 签到日无 daily 档打 sc-cell--none。周末跑这套 e2e 时旧断言钉死 none ⇒ 假红；
  // 期望值按服务端同一份星期开关现算，单一取值，不放松成"两者皆可"。
  const monthParam = `${bp.y}-${String(bp.m).padStart(2, "0")}`;
  const monthJson = await page.evaluate(
    async (ym) => (await (await fetch(`/api/my-calendar?month=${ym}`)).json()) as {
      saturday_sign?: number;
      sunday_sign?: number;
    },
    monthParam,
  );
  const wd = businessWeekday(today);
  const todayTone =
    (wd === 6 && monthJson.saturday_sign !== 1) || (wd === 0 && !monthJson.sunday_sign) ? "off" : "none";
  await expect(todayCell).toHaveClass(new RegExp(`sc-cell--${todayTone}`));
  await expect(page.locator('.sc-legend li[data-tone="bad"]')).toBeHidden();

  // 图例的常驻两项与语义标签
  await expect(page.locator(".sc-legend")).toContainText("周末停签");
  await expect(page.locator(".sc-legend")).toContainText("今天");

  // legacy 日历脚本必须彻底退役（"连引用一起删"）
  for (const legacy of ["/static/js/calendar.js", "sign-calendar-view.js", "pages/user_calendar.js"]) {
    await expect(page.locator(`script[src*="${legacy}"]`)).toHaveCount(0);
  }
});

test("签到日历：点取样工作日 → 面板拉当天日志（端到端脱敏）；换月/今天往返", async ({ page }) => {
  await page.goto("/user/calendar");
  const probe = probeDay();

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
  const thisMonth = businessDayParts();
  await page.locator('[data-sc-shift="1"]').click();
  await expect(page.locator(".sc-month")).toHaveText(businessMonthLabel(1));
  await expect(page.locator(".sc-grid button[data-sc-date]")).toHaveCount(
    businessDaysInMonthShift(1),
  );

  await page.getByRole("button", { name: "今天" }).click();
  await expect(page.locator(".sc-month")).toHaveText(`${thisMonth.y}年${thisMonth.m}月`);
  const today = businessDay();
  await expect(page.locator(`.sc-grid button[data-sc-date="${today}"]`)).toHaveClass(/is-selected/);
  await expect(page.locator("[data-sc-log-date]")).toHaveText(today);
});

/* zvj1：日历的「今天」必须取服务端校准钟（core.js serverNow，其本地字段即服务器墙上时间），
   而不是浏览器本地日期。断言办法：另开一个**浏览器时区≠+8**的上下文，挑一个此刻日期确实
   与业务日不同的时区，页面仍必须高亮业务日。旧实现读 new Date() 时这里会高亮浏览器当天，
   必然与期望不同（断言与宿主时区、运行时刻都无关：时区按当下现算）。 */
test("签到日历：浏览器时区≠+8 时，「今天」仍按服务端校准钟", async ({ browser }) => {
  const expected = businessDay();
  const zone = ["America/Los_Angeles", "Pacific/Kiritimati", "Etc/GMT+12", "Etc/GMT-14"].find(
    (z) => zonedDay(z) !== expected,
  );
  expect(zone, "找不到与业务日不同的时区（辅助函数失效）").toBeTruthy();
  expect(zonedDay(zone as string)).not.toBe(expected); // 环境自检：浏览器当天确实不是业务日

  const context = await browser.newContext({
    baseURL: test.info().project.use.baseURL,
    timezoneId: zone,
  });
  const page = await context.newPage();
  try {
    const resp = await page.request.post("/api/login", {
      data: { username: USER.username, password: USER.password },
    });
    expect(resp.ok()).toBeTruthy();
    await page.goto("/user/calendar");
    await expect(page.locator(".sc-grid button.is-selected")).toHaveAttribute("data-sc-date", expected);
    await expect(page.locator("[data-sc-log-date]")).toHaveText(expected);
  } finally {
    await context.close();
  }
});
