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
  reporter: [["list"]],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "off",
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
