import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

// e2e 只用本机 Chromium，跑在 frontend/e2e/server.py 拉起的临时 Flask 实例上
// （临时 .env/SQLite + 预置审计行；不碰仓库真实数据、不联外网）。
const PORT = Number(process.env.YB_E2E_PORT ?? 8765);

/** 解释器探测：优先显式 YB_E2E_PYTHON（CI / 其他机器），否则试常见 venv 位置。 */
function pythonCommand(): string {
  if (process.env.YB_E2E_PYTHON) return process.env.YB_E2E_PYTHON;
  const candidates = [
    "../.venv/Scripts/python.exe", // 本 worktree 自带
    "../../yiban-auto-sign/.venv/Scripts/python.exe", // 主检出（worktree 通常无 venv）
    "../.venv/bin/python",
  ];
  for (const rel of candidates) {
    if (existsSync(fileURLToPath(new URL(rel, import.meta.url)))) return rel;
  }
  return "python";
}

export default defineConfig({
  testDir: "./e2e",
  // **串行**：整套 e2e 共用一个 Flask 实例，而登录限速与「登录页访问循环」守卫都是
  // 按客户端 IP 计的（同一个 127.0.0.1），并行 worker 会互相撞限速——2026-10-03 实测：
  // 两个 spec 并行时审计页出现「未登录」告警（会话未建立），单跑必绿。
  // 这与本仓 CI 对 pytest 的 `--dist loadfile` 约束同源：共享状态的套件不能拆并行。
  workers: 1,
  fullyParallel: false,
  reporter: [["list"]],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "off",
    // 浏览器时区固定为北京（UTC+8）：e2e/server.py 按业务钟 `yiban.clock`（固定 +8）
    // 种日期，而日历页等用例在**页面内**用 `new Date()` 推导"今天/当月"
    // （`calendar.spec.ts::probeDay` 走 `page.evaluate`）。宿主时区不是 +8 时，页面内
    // 推导会与服务端种子错位。此设定只覆盖浏览器侧；**Node 侧**在进程内算日期的断言
    // （`logs.spec.ts::dates()` 与 `crowded`）不受它影响，另走 `e2e/business-day.ts`。
    timezoneId: "Asia/Shanghai",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `"${pythonCommand()}" e2e/server.py`,
    url: `http://127.0.0.1:${PORT}/login`,
    reuseExistingServer: false,
    timeout: 60_000,
    stdout: "pipe",
  },
});
