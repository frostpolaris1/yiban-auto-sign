/**
 * 薄 GET 客户端（试点页只读）。
 *
 * BASE 由 partials/theme_boot.html 在绘制前写为全局（子路径部署前缀），
 * 这里读取而不是另设配置。同源凭据 + Accept: application/json 与 core.js 的
 * 请求层同口径。
 *
 * 刻意**不**复刻 core.js 的完整语义（CSRF 头、401 清 token 重读 /api/me 再重试、
 * 并发 GET 去重）——那层属于第一个正式迁移页；试点页只做「错误可见 + 手动重试」。
 */
const BASE: string =
  typeof window !== "undefined" && typeof (window as { BASE?: unknown }).BASE === "string"
    ? (window as { BASE: string }).BASE
    : "";

export function apiUrl(path: string): string {
  return path.startsWith("/") ? BASE + path : path;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const resp = await fetch(apiUrl(path), {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  const text = await resp.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!resp.ok) {
    const msg =
      data !== null && typeof data === "object" && "error" in (data as Record<string, unknown>)
        ? String((data as { error: unknown }).error)
        : `请求失败（${resp.status}）`;
    throw new ApiError(msg, resp.status);
  }
  if (data === null) throw new ApiError("响应不是 JSON", resp.status);
  return data as T;
}
