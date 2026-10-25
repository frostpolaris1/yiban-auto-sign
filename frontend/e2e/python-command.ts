import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** e2e 解释器探测：优先显式 `YB_E2E_PYTHON`（CI / 其他机器），否则试常见 venv 位置。
 *
 * `playwright.config.ts` 用它拉起 webServer；`clock-boundary.spec.ts` 用它自起冻结
 * 实例。两处必须同一口径：配置找到的 venv 与用例找到的解释器不一致时，冻结用例会
 * 在另一个解释器上失败。
 *
 * 候选路径按 **frontend/** 目录解析（本文件在 `frontend/e2e/`）。
 */
const FRONTEND_DIR = new URL("..", import.meta.url);

const CANDIDATES = [
  "../.venv/Scripts/python.exe", // 本 worktree 自带
  "../../yiban-auto-sign/.venv/Scripts/python.exe", // 主检出（worktree 通常无 venv）
  "../.venv/bin/python",
];

export function pythonCommand(): string {
  if (process.env.YB_E2E_PYTHON) return process.env.YB_E2E_PYTHON;
  for (const rel of CANDIDATES) {
    if (existsSync(fileURLToPath(new URL(rel, FRONTEND_DIR)))) return rel;
  }
  return "python";
}
