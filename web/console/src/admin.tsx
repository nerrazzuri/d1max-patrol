// 管理页共用的小件(W44 起):读一次的 useLoad、设备一格、App 版本太老、手机扫码添加站点。各页在 adv*.tsx、
// userspage.tsx、robotspage.tsx、layout*.tsx。
import { useCallback, useEffect, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { t, type Key } from "./i18n";
import { Shape } from "./icons";
import { qrGrid } from "./qr";


export function useLoad<T>(path: string): [T | null, () => void, string] {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState("");
  const load = useCallback(() => {
    api<T>("GET", path)
      .then((d) => {
        setData(d);
        setErr("");
      })
      .catch((e) => setErr(e instanceof ApiError ? e.message : String(e)));
  }, [path]);
  useEffect(load, [load]);
  return [data, load, err];
}

// ------------------------------------------------------------------ 设备、版本(机器人页、系统页共用)

interface AdminRobot {
  robot_id: string;
  revoked: boolean;
  expires_at: number;
  manual_only: unknown;
  status?: { online: boolean } | null;
  /** 站点说这份状态还新鲜。狗失联以后 `status.online` 停在最后那一次，要看这个（跟值守页一个口径）。 */
  fresh?: boolean;
  capabilities?: { agent?: string } | null;
  /** 设备健康(W40):狗每 10 s 带一份;老狗、没装上装的是 null。 */
  devices?: { items: DeviceItem[]; stale?: boolean } | null;
}
export interface DeviceItem { id: string; state: "online" | "offline" | "degraded" | "error"; detail?: string }

/** 设备那一格:都在线 / 几台有问题(悬停看是哪几台)/ 没更新。没报是 null。 */
export function deviceSummary(d: AdminRobot["devices"]): { tone: "success" | "warning" | "error"; text: string; title: string } | null {
  if (!d || !Array.isArray(d.items) || d.items.length === 0) return null;
  const bad = d.items.filter((x) => x.state !== "online");
  const title = bad.map((x) => `${x.id}: ${t(`dev_${x.state}` as Key)}${x.detail ? ` · ${x.detail}` : ""}`).join("\n");
  if (d.stale) return { tone: "warning", text: t("devStale"), title };
  if (!bad.length) return { tone: "success", text: t("devAllOk"), title: "" };
  return { tone: bad.some((x) => x.state === "error" || x.state === "offline") ? "error" : "warning",
    text: t("devBad", { n: bad.length }), title };
}
export interface ClientRow { name: string; kind: string; version?: string; platform?: string; api_level?: number; last_used: number }

/** 登录着的 App 太老了没有:它报的接口级别比站点要的低(没报级别的说不清,不算)。 */
export function clientTooOld(c: ClientRow, minAppLevel: number | undefined): boolean {
  return c.kind === "app" && c.api_level != null && minAppLevel != null && c.api_level < minAppLevel;
}

// ------------------------------------------------------------------ 系统



// ------------------------------------------------------------------ 手机扫码添加站点(App V2)

interface Pairing { code: string; name: string; url: string; fingerprint: string }

/** 配对码 = 站点名 + 手机要连的地址 + 证书指纹(没有账号口令)。地址缺省是值班台现在的地址,
 * 手机走别的地址(VPN、另一个网段)就改一下。 */
/** 网页是不是在站点主机本机打开的(手机连不了这个地址)。 */
export function isLoopbackOrigin(origin: string): boolean {
  try {
    const h = new URL(origin).hostname.replace(/^\[|\]$/g, "");
    return h === "localhost" || h === "::1" || /^127\./.test(h);
  } catch {
    return false;
  }
}

/** 二维码里放哪个地址(POC):网页地址手机能连就用它;本机打开的换成站点报的第一个局域网地址;
 * 一个都没有回 null(页面说清楚,不给 127.0.0.1)。 */
export function pickPairUrl(origin: string, lan: string[]): string | null {
  if (!isLoopbackOrigin(origin)) return origin;
  return lan[0] ?? null;
}

/** 可选的地址:网页自己的地址在最前(手机能连的话),再是站点报的主机名和局域网地址;去重。 */
export function pairChoices(origin: string, lan: string[]): string[] {
  const out = isLoopbackOrigin(origin) ? [] : [origin];
  for (const a of lan) if (!out.includes(a)) out.push(a);
  return out;
}

export function PairPhone() {
  const [url, setUrl] = useState(window.location.origin);
  const [asked, setAsked] = useState(isLoopbackOrigin(window.location.origin) ? "" : window.location.origin);
  const [pair, setPair] = useState<Pairing | null>(null);
  const [err, setErr] = useState("");
  const [lan, setLan] = useState<string[] | null>(null);
  useEffect(() => {
    let live = true;
    api<{ addresses: string[] }>("GET", "/api/pairing/addresses")
      .then((d) => {
        if (!live) return;
        setLan(d.addresses);
        const u = pickPairUrl(window.location.origin, d.addresses);
        if (u && isLoopbackOrigin(window.location.origin)) { setUrl(u); setAsked(u); }
      })
      .catch(() => { if (live) setLan([]); });
    return () => { live = false; };
  }, []);
  useEffect(() => {
    let live = true;
    if (!asked) { setPair(null); return; }
    api<Pairing>("GET", `/api/pairing?url=${enc(asked)}`)
      .then((d) => { if (live) { setPair(d); setErr(""); } })
      .catch((e) => { if (live) { setPair(null); setErr(e instanceof ApiError ? e.message : String(e)); } });
    return () => { live = false; };
  }, [asked]);
  const g = pair ? qrGrid(pair.code) : null;
  return (
    <section class="panel">
      <div class="panel-head"><h2>{t("pairTitle")}</h2></div>
      <div class="pair-phone pad">
        {/* 白底黑码:扫码器要的是这个,不跟着深色主题走 */}
        {g ? (
          <svg class="qr" viewBox={`0 0 ${g.size} ${g.size}`} role="img" aria-label={t("pairQrAria")} shape-rendering="crispEdges">
            <rect width={g.size} height={g.size} fill="#fff" />
            <path d={g.d} fill="#000" />
          </svg>
        ) : (
          <div class="qr none">{err ? <span class="state p2"><Shape kind="P2" />{t("pairBadUrl")}</span> : null}</div>
        )}
        <div class="pair-text">
          <p>{t("pairHint")}</p>
          {isLoopbackOrigin(window.location.origin) && lan !== null && lan.length === 0 && (
            <p class="error small" role="alert" data-pair-nolan="1">{t("pairNoLan")}</p>
          )}
          {lan !== null && pairChoices(window.location.origin, lan).length > 1 && (
            <div class="row wrap" data-pair-lan="1">
              {pairChoices(window.location.origin, lan).map((a) => (
                <button key={a} type="button" class={a === asked ? "btn sm primary" : "btn sm"}
                  onClick={() => { setUrl(a); setAsked(a); }}>{a}</button>
              ))}
            </div>
          )}
          <form class="row" onSubmit={(e) => { e.preventDefault(); setAsked(url.trim()); }}>
            <label class="field grow">
              <span>{t("pairUrl")}</span>
              <input type="url" value={url} onInput={(e) => setUrl((e.target as HTMLInputElement).value)} spellcheck={false} autocomplete="off" />
            </label>
            <button type="submit" class="btn" disabled={url.trim() === asked}>{t("pairUpdate")}</button>
          </form>
          {pair && (
            <dl class="kv">
              <dt>{t("pairSite")}</dt><dd>{pair.name}</dd>
              <dt>{t("pairFingerprint")}</dt><dd class="mono small wrap">{pair.fingerprint}</dd>
              <dt>{t("pairCode")}</dt><dd class="mono small wrap">{pair.code}</dd>
            </dl>
          )}
          <p class="muted small">{t("pairNoSecret")}</p>
        </div>
      </div>
    </section>
  );
}
