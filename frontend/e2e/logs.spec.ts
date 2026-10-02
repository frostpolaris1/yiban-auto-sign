import { expect, test } from "@playwright/test";

// 日志页真浏览器端到端（P1b + 2026-10-03 修复后加固）。
//
// 数据由 e2e/server.py 预置：**两天**日志（今天 + 三天前，行内含完整手机号与
// `（today）`/`（older）` 标记）+ 真实事件（今天 2 条签到含一条 attempt=3 的失败、2 条探针；
// 三天前 1 条签到）。两天数据是刻意的——日期导航只有在"存在另一天"时才可观测。
const ADMIN_USER = "admin";
const ADMIN_PASS = "TestPass1234!";

function dates(): { today: string; older: string } {
  const pad = (n: number) => String(n).padStart(2, "0");
  const fmt = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const now = new Date();
  return { today: fmt(now), older: fmt(new Date(now.getTime() - 3 * 86400000)) };
}

test("登录 → 日志页：正文渲染、完整手机号不出现在 DOM、三分区、无数据日期的空态", async ({ page }) => {
  // 整个套件只在这里走一次真实登录表单（同 IP 10 秒内第 4 次访问 /login 会被
  // 服务端的「登录页访问循环」守卫打断——那是给真实用户的保护，测试不该反复撞）
  await page.goto("/login");
  await page.fill("#username", ADMIN_USER);
  await page.fill("#password", ADMIN_PASS);
  await page.click("#login-btn");
  await expect(page).toHaveURL(/\/(data\/dashboard)?$/);

  await page.goto("/data/logs");

  // 1) 日志正文：信息栏报行数，正文 pre 渲染出种子行
  await expect(page.locator(".logs-info")).toContainText("行");
  const box = page.locator(".log-box");
  await expect(box).toBeVisible();
  await expect(box).toContainText("签到成功");
  await expect(box).toContainText("（today）");

  // 2) 端到端脱敏：脱敏形态出现，完整号码**绝不出现**（服务端单出口 + 前端只插值）
  const text = await box.innerText();
  expect(text).toContain("138****8001");
  expect(text).not.toContain("13800138001");

  // 3) 三分区切换（el-tabs）；事件表已种数据 → 行数可见
  await page.getByRole("tab", { name: /签到事件/ }).click();
  const signPane = page.locator(".el-tab-pane:visible");
  await expect(signPane.locator(".el-table__row")).toHaveCount(2);
  await page.getByRole("tab", { name: /探针记录/ }).click();
  const probePane = page.locator(".el-tab-pane:visible");
  await expect(probePane.locator(".el-table__row")).toHaveCount(2);

  // 4) 分区深链：切到探针后 URL 带 ?tab=probe（与旧页 URL 契约一致，可直接分享）
  await expect(page).toHaveURL(/[?&]tab=probe/);

  // 5) 空态：pin 一个没有数据的合法日期 → 日志空态（含「最近有数据日期」跳转出口）
  //    注意先切回「日志」分区：日期输入框在日志面板内，面板未激活时不可见、填不进去
  await page.getByRole("tab", { name: "日志", exact: true }).click();
  await page.fill('input[type="date"]', "2000-01-01");
  await page.getByRole("button", { name: "查看该日日志" }).click();
  const logPane = page.locator(".el-tab-pane:visible");
  await expect(logPane.locator(".logs-empty")).toContainText("（2000-01-01 无签到日志）");
  await expect(logPane.locator(".logs-empty")).toContainText("查看"); // 「最近有数据日期」跳转出口
});

test("日志页：日期导航与事件表（blocking 回归守卫 + 事件真实渲染）", async ({ page }) => {
  // 会话走 API 登录（表单登录已在上一条覆盖）
  const resp = await page.request.post("/api/login", { data: { username: ADMIN_USER, password: ADMIN_PASS } });
  expect(resp.ok()).toBeTruthy();

  const { today, older } = dates();
  await page.goto("/data/logs");
  const box = page.locator(".log-box");
  await expect(box).toContainText(today);

  // ① 日期导航——**这一条是 blocking 回归的守卫**：原实现用服务端回显日期覆盖用户选择，
  //    日期栏整体失效（点了没反应、深链被忽略），此处必红。
  await page.fill('input[type="date"]', older);
  await page.getByRole("button", { name: "查看该日日志" }).click();
  await expect(box).toContainText(older);
  await expect(box).toContainText("（older）");
  await expect(box).not.toContainText("（today）");
  await expect(page).toHaveURL(new RegExp(`date=${older}`));

  // ② 回到今天（pin 态下才出现的按钮）
  await page.getByRole("button", { name: "回到今天" }).click();
  await expect(box).toContainText("（today）");
  await expect(page).not.toHaveURL(/date=/);

  // ③ 签到事件：真实行 + 中文标签（原始码保留在 title）+ attempt>1 的补号
  //    默认按时间**降序**（06:31:05 在前）
  await page.getByRole("tab", { name: /签到事件/ }).click();
  const pane = page.locator(".el-tab-pane:visible");
  await expect(pane.locator(".el-table__row")).toHaveCount(2);
  const badges = pane.locator(".badge");
  await expect(badges.nth(0)).toHaveText("失败");
  await expect(badges.nth(0)).toHaveAttribute("title", "failed");
  await expect(badges.nth(1)).toHaveText("成功");
  await expect(badges.nth(1)).toHaveAttribute("title", "success");
  await expect(pane).toContainText("（第 3 次）");

  // ④ 排序：点「时间」表头 → 转升序，首行变 06:31:01 成功
  await pane.getByText("时间", { exact: true }).click();
  const firstRow = pane.locator(".el-table__row").first();
  await expect(firstRow).toContainText("06:31:01");
  await expect(firstRow).toContainText("成功");

  // ⑤ 探针：非 failed 一律「正常」，failed 显示「异常」（原始码在 title）
  await page.getByRole("tab", { name: /探针记录/ }).click();
  const probePane = page.locator(".el-tab-pane:visible");
  await expect(probePane.locator(".el-table__row")).toHaveCount(2);
  await expect(probePane.locator(".badge").nth(0)).toHaveText("异常");
  await expect(probePane.locator(".badge").nth(0)).toHaveAttribute("title", "failed");
  await expect(probePane.locator(".badge").nth(1)).toHaveText("正常");
  await expect(probePane.locator(".badge").nth(1)).toHaveAttribute("title", "ok");
});
