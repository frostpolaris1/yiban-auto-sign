import { test as pwTest } from "@playwright/test";
import { adminTest, expect, userTest } from "./base";

/**
 * 二段资料（工单 qmz4）前端端到端。
 *
 * 真实用户动作：
 *   ① 账号归属人编辑自己的账号 → 进二段 → 填字段并打开暴露 → 保存；
 *   ② 新增账号跳过二段（默认全关）；
 *   ③ 新增账号进二段，二段保存失败后回一段再前进重试；
 *   ④ 管理员一次保存同时提交一段与脏二段；二段失败可重试；
 *   ⑤ 管理员在详情看到已暴露字段，管理列表永不出现；
 *   ⑥ 管理端删行造下标漂移 → 写二段必须 409。
 * 断言的用户可见结果：
 *   · 我的视图出现/消失该字段（`[data-profile-line]`）；
 *   · 管理详情出现该字段、管理列表永不出现（#201 暴露出口口径）；
 *   · 新增跳过二段时服务端回包不含 profile，且不发二段写请求；
 *   · 建号后重试不得出现第二次 POST（弹窗切编辑态，后续走 PUT）；
 *   · 二段失败时弹窗留开、错误可见，重试只补失败的请求。
 *
 * 用例合并口径：本套件与 myaccounts.spec 共用 60 秒 10 次/IP 的登录限速。
 * 「跳过二段」与「进二段失败重试」合并到同一个新用户；管理端六条判据合并到
 * 同一个管理员会话。合并只为省登录，不合并判据。
 */

const USER_ACCOUNT_PHONE = "13800138001";
/** 管理列表下发的遮罩号（mask_phone 口径；用它定位种子账号，不依赖可被改名用例改掉的名称）。 */
const USER_ACCOUNT_MASKED = "138****8001";
const NEW_USER_PASSWORD = "NewUserPass123!";

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

/** 管理端打开某账号的编辑弹窗（active 页签，按全局下标定位）。 */
async function openAdminEdit(page: import("@playwright/test").Page, idx: number) {
  await page.goto("/work/accounts");
  await page.locator('[data-acct-tab="active"]').click();
  const row = page.locator(`#accounts-tbody tr[data-acct-idx="${idx}"]`);
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "编辑" }).click();
  const dialog = privacyDialog(page);
  await expect(dialog).toBeVisible();
  await dialog.locator("details[data-admin-privacy] summary").click();
  return dialog;
}

/** 一次注册＋登录的自建上下文（新增流程用例用；不改动种子用户）。 */
async function newUserContext(browser: import("@playwright/test").Browser, email: string) {
  const baseURL = `http://127.0.0.1:${process.env.YB_E2E_PORT ?? "8765"}`;
  const ctx = await browser.newContext({ baseURL });
  const req = ctx.request;
  const reg = await req.post("/api/register", { data: { email, password: NEW_USER_PASSWORD, agree: true } });
  expect(reg.ok(), `注册失败：${reg.status()} ${await reg.text()}`).toBeTruthy();
  const login = await req.post("/api/login", { data: { username: email, password: NEW_USER_PASSWORD } });
  expect(login.ok(), `登录失败：${login.status()} ${await login.text()}`).toBeTruthy();
  const page = await ctx.newPage();
  return { ctx, req, page };
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

    // 开：填学号并打开暴露，页脚保存（一段无改动，仍按原行为写一次账号）。
    await card.getByRole("button", { name: "编辑" }).click();
    const dialog = privacyDialog(page);
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: /下一步：隐私资料/ }).click();
    await expect(dialog).toContainText("二段：隐私资料与暴露");
    const field = dialog.locator('[data-privacy-field="student_id"]');
    await field.locator(".el-input__inner").fill("20250001");
    await field.locator(".el-switch").click();
    await expect(dialog).toContainText("管理列表始终不显示");

    await dialog.locator(".el-dialog__footer .btn--primary").click();
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
    await dialog2.locator(".el-dialog__footer .btn--primary").click();
    const confirm2 = page.locator(".el-message-box:visible");
    await expect(confirm2).not.toContainText("即将暴露");
    await confirm2.locator(".el-message-box__btns .el-button--primary").click();
    await expect(dialog2).toBeHidden();

    await expect(page.locator(".account-card").first().locator("[data-profile-line]")).toHaveCount(0);
    view = await (await page.request.get("/api/my-accounts")).json();
    expect(view.accounts[0].profile).toBeUndefined();
  });
});

pwTest.describe("新增账号 · 二段全路径", () => {
  pwTest("跳过二段默认全关；进二段失败后回一段再前进不重发 POST", async ({ browser }) => {
    const { ctx, req, page } = await newUserContext(browser, "e2e-privacy-flow@qq.com");
    // 只数**成功**的建号请求：core.js 对 403 会带新 CSRF 自动重试一次，
    // 按 request 计数会把"同一动作的两次请求"误判成两次建号。
    const post200: string[] = [];
    const accountPuts: string[] = [];
    const profilePuts: string[] = [];
    page.on("response", (r) => {
      const path = new URL(r.url()).pathname;
      if (r.request().method() === "POST" && path === "/api/my-accounts" && r.status() === 200) {
        post200.push(path);
      }
    });
    page.on("request", (r) => {
      const path = new URL(r.url()).pathname;
      if (r.method() === "PUT" && /^\/api\/my-accounts\/\d+$/.test(path)) accountPuts.push(path);
      if (r.method() === "PUT" && /^\/api\/my-accounts\/\d+\/profile$/.test(path)) profilePuts.push(path);
    });

    try {
      // 段一：跳过二段 → 默认全关，且不发任何二段写请求。
      await page.goto("/user/account");
      await page.getByRole("button", { name: "提交我的易班账号" }).click();
      let dialog = page.locator(".el-dialog:visible");
      await expect(dialog).toBeVisible();
      await expect(dialog.getByRole("button", { name: /下一步：隐私资料/ })).toBeVisible();
      await dialog.locator('input[placeholder="如：电力123张三"]').fill("e2e-new-acct");
      await dialog.locator('input[placeholder="11 位手机号"]').fill("13800138099");
      await dialog.locator('input[type="password"]').fill("new-pass-123");
      await dialog.locator(".el-dialog__footer .btn--primary").click();
      await expect(dialog).toBeHidden();
      expect(profilePuts, "跳过二段时不得发任何二段写请求").toEqual([]);
      let view = await (await req.get("/api/my-accounts")).json();
      expect((view.accounts as Array<{ phone: string; profile?: unknown }>).find((a) => a.phone === "13800138099")?.profile)
        .toBeUndefined();

      // 清场：软删首号，释放「每人只能有一个账号」的名额。
      const me1 = await (await req.get("/api/me")).json();
      const first = (view.accounts as Array<{ index: number; phone: string }>).find(
        (a) => a.phone === "13800138099",
      )!;
      const del1 = await req.delete(`/api/my-accounts/${first.index}`, {
        data: { phone: first.phone },
        headers: { "X-CSRF-Token": String(me1.csrf_token ?? "") },
      });
      expect(del1.ok(), `软删首号失败：${del1.status()} ${await del1.text()}`).toBeTruthy();

      // 段二：重新提交 → 进二段 → 二段写失败 → 回一段再前进 → 只补二段。
      post200.length = 0;
      accountPuts.length = 0;
      profilePuts.length = 0;
      let failOnce = true;
      await page.route("**/api/my-accounts/*/profile", async (route) => {
        if (failOnce) {
          failOnce = false;
          await route.fulfill({
            status: 500,
            contentType: "application/json",
            body: JSON.stringify({ error: "模拟二段保存失败" }),
          });
          return;
        }
        await route.continue();
      });
      await page.reload();
      await page.getByRole("button", { name: "提交我的易班账号" }).click();
      dialog = page.locator(".el-dialog:visible");
      await expect(dialog).toBeVisible();
      await dialog.locator('input[placeholder="如：电力123张三"]').fill("e2e-two-stage-acct");
      await dialog.locator('input[placeholder="11 位手机号"]').fill("13800138098");
      await dialog.locator('input[type="password"]').fill("new-pass-456");
      await dialog.getByRole("button", { name: /下一步：隐私资料/ }).click();

      const field = privacyDialog(page).locator('[data-privacy-field="student_id"]');
      await field.locator(".el-input__inner").fill("20250009");
      await field.locator(".el-switch").click();
      await page.locator(".el-dialog:visible .el-dialog__footer .btn--primary").click();
      let confirm = page.locator(".el-message-box:visible");
      await expect(confirm).toContainText("即将暴露");
      await confirm.locator(".el-message-box__btns .el-button--primary").click();

      // 第一次二段写失败：弹窗留开、错误可见、账号只建一次。
      dialog = page.locator(".el-dialog:visible");
      await expect(dialog).toContainText("模拟二段保存失败");
      expect(post200.length, "建号只允许一次成功 POST").toBe(1);

      // 回一段再前进：弹窗已切编辑态，后续走 PUT，不再 POST。
      await dialog.getByRole("button", { name: "上一步" }).click();
      await dialog.getByRole("button", { name: /下一步：隐私资料/ }).click();
      await dialog.locator(".el-dialog__footer .btn--primary").click();
      confirm = page.locator(".el-message-box:visible");
      await confirm.locator(".el-message-box__btns .el-button--primary").click();
      await expect(dialog).toBeHidden();

      expect(post200.length, "回一段再前进后不得出现第二次成功 POST").toBe(1);
      expect(accountPuts.length, "建号后应切编辑态走 PUT").toBeGreaterThan(0);
      expect(profilePuts.length, "二段失败后应重试一次").toBeGreaterThanOrEqual(2);
      view = await (await req.get("/api/my-accounts")).json();
      const created = (view.accounts as Array<{ phone: string; profile?: Record<string, string> }>).find(
        (a) => a.phone === "13800138098",
      );
      expect(created?.profile).toEqual({ student_id: "20250009" });
    } finally {
      // 清场：软删本用例创建的两个账号（含已软删的首号）。
      const view = await (await req.get("/api/my-accounts")).json();
      const me = await (await req.get("/api/me")).json();
      for (const a of (view.accounts as Array<{ index: number; phone: string; deleted?: boolean }>).sort(
        (x, y) => y.index - x.index,
      )) {
        if (a.deleted) continue;
        await req.delete(`/api/my-accounts/${a.index}`, {
          data: { phone: a.phone },
          headers: { "X-CSRF-Token": String(me.csrf_token ?? "") },
        });
      }
      await ctx.close();
    }
  });
});

adminTest.describe("账号管理 · 二段合并保存与暴露出口", () => {
  adminTest("详情/列表/只拨开关/真漂移；一段与脏二段同批保存，二段失败可重试", async ({ page }) => {
    const csrf = await csrfToken(page);
    const list = await (await page.request.get("/api/accounts")).json();
    const row = (list.accounts as Array<{ index: number; name: string; phone: string }>).find(
      (a) => a.phone === USER_ACCOUNT_MASKED,
    );
    expect(row, "种子账号不在 /api/accounts 里").toBeTruthy();
    const idx = row!.index;
    const initial = (await (await page.request.get(`/api/accounts/${idx}/detail`)).json()).account;
    const fullPhone = String(initial.phone);
    const originalName = String(initial.name);
    const model = String(initial.phone_model ?? "");

    async function putProfile(data: Record<string, unknown>): Promise<void> {
      const resp = await page.request.put(`/api/accounts/${idx}/profile`, {
        data: { ...data, phone: fullPhone },
        headers: { "X-CSRF-Token": await csrfToken(page) },
      });
      expect(resp.ok(), `管理面写二段资料失败：${resp.status()} ${await resp.text()}`).toBeTruthy();
    }

    async function clearProfile(): Promise<void> {
      await putProfile({
        profile: { student_id: "", class_name: "" },
        expose: { student_id: false, class_name: false },
      });
    }

    async function restoreName(): Promise<void> {
      const d = (await (await page.request.get(`/api/accounts/${idx}/detail`)).json()).account;
      const snap = JSON.stringify({
        name: d.name,
        phone: d.phone,
        phone_model: d.phone_model,
        status: d.status,
        deleted: d.deleted,
      });
      const resp = await page.request.put(`/api/accounts/${idx}`, {
        data: { name: originalName, phone: fullPhone, password: "", phone_model: model, phone_code: "", _snapshot: snap },
        headers: { "X-CSRF-Token": await csrfToken(page) },
      });
      expect(resp.ok(), `还原名称失败：${resp.status()} ${await resp.text()}`).toBeTruthy();
    }

    const profileBodies: Array<Record<string, unknown>> = [];
    const accountPuts: string[] = [];
    const profilePuts: string[] = [];
    page.on("request", (r) => {
      const path = new URL(r.url()).pathname;
      if (r.method() === "PUT" && path === `/api/accounts/${idx}/profile`) {
        profileBodies.push(r.postDataJSON() as Record<string, unknown>);
        profilePuts.push(path);
      }
      if (r.method() === "PUT" && path === `/api/accounts/${idx}`) accountPuts.push(path);
    });

    await clearProfile();
    try {
      // ---- 判据一：详情出现、列表永不出现；只拨开关的请求体不带 profile 键 ----
      await putProfile({ profile: { student_id: "20250001" }, expose: { student_id: true } });
      await page.goto("/work/accounts");
      await page.locator('[data-acct-tab="active"]').click();
      const table = page.locator("#accounts-tbody");
      const rowEl = table.locator(`tr[data-acct-idx="${idx}"]`);
      await expect(rowEl).toBeVisible();
      expect(await table.textContent()).not.toContain("20250001");

      await rowEl.getByRole("button", { name: "编辑" }).click();
      let dialog = privacyDialog(page);
      await expect(dialog).toBeVisible();
      await dialog.locator("details[data-admin-privacy] summary").click();
      await expect(dialog.locator("[data-detail-profile]")).toContainText("学号：20250001");
      await expect(dialog).toContainText("管理列表始终不显示");
      let field = dialog.locator('[data-privacy-field="student_id"]');
      await expect(field.locator(".el-switch")).toHaveClass(/is-checked/);

      profileBodies.length = 0;
      await field.locator(".el-switch").click();
      await dialog.locator(".el-dialog__footer .btn--primary").click();
      let confirm = page.locator(".el-message-box:visible");
      await expect(confirm).not.toContainText("即将暴露");
      await confirm.locator(".el-message-box__btns .el-button--primary").click();
      await expect(dialog).toBeHidden();

      const toggleBody = profileBodies[profileBodies.length - 1];
      expect(toggleBody).toEqual({ expose: { student_id: false }, phone: fullPhone });
      expect(toggleBody && "profile" in toggleBody).toBe(false);

      // 详情关回后字段消失。
      dialog = await openAdminEdit(page, idx);
      await expect(dialog.locator("[data-detail-profile]")).toHaveCount(0);
      await dialog.getByRole("button", { name: "取消" }).click();

      // 409 真漂移：删掉前一行后旧下标指向他人，写二段必须拒绝。
      const mine = await (await page.request.get("/api/my-accounts")).json();
      expect(mine.accounts.length, "管理员我的视图至少三行").toBeGreaterThanOrEqual(3);
      const first = mine.accounts[0] as { admin_index: number; phone: string };
      const target = mine.accounts[1] as { phone: string };
      const del = await page.request.delete(`/api/accounts/${first.admin_index}`, {
        data: { phone: first.phone },
        headers: { "X-CSRF-Token": csrf },
      });
      expect(del.ok(), `删除首行失败：${del.status()} ${await del.text()}`).toBeTruthy();
      try {
        const drifted = await page.request.put("/api/my-accounts/1/profile", {
          data: { profile: { student_id: "drifted" }, phone: target.phone },
          headers: { "X-CSRF-Token": await csrfToken(page) },
        });
        expect(drifted.status(), "旧下标指向他人时必须 409").toBe(409);
      } finally {
        const restored = await page.request.post(`/api/accounts/${first.admin_index}/restore`, {
          data: { phone: first.phone },
          headers: { "X-CSRF-Token": await csrfToken(page) },
        });
        expect(restored.ok(), `恢复首行失败：${restored.status()} ${await restored.text()}`).toBeTruthy();
      }

      // ---- 判据二：页脚一次保存提交一段＋脏二段；二段失败留开可重试 ----
      accountPuts.length = 0;
      profilePuts.length = 0;
      dialog = await openAdminEdit(page, idx);
      await dialog.locator('input[placeholder="如：电力123张三"]').fill(`${originalName}-merged`);
      field = dialog.locator('[data-privacy-field="student_id"]');
      await field.locator(".el-input__inner").fill("20250002");
      await field.locator(".el-switch").click();
      await dialog.locator(".el-dialog__footer .btn--primary").click();
      confirm = page.locator(".el-message-box:visible");
      await expect(confirm).toContainText("即将暴露");
      await confirm.locator(".el-message-box__btns .el-button--primary").click();
      await expect(dialog).toBeHidden();
      expect(accountPuts.length, "一段应写一次").toBe(1);
      expect(profilePuts.length, "二段应写一次").toBe(1);
      let d = (await (await page.request.get(`/api/accounts/${idx}/detail`)).json()).account;
      expect(d.name).toBe(`${originalName}-merged`);
      expect(d.profile).toEqual({ student_id: "20250002" });

      // 二段失败：弹窗留开、错误可见；重试只补二段。
      accountPuts.length = 0;
      profilePuts.length = 0;
      let failOnce = true;
      await page.route("**/api/accounts/*/profile", async (route) => {
        if (failOnce) {
          failOnce = false;
          await route.fulfill({
            status: 500,
            contentType: "application/json",
            body: JSON.stringify({ error: "模拟二段保存失败" }),
          });
          return;
        }
        await route.continue();
      });
      dialog = await openAdminEdit(page, idx);
      await dialog.locator('input[placeholder="如：电力123张三"]').fill(`${originalName}-merged2`);
      const klass = dialog.locator('[data-privacy-field="class_name"]');
      await klass.locator(".el-input__inner").fill("软件2401");
      await klass.locator(".el-switch").click();
      await dialog.locator(".el-dialog__footer .btn--primary").click();
      confirm = page.locator(".el-message-box:visible");
      await confirm.locator(".el-message-box__btns .el-button--primary").click();

      await expect(dialog).toBeVisible();
      await expect(dialog).toContainText("模拟二段保存失败");
      expect(accountPuts.length, "首次保存一段应写一次").toBe(1);
      expect(profilePuts.length, "首次保存二段应写一次").toBe(1);

      await dialog.locator(".el-dialog__footer .btn--primary").click();
      confirm = page.locator(".el-message-box:visible");
      await confirm.locator(".el-message-box__btns .el-button--primary").click();
      await expect(dialog).toBeHidden();
      expect(accountPuts.length, "重试不得重复写一段").toBe(1);
      expect(profilePuts.length, "重试应补二段").toBe(2);

      d = (await (await page.request.get(`/api/accounts/${idx}/detail`)).json()).account;
      expect(d.name).toBe(`${originalName}-merged2`);
      expect(d.profile).toEqual({ student_id: "20250002", class_name: "软件2401" });
    } finally {
      await clearProfile();
      await restoreName();
    }
  });
});
