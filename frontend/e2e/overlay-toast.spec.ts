import { adminTest, expect } from "./base";

/* 遮罩与成功提示的视觉判据（工单 khx4 / c7rj）。
 * 为什么不用截图对比：这里钉的是可比对的几何与颜色数值，跨机器可复现；
 * 截图只作人工复核，不进 CI。 */

type RGB = [number, number, number];

function parseColor(s: string): RGB {
  const m = s.match(/rgba?\(([^)]+)\)/);
  if (m) return m[1].split(",").slice(0, 3).map((x) => Number(x.trim())) as RGB;
  const c = s.match(/color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/);
  if (c) return [Number(c[1]) * 255, Number(c[2]) * 255, Number(c[3]) * 255];
  throw new Error("无法解析颜色: " + s);
}
function channel(c: number): number {
  c /= 255;
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}
function luminance([r, g, b]: RGB): number {
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}
function contrast(a: string, b: string): number {
  const la = luminance(parseColor(a));
  const lb = luminance(parseColor(b));
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

adminTest("弹窗遮罩：360/4K 单层、正文对比度 AA、2560 起去 blur（原生与 EP）", async ({ page }) => {
  await page.goto("/work/accounts");
  await page.waitForSelector("tbody tr[data-acct-idx]");

  for (const c of [
    { w: 360, h: 792, blur: true },
    { w: 3840, h: 2160, blur: false },
  ]) {
    await page.setViewportSize({ width: c.w, height: c.h });
    await page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));
    await page.locator("tbody tr[data-acct-idx]").first().getByRole("button", { name: "通过" }).click();
    const backdrop = page.locator(".pm-backdrop.is-open");
    await expect(backdrop).toBeVisible();
    await expect(page.locator(".pm-backdrop.is-open")).toHaveCount(1);
    const m = await backdrop.evaluate((node) => {
      const dlg = node as HTMLElement;
      const panel = dlg.querySelector(".pm-panel") as HTMLElement;
      const text = dlg.querySelector(".pm-confirm-text") as HTMLElement;
      const cs = getComputedStyle(dlg);
      return {
        filter: cs.backdropFilter,
        bg: cs.backgroundColor,
        panelBg: getComputedStyle(panel).backgroundColor,
        textColor: getComputedStyle(text).color,
      };
    });
    expect(m.filter, c.w + "px 遮罩 blur 口径").toBe(c.blur ? "blur(3px)" : "none");
    expect(m.bg, c.w + "px 遮罩底色").toContain(c.blur ? "0.72" : "0.92");
    expect(contrast(m.panelBg, m.textColor), c.w + "px 正文对比度").toBeGreaterThanOrEqual(4.5);
    await backdrop.locator(".modal-foot .btn--ghost").click();
    await expect(backdrop).toBeHidden();
  }

  // 暗色主题同一条正文也必须过 AA（实测 6.69:1，比浅色更高）。
  await page.setViewportSize({ width: 360, height: 792 });
  await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
  await page.locator("tbody tr[data-acct-idx]").first().getByRole("button", { name: "通过" }).click();
  const dark = page.locator(".pm-backdrop.is-open");
  await expect(dark).toBeVisible();
  const dm = await dark.evaluate((node) => {
    const dlg = node as HTMLElement;
    const panel = dlg.querySelector(".pm-panel") as HTMLElement;
    const text = dlg.querySelector(".pm-confirm-text") as HTMLElement;
    return { panelBg: getComputedStyle(panel).backgroundColor, textColor: getComputedStyle(text).color };
  });
  expect(contrast(dm.panelBg, dm.textColor), "暗色正文对比度").toBeGreaterThanOrEqual(4.5);
  await dark.locator(".modal-foot .btn--ghost").click();
  await page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));

  // Element Plus 弹窗走 .el-overlay，必须与 .pm-backdrop 同一口径；否则两套遮罩深浅不一。
  for (const c of [
    { w: 1920, blur: true },
    { w: 2560, blur: false },
  ]) {
    await page.setViewportSize({ width: c.w, height: 1080 });
    await page.locator("[data-add-account]").first().click();
    const overlay = page.locator(".el-overlay:visible").first();
    await expect(overlay).toBeVisible();
    const em = await overlay.evaluate((n) => {
      const cs = getComputedStyle(n as HTMLElement);
      return { filter: cs.backdropFilter, bg: cs.backgroundColor };
    });
    expect(em.filter, c.w + "px EP 遮罩 blur 口径").toBe(c.blur ? "blur(3px)" : "none");
    expect(em.bg, c.w + "px EP 遮罩底色").toContain(c.blur ? "0.72" : "0.92");
    await page.keyboard.press("Escape");
    await expect(overlay).toBeHidden();
  }
});

adminTest("成功提示：360 折行均衡，1920/2560 不溢出", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 792 });
  await page.goto("/work/accounts");
  await page.waitForSelector("tbody tr[data-acct-idx]");
  const csrf = String((await (await page.request.get("/api/me")).json()).csrf_token ?? "");
  const name = "e2e-toast-wrap";
  const created = await page.request.post("/api/accounts", {
    data: {
      name,
      phone: "13712340010",
      password: "e2e-phone-pw",
      email: "e2e-toast@wrap-e2e.dev",
      initial_password: "ToastPass123",
    },
    headers: { "X-CSRF-Token": csrf },
  });
  expect(created.ok(), `新建待审核账号失败：${created.status()} ${await created.text()}`).toBeTruthy();

  try {
    // 新建账号先落在待处理区，那里只有通过/驳回；按真实流程审核通过后，正常账号行才有删除。
    const listing0 = await (await page.request.get("/api/accounts")).json();
    const pending = (listing0.accounts as Array<{ index: number; name: string; phone: string }>).find(
      (a) => a.name === name,
    );
    expect(pending, "新建账号不在 /api/accounts 里").toBeTruthy();
    const reviewed = await page.request.post(`/api/accounts/${pending!.index}/review`, {
      data: { action: "approve", phone: pending!.phone },
      headers: { "X-CSRF-Token": csrf },
    });
    expect(reviewed.ok(), `审核账号失败：${reviewed.status()} ${await reviewed.text()}`).toBeTruthy();

    await page.reload();
    await page.getByRole("tab", { name: /正常账号/ }).click();
    const row = page.locator("tbody tr[data-acct-idx]", { hasText: name });
    await expect(row).toBeVisible();
    // 360px 下操作列收进「更多操作」下拉；展开后选删除。
    await row.getByRole("button", { name: "更多操作" }).click();
    await page.locator(".el-dropdown-menu:visible").getByRole("menuitem", { name: "删除", exact: true }).click();
    const modal = page.locator(".pm-backdrop.is-open");
    await expect(modal).toBeVisible();
    await modal.getByRole("button", { name: "删除", exact: true }).click();

    const toast = page.locator(".toast--success").last();
    await expect(toast).toBeVisible();
    await expect(toast.locator(".toast__msg")).toContainText("待删除账号");

    const measure = async () =>
      toast.evaluate((node) => {
        const msg = node.querySelector(".toast__msg") as HTMLElement;
        const box = node.getBoundingClientRect();
        const range = document.createRange();
        range.selectNodeContents(msg);
        const lines = Array.from(range.getClientRects())
          .map((r) => Math.round(r.width * 100) / 100)
          .filter((w) => w > 0);
        return {
          right: Math.round(box.right * 100) / 100,
          viewport: document.documentElement.clientWidth,
          lines,
          overflow: msg.scrollWidth - msg.clientWidth,
        };
      });

    await page.setViewportSize({ width: 360, height: 792 });
    await page.waitForTimeout(80);
    const narrow = await measure();
    expect(narrow.lines.length, "360px 应折行").toBeGreaterThanOrEqual(2);
    expect(narrow.overflow, "360px 不应横向溢出").toBeLessThanOrEqual(1);
    expect(Math.min(...narrow.lines) / Math.max(...narrow.lines), "360px 不应出现孤行").toBeGreaterThanOrEqual(0.6);
    expect(narrow.right, "360px 提示不得出屏").toBeLessThanOrEqual(narrow.viewport);

    for (const w of [1920, 2560]) {
      await page.setViewportSize({ width: w, height: 1080 });
      await page.waitForTimeout(80);
      const wide = await measure();
      expect(wide.lines.length, w + "px 至多 2 行").toBeLessThanOrEqual(2);
      expect(wide.overflow, w + "px 不应横向溢出").toBeLessThanOrEqual(1);
      if (wide.lines.length > 1) {
        expect(Math.min(...wide.lines) / Math.max(...wide.lines), w + "px 不应出现孤行").toBeGreaterThanOrEqual(0.6);
      }
      expect(wide.right, w + "px 提示不得出屏").toBeLessThanOrEqual(wide.viewport);
    }
  } finally {
    const listing = await (await page.request.get("/api/accounts")).json();
    const target = (listing.accounts as Array<{ index: number; name: string; phone: string }>).find(
      (a) => a.name === name,
    );
    if (target) {
      await page.request.delete(`/api/accounts/${target.index}`, {
        data: { phone: target.phone },
        headers: { "X-CSRF-Token": csrf },
      });
    }
  }
});