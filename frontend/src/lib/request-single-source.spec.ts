import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * 请求层单一实现守卫（退役账「双请求层合一」）。
 *
 * 不变式：`frontend/src/**` 里**产生网络请求的实现只有一处** —— `lib/shell.ts` 的
 * `api()`（它委托 core.js 的 `YB.api`）。这条不变式一破，第二套请求语义就自己长出来：
 * CSRF 头、401 清 token 重读 `/api/me` 再重试一次、并发 GET 去重、20s 超时中止、
 * 错误归一化（`.status`/`.isNetwork`）——任何一条在第二份实现里缺席都是静默的。
 *
 * 历史：审计页与试点页曾走 `lib/api.ts` 的 `apiGet`，它**刻意不复刻**上述语义，
 * 于是那两个面完全没有 401 重试（会话过期时表现为"一直转圈/报网络错误"而不是重取身份）。
 * 本次删除第二实现，两个面改调 `shell.api`。
 *
 * 为什么测这个性质：它是**结构性排除**，不是"新代码看起来会处理"——只要没有第二处
 * `fetch(`，就不可能出现第二套请求语义。把这条断言钉住，下一个想加"轻量 GET 客户端"的人
 * 会立刻红。
 *
 * 允许面：`EventSource`（SSE 进度流，浏览器原生重连）不是请求实现，不计。
 */
const SRC = dirname(dirname(fileURLToPath(import.meta.url))); // frontend/src

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (name === "node_modules") continue;
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      sourceFiles(full, out);
      continue;
    }
    if (!/\.(ts|vue|js)$/.test(name)) continue;
    if (/\.spec\.ts$/.test(name) || /\.d\.ts$/.test(name)) continue;
    out.push(full);
  }
  return out;
}

describe("请求层单一实现", () => {
  it("frontend/src 里只有 lib/shell.ts 一处 fetch()", () => {
    const hits = sourceFiles(SRC)
      .filter((f) => /\bfetch\s*\(/.test(readFileSync(f, "utf8")))
      .map((f) => relative(SRC, f).split("\\").join("/"));
    expect(hits).toEqual(["lib/shell.ts"]);
  });

  it("审计页与试点页不再自带请求实现（改调外壳 api）", () => {
    for (const rel of ["audit/Audit.vue", "pilot/Pilot.vue"]) {
      const src = readFileSync(join(SRC, rel), "utf8");
      expect(src, `${rel} 必须走 shell.api`).toContain('from "../lib/shell"');
      expect(src, `${rel} 不得再引第二套请求模块`).not.toMatch(/from "\.\.\/lib\/api"/);
      expect(src, `${rel} 不得裸用 fetch`).not.toMatch(/\bfetch\s*\(/);
    }
  });
});
