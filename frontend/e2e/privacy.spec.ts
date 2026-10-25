import { test as pwTest } from "@playwright/test";
import { adminTest, expect, userTest } from "./base";

/**
 * 二段资料（工单 qmz4）前端端到端。
 *
 * 真实用户动作：
 *   ① 账号归属人编辑自己的账号 → 进二段 → 填一个字段并打开暴露 → 保存；
 *   ② 新增账号只在第一段提交（跳过二段）；
 *   ③ 管理员在账号详情里看到同一字段，而管理列表看不到。
 * 断言的用户可见结果：
 *   · 我的视图出现/消失该字段（`[data-profile-line]`）；
 *   · 管理详情出现该字段、管理列表永不出现（#201 的暴露出口口径）；
 *   · 新增跳过二段时，服务端回包不含 profile（默认全关），且不发二段写请求。
 */

const USER_ACCOUNT_PHONE = "13800138001";
/** 管理列表下发的遮罩号（mask_phone 口径；用它定位种子账号，不依赖可被改名用例改掉的名称）。 */
const USER_ACCOUNT_MASKED = "138****8001";

async function csrfToken(page: import("@playwright/test").Page): Promise<string> {
  const me = await (await page.request.get("/api/me")).json();
  return String(me.csrf_token ?? "");
}

async function clearProfile(page: import("@playwright/test").Page): Promise<void> {
  const resp = await page.request.put("/api/my-accounts/0/profile", {
    data: {
      expose: { real_name: false, student_id: false, college: false, class_name: false, email: false },
      phone: USER_ACCOUNT_PHONE,
    },
    headers: { "X-CSRF-Token": await csrfToken(page) },
  });
  expect(resp.ok(), `清理二段开关失败：${resp.status()} ${await resp.text()}`).toBeTruthy();
}

function privacyDialog(page: import("@playwright/test").Page) {
  return page.locator(".el-dialog:visible").filter({ hasText: "二段：隐私资料与暴露" });
}

userTest.describe("我的账号 · 二段资料与暴露（本人面）", () => {
  userTest("开一个字段 → 我的视图出现；关回 → 消失；保存体带 phone 令牌", async ({ page }) => {
    await page.goto("/user/account");
    const card = page.locator(".account-card").first();
    // 名称可被同套件其它用例改名，按本人视图的完整手机号认人。
    await expect(card.locator(".account-meta")).toContainText(USER_ACCOUNT_PHONE);

    await clearProfile(page);
    await page.reload();
    await expect(card.locator("[data-profile-line]")).toHaveCount(0);

    // 409 漂移防护：请求携带的 phone 令牌不再指向目标行 → 拒绝写入。
    const stale = await page.request.put("/api/my-accounts/0/profile", {
      data: { profile: { student_id: "stale-write" }, phone: "13000000000" },
      headers: { "X-CSRF-Token": await csrfToken(page) },
    });
    expect(stale.status(), "旧 phone 令牌必须 409，不得静默写错行").toBe(409);

    const writes: Array<Record<string, unknown>> = [];
    page.on("request", (r) => {
      const url = new URL(r.url());
      if (r.method() === "PUT" && /^\/api\/my-accounts\/\d+\/profile$/.test(url.pathname)) {
        const raw = r.postDataJSON() as Record<string, unknown>;
        // 409 用例是直连 API；只收集页面自己发出的那一条。
        if (raw?.profile?.student_id !== "stale-write") writes.push(raw);
      }
    });

    // 开：填学号并打开暴露。
    await card.getByRole("button", { name: "编辑" }).click();
    const dialog = privacyDialog(page);
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: /下一步：隐私资料/ }).click();
    await expect(dialog).toContainText("二段：隐私资料与暴露");
    const field = dialog.locator('[data-privacy-field="student_id"]');
    await field.locator(".el-input__inner").fill("20250001");
    await field.locator(".el-switch").click();
    await expect(dialog).toContainText("管理列表始终不显示");

    await dialog.getByRole("button", { name: "保存隐私资料" }).click();
    const confirm = page.locator(".el-message-box:visible");
    await expect(confirm).toContainText("即将暴露");
    await expect(confirm).toContainText("学号");
    await confirm.locator(".el-message-box__btns .el-button--primary").click();
    await expect(dialog).toBeHidden();

    // 我的视图出现该字段；提交体逐键对齐契约（子集 + phone 令牌）。
    await expect(card.locator("[data-profile-line]")).toContainText("学号：20250001");
    expect(writes.length, "页面必须发一条二段写请求").toBeGreaterThan(0);
    expect(writes[0]).toEqual({
      profile: { student_id: "20250001" },
      expose: { student_id: true },
      phone: USER_ACCOUNT_PHONE,
    });

    // 刷新后仍在（已落库），且 API 的「我的视图」同样只下发开着的字段。
    await page.reload();
    await expect(page.locator(".account-card").first().locator("[data-profile-line]")).toContainText(
      "学号：20250001",
    );
    let view = await (await page.request.get("/api/my-accounts")).json();
    expect(view.accounts[0].profile).toEqual({ student_id: "20250001" });

    // 关：同一入口拨回，我的视图随之消失。
    await page.locator(".account-card").first().getByRole("button", { name: "编辑" }).click();
    const dialog2 = privacyDialog(page);
    await expect(dialog2).toBeVisible();
    await dialog2.getByRole("button", { name: /下一步：隐私资料/ }).click();
    await dialog2.locator('[data-privacy-field="student_id"] .el-switch').click();
    await dialog2.getByRole("button", { name: "保存隐私资料" }).click();
    const confirm2 = page.locator(".el-message-box:visible");
    await expect(confirm2).not.toContainText("即将暴露");
    await confirm2.locator(".el-message-box__btns .el-button--primary").click();
    await expect(dialog2).toBeHidden();

    await expect(page.locator(".account-card").first().locator("[data-profile-line]")).toHaveCount(0);
    view = await (await page.request.get("/api/my-accounts")).json();
    expect(view.accounts[0].profile).toBeUndefined();
  });
});

pwTest.describe("新增账号 · 跳过二段", () => {
  pwTest("跳过二段提交 → 默认全关，且不发二段写请求", async ({ browser }) => {
    const baseURL = `http://127.0.0.1:${process.env.YB_E2E_PORT ?? "8765"}`;
    const ctx = await browser.newContext({ baseURL });
    const req = ctx.request;
    const email = "e2e-privacy-new@qq.com";
    const password = "NewUserPass123!";

    const reg = await req.post("/api/register", { data: { email, password, agree: true } });
    expect(reg.ok(), `注册失败：${reg.status()} ${await reg.text()}`).toBeTruthy();
    const login = await req.post("/api/login", { data: { username: email, password } });
    expect(login.ok(), `登录失败：${login.status()} ${await login.text()}`).toBeTruthy();

    const profileWrites: string[] = [];
    const userPage = await ctx.newPage();
    userPage.on("request", (r) => {
      if (r.method() === "PUT" && /\/profile$/.test(new URL(r.url()).pathname)) profileWrites.push(r.url());
    });

    try {
      await userPage.goto("/user/account");
      await userPage.getByRole("button", { name: "提交我的易班账号" }).click();
      const dialog = userPage.locator(".el-dialog:visible");
      await expect(dialog).toBeVisible();
      // 一段可见、二段入口也在（可进），但本次按「跳过」直接提交。
      await expect(dialog.getByRole("button", { name: /下一步：隐私资料/ })).toBeVisible();
      await dialog.locator('input[placeholder="如：电力123张三"]').fill("e2e-new-acct");
      await dialog.locator('input[placeholder="11 位手机号"]').fill("13800138099");
      await dialog.locator('input[type="password"]').fill("new-pass-123");
      await dialog.locator(".el-dialog__footer .btn--primary").click();
      await expect(dialog).toBeHidden();

      const card = userPage.locator(".account-card").first();
      await expect(card.locator(".account-name")).toHaveText("e2e-new-acct");
      await expect(card.locator("[data-profile-line]")).toHaveCount(0);

      const view = await (await req.get("/api/my-accounts")).json();
      expect(view.accounts[0].profile, "默认全关：我的视图不得出现 profile").toBeUndefined();
      expect(profileWrites, "跳过二段时不得发任何二段写请求").toEqual([]);
    } finally {
      // 清理：软删新建账号，避免影响同实例后续用例。
      const me = await (await req.get("/api/me")).json();
      await req.delete("/api/my-accounts/0", {
        data: {},
        headers: { "X-CSRF-Token": String(me.csrf_token ?? "") },
      });
      await ctx.close();
    }
  });
});

adminTest.describe("账号管理 · 详情视图同口径", () => {
  adminTest("详情出现已暴露字段、管理列表永不出现；关回后详情也消失", async ({ page }) => {
    const csrf = await csrfToken(page);
    const list = await (await page.request.get("/api/accounts")).json();
    const row = (list.accounts as Array<{ index: number; name: string; phone: string }>).find(
      (a) => a.phone === USER_ACCOUNT_MASKED,
    );
    expect(row, "种子账号不在 /api/accounts 里").toBeTruthy();
    const idx = row!.index;
    const detail = await (await page.request.get(`/api/accounts/${idx}/detail`)).json();
    const fullPhone = String(detail.account.phone);

    async function seed(on: boolean): Promise<void> {
      const resp = await page.request.put(`/api/accounts/${idx}/profile`, {
        data: {
          ...(on ? { profile: { student_id: "20250001" } } : {}),
          expose: { student_id: on },
          phone: fullPhone,
        },
        headers: { "X-CSRF-Token": csrf },
      });
      expect(resp.ok(), `管理面写二段资料失败：${resp.status()} ${await resp.text()}`).toBeTruthy();
    }

    await seed(true);
    try {
      await page.goto("/work/accounts");
      // 默认页签是「待处理」；种子账号在「正常账号」组。
      await page.locator('[data-acct-tab="active"]').click();
      const table = page.locator("#accounts-tbody");
      const rowEl = table.locator(`tr[data-acct-idx="${idx}"]`);
      await expect(rowEl).toBeVisible();
      // 管理列表是逐行广播面：值不得出现在任何单元格里。
      expect(await table.textContent()).not.toContain("20250001");

      // 详情面：编辑弹窗里的二段区块显示已暴露字段。
      await rowEl.getByRole("button", { name: "编辑" }).click();
      const dialog = page.locator(".el-dialog:visible").filter({ hasText: "二段：隐私资料与暴露" });
      await expect(dialog).toBeVisible();
      await dialog.locator("details[data-admin-privacy] summary").click();
      await expect(dialog.locator("[data-detail-profile]")).toContainText("学号：20250001");
      await expect(dialog).toContainText("管理列表始终不显示");
      const field = dialog.locator('[data-privacy-field="student_id"]');
      await expect(field.locator(".el-switch")).toHaveClass(/is-checked/);

      // 关回：只拨开关，内容键不随提交（避免冲掉既有值）。
      await field.locator(".el-switch").click();
      await dialog.getByRole("button", { name: "保存隐私资料" }).click();
      const confirm = page.locator(".el-message-box:visible");
      await expect(confirm).not.toContainText("即将暴露");
      await confirm.locator(".el-message-box__btns .el-button--primary").click();
      await expect(dialog.locator("[data-detail-profile]")).toHaveCount(0);
    } finally {
      const cleared = await page.request.put(`/api/accounts/${idx}/profile`, {
        data: {
          profile: { student_id: "" },
          expose: { student_id: false },
          phone: fullPhone,
        },
        headers: { "X-CSRF-Token": await csrfToken(page) },
      });
      expect(cleared.ok(), "清理二段资料失败").toBeTruthy();
    }
  });
});
