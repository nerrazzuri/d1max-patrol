// 站点接口(商业化 B1)。网页靠 HttpOnly cookie 认人(脚本拿不到令牌);改东西的请求带 X-D1Max-Web 头防跨站伪造。

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** 站点回的整个 JSON(比如路线校验没过时的逐点 errors);没有 = undefined。 */
    readonly data?: unknown,
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

/** W55:接口回 5xx 时叫它(记进网页错误记录);errlog 接上。 */
export const onServerError: { cb: ((method: string, path: string, status: number, msg: string) => void) | null } = { cb: null };

/** 401 时叫它(回登录页)。 */
export const onUnauthorized: { cb: () => void } = { cb: () => {} };

/** ``signal``:调用方能取消(W44 复查 R5:画面健康请求超时、换画面、卸载时取消在途的)。 */
export async function api<T = unknown>(method: "GET" | "POST", path: string, body?: unknown,
                                       opts: { signal?: AbortSignal } = {}): Promise<T> {
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
      signal: opts.signal,
    });
  } catch {
    throw new ApiError(0, opts.signal?.aborted ? "aborted" : "network");
  }
  let data: unknown = {};
  try {
    data = await resp.json();
  } catch (e) {
    // 成功的回包正文读到一半被取消、断网(W44 复查 N1):不是「回了个空的」,是没问成。
    // 出错的回包(401 等)状态码已经说清楚了,照旧按状态码走(复查 M3:401 要回登录页)。
    if (resp.ok && opts.signal?.aborted) throw new ApiError(0, "aborted");
    if (resp.ok && !(e instanceof SyntaxError)) throw new ApiError(0, "network");
    data = {};                                       // 正文不是 JSON(老接口、空回包):照旧当空的
  }
  if (!resp.ok) {
    const msg = (data as { error?: string }).error ?? `HTTP ${resp.status}`;
    if (resp.status >= 500 && onServerError.cb) onServerError.cb(method, path, resp.status, msg);
    // 登录本身、开页时问「我是谁」的 401 不算「过期」:只有用着用着被踢才回登录页并提示
    if (resp.status === 401 && path !== "/api/login" && path !== "/api/me") onUnauthorized.cb();
    throw new ApiError(resp.status, msg, data);
  }
  return data as T;
}

export const enc = encodeURIComponent;

/** 传文件(W44 第四期:升级包):请求体就是文件本身,别的放头里。回包、出错跟 ``api`` 一样。 */
export async function apiUpload<T = unknown>(path: string, file: Blob, headers: Record<string, string> = {}): Promise<T> {
  let resp: Response;
  try {
    resp = await fetcher(path, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/octet-stream", "X-D1Max-Web": "1", ...headers },
      credentials: "same-origin",
      body: file,
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
    if (resp.status === 401) onUnauthorized.cb();
    throw new ApiError(resp.status, (data as { error?: string }).error ?? `HTTP ${resp.status}`, data);
  }
  return data as T;
}
