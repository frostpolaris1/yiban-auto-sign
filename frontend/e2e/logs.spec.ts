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
  // 本用例含一段 11.5 秒的轮询观测（轮询沿用在途选择），默认 30 秒超时不够。
  test.setTimeout(90_000);
  // 整个套件只在这里走一次真实登录表单（同 IP 10 秒内第 4 次访问 /login 会被
  // 服务端的「登录页访问循环」守卫打断——那是给真实用户的保护，测试不该反复撞）
  await page.goto("/login");
  await page.fill("#username", ADMIN_USER);
  await page.fill("#password", ADMIN_PASS);
  await page.click("#login-btn");
  await expect(page).toHaveURL(/\/(data\/dashboard)?$/);

  await page.goto("/data/logs");

  // 0) 巡检首块（本批新增）：可见窗口必须显式写明；轮级摘要一行一轮；时间线按轮展开。
  //    种子（见 e2e/server.py::_seed_run_events）：**同一业务日两轮**——最新一轮 3 个账号
  //    （1 成一败 1 未开始，耗时 21 秒），较早一轮是并行执行体。两轮是刻意的：下面
  //    「点开较早一轮 + 轮询沿用选择」的行为只有在存在另一轮时才可观测。
  await expect(page.locator("#run-window")).toContainText("保留最近 14 天");
  const runRows = page.locator("#run-summary .el-table__row");
  await expect(runRows).toHaveCount(2);
  await expect(runRows.first()).toContainText("单执行体");
  await expect(runRows.first()).toContainText("21 秒"); // 06:40:00 → 06:40:21
  const tl = page.locator("#run-timeline");
  await expect(tl.locator(".el-table__row")).toHaveCount(8); // 3 claim + 2 start + 成功/失败/收尾
  await expect(tl).toContainText("领取");
  await expect(tl).toContainText("收尾");
  // 执行体只回角色与槽位：身份原串（带主机名）绝不进 DOM
  await expect(tl).not.toContainText("e2e-host");

  // 0b) 轮询沿用在途选择（F5）：点开**较早的一轮**后，10 秒自动刷新（默认开）不得把
  //     选中改回最新一轮。改回即「查昨天那轮」的主用途失效——用户点开的旧轮在 ≤10 秒内
  //     被静默换掉。这条钉的是**接线**（纯函数单测钉不住 loadRuns 传没传 keepSelection）。
  const olderRunRow = runRows.filter({ hasText: "并行执行体" });
  await expect(olderRunRow).toHaveCount(1);
  await olderRunRow.click();
  await expect(olderRunRow.locator(".run-current")).toHaveText("当前");
  await expect(tl).toContainText("06:30:09"); // 已切到较早一轮的时间线
  await page.waitForTimeout(11_500); // 跨过至少一个 10 秒轮询节拍
  await expect(tl).toContainText("06:30:09", { timeout: 5_000 });
  await expect(
    olderRunRow.locator(".run-current"),
    "轮询把在途选择改回最新一轮（轮询必须沿用在途选择）",
  ).toHaveText("当前");

  // 1) 日志正文：级别档**默认收起 INFO**（巡检只看 WARN／ERROR），且信息栏如实报出收起数
  const box = page.locator(".log-box");
  await expect(page.locator(".logs-info")).toContainText("已收起");
  await expect(box).toBeVisible();
  await expect(box).toContainText("单次尝试耗时偏长"); // WARNING 行仍在
  await expect(box).not.toContainText("（today）"); // 带标记的 INFO 行默认收起
  // 切到全量档：INFO 行回来，端到端脱敏照旧（服务端单出口 + 前端只插值）
  await page.locator("#logs-level-warn").uncheck();
  await expect(box).toContainText("（today）");
  await expect(box).toContainText("签到成功");

  // 2) 端到端脱敏：脱敏形态出现，完整号码**绝不出现**（进度流与日志行同一口径）
  const text = await box.innerText();
  expect(text).toContain("138****8001");
  expect(text).not.toContain("13800138001");
  const tlText = await tl.innerText();
  expect(tlText).toContain("138****8001");
  expect(tlText).not.toContain("13800138001");

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
  // 级别档默认收起 INFO：本用例的下文都在核日志正文，先切到全量档（级别档本身
  // 由第一条用例与 tests/test_logs_level_filter.py 钉住）。等到 INFO 行真的出现再往下走：
  // 切档会触发一次重载，而 load() 在途时会丢弃后来的导航请求（既有形状），
  // 不等待就会把下面的「查看该日」吃掉。
  await page.locator("#logs-level-warn").uncheck();
  const box = page.locator(".log-box");
  await expect(box).toContainText("（today）");
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

  // 种子：今天 13800138001 先 success 后 failed → 当日终态为失败、账号数 1（账号口径）。
  // 两张 KPI 已从「活跃账号 / 注册用户」（与下方容量卡同源同数）换成今日成功/失败账号。
  await expect(page.locator("#kpi-accounts-value")).toHaveText("0"); // 今日成功账号
  await expect(page.locator("#kpi-users-value")).toHaveText("1"); // 今日失败账号
  await expect(page.locator("#kpi-users-sub")).toContainText("按账号统计");
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

  // 急停档：把 /api/settings 的 global_pause 改写成 1（**只改响应、不触共享实例的 .env**），
  // 重载后运行状态徽标必须报"暂停"且语气档为 bad。census P1-5：急停生效时前端暂停标签
  // 必须与后端 state 一致（文案 + tone），不得显示"正常运行"（假安心）。
  await page.route("**/api/settings**", async (route) => {
    const body = await (await route.fetch()).json();
    await route.fulfill({ json: { ...body, global_pause: 1 } });
  });
  await page.reload();
  await expect(page.locator("#health-global-pause")).toContainText("全局暂停中");
  await expect(page.locator("#health-global-pause .badge")).toHaveClass(/badge--bad/);
  await page.unroute("**/api/settings**");
  await page.reload();

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
  // 口令字段的标签必须带「易班」限定词，并有一行区分说明（工单 0h7p 的验收锚点）：
  // 管理端丢限定词时，管理员会把易班凭据读成"用户在本站的登录密码"，据此改错凭据。
  await expect(acctForm.locator(".field-label", { hasText: "易班密码" }).first()).toBeVisible();
  await expect(acctForm.locator(".field-help", { hasText: "不影响该用户登录本站" }).first()).toBeVisible();
  await acctForm.getByPlaceholder(/易班登录手机号/).fill("");
  await acctForm.getByRole("button", { name: "添加账号" }).last().click();
  await expect(acctForm.locator(".alert.danger, .el-alert--error").first()).toBeVisible();
  await acctForm.getByRole("button", { name: /取消/ }).first().click();
  await expect(acctForm).toBeHidden();

  // ======== 系统设置页（P3 整页迁 Vue）——同样并入本用例、复用同一管理员会话 ========
  // 为什么并入：与看板/账号页同理，登录限速是全套 e2e 的稀缺资源。选择器取**两栈共通**
  // 的稳定锚点（role=tab、保留的服务端 id、可见文案）——迁移后（Vue）必须继续满足同一批
  // 选择器。本页特有口径按"通用语义"钉：显式保存（改动只标脏、不自动落盘）、保存走口令门
  // （A 档键需当次口令）、保存后回读一致、换行注入被拦、非法值不落盘。
  // e2e/server.py 把 YIBAN_PW_GATE 设为 full，使口令门在 e2e 下**确定性出现**（默认 risk
  // 档同出口免口令，口令门会被"IP 没换"整段跳过，测不到）。
  await page.goto("/work/settings");
  await expect(page.locator("h1.page-title")).toHaveText("系统设置");

  // ① 分区页签：主管理员可见全部七个分区
  for (const name of ["签到调度", "公告", "通知通道", "容量配额", "健康与探针", "执行体", "系统开关"]) {
    await expect(page.getByRole("tab", { name, exact: true })).toBeVisible();
  }

  // ② 调度卡回填：服务端值渲染成可见文案/输入值（不看隐藏 input）。
  // 下拉取两栈共通的 data-select-field 锚点（legacy 自研控件根 / Vue 包裹层都用它），
  // 不用 getByText——选项文本与触发器文本会同时命中（strict mode 冲突）。
  await expect(page.locator('[data-select-field="ss-order"]')).toContainText("列表顺序");
  await expect(page.locator('[data-select-field="ss-dist"]')).toContainText("均匀分布");
  await expect(page.locator("#ss-gap")).toHaveValue("10");
  await expect(page.locator(".time-pair")).toContainText("06:30 至 07:50");

  // ③ 显式保存语义：改动只标脏 + 保存按钮**常驻**（无改动时禁用，不再隐藏），未点保存**不落盘**
  await expect(page.locator("#ss-dirty")).toBeHidden();
  await expect(page.locator("#ss-save")).toBeVisible();
  await expect(page.locator("#ss-save")).toBeDisabled();
  await page.fill("#ss-gap", "11");
  await page.locator("#ss-gap").blur(); // 数字框改动在 blur(change) 才标脏——模拟真实用户离开字段
  await expect(page.locator("#ss-dirty")).toBeVisible();
  await expect(page.locator("#ss-save")).toBeEnabled();
  // 脏状态下切换分区 → 未保存改动守卫（保存并继续 / 放弃修改 / 取消）；取消后留在原分区
  await page.getByRole("tab", { name: "公告", exact: true }).click();
  const dirtyGuard = page.locator(".pm-backdrop").first();
  await expect(dirtyGuard).toBeVisible();
  await expect(dirtyGuard).toContainText("有未保存的修改");
  await dirtyGuard.getByRole("button", { name: /取消/ }).first().click();
  // 通用语义：取消守卫必须**留在原分区**（改动既未保存也未丢弃）。
  // 用 expect.soft：legacy 此处有一个已知缺陷（core.js 的 document 级 tab 委托先于页面脏
  // 守卫切换了分区，取消后停在「公告」），soft 断言让本次运行继续跑完、把其余偏差一并列出；
  // 迁移后（Vue 自管页签）必须转绿。
  await expect.soft(page.locator("#set-panel-schedule")).toBeVisible();
  // 刷新丢弃未保存改动、回到默认分区（两栈一致）；尚未保存 → 仍是旧值 10
  await page.reload();
  await expect(page.locator("#ss-gap")).toHaveValue("10");

  // ④ 保存走口令门（gap_max 属 A 档，full 档下当次必须输口令）：
  //    取消口令框 → 不落盘、脏保留；输入正确口令 → 落盘、脏清除、回读一致
  await page.fill("#ss-gap", "11");
  await page.locator("#ss-gap").blur(); // 数字框改动在 blur(change) 才标脏——模拟真实用户离开字段
  await page.locator("#ss-save").click();
  const pwModal = page.locator(".pm-backdrop").first();
  await expect(pwModal).toBeVisible();
  await expect(pwModal.locator('input[type="password"]')).toBeVisible();
  await pwModal.getByRole("button", { name: /取消/ }).first().click();
  await expect(page.locator("#ss-dirty")).toBeVisible();
  await page.reload();
  await expect(page.locator("#ss-gap")).toHaveValue("10"); // 取消口令 = 未落盘

  await page.fill("#ss-gap", "11");
  await page.locator("#ss-gap").blur(); // 数字框改动在 blur(change) 才标脏——模拟真实用户离开字段
  await page.locator("#ss-save").click();
  const pwModal2 = page.locator(".pm-backdrop").first();
  await expect(pwModal2).toBeVisible();
  await pwModal2.locator('input[type="password"]').fill(ADMIN_PASS);
  await pwModal2.getByRole("button", { name: "确认操作" }).click();
  await expect(page.locator("#ss-dirty")).toBeHidden();
  await page.reload();
  await expect(page.locator("#ss-gap")).toHaveValue("11"); // 保存后回读一致

  // ⑤ 非法值不落盘：容量上限超过后端范围（0~100000）→ 报错且回读仍旧值
  await page.getByRole("tab", { name: "容量配额", exact: true }).click();
  await expect(page.locator("#set-max-users")).toHaveValue("500");
  await expect(page.locator("#set-max-accounts")).toHaveValue("200");
  await page.fill("#set-max-users", "200000");
  await page.locator("#set-max-users").blur();
  await page.locator("#set-cap-save").click();
  // 非法值在后端**校验阶段**就被 400 打回（早于口令门，故不弹口令框），错误就地落在卡内提示
  await expect(page.locator("#set-cap-tip")).toContainText("100000");
  await page.reload();
  await expect(page.locator("#set-max-users")).toHaveValue("500");

  // ⑥ 公告：换行注入被拦（后端禁换行，前端在输入阶段就把换行族换成空格，提交前拦住）
  await page.getByRole("tab", { name: "公告", exact: true }).click();
  await expect(page.locator("#set-announcement")).toBeVisible();
  await page.fill("#set-announcement", "第一行\n第二行");
  await expect(page.locator("#set-announcement")).toHaveValue("第一行 第二行");
  await expect(page.locator("#set-ann-dirty")).toBeVisible();
  // 草稿保存不走口令门（PUT /api/announcement 只写草稿），保存后脏清除
  await page.locator("#set-ann-save").click();
  await expect(page.locator("#set-ann-dirty")).toBeHidden();

  // ⑦ 其余分区渲染（主管理员专属卡也要在）
  await page.getByRole("tab", { name: "通知通道", exact: true }).click();
  // 推送渠道下拉：两栈共通的 data-select-field 锚点（legacy 自研控件根 / Vue 包裹层）
  await expect(page.locator('[data-select-field="sn-type"]')).toBeVisible();
  await expect(page.locator("#sm-to")).toBeVisible();
  await page.getByRole("tab", { name: "健康与探针", exact: true }).click();
  // EP el-switch 的真实 input 是视觉隐藏的（可见件是 .el-switch__core），故用 toBeAttached
  // 而不是 toBeVisible——两栈都满足"该开关控件在位"这条语义。
  await expect(page.locator("#sh-verify")).toBeAttached();
  await expect(page.locator("#sh-probe-enable")).toBeAttached();
  await page.getByRole("tab", { name: "执行体", exact: true }).click();
  await expect(page.locator("#set-exec-table")).toBeVisible();
  await expect(page.locator("#set-exec-kpi-progress")).not.toBeEmpty();
  await page.getByRole("tab", { name: "系统开关", exact: true }).click();
  await expect(page.locator("#set-gp-pause")).toBeVisible();
  await expect(page.locator("#set-rp-pause")).toBeVisible();

  // ======== P3 收官重设计：调度卡（响应式收口 / 画布降级 / A15 / EP 精修） ========
  // 这些断言表达"重设计后应当如何"。迁移前（未重设计）为红：均匀态仍渲染 210px 画布、
  // 页签条在 ≤720 横向溢出、el-select 比 .input 矮 8px。e2e 先行即为了把这些缺口钉成可验收项。

  await page.getByRole("tab", { name: "签到调度", exact: true }).click();
  await expect(page.locator("#set-panel-schedule")).toBeVisible();

  // ⑧a 画布按分布态降级：默认「均匀分布」下不渲染 210px 钟形画布（均匀态没有钟形可画，
  //     画布只是空矩形 + 解释不存在之物的图例），改由一行紧凑说明承担；切到正态才出现画布。
  //     data-dist-state 是重设计新增的稳定锚点。
  const distViz = page.locator("[data-dist-viz]");
  await expect(distViz).toHaveAttribute("data-dist-state", "uniform");
  await expect(page.locator("[data-dist-viz] canvas")).toHaveCount(0);
  await expect(distViz).toContainText("均匀分布");

  // ⑧b 正态图表默认只读（工单 4gvh，用户 2026-10-04 口径）：画布与滑杆这类"直接操作控件"
  //     在窄屏有误触风险，默认一律只读，按下显式「编辑」按钮才可操作。门必须是真的状态
  //     切换（不是视觉覆盖）：只读态指针事件不得到达处理函数、键盘微调不生效、滑杆真禁用。
  //     峰尖拖拽只在正态分布下武装，故先把分布切到「正态分布」再验。
  await page.locator('[data-select-field="ss-dist"] .el-select__wrapper').click();
  await page.getByRole("option", { name: "正态分布（钟形拟人）" }).click();
  await expect(distViz).toHaveAttribute("data-dist-state", "normal");
  const canvas = page.locator("[data-dist-viz] canvas");
  await expect(canvas).toBeVisible();
  await canvas.scrollIntoViewIfNeeded();
  const muInput = page.locator('[data-ed="muMid"]');
  const sgLoInput = page.locator('[data-ed="sgLo"]');
  const editBtn = page.locator("#ss-edit");

  /** 画布中部按下 → 向右下拖动一次（远离时间轴底座 = 走峰尖抓取路径）。 */
  const dragCanvas = async (): Promise<void> => {
    const box = await canvas.boundingBox();
    if (!box) throw new Error("dist-viz canvas 没有可拖拽的边界框");
    const x = box.x + box.width / 2;
    const y = box.y + box.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + 60, y + 30, { steps: 8 });
    await page.mouse.up();
  };

  // ⑧b-1 默认只读（工单行为一）：门关着时拖拽不改变任何参数——先把行为钉在最前，
  //       否则"现状默认可操作"这条实缺口会被后面的选择器断言盖住，红得看不出根因。
  const muIdle = await muInput.inputValue();
  const sgIdle = await sgLoInput.inputValue();
  await dragCanvas();
  await expect.poll(async () => muInput.inputValue()).toBe(muIdle);
  await expect.poll(async () => sgLoInput.inputValue()).toBe(sgIdle);

  // ⑧b-2 默认只读：按钮态可见且可及（aria-pressed），画布真只读（aria-disabled + 只读类）。
  await expect(editBtn).toHaveAttribute("aria-pressed", "false");
  await expect(editBtn).toHaveText("编辑");
  await expect(canvas).toHaveAttribute("aria-disabled", "true");
  await expect(canvas).toHaveClass(/is-readonly/);
  await expect(page.locator("[data-dist-hint]")).toBeHidden();

  // ⑧b-3 只读态：键盘微调不生效（画布不该是隐藏后门）。
  await canvas.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(async () => muInput.inputValue()).toBe(muIdle);

  // ⑧c 按下「编辑」→ 进入可操作态（工单行为二）：按钮态切换可见 + 画布与滑杆同时解锁。
  await editBtn.click();
  await expect(editBtn).toHaveAttribute("aria-pressed", "true");
  await expect(editBtn).toHaveText("完成");
  await expect(canvas).toHaveAttribute("aria-disabled", "false");
  await expect(canvas).not.toHaveClass(/is-readonly/);
  await expect(page.locator("[data-dist-hint]")).toBeVisible();

  // ⑧c-1 峰尖拖拽行为钉（既有功能钉，一条不许丢）：一次真指针拖拽（横向 = 峰时 μ、
  //       纵向 = 散布 σ），断言 μ 读数（峰值中心）与 σ 读数**同时**变化。
  //       为什么必须真拖：这条交互纯指针驱动，单元测试覆盖不到；而 DistViz 早期实现一次手势
  //       步里先 emit μ 再 emit σ，第二次展开的是父组件尚未更新的 props，μ 被旧值静默还原
  //       （每次拖动只有 σ 生效）——CI 全绿也照样是坏的。断言 μ 必须变化正是防该回归的空转钉。
  const muBefore = await muInput.inputValue();
  const sgBefore = await sgLoInput.inputValue();
  await dragCanvas();
  await expect.poll(async () => muInput.inputValue()).not.toBe(muBefore);
  await expect.poll(async () => sgLoInput.inputValue()).not.toBe(sgBefore);

  // ⑧c-2 键盘可达性（重设计新增提示的对应行为钉）：可操作态下画布聚焦后方向键微调峰值时刻。
  await canvas.focus();
  const muBeforeKey = await muInput.inputValue();
  await page.keyboard.press("ArrowRight");
  await expect.poll(async () => muInput.inputValue()).not.toBe(muBeforeKey);

  // ⑧d 退出路径（工单行为三）：再按「完成」回到只读；此后直接操作一律再次失效。
  await editBtn.click();
  await expect(editBtn).toHaveAttribute("aria-pressed", "false");
  await expect(editBtn).toHaveText("编辑");
  await expect(canvas).toHaveClass(/is-readonly/);
  const muLocked = await muInput.inputValue();
  await dragCanvas();
  await expect.poll(async () => muInput.inputValue()).toBe(muLocked);

  // ⑧e 只读门不丢既有保存语义（工单行为四）：⑧c 在可操作态改出的 μ/σ 走既有显式保存
  //     （含口令门）→ 刷新后值保持，且门复位到默认只读。口令门档位 full + 300s 豁免：
  //     本用例内刚复核过口令（见 ④），此处可能不再弹出——两种情形都算通过。判据只钉
  //     "落盘 + 回读一致 + 门复位"；本段刻意不掺掐头去尾（见 ⑧f 放在刷新区之后的原因：
  //     窗口/裁剪一变，峰值中心与散布的派生读数跟着变，混在同段就分不清是"没落盘"
  //     还是"读数换了口径"）。
  const muSaved = await muInput.inputValue();
  const sgSaved = await sgLoInput.inputValue();
  await page.locator("#ss-save").click();
  const pwModal3 = page.locator(".pm-backdrop").first();
  const pwShown = await pwModal3.waitFor({ state: "visible", timeout: 3000 }).then(() => true, () => false);
  if (pwShown) {
    await pwModal3.locator('input[type="password"]').fill(ADMIN_PASS);
    await pwModal3.getByRole("button", { name: "确认操作" }).click();
  }
  await expect(page.locator("#ss-dirty")).toBeHidden();
  await page.reload();
  await page.getByRole("tab", { name: "签到调度", exact: true }).click();
  await expect(distViz).toHaveAttribute("data-dist-state", "normal"); // 分布方式已随本次保存落盘
  await expect(page.locator("#ss-edit")).toHaveAttribute("aria-pressed", "false");
  await expect(muInput).toHaveValue(muSaved);
  await expect(sgLoInput).toHaveValue(sgSaved);

  // 顺序约束：⑧f 起调度卡置脏且门停在开态，直到 ⑤ 的整页 goto 为止都不得插入页内页签点击（否则弹未保存守卫）。
  // ⑧f 同族控件排查（泛化口径：滑杆类同属"直接操作控件"）：掐头/去尾两枚 el-slider 与画布
  //     同吃一道门——默认只读，按下「编辑」后才可操作。判据取两条：滑杆按钮的 aria-disabled
  //     （可及性语义）+ 点跑道不改变读数（真行为，不是只有灰壳）。逐个列证据，不做静默豁免。
  const edgeFrontSlider = page.locator('[data-range-field="ss-edge-front"] [role="slider"]');
  const edgeBackSlider = page.locator('[data-range-field="ss-edge-back"] [role="slider"]');
  const edgeFrontHelp = page.locator(".sched-f-front .field-help");
  const edgeFrontRunway = page.locator('[data-range-field="ss-edge-front"] .el-slider__runway');
  /** 在掐头滑杆跑道 80% 处点一下（EP 的 mousedown 就跳到该刻度；禁用时该分支早退）。
      先把字段滚到视口中线：保存行 `position: sticky; bottom: 0` 会盖住视口下缘，落在卡片
      底部的滑杆若贴边，同坐标的点击会打在吸底行上，断言就变成"什么都没测"。 */
  const slideEdge = async (): Promise<void> => {
    await page.locator('[data-range-field="ss-edge-front"]').evaluate((el) => el.scrollIntoView({ block: "center" }));
    const box = await edgeFrontRunway.boundingBox();
    if (!box) throw new Error("掐头滑杆没有可操作的边界框");
    await page.mouse.click(box.x + box.width * 0.8, box.y + box.height / 2);
  };
  await expect(edgeFrontSlider).toHaveAttribute("aria-disabled", "true");
  await expect(edgeBackSlider).toHaveAttribute("aria-disabled", "true");
  const edgeIdle = await edgeFrontHelp.textContent();
  await slideEdge();
  await expect.poll(async () => edgeFrontHelp.textContent()).toBe(edgeIdle);
  await editBtn.click();
  await expect(edgeFrontSlider).toHaveAttribute("aria-disabled", "false");
  await expect(edgeBackSlider).toHaveAttribute("aria-disabled", "false");
  // 同一个手势、同一处坐标：门开了才改值。两半边合起来才证明"门"在起作用——
  // 只测半边，会把"点没点到"误读成"门生效"。
  await slideEdge();
  await expect.poll(async () => edgeFrontHelp.textContent()).not.toBe(edgeIdle);

  // ⑨ A15：三个布尔开关（周六 / 周日 / 自选）合并进同一列并各自带标注。
  await expect(page.locator("#ss-sat")).toBeAttached();
  await expect(page.locator("#ss-sun")).toBeAttached();
  await expect(page.locator("#ss-time-pref")).toBeAttached();
  await expect(page.locator("#set-schedule")).toContainText("周末与自选");

  // ⑩ EP 控件精修：同排 el-select 与原生 .input 同高（重设计前 32 vs 40，差 8px）。
  const selH = await page.locator('[data-select-field="ss-order"] .el-select__wrapper').evaluate((e) => e.getBoundingClientRect().height);
  const gapH = await page.locator("#ss-gap").evaluate((e) => e.getBoundingClientRect().height);
  expect(Math.abs(selH - gapH), `el-select ${selH}px 与 .input ${gapH}px 不同高`).toBeLessThanOrEqual(1);

  // ⑪ 响应式（用户裁决 2026-10-04）：≤720 页签回归**单行下划线横滚**，靠"下一个页签被视口
  //     裁掉一部分"（半露）引导右滑 —— 半露是期望形态，不是缺陷。断言：① 页面级无横向溢出；
  //     ② 页签条有意可横向滚动；③ 调度卡正文无横向溢出；④ 360/375 下确实存在被裁的半露页签；
  //     ⑤ 深链/切换后活动页签完整滚入可视区。
  for (const vw of [720, 375]) {
    await page.setViewportSize({ width: vw, height: vw === 375 ? 792 : 900 });
    await page.waitForTimeout(150);
    // ① 页面级无横向溢出（横滚只发生在页签条内部）
    const pageOver = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(pageOver, `${vw}px 整页横向溢出`).toBeLessThanOrEqual(1);
    // ② 页签条有意可横向滚动
    const overflowX = await page.locator("[data-settings-tabs] .tabs-scroll").evaluate((el) => getComputedStyle(el).overflowX);
    expect(["auto", "scroll"], `${vw}px 页签条不是可横滚容器`).toContain(overflowX);
    // ③ 调度卡正文无横向溢出
    const cardOverflow = await page.locator("#set-schedule").evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(cardOverflow, `${vw}px 调度卡横向溢出`).toBeLessThanOrEqual(1);
  }

  // ④ 半露：360/375 下必有一个页签右缘被视口裁掉、左缘仍在视口内（右滑引导）。
  for (const vw of [375, 360]) {
    await page.setViewportSize({ width: vw, height: 792 });
    await page.waitForTimeout(150);
    await page.locator("[data-settings-tabs] .tabs-scroll").evaluate((el) => { el.scrollLeft = 0; });
    await page.waitForTimeout(100);
    const boxes = await page.locator("[data-settings-tabs] .tab").evaluateAll(
      (els) => els.map((e) => { const r = e.getBoundingClientRect(); return { left: r.left, right: r.right }; }),
    );
    const halfRevealed = boxes.some((b) => b.left < vw - 1 && b.right > vw + 1);
    expect(halfRevealed, `${vw}px 未出现被视口裁切的"半露"页签（右滑引导缺失）`).toBe(true);
  }

  // ⑤ 深链 ?tab=switches 后活动页签完整可见（只横滚页签条本身）。360 下「系统开关」在末尾，
  //    必须被 scrollTabIntoView（只横滚页签条自身，改 scrollLeft）带进可视区，否则深链进来
  //    的用户看不到自己所在分区。
  await page.setViewportSize({ width: 360, height: 792 });
  await page.goto("/work/settings?tab=switches");
  await expect(page.locator("#set-panel-switches")).toBeVisible();
  await expect.poll(async () => {
    const r = await page.locator("#set-tab-switches").boundingBox();
    return !!r && r.x >= -1 && r.x + r.width <= 361;
  }, { message: "360px 深链到「系统开关」后活动页签未完整滚入视口" }).toBe(true);

  // ⑥ snap 恢复幂等（回归钉）：连续快速切分区（键盘自重复 / 连点，间隔 < smooth 滚动时长）
  //    会让多次 scrollTabIntoView 在途，而同一段滚动只派发一次 scrollend → 两次 restore 顺次
  //    触发。早期实现把"捕获到的内联值"写回，第二次调用捕获到的正是第一次写入的 "none"，
  //    末次写回把内联 none 永久留下（覆盖样式表的 x proximity，吸附增强静默失效且不自愈）。
  //    构造：每次先把页签条停在末尾（此时左侧的「通知通道 / 公告」都在视口外），随后快速
  //    连点它们——两次调用都非"已完整可见"的早退，滚动互相重叠。断言：
  //    内联 scroll-snap-type 清空、计算值仍为样式表的 x 轴吸附。
  //    非空转：把实现退回"写回捕获值"后，该钉在 computed === "none" 处变红（已实测）。
  const tabsBox = page.locator("[data-settings-tabs] .tabs-scroll");
  for (let i = 0; i < 3; i++) {
    await tabsBox.evaluate((el) => { const b = el as HTMLElement; b.scrollLeft = b.scrollWidth; });
    await page.evaluate(() => (document.getElementById("set-tab-notify") as HTMLElement).click());
    await page.evaluate(() => (document.getElementById("set-tab-announcement") as HTMLElement).click());
  }
  await expect(page.locator("#set-panel-announcement")).toBeVisible();
  await expect.poll(async () =>
    tabsBox.evaluate((el) => (el as HTMLElement).style.scrollSnapType),
  { message: "连续切分区后内联 scroll-snap-type 未清空（snap 恢复不是幂等的）" }).toBe("");
  await expect.poll(async () =>
    tabsBox.evaluate((el) => getComputedStyle(el).scrollSnapType),
  { message: "连续切分区后计算值不是 x 轴吸附（内联 none 残留，吸附增强失效）" }).toMatch(/^\s*x(\s+proximity)?$/);
  // 半露在 snap 恢复后仍稳定存在（proximity 下静止态不得被吸附收拢）
  await tabsBox.evaluate((el) => { (el as HTMLElement).scrollLeft = 0; });
  const boxes2 = await page.locator("[data-settings-tabs] .tab").evaluateAll(
    (els) => els.map((e) => { const r = e.getBoundingClientRect(); return { left: r.left, right: r.right }; }),
  );
  expect(boxes2.some((b) => b.left < 359 && b.right > 361), "snap 恢复后半露消失").toBe(true);

  await page.setViewportSize({ width: 1280, height: 720 });

  // ⑫ 大视口密度分级（1439/1440 边界）：≥1440 调度卡表单升三列（密度随视口放大、不留
  //     大片空白），1439 仍是双列；两档都无横向溢出。三列断言按计算样式的列数取，
  //     直接钉住"密度分级"这条设计裁决。
  //     注意：⑤ 的深链把活动分区留在了「系统开关」，此处必须先切回「签到调度」——隐藏
  //     （display:none）面板没有布局，getComputedStyle 的列数不可信。
  await page.getByRole("tab", { name: "签到调度", exact: true }).click();
  await expect(page.locator("#set-panel-schedule")).toBeVisible();
  const cols3 = async (): Promise<number> =>
    page.locator("#set-schedule .form-grid").evaluate(
      (el) => getComputedStyle(el).gridTemplateColumns.trim().split(/\s+/).length,
    );
  await page.setViewportSize({ width: 1439, height: 900 });
  await page.waitForTimeout(120);
  expect(await cols3(), "1439px 应为双列").toBe(2);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(120);
  expect(await cols3(), "1440px 应升为三列").toBe(3);
  const wideOverflow = await page.locator("#set-schedule").evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(wideOverflow, "1440px 调度卡横向溢出").toBeLessThanOrEqual(1);
  await page.setViewportSize({ width: 1280, height: 720 });

  // ======== 凭据文案 + 搜索提交（0h7p + 311u）——并入本用例复用同一管理员会话 ========
  // 判据（工单原文）：
  //   A（0h7p）管理端账号表单的口令字段必须读得出「不是本站登录密码」——标签带「易班」限定词，
  //     并有一行区分说明。生产实证：管理员把该字段当成本站登录密码改，账号归属人至今登不上。
  //   B（311u）三页七个搜索框在移动端既要有可见按钮、又要能回车提交；桌面「打字即时筛选」
  //     是既有语义，不得退化。
  // 为什么并入本用例：与看板/账号页同理，/api/login 有 60 秒 10 次/IP 的限速，整套 e2e 共用
  // 同一实例与 127.0.0.1，单开一条 spec 会多一次登录、把后面的用例顶到 429。

  // ---- A. 管理端账号表单：口令字段标签 + 区分说明 ----
  await page.goto("/work/accounts");
  await page.locator("[data-add-account]").first().click();
  const pwForm = page.locator(".pm-backdrop, .el-dialog").first();
  await expect(pwForm).toBeVisible();
  // 用 CSS :has 结构定位（不用 filter({has})：那里的 inner locator 是相对外层再查一次的，
  // 把已经锚在 pwForm 上的 locator 传进去会变成双侧锚定、一条也命不中）。
  // 添加入口默认「不绑定」，手填分支（含「初始密码」）不渲染 ⇒ 本页只有一个 password 输入。
  const pwField = pwForm.locator('label.field:has(input[type="password"])').first();
  await expect(pwField.locator(".field-label"), "管理端口令标签丢了「易班」限定词").toHaveText(/易班密码/);
  await expect(pwField.locator(".field-help"), "缺「不是本站登录密码」区分说明").toContainText("不是本站登录密码");
  await pwForm.getByRole("button", { name: /取消/ }).first().click();
  await expect(pwForm).toBeHidden();

  // ---- B1. 桌面非退化：三页七个框都在 form[role=search] 内，打字即筛（不按回车也生效） ----
  const boxes = page.locator('form[role="search"]');
  // 721~1100px 是「工具行不换行 + 组内多了一个搜索按钮」的挤压区：本批把搜索框与按钮并排后，
  // 桌面宽度预算变紧的地方就在这里（≤720 走的是另一套 grid/整行规则，360px 守卫测不到它）。
  const midOverflow = async (label: string) => {
    const over = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(over, `${label} 在 900px 出现横向滚动`).toBeLessThanOrEqual(1);
  };
  await page.goto("/work/users");
  await expect(boxes, "用户页应有 3 个可检索框").toHaveCount(3);
  await page.locator('[data-usr-tab="normal"]').click();
  await page.fill("#usr-normal-search", "e2e-user");
  await expect(page.locator("#usr-normal-count"), "桌面端打字即筛（无需回车）").toHaveText(/1 人匹配 \/ 共 2 人/);
  await page.fill("#usr-normal-search", "");
  await page.setViewportSize({ width: 900, height: 800 });
  await midOverflow("用户页");
  await page.goto("/work/accounts");
  await expect(boxes, "账号页应有 3 个可检索框").toHaveCount(3);
  await page.locator('[data-acct-tab="active"]').click();
  await midOverflow("账号页");
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto("/data/logs");
  await expect(boxes, "日志页应有 1 个检索框").toHaveCount(1);

  // ---- B2. 移动端 360×792@DPR4：按钮与回车两条路径都生效 ----
  // 必须另开 context：deviceScaleFactor / isMobile 只能在建 context 时给（setViewportSize
  // 改不了这两项）。会话用 storageState 继承，不额外调 /api/login（登录限速是全套稀缺资源）。
  const mobileCtx = await page.context().browser()!.newContext({
    viewport: { width: 360, height: 792 },
    deviceScaleFactor: 4,
    isMobile: true,
    hasTouch: true,
    storageState: await page.context().storageState(),
  });
  const mp = await mobileCtx.newPage();
  const focusedId = () => mp.evaluate(() => document.activeElement?.id ?? "");
  // 窄屏守卫：检索组由「一个控件独占一行」改成「输入框 + 按钮同排」，最坏的失效不是按钮
  // 不可见，而是它把行撑出视口——那种情况 Playwright 的 toBeVisible 照样通过。
  const noOverflow = async (label: string) => {
    const over = await mp.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(over, `${label} 在 360px 出现横向滚动`).toBeLessThanOrEqual(1);
  };

  // 用户页：三个可检索组各有一个「搜索」按钮（进无障碍树），回车提交不整页重载、且收起键盘
  await mp.goto("/work/users");
  for (const key of ["pending", "normal", "vacant"]) {
    await mp.locator(`[data-usr-tab="${key}"]`).click();
    const btn = mp.locator(`#usr-panel-${key} form[role="search"] button[type="submit"]`);
    await expect(btn).toBeVisible();
    await expect(btn).toHaveAccessibleName(/搜索/);
  }
  await mp.locator('[data-usr-tab="normal"]').click();
  const usrInput = mp.locator("#usr-normal-search");
  const usersUrl = mp.url();
  await noOverflow("用户页");
  // **注意断言能证明什么**：桌面/移动端的筛选本来就是即时的（v-model 驱动 computed），
  // 所以"筛选生效"在 fill 那一刻就成立，它证明不了提交路径通。提交路径真正独有、且用户
  // 可见的两个效果是：① 没有被原生 GET 接管（URL 不变、内存态不丢）；② 输入框失焦
  // （移动端软键盘收起，结果才露得出来）。两条路径各测这两个。
  await usrInput.fill("e2e-user");
  await expect(mp.locator("#usr-normal-count")).toHaveText(/1 人匹配 \/ 共 2 人/); // 即时筛选（上下文）
  await usrInput.press("Enter");
  expect(mp.url(), "回车被原生 GET 接管（整页重载）").toBe(usersUrl);
  await expect
    .poll(focusedId, { message: "回车后输入框未失焦（移动端键盘挡着结果）" })
    .not.toBe("usr-normal-search");
  // 按钮路径：不重载（点击本身会把焦点从输入框拿走，故不再断言失焦——那条断言测不到
  // commitSearch()，属误导性覆盖）
  await usrInput.fill("no-such-user-zzz");
  await mp.locator('#usr-panel-normal form[role="search"] button[type="submit"]').click();
  await expect(mp.locator("#usr-normal-count")).toHaveText(/0 人匹配 \/ 共 2 人/);
  expect(mp.url(), "点按钮被原生 GET 接管").toBe(usersUrl);

  // 组合输入（中文输入法）期的那次回车是「提交候选词」，不是检索指令：它不得让输入框失焦
  // （失焦收起软键盘，而拼音串还没上屏）。本页与账号页各钉一份——同族的日志/审计两页早已
  // 各自钉住，只留一处会被"改一处忘另一处"绕过。Chromium 不为 isComposing 跳过隐式提交，
  // 故这里钉的是提交路径上的守卫，不是 keydown 处理器里的提前返回。
  // 用 CDP 造真组合态；后半段在同一页做非组合期对照，证明"仍是焦点"不是"回车没生效"的假绿。
  const cdpUsr = await mobileCtx.newCDPSession(mp);
  await usrInput.fill("e2e-user");
  await expect(mp.locator("#usr-normal-count")).toHaveText(/1 人匹配 \/ 共 2 人/); // 组合前的上下文
  await usrInput.click();
  await cdpUsr.send("Input.imeSetComposition", { text: "zhongguo", selectionStart: 8, selectionEnd: 8 });
  await usrInput.press("Enter");
  await mp.waitForTimeout(400);
  expect(await focusedId(), "组合期的回车让输入框失焦（发出去的是未提交的拼音串）").toBe("usr-normal-search");
  expect(mp.url(), "组合期回车被原生 GET 接管").toBe(usersUrl);
  // 对照 + 即时筛选不变（同页两条）：换干净页面后打字即筛（不必回车或点按钮），非组合期回车
  // 照常失焦。两条合起来证明上面的"仍是焦点"不是"回车没生效"的假绿，也证明守卫没有引入
  // "提交前不过滤"的第二态。
  await mp.goto("/work/users");
  await mp.locator('[data-usr-tab="normal"]').click();
  await mp.locator("#usr-normal-search").fill("e2e-user");
  await expect(mp.locator("#usr-normal-count"), "打字即筛的既有语义被改动").toHaveText(/1 人匹配 \/ 共 2 人/);
  await mp.locator("#usr-normal-search").press("Enter");
  await expect.poll(focusedId, { message: "非组合期回车没失焦（对照失败）" }).not.toBe("usr-normal-search");

  // 账号页（页签深链是既有契约：点页签会写 ?tab=，故 URL 基线要在点完之后取）
  await mp.goto("/work/accounts");
  await mp.locator('[data-acct-tab="active"]').click();
  await expect(mp).toHaveURL(/[?&]tab=active/);
  const acctUrl = mp.url();
  const acctBtn = mp.locator('#acct-panel-active form[role="search"] button[type="submit"]');
  await expect(acctBtn).toBeVisible();
  await expect(acctBtn).toHaveAccessibleName(/搜索/);
  const acctInput = mp.locator("#active-search");
  await noOverflow("账号页");
  await acctInput.fill("e2e-user-acct");
  await acctInput.press("Enter");
  await expect(mp.locator("#accounts-tbody tr")).toHaveCount(1);
  expect(mp.url(), "回车被原生 GET 接管").toBe(acctUrl);
  await expect.poll(focusedId, { message: "回车后输入框未失焦" }).not.toBe("active-search");
  await acctInput.fill("e2e-admin-acct");
  await acctBtn.click();
  await expect(mp.locator("#accounts-tbody tr")).toHaveCount(1);
  await expect(mp.locator("#accounts-tbody tr").first()).toContainText("e2e-admin-acct");
  expect(mp.url(), "点按钮被原生 GET 接管").toBe(acctUrl); // 同上：点击路径不断言失焦（点击自带）

  // 账号页的组合输入守卫（本页是另一份载体：表单与处理函数各有一份，故与用户页各钉各的）
  const cdpAcct = await mobileCtx.newCDPSession(mp);
  await acctInput.fill("e2e-user-acct");
  await expect(mp.locator("#accounts-tbody tr")).toHaveCount(1);
  await acctInput.click();
  await cdpAcct.send("Input.imeSetComposition", { text: "zhongguo", selectionStart: 8, selectionEnd: 8 });
  await acctInput.press("Enter");
  await mp.waitForTimeout(400);
  expect(await focusedId(), "组合期的回车让输入框失焦（发出去的是未提交的拼音串）").toBe("active-search");
  expect(mp.url(), "组合期回车被原生 GET 接管").toBe(acctUrl);
  // 对照 + 即时筛选不变（与用户页同两条口径，本页各自成立）
  await mp.goto("/work/accounts");
  await mp.locator('[data-acct-tab="active"]').click();
  const acctInput2 = mp.locator("#active-search");
  await acctInput2.fill("e2e-user-acct");
  await expect(mp.locator("#accounts-tbody tr"), "打字即筛的既有语义被改动").toHaveCount(1);
  await acctInput2.press("Enter");
  await expect.poll(focusedId, { message: "非组合期回车没失焦（对照失败）" }).not.toBe("active-search");

  // 日志页：检索是服务端查询（关键字随请求发出），回车与按钮两条路径都要生效
  await mp.goto("/data/logs");
  const logForm = mp.locator('form[role="search"]');
  await expect(logForm).toHaveCount(1);
  await noOverflow("日志页");
  const logInput = logForm.locator('input[type="search"]');
  // 本段多条断言数"请求恰好 N 个"，而本页每 10s 自动轮询一次 /api/logs：轮询落进观测窗就
  // 是伪红（400ms 窗实测约 4%/次）。先关掉自动刷新（关掉后实测 12 秒 0 次轮询；该设置存
  // localStorage，同一 context 内换页后仍生效），并在每次归零前等首屏那一次请求落地。
  // 级别档同样先切到全量档：本段要核日志正文（默认档会收起带标记的 INFO 行）。
  await mp.locator(".logs-check input").uncheck();
  await mp.locator("#logs-level-warn").uncheck();
  await expect(mp.locator(".log-box")).toContainText("（today）");
  await logInput.fill("无匹配关键字-zzz");
  await logInput.press("Enter");
  await expect(mp.locator(".logs-empty")).toContainText("无匹配日志行");
  await logInput.fill("签到成功");
  await logForm.locator('button[type="submit"]').click();
  await expect(mp.locator(".log-box")).toContainText("签到成功");
  await expect(mp.locator(".log-box")).not.toContainText("生成定位");
  // 一次回车只发一次检索请求。**这条钉的性质要看清**：该页 load() 首行有 `if (busy) return;`，
  // 所以"1 个请求"也可能由 busy 兜住，而不是由处理器内的 preventDefault 保证；真正钉住后者的是
  // 审计页那条同名断言（该页 search() 无重入守卫，去掉 preventDefault 即变红）。这里保留是防
  // "发两遍请求"这一用户可见后果的回归守卫。
  let logReqs = 0;
  await mp.route("**/api/logs**", (route) => {
    logReqs += 1;
    void route.continue();
  });
  await logInput.fill("（today）");
  logReqs = 0;
  await logInput.press("Enter");
  await expect(mp.locator(".log-box")).toContainText("（today）");
  await expect.poll(() => logReqs, { message: "回车没发出检索请求" }).toBeGreaterThan(0);
  expect(logReqs, "一次回车发了两遍检索请求").toBe(1);

  // 组合输入（中文输入法）期的那次回车**不得**发起检索——否则发出去的是**未提交**的拼音串
  // （守卫缺失时实测请求 URL 为 `?q=zhongguo`）。Chromium 不为 isComposing 跳过隐式提交，
  // 故这条钉的是"提交路径上的守卫"而不是"keydown 处理器里的提前返回"。
  // 用 CDP 造真组合态（Input.imeSetComposition 会派发 compositionstart，实测 keydown 的
  // isComposing 也为真）。后半段换一张干净页面做对照：非组合期回车照常发出 1 个请求，
  // 证明前半段的"0"不是"回车根本没生效"的假绿。
  const cdp = await mobileCtx.newCDPSession(mp);
  await mp.goto("/data/logs");
  await mp.locator("#logs-level-warn").uncheck(); // 全量档：下文的标记与关键字都落在 INFO 行
  await expect(mp.locator(".log-box")).toContainText("（today）"); // 首屏请求先落地，勿落进观测窗
  await logInput.click();
  await cdp.send("Input.imeSetComposition", { text: "zhongguo", selectionStart: 8, selectionEnd: 8 });
  logReqs = 0;
  await logInput.press("Enter");
  await mp.waitForTimeout(400);
  expect(logReqs, "组合期的回车发起了检索（发出去的是未提交的拼音串）").toBe(0);
  await mp.goto("/data/logs");
  await mp.locator("#logs-level-warn").uncheck();
  await expect(mp.locator(".log-box")).toContainText("（today）");
  await logInput.fill("签到成功");
  logReqs = 0;
  await logInput.press("Enter");
  await expect.poll(() => logReqs, { message: "非组合期回车没发出检索请求（对照失败）" }).toBe(1);

  // 审计页也进移动端无横向溢出守备面（工单 w26p）：起止日期那一行 360px 下原本不换行，
  // 第二个日期框右边缘越界 18px。页面级 scrollWidth 是代理指标，故同时钉直接指标：
  // 两个日期框都完整落在视口内（换行生效后它们各占一行或并排收缩，两种形态都满足）。
  await mp.goto("/data/audit");
  await noOverflow("审计页");
  const dateInputs = mp.locator(".audit-dates input");
  await expect(dateInputs).toHaveCount(2);
  const vp = mp.viewportSize()!;
  for (const idx of [0, 1]) {
    const box = await dateInputs.nth(idx).boundingBox();
    expect(box, `审计页第 ${idx + 1} 个日期框取不到位置`).not.toBeNull();
    expect(box!.x + box!.width, `审计页第 ${idx + 1} 个日期框右边缘越界`).toBeLessThanOrEqual(vp.width);
  }
  await mobileCtx.close();

  // 宽视口这一头也要钉（首轮审查 D1）：窄屏换行必须只在窄屏生效。`.input` 的 width:100%
  // 让 flex 基宽等于行宽，无条件 wrap 会把两个日期框在宽屏也压成两行——实测 1280/1100/1024/
  // 900/768 各档两框分行、每框被拉到容器宽。此断言钉「两框同一行」（y 相同），
  // 与上面 360px 那条（两框都在视口内）两头闭合。
  for (const vw of [768, 1280]) {
    await page.setViewportSize({ width: vw, height: 800 });
    await page.goto("/data/audit");
    const wideDates = page.locator(".audit-dates input");
    await expect(wideDates).toHaveCount(2);
    const boxes = await Promise.all([wideDates.nth(0).boundingBox(), wideDates.nth(1).boundingBox()]);
    expect(boxes[0] && boxes[1], `${vw}px 取不到日期框位置`).toBeTruthy();
    const dy = Math.abs(boxes[0]!.y - boxes[1]!.y);
    expect(dy, `${vw}px 起止日期两框不在同一行：y=${boxes[0]!.y} / ${boxes[1]!.y}`).toBeLessThanOrEqual(1);
  }
});
