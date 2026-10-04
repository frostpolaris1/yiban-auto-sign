import { expect, test } from "@playwright/test";

// 用户端账号页真浏览器端到端（P2 整页迁移后）。
// 数据由 e2e/server.py 预置：普通用户 e2e-user@example.com 名下有一个生效账号
// （e2e-user-acct / 13800138001）。会话走 API 登录（表单登录由 logs.spec 覆盖一次即可，
// 避免反复访问 /login 撞上服务端的登录循环守卫）。
const USER_EMAIL = "e2e-user@example.com";
const USER_PASS = "UserPass123!";

async function loginUser(page: import("@playwright/test").Page): Promise<void> {
  const resp = await page.request.post("/api/login", { data: { username: USER_EMAIL, password: USER_PASS } });
  expect(resp.ok()).toBeTruthy();
}

test("用户端账号页：账号行/调度提示/自选时段/邮件开关/改密入口，且无管理端专有卡", async ({ page }) => {
  await loginUser(page);
  await page.goto("/user/account");

  // 页头 + 调度提示条（由 /api/me 填充）
  await expect(page.locator("h1.page-title")).toHaveText("账号与设置");
  await expect(page.locator(".schedule-banner")).toContainText("签到方式");

  // 账号行：名称 / 状态徽标 / **完整手机号**（自己看自己的账号按既有设计显示完整号）
  const card = page.locator(".account-card").first();
  await expect(card.locator(".account-name")).toHaveText("e2e-user-acct");
  await expect(card.locator(".account-title-row .badge")).toHaveText("已生效");
  await expect(card.locator(".account-meta")).toContainText("13800138001");

  // 生效账号的四个动作（日历链接 / 暂停 / 编辑 / 删除）
  const actions = card.locator(".account-actions");
  await expect(actions.getByRole("link", { name: "签到日历" })).toHaveAttribute("href", /\/user\/calendar$/);
  await expect(actions.getByRole("button", { name: "暂停签到" })).toBeVisible();
  await expect(actions.getByRole("button", { name: "编辑" })).toBeVisible();
  await expect(actions.getByRole("button", { name: "删除" })).toBeVisible();

  // 自选签到时间：默认收起 → 展开后槽位网格可见
  await expect(page.locator(".slot-grid")).toBeHidden();
  await page.getByRole("button", { name: /展开配置|收起/ }).click();
  await expect(page.locator(".slot-grid")).toBeVisible();
  await expect(page.locator(".slot-grid .slot").first()).toBeVisible();

  // 邮件提醒卡与改密入口存在
  await expect(page.locator(".card", { hasText: "邮件提醒" }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: /修改密码/ })).toBeVisible();

  // 侧栏账号块只显示邮箱本地部（legacy renderSidebarEmail 口径）；完整邮箱进 title
  await expect(page.locator("[data-account-email]").first()).toHaveText("e2e-user");
  await expect(page.locator("[data-account-email]").first()).toHaveAttribute("title", USER_EMAIL);

  // 用户端专有：注销账号；管理端专有：显示偏好（**不应出现**）
  await expect(page.getByRole("button", { name: /注销账号/ })).toBeVisible();
  await expect(page.locator("h2.panel-title", { hasText: "显示偏好" })).toHaveCount(0);

  // 侧栏「退出登录」可用（回归守卫，2026-10-03 实测踩过）：该按钮原先靠**各页自己绑**
  // `[data-user-logout]`，账号页迁到 Vue 后没人再绑 → 退出成了死键。现改与管理端
  // sidebar/topbar 同一写法（onclick 调全局 doLogout），不再需要任何页面级绑定。
  // 断言放在本条用例末尾（而非独立用例）：e2e 的登录限速按客户端 IP 计，多一次 API 登录
  // 就会把整套顶到 429——共享实例的套件里，登录次数本身是要省着用的资源。
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.locator("#login-form")).toBeVisible();
  // 会话确已清除：再访问用户端页面会被守卫弹回登录页
  await page.goto("/user/account");
  await expect(page).toHaveURL(/\/login$/);
});

test("用户端账号页：暂停/恢复往返、编辑保存、改密弹窗、注销确认（取消）", async ({ page }) => {
  await loginUser(page);
  await page.goto("/user/account");
  const card = page.locator(".account-card").first();
  const badge = card.locator(".account-title-row .badge");

  // ① 暂停：确认框 → 就地状态行 + 徽标变「已取消」
  await card.getByRole("button", { name: "暂停签到" }).click();
  await page.locator(".modal-foot .btn:not(.btn--ghost)").click();
  await expect(card.locator(".state-line--flash")).toContainText("已暂停签到");
  await expect(badge).toHaveText("已取消");

  // ② 恢复：无需确认框（legacy 语义），徽标回到「已生效」
  await card.getByRole("button", { name: "恢复签到" }).click();
  await expect(card.locator(".state-line--flash")).toContainText("已恢复签到");
  await expect(badge).toHaveText("已生效");

  // ③ 编辑：改名称 → 保存 → 行内名称更新（覆盖表单口径 + PUT + 重新拉取）
  await card.getByRole("button", { name: "编辑" }).click();
  const dialog = page.locator(".el-dialog");
  await expect(dialog).toBeVisible();
  const nameInput = dialog.locator('input[placeholder="如：我的易班账号"]');
  await nameInput.fill("e2e-改名后");
  await dialog.locator(".el-dialog__footer .btn--primary").click();
  await expect(dialog).toBeHidden();
  await expect(card.locator(".account-name")).toHaveText("e2e-改名后");

  // ④ 改密入口：复用 core.js 的共享弹窗（本页显式加载了 change-password.js）
  await page.getByRole("button", { name: /修改密码/ }).click();
  await expect(page.locator(".pm-backdrop")).toBeVisible();
  await expect(page.locator(".pm-backdrop")).toContainText("新密码");
  await page.keyboard.press("Escape");

  // ⑤ 注销账号：确认框出现（此处只验证入口与文案，不真的注销——那会销毁会话）
  await page.getByRole("button", { name: /注销账号/ }).click();
  await expect(page.locator(".pm-backdrop")).toContainText("7 天内可撤销恢复");
  await expect(page.locator(".modal-foot .btn:not(.btn--ghost)")).toHaveText("继续注销");
  await page.keyboard.press("Escape");
});

test("管理端我的账号：归属邮箱偏好可用、无注销卡、账号行与改密入口在位", async ({ page }) => {
  const resp = await page.request.post("/api/login", { data: { username: "admin", password: "TestPass1234!" } });
  expect(resp.ok()).toBeTruthy();
  await page.goto("/my/account");

  await expect(page.locator("h1.page-title")).toHaveText("我的账号");
  await expect(page.locator(".account-card").first().locator(".account-name")).toHaveText("e2e-admin-acct");
  await expect(page.getByRole("button", { name: /修改密码/ })).toBeVisible();
  await expect(page.locator(".card", { hasText: "邮件提醒" }).first()).toBeVisible();

  // 管理端专有：显示偏好在位且可切换（本机偏好，落 localStorage，不写服务端）
  const prefCard = page.locator(".card", { hasText: "显示偏好" }).first();
  await expect(prefCard).toBeVisible();
  const sw = prefCard.locator("input[type=checkbox]");
  const label = prefCard.locator("label.switch");
  await expect(sw).toBeChecked(); // 默认开（键缺失=开）
  // 点 label 而不是 input：legacy 的 .switch 用 .track 覆盖真实 checkbox，直接点 input 会被拦截
  await label.click();
  await expect(prefCard.locator(".set-tip")).toHaveText("已关闭");
  expect(await page.evaluate(() => localStorage.getItem("yiban-owner-email"))).toBe("0");
  await label.click();
  expect(await page.evaluate(() => localStorage.getItem("yiban-owner-email"))).toBe("1");

  // 用户端专有：注销账号卡**不得出现**
  await expect(page.getByRole("button", { name: /注销账号/ })).toHaveCount(0);

  // ---- 用户管理页（/work/users，P3 迁移后）----
  // 断言搭在本条用例里而不是新开 spec：登录限速是 **60 秒窗口 10 次/IP**，整套 e2e 已经
  // 贴着上限（多一次登录就把后面撞成 429）。这里复用 admin 会话，不额外登录。
  await page.goto("/work/users");
  await expect(page.locator("h1.page-title")).toHaveText("用户管理");

  // 页签与计数：四组里「已注销」无数据时连标签一起收掉
  await expect(page.locator('[data-usr-tab="pending"]')).toBeVisible();
  await expect(page.locator('[data-usr-tab="normal"]')).toContainText("2 人");
  await expect(page.locator('[data-usr-tab="deleted"]')).toHaveCount(0);

  // 默认分区是「待处理」（本环境为空）→ 空态给「下一步」而不是死路
  await expect(page.locator('[data-usr-panel="pending"] .empty__msg')).toHaveText("暂无待处理用户");
  await page.locator('[data-usr-panel="pending"] [data-empty-tab="normal"]').click();
  // 分区深链：切分区即写 ?tab=（本页自管，不走 core.js 的 data-tab-group 契约）
  await expect(page).toHaveURL(/[?&]tab=normal/);

  // 正式用户：内置主管理员行不可改；普通用户行只显示**遮罩**邮箱
  const normal = page.locator('[data-usr-panel="normal"]');
  await expect(normal.locator(".usr-row-master .usr-cell-mail")).toContainText("（主管理员）");
  await expect(normal.locator(".usr-row-master .usr-cell-actions")).toContainText("不可改");
  const row = normal.locator("tbody tr:not(.skel-row):not(.usr-row-master)").first();
  await expect(row.locator(".usr-cell-mail")).toContainText("e2e***@example.com");
  await expect(row.locator(".usr-cell-count")).toHaveText("1");
  // 完整邮箱绝不进 DOM（服务端下发完整值，客户端只在内存态持有）
  expect(await page.content()).not.toContain("e2e-user@example.com");

  // 行操作菜单：主管理员可切管理员身份 + 重置密码 / 清空账号 / 删除用户
  await row.getByRole("button", { name: /更多操作/ }).click();
  const menu = page.locator(".el-dropdown-menu:visible");
  await expect(menu).toContainText("设为管理员");
  await expect(menu).toContainText("重置密码");
  await expect(menu).toContainText("清空账号");
  await expect(menu).toContainText("删除用户");
  await page.keyboard.press("Escape");

  // 勾选一行 → 批量条显形（计数 + 动作集）
  await row.locator('input[type="checkbox"]').check();
  const bar = page.locator("#usr-batch-normal");
  await expect(bar).toBeVisible();
  await expect(bar).toContainText("已选 1 个");
  await expect(bar.getByRole("button", { name: "重置密码" })).toBeVisible();
  await expect(bar.getByRole("button", { name: "删除" })).toBeVisible();
  await bar.getByRole("button", { name: "取消选择" }).click();
  await expect(bar).toBeHidden();

  // 检索：无匹配 → 「无匹配结果」+「清除筛选」（而不是把按钮藏掉）
  await page.fill("#usr-normal-search", "no-such-user-zzz");
  await expect(normal.locator(".empty__msg")).toHaveText("无匹配结果");
  await normal.locator("[data-empty-clear]").click();
  await expect(row.locator(".usr-cell-mail")).toContainText("e2e***@example.com");
});
