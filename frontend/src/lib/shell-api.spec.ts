import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * 外壳请求层行为等价判据（uz7w 拆壳批）。
 *
 * 不变式（任何时刻都不该被违反）：
 *   1. 并发 GET 去重：同一键的未落地请求只发一次网络请求、共享同一 Promise；
 *      落地后键即移除——没有响应缓存，再取必发新请求。
 *   2. 缓存键形状：path + body + headers + conditional 全等才算同一键。
 *   3. ETag/304 语义：只有显式 conditional 的 GET 才带 If-None-Match；200 存 ETag、
 *      304 返回 notModified 哨兵（不当错误）、其余状态清 ETag。
 *   4. apiCached：TTL 内命中不执行 fn；失败/空结果不写缓存；写请求成功后整体失效。
 *   5. 单一定义点：inflightGets / apiCached 在全站前端源码里只有一份实现。
 *
 * 手法与 Python 侧真跑门一致（tests/test_users_exit_surface_frontend.py 的
 * `_extract_js_function`）：从 core.js 源码按花括号配对抽出函数与声明，在
 * happy-dom 环境里注入桩 fetch 真跑。core.js 是 classic script，无法 import——
 * 抽取跑是唯一能钉「行为」而不止钉「源码形状」的手段。
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "..", "..", "..");
const CORE_JS = join(REPO, "web", "static", "js", "core.js");
const coreSrc = readFileSync(CORE_JS, "utf8");

/** 从源码里按花括号配对抽出一个 `function NAME(...) {...}` 的完整文本。 */
function extractFn(src: string, name: string): string {
  const head = src.indexOf(`function ${name}(`);
  if (head < 0) throw new Error(`core.js 未找到 function ${name}(`);
  const bodyStart = src.indexOf("{", head);
  let depth = 0;
  for (let i = bodyStart; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") {
      depth--;
      if (depth === 0) return src.slice(head, i + 1);
    }
  }
  throw new Error(`function ${name}( 花括号不配对`);
}

/** 抽出一个单行 `var NAME = ...;` 声明。 */
function extractVar(src: string, name: string): string {
  const m = src.match(new RegExp(`var ${name} = .*;`));
  if (!m) throw new Error(`core.js 未找到 var ${name} 声明`);
  return m[0];
}

/** 组装可独立求值的请求层源码：声明 + 函数，全部来自 core.js 原文，零改写。 */
const PIECES = [
  extractVar(coreSrc, "APP_BASE"),
  extractVar(coreSrc, "csrfToken"),
  extractVar(coreSrc, "mePromise"),
  extractVar(coreSrc, "etagStore"),
  extractVar(coreSrc, "API_TIMEOUT_MS"),
  extractVar(coreSrc, "inflightGets"),
  extractVar(coreSrc, "CACHE_PREFIX"),
  extractVar(coreSrc, "ENV_LINE_BREAK_CODES"),
  ["forEach", "$", "url", "httpError", "networkError", "timeoutError", "genericMessage",
    "fetchMe", "perform", "handleResponse", "refreshThenRetry", "normalizeRequest",
    "api", "cacheGet", "cacheSet", "cacheClearAll", "cacheDrop", "apiCached",
    "hasEnvLineBreak", "envBodyLineBreak"].map((n) => extractFn(coreSrc, n)).join("\n"),
].join("\n");

type ApiModule = {
  api: (m: string, p: string, b?: unknown, o?: { conditional?: boolean }) => Promise<unknown>;
  apiCached: (k: string, ttl: number, fn: () => Promise<unknown> | unknown) => Promise<unknown>;
  cacheDrop: (k: string) => void;
  state: () => { inflightGets: Record<string, unknown>; etagStore: Record<string, string>; csrfToken: string };
};

function loadModule(fetchImpl: typeof fetch): ApiModule {
  const factory = new Function(
    "window", "document", "sessionStorage", "localStorage", "fetch", "AbortController", "location",
    `${PIECES}\nreturn { api: api, apiCached: apiCached, cacheDrop: cacheDrop, state: function () { return { inflightGets: inflightGets, etagStore: etagStore, csrfToken: csrfToken }; } };`,
  );
  return factory(window, window.document, window.sessionStorage, window.localStorage, fetchImpl, window.AbortController, window.location);
}

/** 可编程 fetch 桩：按登记的响应序列逐个消费；记录每次调用。 */
function makeFetch(responder: (url: string, init: RequestInit) => { status: number; body?: unknown; headers?: Record<string, string> } | Promise<never>) {
  const calls: { url: string; init: RequestInit }[] = [];
  const impl = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init: init ?? {} });
    const r = responder(url, init ?? {});
    const body = r && typeof (r as { then?: unknown }).then === "function" ? (r as unknown as Promise<never>) : Promise.resolve(r as { status: number; body?: unknown; headers?: Record<string, string> });
    return body.then((resp: { status: number; body?: unknown; headers?: Record<string, string> }) => ({
      status: resp.status,
      ok: resp.status >= 200 && resp.status < 300,
      headers: { get: (k: string) => (resp.headers ?? {})[k] ?? null },
      text: () => Promise.resolve(resp.body === undefined ? "" : JSON.stringify(resp.body)),
    }));
  }) as unknown as typeof fetch;
  return { calls, impl };
}

function jsonResponse(status: number, body: unknown, headers?: Record<string, string>) {
  return { status, body, headers };
}

let NOW = 1_700_000_000_000;

beforeEach(() => {
  window.sessionStorage.clear();
  NOW = 1_700_000_000_000;
  vi.spyOn(Date, "now").mockImplementation(() => NOW);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("外壳请求层：并发 GET 去重", () => {
  it("同键未落地请求只发一次、共享同一结果；落地后再取发新请求", async () => {
    let release!: (v: { status: number; body: unknown }) => void;
    const gate = new Promise<{ status: number; body: unknown }>((res) => { release = res; });
    const { calls, impl } = makeFetch(() => gate);
    const mod = loadModule(impl);

    const p1 = mod.api("GET", "/api/x");
    const p2 = mod.api("GET", "/api/x");
    expect(calls.length, "去重后只发一次网络请求").toBe(1);
    expect(Object.keys(mod.state().inflightGets).length).toBe(1);

    release(jsonResponse(200, { ok: true, v: 1 }));
    await expect(p1).resolves.toEqual({ ok: true, v: 1 });
    await expect(p2).resolves.toEqual({ ok: true, v: 1 });
    expect(Object.keys(mod.state().inflightGets).length, "落地后键即移除").toBe(0);

    const { calls: calls3, impl: impl3 } = makeFetch(() => jsonResponse(200, { ok: true, v: 3 }));
    const mod3 = loadModule(impl3);
    await mod3.api("GET", "/api/x");
    await mod3.api("GET", "/api/x");
    expect(calls3.length, "落地后的第二次取数必须发新请求").toBe(2);
    void impl;
  });

  it("缓存键含 body / headers / conditional：任一不同即不同键", () => {
    const { calls, impl } = makeFetch(() => jsonResponse(200, { ok: true }));
    const mod = loadModule(impl);
    mod.api("GET", "/api/x");
    mod.api("GET", "/api/x", { a: 1 });
    mod.api("GET", "/api/x", { a: 1 }, { conditional: true });
    mod.api("GET", "/api/x", { a: 1 }, { conditional: true });
    expect(calls.length, "三种键互不去重，第四个与第三个同键去重").toBe(3);
  });

  it("写请求不走去重", () => {
    const { calls, impl } = makeFetch(() => jsonResponse(200, { ok: true }));
    const mod = loadModule(impl);
    mod.api("POST", "/api/x", { a: 1 });
    mod.api("POST", "/api/x", { a: 1 });
    expect(calls.length).toBe(2);
  });
});

describe("外壳请求层：ETag / 304 语义", () => {
  it("conditional 200 存 ETag，下次同键带 If-None-Match；304 回 notModified 哨兵", async () => {
    const responses = [
      jsonResponse(200, { ok: true, v: 1 }, { ETag: '"v1"' }),
      jsonResponse(304),
    ];
    const { calls, impl } = makeFetch(() => responses.shift() ?? jsonResponse(500, { ok: false }));
    const mod = loadModule(impl);

    await mod.api("GET", "/api/list", null, { conditional: true });
    expect(mod.state().etagStore["/api/list"]).toBe('"v1"');

    const second = await mod.api("GET", "/api/list", null, { conditional: true });
    expect(calls.length).toBe(2);
    const headers = calls[1].init.headers as Record<string, string>;
    expect(headers["If-None-Match"], "conditional GET 必须携带上次的 ETag").toBe('"v1"');
    expect(second, "304 不当错误，回 notModified 哨兵").toEqual({ ok: true, notModified: true });
  });

  it("非 conditional 的 GET 绝不带 If-None-Match", async () => {
    const responses = [
      jsonResponse(200, { ok: true }, { ETag: '"v1"' }),
      jsonResponse(200, { ok: true }),
    ];
    const { calls, impl } = makeFetch(() => responses.shift() ?? jsonResponse(500, { ok: false }));
    const mod = loadModule(impl);
    await mod.api("GET", "/api/list");
    await mod.api("GET", "/api/list");
    expect(calls[1].init.headers as Record<string, string>).not.toHaveProperty("If-None-Match");
  });

  it("非 304 的失败响应清除已存 ETag", async () => {
    const responses = [
      jsonResponse(200, { ok: true }, { ETag: '"v1"' }),
      jsonResponse(500, { ok: false, error: "x" }),
    ];
    const { impl } = makeFetch(() => responses.shift() ?? jsonResponse(500, { ok: false }));
    const mod = loadModule(impl);
    await mod.api("GET", "/api/list", null, { conditional: true });
    await mod.api("GET", "/api/list", null, { conditional: true }).catch(() => undefined);
    expect(mod.state().etagStore["/api/list"], "500 后 ETag 必须清除").toBeUndefined();
  });
});

describe("外壳请求层：写请求行分隔符前置拦截", () => {
  it("body 含行分隔符的写请求在提交前被拒绝，不发网络请求", async () => {
    // 常量在 core.js 载入期读取（模板先行渲染，同一口径），故先置再装载。
    (window as { YB_ENV_LINE_BREAK_CODES?: number[] }).YB_ENV_LINE_BREAK_CODES = [0x2028, 0x2029];
    const { calls, impl } = makeFetch(() => jsonResponse(200, { ok: true }));
    const mod = loadModule(impl);
    await expect(mod.api("POST", "/api/settings", { v: "a\u2028b" })).rejects.toThrow(/行分隔符/);
    expect(calls.length, "被拦截的请求不得触网").toBe(0);
    delete (window as { YB_ENV_LINE_BREAK_CODES?: number[] }).YB_ENV_LINE_BREAK_CODES;
  });
});

describe("外壳数据缓存 apiCached", () => {
  it("TTL 内命中不执行 fn；过期后重取；cacheDrop 强制失效", async () => {
    const { impl } = makeFetch(() => jsonResponse(200, { ok: true }));
    const mod = loadModule(impl);
    let calls = 0;
    const fn = () => {
      calls++;
      return Promise.resolve({ n: calls });
    };

    await mod.apiCached("k", 60_000, fn);
    await mod.apiCached("k", 60_000, fn);
    expect(calls, "TTL 内第二次读必须命中缓存").toBe(1);

    NOW += 61_000;
    await mod.apiCached("k", 60_000, fn);
    expect(calls, "过期后必须重取").toBe(2);

    NOW += 1000;
    mod.cacheDrop("k");
    await mod.apiCached("k", 60_000, fn);
    expect(calls, "cacheDrop 后必须真发网络").toBe(3);
  });

  it("失败与空结果不写缓存", async () => {
    const { impl } = makeFetch(() => jsonResponse(200, { ok: true }));
    const mod = loadModule(impl);
    await mod.apiCached("k", 60_000, () => Promise.reject(new Error("x"))).catch(() => undefined);
    let calls = 0;
    await mod.apiCached("k", 60_000, () => {
      calls++;
      return Promise.resolve({ n: 1 });
    });
    expect(calls, "失败不得写缓存（下一拍必须真取）").toBe(1);

    await mod.apiCached("empty", 60_000, () => Promise.resolve(null));
    let emptyCalls = 0;
    await mod.apiCached("empty", 60_000, () => {
      emptyCalls++;
      return Promise.resolve({ n: 1 });
    });
    expect(emptyCalls, "空结果不得写缓存").toBe(1);
  });

  it("写请求成功后整体失效缓存与 ETag 表", async () => {
    const responses = [
      jsonResponse(200, { ok: true, v: 1 }, { ETag: '"v1"' }),
      jsonResponse(200, { ok: true }),
      jsonResponse(200, { ok: true }),
    ];
    const { calls, impl } = makeFetch(() => responses.shift() ?? jsonResponse(500, { ok: false }));
    const mod = loadModule(impl);
    let n = 0;
    await mod.apiCached("k", 60_000, () => Promise.resolve({ n: ++n }));
    await mod.api("GET", "/api/list", null, { conditional: true });
    expect(Object.keys(mod.state().etagStore).length).toBe(1);

    await mod.api("POST", "/api/x", { a: 1 });
    let calls2 = 0;
    await mod.apiCached("k", 60_000, () => {
      calls2++;
      return Promise.resolve({ n: ++n });
    });
    expect(calls2, "写后缓存必须整体失效，重读必须真取").toBe(1);
    expect(mod.state().etagStore, "写后 ETag 表必须清空").toEqual({});
    expect(calls.length, "网络请求 = 条件 GET + POST（apiCached 的 fn 不触网）").toBe(2);
  });
});

describe("请求层单一定义点", () => {
  it("inflightGets / apiCached 全站只有 core.js 一份实现", () => {
    const roots = [join(REPO, "web", "static", "js"), join(REPO, "frontend", "src")];
    const hits: string[] = [];
    const walk = (dir: string): void => {
      for (const name of readdirSync(dir)) {
        if (name === "node_modules" || name === "vendor") continue;
        const full = join(dir, name);
        if (statSync(full).isDirectory()) {
          walk(full);
          continue;
        }
        if (!/\.(js|ts|vue)$/.test(name) || /\.spec\.ts$/.test(name) || /\.d\.ts$/.test(name)) continue;
        const src = readFileSync(full, "utf8");
        if (/var inflightGets\s*=/.test(src) || /function apiCached\s*\(/.test(src)) {
          hits.push(relative(REPO, full).split("\\").join("/"));
        }
      }
    };
    for (const r of roots) walk(r);
    expect(hits, "取数缓存/去重只允许 core.js 一份实现").toEqual(["web/static/js/core.js"]);
  });
});
