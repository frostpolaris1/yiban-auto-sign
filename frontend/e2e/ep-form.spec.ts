import type { Page } from "@playwright/test";
import { adminTest as test, expect } from "./base";

/* EP 形态钉子（ph8「EP 形态追平 Adminator」）。
   模拟的真实用户动作：管理员打开试点页 / 设置页 / 队列页 / 账号页，
   看见 Element Plus 的按钮、表格、下拉、栅格开关、抽屉、确认框。
   断言的用户可见结果：这些组件的**实际计算样式**（getComputedStyle /
   元素盒高）等于 Adminator 令牌刻度，而不是只查 CSS 文本。

   四族各 ≥3 个属性；亮/暗两轮同跑。换肤只切外壳的两个标志
   （html[data-theme] 与 id 为 dark 的 class），与 core.js / theme.ts 同口径。 */

const LIGHT = {
  border: "rgb(228, 232, 239)", // --border #E4E8EF
  borderSoft: "rgb(238, 241, 245)", // --border-soft #EEF1F5
  bgCard: "rgb(255, 255, 255)", // --bg-card #FFFFFF
  bgHover: "rgb(248, 250, 252)", // --bg-hover #F8FAFC
  controlBorder: "rgb(138, 148, 166)", // --control-border #8A94A6
  primary: "rgb(37, 99, 235)", // --primary #2563EB
  ring: "rgba(37, 99, 235, 0.18)", // --primary-ring
  switchOff: "rgb(134, 143, 160)", // --switch-off-bg #868FA0
  shadowLg: "rgba(15, 23, 42, 0.08) 0px 10px 15px -3px", // --shadow-lg
};

const DARK = {
  border: "rgb(34, 44, 66)", // --border #222C42
  borderSoft: "rgb(26, 34, 55)", // --border-soft #1A2237
  bgCard: "rgb(20, 27, 45)", // --bg-card #141B2D
  bgHover: "rgb(28, 36, 56)", // --bg-hover #1C2438
  controlBorder: "rgb(102, 116, 140)", // --control-border #66748C
  primary: "rgb(96, 165, 250)", // --primary #60A5FA
  ring: "rgba(96, 165, 250, 0.24)", // --primary-ring
  switchOff: "rgb(100, 116, 139)", // --switch-off-bg #64748B
  shadowLg: "rgba(0, 0, 0, 0.5) 0px 10px 15px -3px", // --shadow-lg
};

/** 换肤并停掉过渡：形态断言读的是终值，不读过渡中间帧。 */
async function setTheme(page: Page, theme: "light" | "dark"): Promise<void> {
  await page.evaluate((t) => {
    document.documentElement.setAttribute("data-theme", t);
    document.documentElement.classList.toggle("dark", t === "dark");
  }, theme);
}

async function noMotion(page: Page): Promise<void> {
  await page.addStyleTag({
    content: "*,*::before,*::after{transition:none!important;animation:none!important}",
  });
}

for (const [mode, t] of [
  ["light", LIGHT],
  ["dark", DARK],
] as const) {
  test(`EP 形态（${mode}）：按钮与表格 = 令牌刻度`, async ({ page }) => {
    await page.goto("/work/pilot");
    const btn = page.locator(".btn-row .el-button").first();
    await expect(btn).toBeVisible();
    const cell = page.locator(".el-table__body td.el-table__cell").first();
    const row = page.locator(".el-table__body tr").first();
    await expect(cell).toBeVisible();
    await noMotion(page);
    await setTheme(page, mode);

    // 按钮：圆角 --r-ctl / 高 --ctl-h / 内距 Adminator .btn(9 14) / 聚焦环 --primary-ring。
    const button = await btn.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        radius: s.borderRadius,
        height: s.height,
        padX: s.paddingLeft,
        padY: s.paddingTop,
        ring: s.getPropertyValue("--el-button-outline-color").trim(),
      };
    });
    expect(button.radius, `${mode} 按钮圆角`).toBe("8px");
    expect(button.height, `${mode} 按钮高度`).toBe("40px");
    expect(button.padX, `${mode} 按钮横向内距`).toBe("14px");
    expect(button.padY, `${mode} 按钮纵向内距`).toBe("9px");
    expect(button.ring, `${mode} 按钮聚焦环令牌`).toBe(t.ring);

    // 聚焦环落到实际 outline：键盘可达路径的真实渲染值。
    await btn.focus();
    const outline = await btn.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        visible: el.matches(":focus-visible"),
        color: s.outlineColor,
        width: s.outlineWidth,
      };
    });
    expect(outline.visible, `${mode} 按钮 :focus-visible`).toBe(true);
    expect(outline.color, `${mode} 按钮 outline 色`).toBe(t.ring);
    expect(outline.width, `${mode} 按钮 outline 宽`).toBe("2px");

    // 表格：行高 48（已拍板）/ 行线 --border-soft / 表头线 --border / 表头底 --bg-card。
    const td = await cell.evaluate((el) => {
      const s = getComputedStyle(el);
      const inner = el.querySelector(".cell");
      return {
        height: Math.round(el.getBoundingClientRect().height),
        border: s.borderBottomColor,
        padX: inner ? getComputedStyle(inner).paddingLeft : "",
      };
    });
    expect(td.height, `${mode} 表格行高`).toBe(48);
    expect(td.border, `${mode} 行分隔线色`).toBe(t.borderSoft);
    expect(td.padX, `${mode} 单元格横向内距`).toBe("14px");

    const th = page.locator(".el-table__header th.el-table__cell").first();
    const head = await th.evaluate((el) => {
      const s = getComputedStyle(el);
      return { border: s.borderBottomColor, bg: s.backgroundColor };
    });
    expect(head.border, `${mode} 表头分隔线色`).toBe(t.border);
    expect(head.bg, `${mode} 表头底色`).toBe(t.bgCard);

    // 行 hover 底 = --bg-hover。
    await row.hover();
    await expect
      .poll(() => cell.evaluate((el) => getComputedStyle(el).backgroundColor), {
        message: `${mode} 行 hover 底色`,
      })
      .toBe(t.bgHover);
  });
}

test("EP 形态：下拉与开关 = 令牌刻度（亮/暗）", async ({ page }) => {
  await page.goto("/work/settings");
  const select = page.locator("#set-panel-schedule .el-select__wrapper").first();
  await expect(select).toBeVisible();
  const sw = page.locator(".el-switch").first();
  await expect(sw).toBeVisible();
  await noMotion(page);

  for (const [mode, t] of [
    ["light", LIGHT],
    ["dark", DARK],
  ] as const) {
    await setTheme(page, mode);

    // 下拉：高 --ctl-h-lg(40) / 圆角 --r-ctl / 描边 --control-border。
    const selectStyle = await select.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        height: Math.round(el.getBoundingClientRect().height),
        radius: s.borderRadius,
        shadow: s.boxShadow,
      };
    });
    expect(selectStyle.height, `${mode} 下拉高度`).toBe(40);
    expect(selectStyle.radius, `${mode} 下拉圆角`).toBe("8px");
    expect(selectStyle.shadow, `${mode} 下拉描边`).toContain(t.controlBorder);

    // 开关：高 --ctl-h-md(32) / 轨道色 = 开 --primary 或关 --switch-off-bg。
    const checked = (await sw.getAttribute("aria-checked")) === "true";
    const track = sw.locator(".el-switch__core");
    const switchStyle = await sw.evaluate((el) => ({
      height: Math.round(el.getBoundingClientRect().height),
    }));
    expect(switchStyle.height, `${mode} 开关高度`).toBe(32);
    await expect
      .poll(() => track.evaluate((el) => getComputedStyle(el).backgroundColor), {
        message: `${mode} 开关轨道色`,
      })
      .toBe(checked ? t.primary : t.switchOff);
  }
});

test("EP 形态：抽屉与确认框 = 令牌刻度（亮/暗）", async ({ page }) => {
  await page.goto("/work/queue");
  const failedRow = page.locator('[data-queue-group="failed"] [data-queue-row]').first();
  await expect(failedRow).toBeVisible();
  await failedRow.locator('[data-queue-action="detail"]').click();

  const drawer = page.locator(".el-drawer");
  await expect(drawer).toBeVisible();
  await noMotion(page);

  for (const [mode, t] of [
    ["light", LIGHT],
    ["dark", DARK],
  ] as const) {
    await setTheme(page, mode);

    // 方向标：贴边线只挂在右侧滑入的 .rtl 上。
    expect(
      await drawer.evaluate((el) => el.classList.contains("rtl")),
      `${mode} 抽屉方向标 rtl`,
    ).toBe(true);

    // 抽屉：底 --bg-card / 描边 --border / 阴影 --shadow-lg / 贴边圆角 0。
    await expect
      .poll(() => drawer.evaluate((el) => getComputedStyle(el).backgroundColor), {
        message: `${mode} 抽屉底色`,
      })
      .toBe(t.bgCard);
    const d = await drawer.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        border: s.borderLeftColor,
        borderWidth: s.borderLeftWidth,
        shadow: s.boxShadow,
        radius: s.borderRadius,
      };
    });
    expect(d.borderWidth, `${mode} 抽屉左边框宽`).toBe("1px");
    expect(d.border, `${mode} 抽屉左边框色`).toBe(t.border);
    expect(d.shadow, `${mode} 抽屉阴影`).toContain(t.shadowLg);
    expect(d.radius, `${mode} 抽屉贴边圆角`).toBe("0px");
  }

  await drawer.locator(".el-drawer__close-btn").click();
  await expect(drawer).toBeHidden();

  // 确认框（消息框）：圆角 --r-card / 描边 --border / 阴影 --shadow-lg。
  await page.goto("/work/accounts");
  const accountRow = page.locator("tbody tr[data-acct-idx]").first();
  await expect(accountRow).toBeVisible();
  await accountRow.getByRole("button", { name: "通过", exact: true }).click();

  const box = page.locator(".el-message-box");
  await expect(box).toBeVisible();
  await noMotion(page);

  for (const [mode, t] of [
    ["light", LIGHT],
    ["dark", DARK],
  ] as const) {
    await setTheme(page, mode);
    const m = await box.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        radius: s.borderRadius,
        border: s.borderTopColor,
        shadow: s.boxShadow,
      };
    });
    expect(m.radius, `${mode} 确认框圆角`).toBe("14px");
    expect(m.border, `${mode} 确认框描边`).toBe(t.border);
    expect(m.shadow, `${mode} 确认框阴影`).toContain(t.shadowLg);
  }

  await box.getByRole("button", { name: "取消" }).click();
  await expect(box).toBeHidden();
});
