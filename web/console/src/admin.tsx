// 「高级 → 系统」(商业化 B1b 起):体检、版本、手机配对。摄像头登记在「布置 → 摄像头」(W44 第三期)。
// 用户页挪到 userspage.tsx、机器人页挪到 robotspage.tsx(W44 第二期)。
import { useCallback, useEffect, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { hm } from "./data";
import { t, type Key } from "./i18n";
import { Shape } from "./icons";
import { qrGrid } from "./qr";

type Say = (s: string) => void;

function useLoad<T>(path: string): [T | null, () => void, string] {
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
interface Versions { site: { version: string; min_app_level?: number }; robots: { robot_id: string; verdict: string; sidecar_proto?: number | null }[]; clients?: ClientRow[] }

/** 登录着的 App 太老了没有:它报的接口级别比站点要的低(没报级别的说不清,不算)。 */
export function clientTooOld(c: ClientRow, minAppLevel: number | undefined): boolean {
  return c.kind === "app" && c.api_level != null && minAppLevel != null && c.api_level < minAppLevel;
}

// ------------------------------------------------------------------ 系统

interface Health { ok: boolean; version: string; checks: Record<string, { ok: boolean; detail?: string }> }

const CRITICAL = new Set(["db", "loop", "broker", "disk"]);

export function SystemPage(_p: { say?: Say }) {
  const [health, reload] = useLoad<Health>("/api/health");
  const [versions] = useLoad<Versions>("/api/versions");
  useEffect(() => {
    const id = window.setInterval(reload, 30_000);
    return () => window.clearInterval(id);
  }, [reload]);
  const checks = Object.entries(health?.checks ?? {});
  return (
    <div class="admin">
      <section class="panel">
        <div class="panel-head">
          <h1>{t("health")}</h1>
          {health && (
            <span class={health.ok ? "state tone-success" : "state p2"}>{!health.ok && <Shape kind="P2" />}{health.ok ? t("healthOk") : t("healthBad")}</span>
          )}
          <span class="muted small push">{health ? t("siteVersion", { v: health.version }) : ""} {health ? hm(Date.now()) : ""}</span>
        </div>
        <table class="grid">
          <tbody>
            {checks.map(([k, c]) => (
              <tr key={k}>
                <td class="strong">{t(`check_${k}` as Key)}{CRITICAL.has(k) ? <span class="muted small"> · {t("critical")}</span> : null}</td>
                <td>{c.ok ? <span class="state tone-success">OK</span> : <span class={CRITICAL.has(k) ? "state p1" : "state p2"}><Shape kind={CRITICAL.has(k) ? "P1" : "P2"} />{t("checkFail")}</span>}</td>
                <td class="muted small">{c.detail ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>{t("versions")}</h2></div>
        <table class="grid">
          <tbody>
            {(versions?.robots ?? []).map((r) => (
              <tr key={r.robot_id}><td class="mono strong">{r.robot_id}</td><td>{t(`compat_${r.verdict}` as Key)}</td><td class="muted small">{r.sidecar_proto != null ? `sidecar ${r.sidecar_proto}` : ""}</td></tr>
            ))}
          </tbody>
        </table>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>{t("clients")}</h2><span class="muted small push">{t("clientsHint")}</span></div>
        <table class="grid">
          <tbody>
            {(versions?.clients ?? []).length === 0 && <tr><td class="muted">{t("clientsNone")}</td></tr>}
            {(versions?.clients ?? []).map((c, i) => {
              const old = clientTooOld(c, versions?.site.min_app_level);
              return (
                <tr key={i}>
                  <td class="strong">{c.name}</td>
                  <td>{c.kind === "web" ? t("clientWeb") : `${t("clientApp")} ${c.version ?? "?"}`}</td>
                  <td class="muted small">{[c.platform, c.api_level != null ? `API ${c.api_level}` : ""].filter(Boolean).join(" · ")}</td>
                  <td>{old ? <span class="state p2"><Shape kind="P2" />{t("clientOld")}</span> : ""}</td>
                  <td class="muted small mono">{hm(c.last_used)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
      <PairPhone />
      <section class="panel">
        <div class="panel-head">
          <h2>{t("supportBundle")}</h2>
          <a class="btn push" href="/api/support-bundle" download="d1max-support.tar.gz">{t("supportBundle")}</a>
        </div>
        <p class="muted small pad">{t("supportHint")}</p>
      </section>
    </div>
  );
}

// ------------------------------------------------------------------ 手机扫码添加站点(App V2)

interface Pairing { code: string; name: string; url: string; fingerprint: string }

/** 配对码 = 站点名 + 手机要连的地址 + 证书指纹(没有账号口令)。地址缺省是值班台现在的地址,
 * 手机走别的地址(VPN、另一个网段)就改一下。 */
function PairPhone() {
  const [url, setUrl] = useState(window.location.origin);
  const [asked, setAsked] = useState(window.location.origin);
  const [pair, setPair] = useState<Pairing | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
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
