import { adminTest as test, expect } from "./base";

/* 队列页（lcpx 试点）端到端判据。模拟的真实用户动作与可见结果：
   ① 管理员打开「队列」页 → 三组待办各按种子渲染，顶端一行今日摘要；
   ② 管理员在待审核行点「通过」并确认 → 该行从待审核消失，账号当日无结论 ⇒ 出现在
      「今日未签」组（真调 POST /api/accounts/<idx>/review）；
   ③ 管理员点「详情」 → 右侧抽屉展示对象详情与近 30 天事件；完整手机号（详情接口
      回传的明文 PII）不得出现在页面任何可见文本里；
   ④ 360 视口下抽屉全屏（宽度＝视口宽）。
   分页有界：5k 种子（YB_E2E_BULK_ACCOUNTS）下每组渲染行数 ≤ 页大小。
   种子来自 e2e/server.py：pending=137****7003、rejected=136****6004（都渲染在
   待审核组）、失败=138****8001（当日终态 failed）、未签=139****9002。
   审核流用例吃**自建**账号（createPendingAccount），共享种子零变更——
   responsive.spec 的待处理徽标断言依赖种子 pending+rejected=2。 */

const PENDING_PHONE = "137****7003";
const REJECTED_PHONE = "136****6004";
const FAILED_PHONE = "138****8001";
const UNSIGNED_PHONE = "139****9002";
/** 种子明文号：任何时刻都不允许出现在页面可见文本中。 */
const RAW_PHONE = "13800138001";
const BULK = Number(process.env.YB_E2E_BULK_ACCOUNTS ?? 0);

test("队列页三组渲染＋今日摘要", async ({ page }) => {
  test.setTimeout(BULK >= 1000 ? 120_000 : 30_000);
  await page.goto("/work/queue");

  const summary = page.locator("[data-queue-summary]");
  await expect(summary).toBeVisible();
  // 业务日（业务钟今天）与窗口：格式来自服务端，断言结构而非具体日期
  await expect(summary).toContainText(/\d{4}-\d{2}-\d{2} 周[一二三四五六日]/);
  await expect(summary).toContainText(/窗口 \d{2}:\d{2} ~ \d{2}:\d{2}/);
  // 种子：active 账号 2 个（5k 模式另有 BULK 个）＋当日失败 1 个
  await expect(summary).toContainText(`账号 ${2 + BULK}`);
  await expect(summary).toContainText("失败 1");

  // 待审核组：pending ＋ rejected 两行（服务端 group=pending 口径）
  const pendingGroup = page.locator('[data-queue-group="pending"]');
  await expect(pendingGroup.locator(`[data-queue-row][data-phone="${PENDING_PHONE}"]`)).toBeVisible();
  const rejectedRow = pendingGroup.locator(`[data-queue-row][data-phone="${REJECTED_PHONE}"]`);
  await expect(rejectedRow).toBeVisible();
  await expect(rejectedRow).toContainText("已拒绝");
  await expect(rejectedRow).toContainText("e2e 驳回示例");

  // 今日失败组：138****8001，消息来自当日终态文件
  const failedRow = page.locator('[data-queue-group="failed"]')
    .locator(`[data-queue-row][data-phone="${FAILED_PHONE}"]`);
  await expect(failedRow).toBeVisible();
  await expect(failedRow).toContainText("密码错误");

  // 今日未签组：当日无结论的 active 账号
  await expect(page.locator('[data-queue-group="unsigned"]')
    .locator(`[data-queue-row][data-phone="${UNSIGNED_PHONE}"]`)).toBeVisible();
});

/** 审核流用例的自建账号工具链：**种子零变更、用户表零变化**。
    为什么这么绕：种子 pending/rejected 数被 responsive.spec 的待处理徽标断言依赖
    （pending+rejected=2）；注册新用户会打破 race/logs 的「共 2 人」；e2e-user 已持有
    账号（每用户限 1）不能自助再提交。故走管理面：裸添加（active，badge 无关）→
    PUT 改绑手机号（既有语义：改绑一律回待审核；full 口令门带 confirm_password）→
    得到一个可审核的 pending 行。用例内 approve 后徽标计数净零。
    5k 种子下容量闸门会拦添加（403「账号数量已达上限」），审核用例整体 skip。 */
const ADMIN_PASS = "TestPass1234!"; // 与 e2e/base.ts 的 ADMIN 同源

/** 用例级清理：软删自建行（deleted ⇒ 不计入待处理徽标与任何队列组）。
    每一步都用 try/finally 调它——用例中途失败也不许把共享计数留在原处。 */
async function softDeleteRow(
  page: import("@playwright/test").Page,
  maskedPhone: string,
): Promise<void> {
  const me = await page.request.get("/api/me");
  const csrf = (await me.json()).csrf_token as string;
  const headers = { "X-CSRF-Token": csrf, "Content-Type": "application/json" };
  for (const group of ["pending", "active"]) {
    const resp = await page.request.get(
      `/api/accounts?group=${group}&q=${maskedPhone}`, { headers },
    );
    const rows = (await resp.json()).accounts ?? [];
    const hit = rows.find((r: { phone?: string }) => r.phone === maskedPhone);
    if (hit) {
      await page.request.delete(`/api/accounts/${hit.index}`, {
        data: { phone: maskedPhone }, headers,
      });
      return;
    }
  }
}

async function createPendingRow(
  page: import("@playwright/test").Page,
  activePhone: string,
  pendingPhone: string,
): Promise<void> {
  const me = await page.request.get("/api/me");
  const csrf = (await me.json()).csrf_token as string;
  const headers = { "X-CSRF-Token": csrf, "Content-Type": "application/json" };
  const add = await page.request.post("/api/accounts", {
    data: { name: `e2e-queue-${pendingPhone.slice(-4)}`, phone: activePhone, password: "QueuePass123!" },
    headers,
  });
  if (!add.ok()) throw new Error(`e2e 裸添加失败: ${add.status()} ${await add.text()}`);
  const list = await (await page.request.get(
    `/api/accounts?group=active&q=${activePhone}`, { headers },
  )).json();
  const idx = (list.accounts ?? [])[0]?.index;
  if (idx == null) throw new Error("e2e 找不到自建账号的下标");
  // PUT 改绑回待审：乐观锁指纹五字段与库内一致；改绑清 reject_reason（既有口径）
  const put = await page.request.put(`/api/accounts/${idx}`, {
    data: {
      name: `e2e-queue-${pendingPhone.slice(-4)}`,
      phone: pendingPhone,
      _snapshot: JSON.stringify({
        name: `e2e-queue-${pendingPhone.slice(-4)}`,
        phone: activePhone,
        phone_model: "",
        status: "active",
        deleted: false,
      }),
      confirm_password: ADMIN_PASS,
    },
    headers,
  });
  if (!put.ok()) throw new Error(`e2e 改绑回审失败: ${put.status()} ${await put.text()}`);
}

test("待审核行[通过]：真调 review 端点后行消失并进今日未签组", async ({ page }) => {
  test.skip(BULK >= 1000, "5k 种子触发容量闸门，审核流不可过闸（常规模式已覆盖）");
  const PHONE = "134****4002"; // 自建行（activePhone 13400134001 → 改绑 13400134002）
  await createPendingRow(page, "13400134001", "13400134002");
  try {
    await page.goto("/work/queue");
    const row = page.locator('[data-queue-group="pending"]')
      .locator(`[data-queue-row][data-phone="${PHONE}"]`);
    await expect(row).toBeVisible();

    // 判「端点真被调且成功」＝等一个 200 的 review 响应（若首个请求撞上外壳的
    // CSRF 重试路径，403 不匹配谓词，重试的 200 才算数——不钉第几次尝试）。
    const reviewOk = page.waitForResponse(
      (r) => r.url().includes("/review") && r.request().method() === "POST" && r.status() === 200,
    );
    await row.locator('[data-queue-action="approve"]').click();
    // 确认桥（EP MessageBox）：确认才发请求
    await page.locator(".el-message-box__btns .el-button--primary").click();
    await reviewOk;

    // 行离开待审核组
    await expect(row).toHaveCount(0);
    // 账号已生效、当日无结论 ⇒ 出现在今日未签组
    await expect(page.locator('[data-queue-group="unsigned"]')
      .locator(`[data-queue-row][data-phone="${PHONE}"]`)).toBeVisible();
  } finally {
    await softDeleteRow(page, PHONE);
  }
});

test("待审核行[驳回]：填理由即提交（不叠第二个确认框），行变已拒绝", async ({ page }) => {
  test.skip(BULK >= 1000, "5k 种子触发容量闸门（同审用例口径）");
  const PHONE = "134****4006"; // 自建行（13400134005 → 改绑 13400134006）
  await createPendingRow(page, "13400134005", "13400134006");
  try {
    await page.goto("/work/queue");
    const row = page.locator('[data-queue-group="pending"]')
      .locator(`[data-queue-row][data-phone="${PHONE}"]`);
    const reviewOk = page.waitForResponse(
      (r) => r.url().includes("/review") && r.request().method() === "POST" && r.status() === 200,
    );
    await row.locator('[data-queue-action="reject"]').click();
    // 理由弹窗（外壳 promptDialog）：必填，填入后点「驳回」即提交
    const prompt = page.locator(".modal, .pm-modal, [role=dialog]").filter({ has: page.locator("input.input") }).first();
    await prompt.locator("input.input").fill("识别码不匹配");
    await prompt.getByRole("button", { name: "驳回" }).click();
    await reviewOk;

    // 理由弹窗本身即确认：不再出现第二个确认框
    await expect(page.locator(".el-message-box")).toHaveCount(0);
    // 行留在待审核组，状态变「已拒绝」并显示理由（不跳页）
    await expect(row).toContainText("已拒绝");
    await expect(row).toContainText("识别码不匹配");
  } finally {
    await softDeleteRow(page, PHONE); // 已拒绝行同属待处理徽标，必须清走
  }
});

test("抽屉：开→详情与近 30 天事件→合；明文手机号零出现", async ({ page }) => {
  await page.goto("/work/queue");
  const failedRow = page.locator('[data-queue-group="failed"]')
    .locator(`[data-queue-row][data-phone="${FAILED_PHONE}"]`);
  await failedRow.locator('[data-queue-action="detail"]').click();

  const drawer = page.locator(".el-drawer");
  await expect(drawer).toBeVisible();
  // 初始焦点落关闭钮（安全侧，与 ep-confirm 焦点落取消侧同一口径）
  await expect.poll(
    () => page.evaluate(() => !!document.activeElement?.closest(".el-drawer__close-btn")),
    { message: "抽屉打开后初始焦点必须在关闭钮上" },
  ).toBe(true);
  await expect(drawer).toContainText(FAILED_PHONE); // 只允许遮罩形
  // 近 30 天事件：种子今日有 success + failed 两条（13800138001）
  const events = drawer.locator("[data-drawer-events]");
  await expect(events).toContainText("密码错误");
  // 完整手机号（详情接口在内存里持有）绝不上屏
  await expect(page.locator("body")).not.toContainText(RAW_PHONE);

  await drawer.locator(".el-drawer__close-btn").click();
  await expect(drawer).toBeHidden();
});

test("抽屉开→改→合：抽屉内[通过]走同一确认桥，成功后收抽屉", async ({ page }) => {
  test.skip(BULK >= 1000, "5k 种子触发容量闸门，审核流不可过闸（常规模式已覆盖）");
  const PHONE = "134****4004"; // 自建行（activePhone 13400134003 → 改绑 13400134004）
  await createPendingRow(page, "13400134003", "13400134004");
  try {
    await page.goto("/work/queue");
    const pendingRow = page.locator('[data-queue-group="pending"]')
      .locator(`[data-queue-row][data-phone="${PHONE}"]`);
    await pendingRow.locator('[data-queue-action="detail"]').click();

    const drawer = page.locator(".el-drawer");
    await expect(drawer).toBeVisible();
    const reviewOk = page.waitForResponse(
      (r) => r.url().includes("/review") && r.request().method() === "POST" && r.status() === 200,
    );
    await drawer.locator('[data-drawer-action="approve"]').click();
    await page.locator(".el-message-box__btns .el-button--primary").click();
    await reviewOk;

    // 改成功 ⇒ 抽屉收起，行离开待审核组并出现在今日未签组
    await expect(drawer).toBeHidden();
    await expect(pendingRow).toHaveCount(0);
    await expect(page.locator('[data-queue-group="unsigned"]')
      .locator(`[data-queue-row][data-phone="${PHONE}"]`)).toBeVisible();
  } finally {
    await softDeleteRow(page, PHONE);
  }
});

test("360 视口：抽屉全屏（宽度＝视口宽）", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto("/work/queue");
  const unsignedRow = page.locator('[data-queue-group="unsigned"]')
    .locator(`[data-queue-row][data-phone="${UNSIGNED_PHONE}"]`);
  await unsignedRow.locator('[data-queue-action="detail"]').click();

  const drawer = page.locator(".el-drawer.obj-drawer--full");
  await expect(drawer).toBeVisible();
  const box = await drawer.boundingBox();
  expect(box?.width ?? 0).toBeGreaterThanOrEqual(358);
  expect(box?.width ?? 999).toBeLessThanOrEqual(360);
  await expect(page.locator(".el-drawer.obj-drawer--side")).toHaveCount(0);
});

test.describe("5k 有界判据（需 YB_E2E_BULK_ACCOUNTS>=1000）", () => {
  test.skip(BULK < 1000, "需 YB_E2E_BULK_ACCOUNTS>=1000：本用例量的是 5k 规模");
  test.setTimeout(120_000);

  test("每组渲染行数 ≤ 页大小，分页控件可见", async ({ page }) => {
    await page.goto("/work/queue");
    const unsigned = page.locator('[data-queue-group="unsigned"]');
    await expect(unsigned.locator("[data-queue-row]").first()).toBeVisible();
    // 不变式：DOM 行数与服务端页大小同阶（不随 5k 线性增长）
    const rowCount = await unsigned.locator("[data-queue-row]").count();
    expect(rowCount).toBeLessThanOrEqual(20);
    await expect(unsigned.locator(".el-pagination")).toBeVisible();
    const failed = page.locator('[data-queue-group="failed"]');
    const failedRows = await failed.locator("[data-queue-row]").count();
    expect(failedRows).toBeLessThanOrEqual(20);
  });
});
