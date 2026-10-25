// 业务日辅助（**Node 侧**）：与后端 `yiban.clock` 同一口径——北京时间（UTC+8）。
//
// 为什么需要这个文件（而不是只加 playwright.config 的 timezoneId）：
//   `use.timezoneId` 只作用于**浏览器上下文**——页面内 `new Date()`（`page.evaluate`）
//   才是北京时区。本目录里在 **Node 进程**内算日期的断言代码（`logs.spec.ts` 的
//   `dates()` 与 `crowded`）完全不受 `timezoneId` 影响，仍读宿主时区。宿主时区不是
//   UTC+8 时，这些日期会与 `e2e/server.py` 按业务钟（`yiban.clock`，固定 +8）种下的
//   数据错位，用例变成伪红／伪绿。
//
// 口径：中国自 1991 年起无夏令时、全国单一时区，故用固定 +8 偏移，与后端等价。
//   默认取真实时钟；设置 `YB_E2E_FROZEN_NOW` 时改取该冻结锚点，与
//   `e2e/server.py` 的冻结钟同口径。未设变量时开发者直接 `npx playwright test`
//   仍按真实时钟工作。
// 手法：把时点平移到 UTC+8 的"墙钟"，再用 `getUTC*` 取年月日。`getUTC*` 不读宿主
//   时区，故结果与宿主时区无关（对照：`getFullYear/getMonth/getDate` 读宿主时区）。

/** 北京时区相对 UTC 的固定偏移（毫秒）。与后端 `yiban.clock.TZ` 同一口径。 */
export const BUSINESS_UTC_OFFSET_MS = 8 * 60 * 60 * 1000;

const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * 冻结锚点（毫秒）：与 `e2e/server.py` 的 `YB_E2E_FROZEN_NOW` 同一口径。
 * 未设置 → `null`（真钟模式）。
 */
export function frozenBaseMs(): number | null {
  const raw = String(process.env.YB_E2E_FROZEN_NOW ?? "").trim();
  if (!raw) return null;
  const text = raw.replace(" ", "T");
  // 无时区标记的串按北京时间解释（+08:00），与后端 clock 同口径。
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(text);
  const ms = Date.parse(hasZone ? text : text + "+08:00");
  if (!Number.isFinite(ms)) {
    throw new Error("YB_E2E_FROZEN_NOW 无法解析：" + raw);
  }
  return ms;
}

/**
 * Node 侧「现在」（毫秒）。
 *
 * 冻结模式（`YB_E2E_FROZEN_NOW`）下取锚点，断言才与服务端种子同一天。
 * `YB_E2E_FROZEN_ADVANCE=1` 时服务端钟在前进，Node 侧追不上同一相位；
 * 此时**响亮拒绝**，日期请从 `/api/clock` 取（不许静默错位）。
 */
export function businessBaseMs(): number {
  if (String(process.env.YB_E2E_FROZEN_ADVANCE ?? "") === "1") {
    throw new Error("动态冻结钟下不得用 Node 侧业务日助手：日期请取 /api/clock");
  }
  return frozenBaseMs() ?? Date.now();
}

/** 日期串 `YYYY-MM-DD` 平移 `delta` 天（纯字符串运算，不读任何时钟）。 */
export function shiftDay(day: string, delta: number): string {
  const [y, m, d] = day.split("-").map(Number);
  const t = new Date(Date.UTC(y, m - 1, d + delta));
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())}`;
}

/**
 * 业务时区（UTC+8）下"今天往前 daysAgo 天"的日期串 `YYYY-MM-DD`。
 *
 * `baseMs` 默认取 `businessBaseMs()`（真钟或冻结锚点）；显式传入只服务探针——
 * 探针用固定时点钉跨日边界，不依赖跑探针的真实时刻。
 */
export function businessDay(daysAgo = 0, baseMs: number = businessBaseMs()): string {
  const shifted = new Date(baseMs + BUSINESS_UTC_OFFSET_MS - daysAgo * DAY_MS);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}`;
}

/** 业务日的年月日数字（`m` 为 1–12）：月份标题、当月天数、星期都从这一份取。 */
export function businessDayParts(daysAgo = 0, baseMs: number = businessBaseMs()) {
  const shifted = new Date(baseMs + BUSINESS_UTC_OFFSET_MS - daysAgo * DAY_MS);
  return { y: shifted.getUTCFullYear(), m: shifted.getUTCMonth() + 1, d: shifted.getUTCDate() };
}

/** 业务日所在月份的天数（Node 侧，不读宿主时区）。 */
export function businessDaysInMonth(baseMs: number = businessBaseMs()): number {
  const { y, m } = businessDayParts(0, baseMs);
  return new Date(Date.UTC(y, m, 0)).getUTCDate();
}

/** 业务日 `YYYY-MM-DD` 的星期（0=周日…6=周六）；按 UTC 构造，故与宿主时区无关。 */
export function businessWeekday(day: string): number {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).getUTCDay();
}

/** 业务日 `YYYY-MM-DD` 平移 delta 个月后的「YYYY年M月」标签（与日历页 `.sc-month` 同格式）。 */
export function businessMonthLabel(deltaMonths = 0, baseMs: number = businessBaseMs()): string {
  const { y, m } = businessDayParts(0, baseMs);
  const total = y * 12 + (m - 1) + deltaMonths;
  return `${Math.floor(total / 12)}年${(total % 12) + 1}月`;
}

/** 业务月平移 delta 个月后的天数（换月断言用）。 */
export function businessDaysInMonthShift(deltaMonths: number, baseMs: number = businessBaseMs()): number {
  const { y, m } = businessDayParts(0, baseMs);
  const total = y * 12 + (m - 1) + deltaMonths;
  return new Date(Date.UTC(Math.floor(total / 12), (total % 12) + 1, 0)).getUTCDate();
}

/**
 * 指定 IANA 时区下的日期串 `YYYY-MM-DD`（Node 侧）。用于构造「浏览器时区与业务钟不同」
 * 的断言：把该串与 businessDay() 对比即可证明页面读的是哪个时钟。
 */
export function zonedDay(zone: string, baseMs: number = businessBaseMs()): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(baseMs));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}
