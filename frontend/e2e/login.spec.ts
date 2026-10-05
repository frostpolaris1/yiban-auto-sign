import { expect, test } from "@playwright/test";

// 登录页真浏览器端到端（P2 整页迁移到 Vue 后）。
//
// **只访问一次 /login，且不调用登录接口**：本套 e2e 共用一个 Flask 实例，登录限速与
// 「登录页访问循环」守卫都按客户端 IP 计——真表单登录由 logs.spec 覆盖一次、错误路径由
// audit.spec 覆盖一次，这里再撞第三次只会把整套顶到守卫上。故本文件只测**页面自身的
// 交互**（页签/字段级校验/协议模态），不提交任何表单。

test("登录页：页签切换、注册字段级即时校验、协议模态（服务端正文）", async ({ page }) => {
  await page.goto("/login");

  // ① 初始态：登录面板可见、注册面板隐藏、标题是「登录」
  await expect(page.locator("#login-form")).toBeVisible();
  await expect(page.locator("#register-pane")).toBeHidden();
  await expect(page.locator("#auth-title")).toHaveText("登录");
  await expect(page.locator("#auth-sub")).toContainText("使用注册邮箱进入签到管理面板");

  // ② 切到注册：整组显隐 + 标题同步（避免出现「标题说登录、表单是注册」）
  await page.click('[data-auth-tab="register"]');
  await expect(page.locator("#register-form")).toBeVisible();
  await expect(page.locator("#login-pane")).toBeHidden(); // 含该模式下的切换提示
  await expect(page.locator("#auth-title")).toHaveText("注册");
  await expect(page.locator('[data-auth-tab="register"]')).toHaveAttribute("aria-selected", "true");
  await expect(page.locator('[data-auth-tab="login"]')).toHaveAttribute("aria-selected", "false");

  // ③ 邮箱字段级校验：格式错 → 字段下方报错；改对 → 立即收起
  await page.fill("#reg-email", "not-an-email");
  await expect(page.locator("#reg-email-error")).toBeVisible();
  await expect(page.locator("#reg-email-error")).toContainText("邮箱格式不正确");
  await expect(page.locator("#reg-email")).toHaveClass(/is-invalid/);
  await page.fill("#reg-email", "a".repeat(33) + "@example.com");
  await expect(page.locator("#reg-email-error")).toContainText("邮箱用户名部分过长");
  await page.fill("#reg-email", "e2e-new@example.com");
  await expect(page.locator("#reg-email-error")).toBeHidden();
  await expect(page.locator("#reg-email")).not.toHaveClass(/is-invalid/);

  // ④ 口令字段级校验：提示里带**实时计数**（类别数来自 core.js 的策略，本页不重写）
  await page.fill("#reg-password", "Abcdefg1");
  await expect(page.locator("#reg-password-error")).toBeVisible();
  await expect(page.locator("#reg-password-error")).toContainText("（当前 8 位，3 类）");
  await page.fill("#reg-password", "Abcdefgh12");
  await expect(page.locator("#reg-password-error")).toBeHidden();

  // ⑤ 确认密码：不一致即报错，改一致即收起
  await page.fill("#reg-password2", "Abcdefgh99");
  await expect(page.locator("#reg-password2-error")).toBeVisible();
  await expect(page.locator("#reg-password2-error")).toContainText("两次输入的密码不一致");
  await page.fill("#reg-password2", "Abcdefgh12");
  await expect(page.locator("#reg-password2-error")).toBeHidden();

  // ⑥ 密码可见性开关：type 在 password/text 间切换，aria-pressed 同步
  await expect(page.locator("#reg-password")).toHaveAttribute("type", "password");
  await page.click('[data-pw-toggle="reg-password"]');
  await expect(page.locator("#reg-password")).toHaveAttribute("type", "text");
  await expect(page.locator('[data-pw-toggle="reg-password"]')).toHaveAttribute("aria-pressed", "true");
  await page.click('[data-pw-toggle="reg-password"]');
  await expect(page.locator("#reg-password")).toHaveAttribute("type", "password");

  // ⑦ 协议模态：正文来自服务端渲染的惰性 <template>（Vue 侧零 v-html，只 cloneNode）
  await page.click('[data-doc="agreement"]');
  const modal = page.locator(".pm-backdrop");
  await expect(modal).toBeVisible();
  await expect(modal).toContainText("用户协议");
  await expect(modal).toContainText("该文档尚未发布"); // e2e 临时环境没有 USER_AGREEMENT.md
  await expect(modal.getByRole("button", { name: "我知道了" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(modal).toBeHidden();

  // ⑧ 底部「直接登录」切回登录面板（独立于 role=tab 的切换入口）
  await page.click('[data-auth-switch="login"]');
  await expect(page.locator("#login-form")).toBeVisible();
  await expect(page.locator("#auth-title")).toHaveText("登录");

  // ⑨ 未勾选协议时提交注册：顶部错误框出现（不对应任何单个字段，必须弹顶部）
  await page.click('[data-auth-tab="register"]');
  await page.click("#register-btn");
  await expect(page.locator("#reg-error-box")).toBeVisible();
  await expect(page.locator("#reg-error-box")).toContainText("请先阅读并同意");
});
