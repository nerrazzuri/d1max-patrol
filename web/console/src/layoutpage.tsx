// 「管理 → 布置」(W44 第三期,设计 §5.2):一张大地图,上面五个标签 —— 防区、禁行区、巡逻路线、待命点、摄像头。
// 选哪个标签只能改那一样,别的四样在图上淡着画、只作参考。右上角「保存」(这个标签有没存的改动才亮)和「历史版本」
// (谁、几点改的,一键退回;退回走原来的保存,禁行区放宽照样要确认)。两个人同时改:后保存的看到「刚才有人改过」。
// 这个文件是外框:选图、拉五样数据、淡层、保存按钮、历史抽屉、切标签前问没存的。各标签在 layout*.tsx。
import type { ComponentChildren, FunctionComponent } from "preact";
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { hm } from "./data";
import { Confirm } from "./dialogs";
import { t, type Key } from "./i18n";
import { MapCanvas, type CanvasArea, type Screen } from "./mapcanvas";
import { toPx, type NavZone, type PreviewMeta, type Pt } from "./mapview";

type Say = (s: string) => void;
export type LayoutTab = "zones" | "nogo" | "routes" | "standby" | "cameras";
export const LAYOUT_TABS: LayoutTab[] = ["zones", "nogo", "routes", "standby", "cameras"];
const TAB_KEY: Record<LayoutTab, Key> = { zones: "lyZones", nogo: "lyNogo", routes: "lyRoutes", standby: "lyStandby", cameras: "lyCameras" };
/** 历史版本里的种类(站点 layout_history.kind)。 */
const HISTORY_KIND: Record<LayoutTab, string> = { zones: "zones", nogo: "nogo", routes: "route", standby: "standby", cameras: "cameras" };

export interface MapRow { map_id: string; version: string; note: string; active?: boolean }
export interface Area { zone: string; points: Pt[] }
export interface NavZones {
  revision: number; zones: NavZone[]; auto_confirmed?: boolean; confirm_text?: string; held?: NavZone[];
  confirmed?: { revision: number; by: string; at_ms: number; current: boolean; how: string } | null;
}
export interface LayoutRoute { id: string; revision: number; waypoints: { name: string; x: number; y: number; yaw: number }[] }
export interface StandbyPt { robot_id: string; name: string; x: number; y: number; yaw: number; map_id: string; map_version: string; default?: boolean }
export interface Placement { map_id: string; map_version: string; x: number; y: number; yaw: number; fov_deg: number }
export interface CamRow { name: string; zone: string; onvif_url: string; rtsp_url?: string; username?: string; has_password?: boolean; motion?: boolean; placement?: Placement | null;
  warnings?: { code: string; text: string }[] }
export interface LayerData { areas: Area[]; nav: NavZones | null; routes: LayoutRoute[]; standby: StandbyPt[]; cameras: CamRow[]; robots: string[] }

export interface TabProps {
  map: MapRow;
  base: string;
  meta: PreviewMeta;
  data: LayerData;
  reload: (k?: keyof LayerData) => Promise<void>;
  setDirty: (b: boolean) => void;
  /** 外框的「保存」按钮调它;回 409 时外框说「刚才有人改过」。 */
  registerSave: (fn: (() => Promise<void>) | null) => void;
  say: Say;
  /** 画好淡层的地图;标签只给自己那一样。 */
  Map: FunctionComponent<OwnLayer>;
}

/** 标签自己那一层(在淡层上面画)。 */
export interface OwnLayer {
  areas?: CanvasArea[];
  world?: ComponentChildren;
  marks?: (s: Screen) => ComponentChildren;
  onPick?: (px: number, py: number) => void;
  drawing?: boolean;
  overlay?: ComponentChildren;
}

function errText(e: unknown): string {
  return t("failed", { why: e instanceof ApiError ? e.message : String(e) });
}

/** 预览像素 → 地图坐标(米)。 */
export function toMap(meta: PreviewMeta, px: number, py: number): Pt {
  return [Math.round((meta.left_x + px * meta.m_per_px) * 100) / 100, Math.round((meta.top_y - py * meta.m_per_px) * 100) / 100];
}

export function polyPx(meta: PreviewMeta, pts: Pt[]): string {
  return pts.map(([x, y]) => { const p = toPx(meta, x, y); return `${p.px},${p.py}`; }).join(" ");
}

/** 摄像头的扇形(预览像素):位置、朝向(弧度,地图系逆时针)、张角、半径(米)。 */
export function fanPath(meta: PreviewMeta, p: Placement, r = 4): string {
  const c = toPx(meta, p.x, p.y);
  const half = (p.fov_deg * Math.PI) / 360;
  const end = (a: number) => toPx(meta, p.x + r * Math.cos(a), p.y + r * Math.sin(a));
  const a = end(p.yaw - half);
  const b = end(p.yaw + half);
  const R = r / meta.m_per_px;
  return `M${c.px},${c.py} L${a.px},${a.py} A${R},${R} 0 ${p.fov_deg > 180 ? 1 : 0} 0 ${b.px},${b.py} Z`;
}

/** 读 ``#/admin/layout?tab=routes`` 里的标签。 */
export function tabFromHash(hash: string): LayoutTab {
  const q = new URLSearchParams(hash.split("?")[1] ?? "");
  const v = q.get("tab");
  return (LAYOUT_TABS as string[]).includes(v ?? "") ? (v as LayoutTab) : "zones";
}

const EMPTY: LayerData = { areas: [], nav: null, routes: [], standby: [], cameras: [], robots: [] };

export function LayoutPage({ say, robots, tabs }: { say: Say; robots: string[]; tabs: Partial<Record<LayoutTab, FunctionComponent<TabProps>>> }) {
  const [maps, setMaps] = useState<MapRow[]>([]);
  const [sel, setSel] = useState<MapRow | null>(null);
  const [meta, setMeta] = useState<PreviewMeta | null>(null);
  const [data, setData] = useState<LayerData>(EMPTY);
  const [tab, setTab] = useState<LayoutTab>(() => tabFromHash(typeof location === "undefined" ? "" : location.hash));
  const [dirty, setDirty] = useState(false);
  const [ask, setAsk] = useState<(() => void) | null>(null);
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState(false);
  const [gen, setGen] = useState(0);
  const saveFn = useRef<(() => Promise<void>) | null>(null);

  useEffect(() => {
    api<{ maps: MapRow[] }>("GET", "/api/maps").then((d) => {
      setMaps(d.maps ?? []);
      setSel((s) => s ?? d.maps.find((m) => m.active) ?? d.maps[d.maps.length - 1] ?? null);
    }).catch((e) => say(errText(e)));
  }, []);
  const base = sel ? `/api/maps/${enc(sel.map_id)}/${enc(sel.version)}` : "";
  const cur = useRef(base);
  cur.current = base;

  const reload = useCallback(async (k?: keyof LayerData) => {
    if (!sel) return;
    const b = base;
    const want = (x: keyof LayerData) => !k || k === x;
    const q = `map_id=${enc(sel.map_id)}&map_version=${enc(sel.version)}`;
    const [areas, nav, routes, standby, cameras] = await Promise.all([
      want("areas") ? api<{ areas: Area[] }>("GET", `${b}/areas`).then((d) => d.areas).catch(() => null) : null,
      want("nav") ? api<NavZones>("GET", `${b}/zones`).catch(() => null) : null,
      want("routes") ? api<{ routes: LayoutRoute[] }>("GET", `/api/routes?${q}`).then((d) => d.routes ?? []).catch(() => null) : null,
      want("standby") ? Promise.all(robots.map((r) => api<{ points: StandbyPt[] }>("GET", `/api/robots/${enc(r)}/standby`)
        .then((d) => (d.points ?? []).map((p) => ({ ...p, robot_id: r }))).catch(() => [] as StandbyPt[])))
        .then((all) => all.flat().filter((p) => p.map_id === sel.map_id && p.map_version === sel.version)) : null,
      want("cameras") ? api<{ cameras: CamRow[] }>("GET", "/api/admin/cameras").then((d) => d.cameras ?? []).catch(() => null) : null,
    ]);
    if (cur.current !== b) return;
    setData((o) => ({
      areas: areas ?? (want("areas") ? [] : o.areas), nav: want("nav") ? nav : o.nav, routes: routes ?? (want("routes") ? [] : o.routes),
      standby: standby ?? (want("standby") ? [] : o.standby), cameras: cameras ?? (want("cameras") ? [] : o.cameras), robots,
    }));
    if (want("nav")) api<PreviewMeta>("GET", `${b}/preview`).then((m) => { if (cur.current === b) setMeta(m); }).catch(() => {});
  }, [base, robots.join(",")]);

  useEffect(() => {
    setMeta(null);
    setData(EMPTY);
    setDirty(false);
    setConflict(false);
    if (!base) return;
    void reload();
  }, [base]);

  const guard = (go: () => void) => (dirty ? setAsk(() => go) : go());
  const switchTab = (x: LayoutTab) => guard(() => {
    setTab(x);
    setDirty(false);
    setConflict(false);
    saveFn.current = null;
    if (typeof location !== "undefined") replaceHash(`#/admin/layout?tab=${x}`);
  });

  // 地址里的 tab 变了(浏览器后退、前进、手改地址)跟着换;有没存的先问,不丢就把地址改回来(演示站点实测查出)
  const tabNow = useRef(tab);
  tabNow.current = tab;
  useEffect(() => {
    const on = () => {
      if (!location.hash.startsWith("#/admin/layout")) return;
      const x = tabFromHash(location.hash);
      if (x !== tabNow.current) switchTab(x);
    };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  });

  async function save() {
    if (!saveFn.current) return;
    setBusy(true);
    setConflict(false);
    try {
      await saveFn.current();
      setDirty(false);
      // 站点存下的那一份可能跟草稿不完全一样(临时区的天数换成到期时刻、坐标取整……):标签按新数据重开
      setGen((g) => g + 1);
      say(t("saved"));
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setConflict(true);
      else say(errText(e));
    } finally {
      setBusy(false);
    }
  }

  const Map = useCallback((own: OwnLayer) => (meta && sel ? (
    <div class={`layout-map tab-${tab}`}>
      <MapCanvas meta={meta} raster={`${base}/preview.png`} label={t("lyTitle")}
        areas={[...(tab === "zones" ? [] : data.areas.map((a) => ({ name: a.zone, pts: a.points, kind: "area" as const }))), ...(own.areas ?? [])]}
        navZones={tab !== "nogo"}
        drawing={own.drawing} onPick={own.onPick} overlay={own.overlay}
        world={<>
          <g class="dim">{dimWorld(tab, meta, data, sel)}</g>
          {own.world}
        </>}
        marks={(s) => <>
          <g class="dim">{dimMarks(tab, data, s)}</g>
          {own.marks?.(s)}
        </>} />
    </div>
  ) : <p class="empty">{t("noMap")}</p>), [meta, sel, tab, data, base]);

  const Tab = tabs[tab];
  return (
    <section class="layout">
      <div class="panel-head">
        <h1>{t("lyTitle")}</h1>
        <label class="field-inline">{t("mapLabel")}
          <select value={sel ? `${sel.map_id}\u0000${sel.version}` : ""} onChange={(e) => {
            const [m, v] = e.currentTarget.value.split("\u0000");
            const next = maps.find((x) => x.map_id === m && x.version === v) ?? null;
            const el = e.currentTarget;
            if (dirty) { el.value = sel ? `${sel.map_id}\u0000${sel.version}` : ""; setAsk(() => () => setSel(next)); } else setSel(next);
          }}>
            {maps.map((m) => <option key={`${m.map_id}:${m.version}`} value={`${m.map_id}\u0000${m.version}`}>
              {m.map_id} · v{m.version}{m.active ? ` · ${t("lyActive")}` : ""}{m.note ? ` · ${m.note}` : ""}</option>)}
          </select>
        </label>
        <span class="push" />
        <button type="button" class="btn" onClick={() => setHistory(!history)} aria-pressed={history}>{t("lyHistory")}</button>
        <button type="button" class="btn primary ly-save" disabled={!dirty || busy} onClick={() => void save()}>{t("save")}</button>
      </div>
      <p class="muted lead">{t("lyLead")}</p>
      <div class="ly-tabs" role="tablist">
        {LAYOUT_TABS.map((x) => (
          <button key={x} type="button" role="tab" aria-selected={tab === x} onClick={() => tab !== x && switchTab(x)}>{t(TAB_KEY[x])}</button>
        ))}
      </div>
      {conflict && (
        <div class="ly-conflict" role="alert">
          <span>{t("lyConflict")}</span>
          <button type="button" class="btn sm" onClick={() => { setConflict(false); setDirty(false); setGen((g) => g + 1); void reload(); }}>{t("lyLatest")}</button>
        </div>
      )}
      {dirty && <p class="arm-dirty" role="status">{t("armDirty")}</p>}
      <div class={history ? "ly-body with-history" : "ly-body"}>
        <div class="ly-main">
          {sel && meta && Tab ? (
            <Tab key={`${tab}|${base}|${gen}`} map={sel} base={base} meta={meta} data={data} reload={reload} say={say} Map={Map}
              setDirty={setDirty} registerSave={(fn) => { saveFn.current = fn; }} />
          ) : !sel ? <p class="empty">{t("noMap")}</p> : null}
        </div>
        {history && sel && (
          <HistoryDrawer key={`${HISTORY_KIND[tab]}|${tab === "cameras" ? "" : base}`} kind={HISTORY_KIND[tab]} map={tab === "cameras" ? null : sel} say={say}
            onRestored={() => { setDirty(false); setGen((g) => g + 1); void reload(); }} onClose={() => setHistory(false)} />
        )}
      </div>
      {ask && (
        <Confirm text={t("lyDiscard")} yes={t("lyDiscardYes")} danger onNo={() => { setAsk(null); replaceHash(`#/admin/layout?tab=${tab}`); }}
          onYes={() => { const go = ask; setAsk(null); setDirty(false); go(); }} />
      )}
    </section>
  );
}

function replaceHash(h: string) {
  try {
    window.history.replaceState(null, "", h);
  } catch {
    /* 测试环境 */
  }
}

/** 别的标签的东西淡着画(世界层,预览像素):禁行区的草稿由禁行区标签自己画;路线画折线。 */
/** 放在这张图这一版上的摄像头。 */
export function camsOn(cams: CamRow[], map: MapRow): CamRow[] {
  return cams.filter((c) => c.placement && c.placement.map_id === map.map_id && c.placement.map_version === map.version);
}

function dimWorld(tab: LayoutTab, meta: PreviewMeta, d: LayerData, map: MapRow): ComponentChildren {
  return (
    <>
      {tab !== "routes" && d.routes.map((r) => r.waypoints.length > 1 && (
        <polyline key={r.id} class="ly-route" points={polyPx(meta, r.waypoints.map((w) => [w.x, w.y] as Pt))} />
      ))}
      {tab !== "cameras" && camsOn(d.cameras, map).map((c) => <path key={c.name} class="ly-fan" d={fanPath(meta, c.placement as Placement)} />)}
    </>
  );
}

/** 标记层(屏上坐标)淡着画:待命点。 */
function dimMarks(tab: LayoutTab, d: LayerData, s: Screen): ComponentChildren {
  if (tab === "standby") return null;
  return d.standby.map((p) => {
    const [x, y] = s.map(p.x, p.y);
    return <rect key={`${p.robot_id}/${p.name}`} class="ly-standby" x={x - 5} y={y - 5} width={10} height={10} rx={2} />;
  });
}

interface HistoryRow { id: number; kind: string; item: string; by: string; at_ms: number; note: string; summary?: string }

/** 历史抽屉。外审 R3:一个抽屉只管一个「哪一层 + 哪张图哪一版」(上面按这个 key 重建,换了就整个换掉,
 *  旧的列表、确认框跟着没了);晚到的回包按代次不认;退回请求带上这三样,站点对不上就拒绝。 */
function HistoryDrawer({ kind, map, say, onRestored, onClose }: {
  kind: string; map: MapRow | null; say: Say; onRestored: () => void; onClose: () => void;
}) {
  const [rows, setRows] = useState<HistoryRow[] | null>(null);
  const [restoring, setRestoring] = useState<HistoryRow | null>(null);
  const gen = useRef(0);
  const expect = { kind, map_id: map?.map_id ?? "", version: map?.version ?? "" };
  const load = () => {
    const my = ++gen.current;
    setRows(null);
    const q = `kind=${enc(expect.kind)}&map_id=${enc(expect.map_id)}&version=${enc(expect.version)}`;
    api<{ history: HistoryRow[] }>("GET", `/api/layout/history?${q}`)
      .then((d) => { if (my === gen.current) setRows(d.history ?? []); })
      .catch(() => { if (my === gen.current) setRows([]); });
  };
  useEffect(() => { load(); return () => { gen.current += 1; }; }, []);
  return (
    <aside class="panel ly-history" aria-label={t("lyHistory")}>
      <div class="panel-head">
        <h2>{t("lyHistory")}</h2>
        <button type="button" class="btn sm push" onClick={onClose}>{t("close")}</button>
      </div>
      {rows == null ? <p class="muted">…</p> : rows.length === 0 ? <p class="muted small">{t("lyHistoryNone")}</p> : (
        <ul class="plain">
          {rows.map((r, i) => (
            <li key={r.id}>
              <div>
                <time class="mono small">{new Date(r.at_ms).toLocaleDateString("sv-SE")} {hm(r.at_ms)}</time> <b>{r.by}</b>
                {r.item && <span class="muted small"> · {r.item}</span>}
                {r.note.startsWith("restored") && <span class="tag">{t("lyRestoredTag")}</span>}
                {r.summary && <div class="muted small">{r.summary}</div>}
              </div>
              {i > 0 && <button type="button" class="btn sm" onClick={() => setRestoring(r)}>{t("lyRestore")}</button>}
              {i === 0 && <span class="muted small">{t("lyCurrent")}</span>}
            </li>
          ))}
        </ul>
      )}
      {restoring && (
        <Confirm text={t(kind === "nogo" ? "lyRestoreNogo" : kind === "route" ? "lyRestoreRoute" : "lyRestoreAsk", { t: hm(restoring.at_ms), by: restoring.by })}
          yes={t("lyRestore")} onNo={() => setRestoring(null)}
          onYes={() => {
            const r = restoring;
            setRestoring(null);
            api<{ warnings?: { camera: string; problem: string }[] }>("POST", `/api/layout/history/${r.id}/restore`, { expect })
              .then((d) => {
                say((d.warnings ?? []).some((w) => w.problem === "password_missing") ? t("lyRestoredPw") : t("lyRestored"));
                load();
                onRestored();
              })
              .catch((e) => say(errText(e)));
          }} />
      )}
    </aside>
  );
}
