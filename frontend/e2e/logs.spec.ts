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

/** 看板热力图月份标签（`YYYY 年 M 月`）按 delta 月平移后的期望串（行为钉用）。 */
function shiftLabel(label: string, delta: number): string {
  const m = label.match(/(\d{4})\s*年\s*(\d{1,2})\s*月/);
  if (!m) throw new Error("无法解析月份标签：" + label);
  let year = Number(m[1]);
  let month = Number(m[2]) + delta;
  year += Math.floor((month - 1) / 12);
  month = ((((month - 1) % 12) + 12) % 12) + 1;
  return `${year} 年 ${month} 月`;
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

test("管理端数据面：日志页日期导航/事件表 + 数据看板 + 账号管理（复用同一管理员会话）", async ({ page }) => {
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

  // ======== 数据看板（P3 整页迁 Vue）——**并进本用例复用同一次管理员登录** ========
  // 为什么合并在日志用例里：/api/login 有 60 秒窗口 10 次/IP 的独立限速，整套 e2e 共用
  // 同一 Flask 实例与 127.0.0.1，单开一条看板/账号用例会多一次登录、把后面的用例顶到 429
  // （2026-10-03 实测：账号管理页单开 spec 时 myaccounts 的管理员登录被顶到 429）。
  // 种子数据与本文件同源（今天 2 条签到：success + failed；两个 active 账号；一个用户；
  // 另有三条账号管理页种子：待审核 1 + 已拒绝 1 + 软删除 1，供下方账号管理页断言用），
  // 故看板口径可精确断言：成功率 50.0%、趋势 成功 2 · 失败 1 · 跳过 0、分布总数 1、
  // 待处理账号 = 待审核 1 + 已拒绝 1 = 2。
  await page.goto("/data/dashboard");
  await expect(page.locator("#dashboard-root")).toBeVisible();

  await expect(page.locator("#kpi-accounts-value")).toContainText("2");
  await expect(page.locator("#kpi-users-sub")).toContainText("剩余注册名额 499");
  await expect(page.locator("#kpi-rate-value")).toContainText("50.0");
  await expect(page.locator("#kpi-rate-sub")).toContainText("成功 1 · 失败 1");
  await expect(page.locator("#kpi-rate-pill")).toContainText("无昨日对比");
  await expect(page.locator("#kpi-pending-value")).toContainText("2");
  await expect(page.locator("#kpi-pending-sub")).toContainText("待审核 1 · 已拒绝 1");

  // 三张图：Canvas 真实绘制（Chart.js 由 vendor defer 先行加载）+ 元数据行
  await expect(page.locator("#trend-coverage")).toHaveText("最近 30 天 · 仅真实签到");
  const trendBox = await page.locator("#chart-trend").boundingBox();
  expect(trendBox && trendBox.width > 100 && trendBox.height > 50).toBeTruthy();
  await expect(page.locator("#trend-meta")).toContainText("日均签到事件（次）");
  await expect(page.locator("#dist-meta .chart-meta-cell").first()).toContainText("1");
  await expect(page.locator("#slots-meta")).toContainText("最热门时段");

  // 热力图：今天格唯一、换月标签变化、脚注口径文案
  await expect(page.locator("#mini-cal .mini-cal-wd")).toHaveCount(7);
  await expect(page.locator("#mini-cal .mini-cal-day.is-today")).toHaveCount(1);
  await expect(page.locator("#cal-note")).toContainText("不含探针");
  const calBefore = await page.locator("#cal-label").innerText();
  await page.locator("#cal-prev").click();
  await expect(page.locator("#cal-label")).not.toHaveText(calBefore);

  // 换月状态机行为钉（code-review 必修）：动效只是视觉，月份状态必须**同步**推进，且任何
  // 一次切换都要取消在途切换。用同一 JS 任务内派发合成 click（detail=1 = 鼠标 / detail=0 =
  // 键盘）确保两次事件都落在 160ms 动效窗口内、可复现。
  // ① 连点两次「下一月」→ 净进 2 个月：若把 calMonth 赋值推迟到 enter()，两次都从旧月算
  //    next，seq=2 覆盖 seq=1，只会进 1 个月。
  const m0 = await page.locator("#cal-label").innerText();
  await page.evaluate(() => {
    const b = document.querySelector("#cal-next") as HTMLElement;
    for (let i = 0; i < 2; i++) b.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 }));
  });
  await expect(page.locator("#cal-label")).toHaveText(shiftLabel(m0, 2));
  // ② 鼠标「上一月」动效在途时按键盘（即时分支）→ 以键盘目标为准（-1 后 +1 回到原月）。
  //    若即时分支不自增 seq，在途 enter() 会把月份改回 -1，键盘操作被静默丢弃。
  const m1 = await page.locator("#cal-label").innerText();
  await page.evaluate(() => {
    const prev = document.querySelector("#cal-prev") as HTMLElement;
    const next = document.querySelector("#cal-next") as HTMLElement;
    prev.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 }));
    next.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 0 }));
  });
  await expect(page.locator("#cal-label")).toHaveText(m1);

  // 容量卡与运行状态
  await expect(page.locator("#capacity-accounts")).toContainText("2 / 200");
  await expect(page.locator("#capacity-users")).toContainText("1 / 500");
  await expect(page.locator("#health-clock")).toContainText("时钟已同步");
  await expect(page.locator("#health-global-pause")).toContainText("正常运行");
  await expect(page.locator("#health-announcement")).toContainText("暂无公告");
  await expect(page.locator("#health-ping")).toContainText("尚未检测");
  await page.locator("#ping-btn").click();
  await expect(page.locator("#health-ping .badge")).toBeVisible();

  // 失败降级（同一会话）：拦截签到事件接口 → 页级状态条 + 卡内错误行
  await page.route("**/api/admin/sign-events**", (route) => route.fulfill({
    status: 500,
    contentType: "application/json",
    body: JSON.stringify({ error: "boom" }),
  }));
  await page.reload();
  await expect(page.locator("#dash-status")).toBeVisible();
  await expect(page.locator("#dash-status-text")).toContainText("部分数据加载失败");
  await expect(page.locator("#dash-retry-btn")).toBeVisible();
  await expect(page.locator("#kpi-rate-sub")).toContainText("签到事件加载失败");
  await expect(page.locator("#cal-note")).toContainText("签到事件加载失败");
  await expect(page.locator("[data-overlay='trend']")).toContainText("签到事件加载失败");

  // ======== 账号管理页（P3 整页迁 Vue）——同样并入本用例、复用同一管理员会话 ========
  // 为什么并入（而不是单开 accounts.spec）：与上面看板同理，登录限速是全套 e2e 的稀缺
  // 资源；账号页单开一条用例会多一次 /api/login，把 myaccounts 的管理员登录顶到 429
  // （2026-10-03 实测）。选择器取**两栈共通**的稳定锚点（role=tab、保留的服务端 id、
  // 可见文案、placeholder）——迁移后（Vue）必须继续满足同一批选择器。
  // 放在最后：上面的失败降级块会把本页置为 degraded，账号页需在干净会话上断言。
  // 先钉**读方向**的分区深链：带 ?tab= 加载要直接落到该分区（写方向在下面点页签时钉）。
  await page.goto("/work/accounts?tab=deleted");
  await expect(page.locator("#acct-panel-deleted")).toBeVisible();
  await expect(page.getByRole("tab", { name: /待删除账号/ })).toHaveAttribute("aria-selected", "true");
  await page.goto("/work/accounts");

  // ① 页面骨架：标题、三组页签与计数、KPI、待处理提醒
  await expect(page.locator("h1.page-title")).toHaveText("账号管理");
  await expect(page.getByRole("tab", { name: /待处理账号/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /正常账号/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /待删除账号/ })).toBeVisible();
  // 计数放在页签上：待处理=2（待审核 1 + 已拒绝 1），正常=2，待删除=1。
  await expect(page.getByRole("tab", { name: /待处理账号/ })).toContainText("2");
  await expect(page.getByRole("tab", { name: /正常账号/ })).toContainText("2");
  await expect(page.getByRole("tab", { name: /待删除账号/ })).toContainText("1");
  // KPI 卡（今日签到统计）：成功/失败/待签/跳过。
  await expect(page.locator("#stat-success")).toHaveText("0");
  await expect(page.locator("#stat-failed")).toHaveText("0");
  await expect(page.locator("#stat-waiting")).toHaveText("2");
  await expect(page.locator("#stat-skipped")).toHaveText("0");
  await expect(page.locator("#pending-tip")).toBeVisible();
  await expect(page.locator("#pending-count")).toHaveText("2");

  // ② 待处理表：状态徽标 + 脱敏手机号（完整号绝不进 DOM）
  const pendingRows = page.locator("#accounts-pending-tbody tr");
  await expect(pendingRows).toHaveCount(2);
  await expect(pendingRows.first()).toContainText("e2e-pending-acct");
  await expect(pendingRows.first()).toContainText("待审核");
  await expect(pendingRows.last()).toContainText("已拒绝");
  await expect(pendingRows.first().locator(".acct-cell-phone")).toContainText("137****7003");
  const accountsHtml = await page.content();
  expect(accountsHtml).not.toContain("13700137003");
  expect(accountsHtml).not.toContain("13900139002");

  // ③ 页签切换 + ?tab= 深链
  await page.getByRole("tab", { name: /正常账号/ }).click();
  await expect(page).toHaveURL(/[?&]tab=active/);
  await expect(page.locator("#acct-panel-active")).toBeVisible();
  const activeRows = page.locator("#accounts-tbody tr");
  await expect(activeRows).toHaveCount(2);
  await expect(activeRows.first()).toContainText("e2e-user-acct");
  // 待删除组：软删账号带「待删除」徽标与恢复/彻底删除动作
  await page.getByRole("tab", { name: /待删除账号/ }).click();
  await expect(page).toHaveURL(/[?&]tab=deleted/);
  const deletedRows = page.locator("#accounts-deleted-tbody tr");
  await expect(deletedRows).toHaveCount(1);
  await expect(deletedRows.first()).toContainText("e2e-deleted-acct");
  await expect(deletedRows.first()).toContainText("待删除");
  await expect(deletedRows.first().getByRole("button", { name: "恢复" })).toBeVisible();
  await expect(deletedRows.first().getByRole("button", { name: "彻底删除" })).toBeVisible();

  // ④ 检索：无匹配 → 「无匹配结果」+ 清除筛选出口（不是死路）
  await page.getByRole("tab", { name: /正常账号/ }).click();
  await page.fill("#active-search", "no-such-account-zzz");
  await expect(activeRows).toHaveCount(0);
  await expect(page.locator("#accounts-empty .empty__msg")).toHaveText("无匹配结果");
  await page.locator("#accounts-empty [data-empty-clear]").click();
  await expect(activeRows).toHaveCount(2);

  // ⑤ 全选 → 批量条显形（计数 + 取消选择）
  await page.locator("#select-all-active").check();
  await expect(page.locator("#batch-bar-active")).toBeVisible();
  await expect(page.locator("#batch-count-active")).toHaveText("2");
  await page.locator("#batch-bar-active [data-batch-clear]").click();
  await expect(page.locator("#batch-bar-active")).toBeHidden();

  // ⑥ 审核「通过」先弹确认，取消则不改动（受控动作不裸奔）
  await page.getByRole("tab", { name: /待处理账号/ }).click();
  const firstPending = pendingRows.first();
  await firstPending.getByRole("button", { name: "通过" }).click();
  const acctModal = page.locator(".pm-backdrop, .el-message-box, .el-dialog").first();
  await expect(acctModal).toBeVisible();
  await expect(acctModal).toContainText("e2e-pending-acct");
  await acctModal.getByRole("button", { name: /取消/ }).first().click();
  // 取消后仍是待处理，计数不变
  await expect(page.getByRole("tab", { name: /待处理账号/ })).toContainText("2");

  // ⑦ 添加账号表单：打开 → 必填校验 → 取消
  await page.locator("[data-add-account]").first().click();
  const acctForm = page.locator(".pm-backdrop, .el-dialog").first();
  await expect(acctForm).toBeVisible();
  await expect(acctForm.getByText("添加账号").first()).toBeVisible();
  await acctForm.getByPlaceholder(/易班登录手机号/).fill("");
  await acctForm.getByRole("button", { name: "添加账号" }).last().click();
  await expect(acctForm.locator(".alert.danger, .el-alert--error").first()).toBeVisible();
  await acctForm.getByRole("button", { name: /取消/ }).first().click();
  await expect(acctForm).toBeHidden();
});
