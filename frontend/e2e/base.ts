import { test as base, expect, type Page } from "@playwright/test";

import { frozenBaseMs } from "./business-day";

// 每个用例用**自己的**会话登录，不共享 Cookie 文件。
//
// 为什么不能整套只登录一次：内置管理员的登录会换发 YIBAN_ADMIN_SID（web/security.py
// _issue_admin_sid），注册用户登录会换发 users.sid——同一个账号第二次登录会把上一次的
// Cookie 立刻作废。任何一条用例再登录同一账号，其它用例的 storageState 就整体失效
// （实测：整套 12 failed，页面全被踢回 /login）。
//
// 登录不计入被审查的页面行为；登录限速由 pytest 覆盖，e2e/server.py 在本临时实例内
// 抬高窗口上限，避免 20+ 次登录撞 60 秒 10 次/IP。

const ADMIN = { username: "admin", password: "TestPass1234!" };
// 导出给需要在**自建上下文**里登录的用例（如按指定时区新建 browser context）。
export const USER = { username: "e2e-user@example.com", password: "UserPass123!" };

/**
 * 严格冻结模式（`YB_E2E_FROZEN_NOW` 且未开 `ADVANCE`）下，把**页面内**的 Date
 * 也钉到同一锚点。
 *
 * 为什么需要：`/api/clock` 的 `now` 与 `server_ts` 都随冻结钟走，浏览器的
 * `new Date()` 仍是真实时间。看板「时钟」卡比较两者，偏差 >5 秒就显示「偏差」；
 * 真实日期与锚点差一天时，既有用例的「时钟已同步」断言必红。
 *
 * 边界（AGENTS §32 等价面）：此脚本让浏览器线程与服务器同钟，等价于「用户机器
 * 时钟与服务端一致」这一常规条件。它同时削弱了「页面误读浏览器日期」的判别力
 * ——同区下两者相同。该性质由 `calendar.spec` 的跨时区用例另行钉住；默认真钟
 * 模式不受影响。动态冻结（ADVANCE=1）不注入：页面必须自己从 `/api/clock` 校准。
 */
const FROZEN_MS: number | null =
  String(process.env.YB_E2E_FROZEN_ADVANCE ?? "") === "1" ? null : frozenBaseMs();
const FROZEN_CLOCK_SCRIPT: string | null =
  FROZEN_MS === null
    ? null
    : `(() => {
        const RealDate = Date;
        const fixed = ${FROZEN_MS};
        class FrozenDate extends RealDate {
          constructor(...args) {
            if (args.length === 0) super(fixed);
            else super(...args);
          }
          static now() { return fixed; }
        }
        window.Date = FrozenDate;
      })()`;

async function loginAs(page: Page, who: { username: string; password: string }): Promise<void> {
  const resp = await page.request.post("/api/login", { data: who });
  if (!resp.ok()) {
    throw new Error("e2e 预登录失败: " + who.username + " -> HTTP " + resp.status());
  }
}

async function installFrozenClock(page: Page): Promise<void> {
  if (FROZEN_CLOCK_SCRIPT) await page.addInitScript(FROZEN_CLOCK_SCRIPT);
}

/** 管理端用例：每条用例新建会话并以管理员登录。 */
export const adminTest = base.extend({
  page: async ({ page }, use) => {
    await installFrozenClock(page);
    await loginAs(page, ADMIN);
    await use(page);
  },
});

/** 用户端用例：每条用例新建会话并以普通用户登录。 */
export const userTest = base.extend({
  page: async ({ page }, use) => {
    await installFrozenClock(page);
    await loginAs(page, USER);
    await use(page);
  },
});

export { expect };
