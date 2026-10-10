// 站点接口(商业化 B1)。网页靠 HttpOnly cookie 认人(脚本拿不到令牌);改东西的请求带 X-D1Max-Web 头防跨站伪造。

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export type Fetch = typeof fetch;

let fetcher: Fetch = (...a) => fetch(...a);
/** 测试换掉网络。 */
export function setFetch(f: Fetch): void {
  fetcher = f;
}

/** 401 时叫它(回登录页)。 */
export const onUnauthorized: { cb: () => void } = { cb: () => {} };

export async function api<T = unknown>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    headers["X-D1Max-Web"] = "1";
  }
  let resp: Response;
  try {
    resp = await fetcher(path, {
      method,
      headers,
      credentials: "same-origin",
      body: method === "GET" ? undefined : JSON.stringify(body ?? {}),
    });
  } catch {
    throw new ApiError(0, "network");
  }
  let data: unknown = {};
  try {
    data = await resp.json();
  } catch {
    data = {};
  }
  if (!resp.ok) {
    const msg = (data as { error?: string }).error ?? `HTTP ${resp.status}`;
    // 登录本身、开页时问「我是谁」的 401 不算「过期」:只有用着用着被踢才回登录页并提示
    if (resp.status === 401 && path !== "/api/login" && path !== "/api/me") onUnauthorized.cb();
    throw new ApiError(resp.status, msg);
  }
  return data as T;
}

export const enc = encodeURIComponent;
