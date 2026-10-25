import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

import { shiftDay } from "./business-day";
import { pythonCommand } from "./python-command";

/* 冻结钟的跨业务日守卫（P0 2026-10-25 的三族形状）。
 *
 * 钉的性质：**业务日只由服务端时钟决定**。零点前种下的「今天」在零点后
 * 必须整体归入前一日；页面的「今天」、日历选中、今日 KPI 一起翻转。
 *
 * 做法：本用例自起一个临时实例（e2e/server.py），锚点 23:55、钟按真实时间
 * 前进（倍速 15）。5 个业务分钟＝20 秒真实时间，不必真等 5 分钟。
 * 真钟模式不受影响：本实例只属本用例，跑完即杀。
 *
 * 与主实例的关系：主实例仍由 playwright.config 拉起（真钟）。本用例不复用
 * 它——它的种子时间不可改，且跨日会把共享实例的既有断言带红。
 *
 * 会话：直接用默认 page 的上下文打绝对 URL。cookie 不区分端口，故冻结实例
 * 的会话与主实例同域；本用例只在冻结实例上操作，互不影响。
 */

const PORT = Number(process.env.YB_E2E_FROZEN_PORT ?? 8777);
const BASE = `http://127.0.0.1:${PORT}`;
/** 锚点：2026-10-26（周一）23:55（北京时间）。 */
const ANCHOR = "2026-10-26T23:55:00";
const DAY = "2026-10-26";
const NEXT = shiftDay(DAY, 1);
const ADMIN = { username: "admin", password: "TestPass1234!" };
const USER = { username: "e2e-user@example.com", password: "UserPass123!" };

const SERVER = fileURLToPath(new URL("./server.py", import.meta.url));
const FRONTEND_DIR = fileURLToPath(new URL("..", import.meta.url));

function startFrozenServer(): ChildProcessWithoutNullStreams {
  const child = spawn(pythonCommand(), [SERVER], {
    cwd: FRONTEND_DIR,
    env: {
      ...process.env,
      YB_E2E_PORT: String(PORT),
      YB_E2E_FROZEN_NOW: ANCHOR,
      YB_E2E_FROZEN_ADVANCE: "1",
      YB_E2E_FROZEN_SCALE: "15",
    },
    stdio: "pipe",
  });
  // 缓冲启动日志：起不来时把真实原因带进失败消息。
  const out: string[] = [];
  child.stdout.on("data", (d) => out.push(String(d)));
  child.stderr.on("data", (d) => out.push(String(d)));
  (child as unknown as { _e2eLog: string[] })._e2eLog = out;
  return child;
}

async function waitReady(timeoutMs = 60_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let last = "";
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(BASE + "/login");
      // 必须读完 body：未消费的响应体会让 undici 的 parser 停在 paused 状态，
      // 连接结束时触发 Node 24 的 assert(!this.paused)（实测崩溃整个 worker）。
      await resp.text();
      if (resp.status === 200) return;
      last = `HTTP ${resp.status}`;
    } catch (err) {
      last = String(err);
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`冻结实例未就绪（${last}）`);
}

test("冻结钟跨业务日：23:55 的「今天」在零点后归入前一日", async ({ page }) => {
  // 跨日等待 20 秒＋多次页面装载，默认 30 秒不够。
  test.setTimeout(120_000);
  const child = startFrozenServer();
  const diagnostics = (child as unknown as { _e2eLog: string[] })._e2eLog;
  try {
    await waitReady();

    // ---- 零点前：服务端与页面都认 2026-10-26 ----
    const loginUser = await page.request.post(BASE + "/api/login", { data: USER });
    expect(loginUser.ok(), `用户登录失败：${loginUser.status()}`).toBeTruthy();

    const before = await (await page.request.get(BASE + "/api/clock")).json();
    expect(before.tz_offset_min, "业务时区必须是 +480").toBe(480);
    expect(String(before.now).slice(0, 10), "零点前业务日").toBe(DAY);

    await page.goto(BASE + "/user/calendar");
    await expect(page.locator(".sc-grid button.is-selected")).toHaveAttribute("data-sc-date", DAY);
    await expect(page.locator("[data-sc-log-date]")).toHaveText(DAY);
    // 零点前的「今天」有种子日志；跨日后这份数据必须仍属 DAY。
    await expect(page.locator("[data-sc-log]")).toContainText("签到成功");

    // ---- 等时钟真的跨过业务日零点（倍速 15 ⇒ 约 20 秒）----
    await expect
      .poll(
        async () => {
          const c = await (await page.request.get(BASE + "/api/clock")).json();
          return String(c.now).slice(0, 10);
        },
        { timeout: 90_000, intervals: [500] },
      )
      .toBe(NEXT);

    // ---- 零点后：页面按新业务日重新校准 ----
    await page.goto(BASE + "/user/calendar");
    await expect(page.locator(".sc-grid button.is-selected")).toHaveAttribute("data-sc-date", NEXT);
    await expect(page.locator("[data-sc-log-date]")).toHaveText(NEXT);
    await expect(page.locator("[data-sc-log]")).toContainText(`${NEXT} 暂无签到记录`);

    // 前一日的数据没有丢：点 DAY 仍能拉到那条日志（只是不再是「今天」）。
    await page.locator(`.sc-grid button[data-sc-date="${DAY}"]`).click();
    await expect(page.locator("[data-sc-log-date]")).toHaveText(DAY);
    await expect(page.locator("[data-sc-log]")).toContainText("签到成功");

    // ---- 今日 KPI 随业务日翻转：DAY 的终态不再算「今天」----
    const loginAdmin = await page.request.post(BASE + "/api/login", { data: ADMIN });
    expect(loginAdmin.ok(), `管理员登录失败：${loginAdmin.status()}`).toBeTruthy();
    await page.goto(BASE + "/data/dashboard");
    await expect(page.locator("#dashboard-root")).toBeVisible();
    await expect(page.locator("#kpi-users-value"), "跨日后今日失败账号必须归零").toHaveText("0");
    await expect(page.locator("#kpi-accounts-value"), "跨日后今日成功账号必须归零").toHaveText("0");
    // 非日期口径的 KPI 仍在：证明页面数据真的加载了，不是「全 0 = 没取到」。
    await expect(page.locator("#kpi-pending-value")).toContainText("2");
    // 健康卡显示的是服务端业务日（冻结值），不是浏览器真实日期。
    // 注意：这里必然同时报「偏差」——冻结把服务端挪到了别的日期，
    // 浏览器真实钟没动。本门钉的是业务日归属，不是墙上时钟同步。
    await expect(page.locator("#health-clock")).toContainText(NEXT);
  } catch (err) {
    throw new Error(`${String(err)}\n--- 冻结实例输出 ---\n${diagnostics.join("")}`);
  } finally {
    child.kill();
  }
});
