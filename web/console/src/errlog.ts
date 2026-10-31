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

/** 一批最多多少字节(站点收请求最多 64 KiB,留余量;外审 W55-1 第 14 条)。 */
export const BATCH_BYTES = 48_000;
const enc = new TextEncoder();

/** 从队头取一批:条数不超过 200、JSON 字节数不超过 ``BATCH_BYTES``(单条太大的截短)。 */
function takeBatch(): Item[] {
  const out: Item[] = [];
  let bytes = 64;
  while (queue.length && out.length < 200) {
    const it = queue[0];
    const n = enc.encode(JSON.stringify(it)).length + 1;
    if (out.length && bytes + n > BATCH_BYTES) break;
    queue.shift();
    out.push(n > BATCH_BYTES ? { ...it, message: it.message.slice(0, 500), detail: it.detail.slice(0, 500) } : it);
    bytes += n;
  }
  return out;
}

export async function flush(): Promise<void> {
  if (!queue.length) return;
  const items = takeBatch();
  try {
    await api("POST", "/api/client-errors", { client: "web", items });
  } catch (e) {
    // 没登录、站点收不下(413):这一批丢掉,免得永远卡在同一批;连不上:放回去下次再传
    if (e instanceof ApiError && (e.status === 401 || e.status === 413 || e.status === 400)) return;
    queue.unshift(...items.slice(0, MAX - queue.length));
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
