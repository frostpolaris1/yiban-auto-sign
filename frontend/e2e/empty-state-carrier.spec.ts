import { adminTest, expect } from "./base";

/* 空态载体的判据（uz7w「手搓空态换 EP 自带件」）。

 * 为什么钉载体而不只钉文案：换件的价值是「不再维护手搓空态构件」。
 * 只断言「出现了无匹配结果」时，旧 `<p class="empty">` 也能满足，门抓不到回退。
 * 故断言两件事：
 *   ① 空态根就是 EP 的 `.el-empty`（同一元素带 id 与 class）；
 *   ② 它不再是旧构件（不是 <p>、不带 .empty 类）。
 * 两页各一条：账号管理（/work/accounts）与用户管理（/work/users）。

 * 触发方式选「检索无匹配」：不确定种子有几行，但一定不会匹配这串随机串，
 * 空态必然出现；且它同时覆盖「筛出来的空」分支（带清除筛选按钮）。 */

const NONSENSE = "zzz-不可能匹配-9f3a";

adminTest("账号页空态根是 .el-empty（不是旧 <p class=empty>）", async ({ page }) => {
  await page.goto("/work/accounts");
  await page.waitForSelector("tbody tr[data-acct-idx]");

  await page.locator("#pending-search").fill(NONSENSE);
  const emptyRoot = page.locator("#accounts-pending-empty");
  await expect(emptyRoot).toBeVisible();
  await expect(emptyRoot).toHaveClass(/(^|\s)el-empty(\s|$)/);
  await expect(emptyRoot).toContainText("无匹配结果");

  const shape = await emptyRoot.evaluate((node) => ({
    tag: node.tagName,
    isLegacyP: node.tagName === "P",
    hasLegacyClass: node.classList.contains("empty"),
  }));
  expect(shape.tag, "空态根必须是 EP 的 div，不是旧 <p>").toBe("DIV");
  expect(shape.isLegacyP, "旧构件用的是 <p class=empty>").toBe(false);
  expect(shape.hasLegacyClass, "旧构件类名 .empty 不应再出现").toBe(false);

  // 清除筛选仍可用（动作槽保留）：点它之后空态消失。
  await emptyRoot.locator("[data-empty-clear]").click();
  await expect(emptyRoot).toBeHidden();
});

adminTest("用户页空态根是 .el-empty（不是旧 <p class=empty>）", async ({ page }) => {
  await page.goto("/work/users");
  // 不假设 pending 组有行（默认种子可能为空）：等检索框就位即可，
  // 空态在「本来就没有」与「筛掉了」两种情况下都必须是同一个 EP 载体。
  await page.waitForSelector("#usr-pending-search");
  await page.locator("#usr-pending-search").fill(NONSENSE);
  const emptyRoot = page.locator("#usr-pending-empty");
  await expect(emptyRoot).toBeVisible();
  await expect(emptyRoot).toHaveClass(/(^|\s)el-empty(\s|$)/);

  const shape = await emptyRoot.evaluate((node) => ({
    tag: node.tagName,
    isLegacyP: node.tagName === "P",
    hasLegacyClass: node.classList.contains("empty"),
  }));
  expect(shape.tag, "空态根必须是 EP 的 div，不是旧 <p>").toBe("DIV");
  expect(shape.isLegacyP, "旧构件用的是 <p class=empty>").toBe(false);
  expect(shape.hasLegacyClass, "旧构件类名 .empty 不应再出现").toBe(false);
});
