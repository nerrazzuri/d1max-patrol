// W55:网页自己的错误记下来,每 10 分钟传给站点一次(登录着的时候;站点留 14 天、进诊断包)。
// 记:页面上没接住的异常、没接住的 Promise 失败、接口回 5xx 或连不上。不记画面、不记密码、不记请求体。
import { api, ApiError, onServerError } from "./api";

interface Item { at_ms: number; kind: string; page: string; message: string; detail: string; app_version: string; platform: string }

const MAX = 500;
const queue: Item[] = [];
const VERSION = (document.querySelector('meta[name="d1max-version"]') as HTMLMetaElement | null)?.content ?? "";

export function note(kind: string, message: string, detail = ""): void {
  queue.push({ at_ms: Date.now(), kind, page: location.hash.slice(0, 120), message: message.slice(0, 2000),
    detail: detail.slice(0, 2000), app_version: VERSION, platform: navigator.userAgent.slice(0, 120) });
  if (queue.length > MAX) queue.splice(0, queue.length - MAX);
}

export async function flush(): Promise<void> {
  if (!queue.length) return;
  const items = queue.splice(0, 200);
  try {
    await api("POST", "/api/client-errors", { client: "web", items });
  } catch (e) {
    // 没登录、站点连不上:放回去下次再传(不再记这一次失败本身,免得越积越多)
    if (!(e instanceof ApiError) || e.status !== 401) queue.unshift(...items.slice(0, MAX - queue.length));
  }
}

export function installErrorLog(): void {
  window.addEventListener("error", (e) => note("error", String(e.message), `${e.filename ?? ""}:${e.lineno ?? ""}`));
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    if (r instanceof ApiError && (r.status === 0 || r.status < 500)) return;   // 断网、业务拒绝不算网页出错
    note("unhandled", r instanceof Error ? r.message : String(r), r instanceof Error ? (r.stack ?? "") : "");
  });
  onServerError.cb = (method, path, status, msg) => {
    if (path !== "/api/client-errors") note("api", `${method} ${path.split("?")[0]} → ${status}`, msg);
  };
  window.setInterval(() => void flush(), 10 * 60_000);
}
