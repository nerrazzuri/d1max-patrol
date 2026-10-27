import type { ComponentChildren } from "preact";
// 地图页签(B0 专业版 2.4):地图铺满 + 右侧栏。正常的狗绿点 + 朝向;异常的用形状 + 颜色;P1 位置是
// 菱形 + 报警圈(未确认时闪)。侧栏只给**选中**的狗拉前后两路小画面(有 P1 时默认选 P1 的狗)。
// 地图本身(转正、全图、缩放平移、区域名、图例、比例尺、平面图)在 mapcanvas.tsx;狗和报警点画在屏上坐标里
// (大小不跟着缩放变),位置用跟底图同一个变换算。
import { useEffect, useMemo, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { hm, useDutySettings } from "./data";
import { alertTitle, lang, t, type Key } from "./i18n";
import type { UnitInfo } from "./live";
import { etaNext, LAYERS, loadLayers, saveLayers, shiftStart, staleAfter, staleCells, tonightEvents, zoneDecor,
  type AuthzRow, type Cell, type Layer, type ModeView } from "./maplayers";
import { MapCanvas, type CanvasArea, type LegendKey, type Screen } from "./mapcanvas";
import { heading, namedAreas, onMap, toPx, type PreviewMeta, type Pt } from "./mapview";
import { abnormal, alarmState, canArm, canDispatch, canFindPose, robotMap, type Alert } from "./model";
import { notAccepted, usePosePick, type CommandReply } from "./posepick";
import { pxToMap, yawToward } from "./routes";
import { RunLegendItems, VizWorld, vizAvoid, vizHas, vizMarks, vizOnMap } from "./runlayers";
import { useEpisodes, useRunViz } from "./runviz";

/** 屏上的大小(像素):狗的圆点半径、报警圈半径、狗编号字号。 */
const BOT_R = 7;
const PIN_R = 14;
const LABEL_PX = 12;

/**
 * 地图页上的区域:站点在预览里给的 areas(有名字的区域)在前;防区接口(老站点只有它)里同名的不重画。
 */
export function mapAreas(meta: PreviewMeta, sec: { zone: string; points: Pt[] }[]): CanvasArea[] {
  const named = namedAreas(meta).map((a): CanvasArea => ({ name: a.name, pts: a.pts, kind: "area" }));
  const seen = new Set(named.map((a) => a.name));
  for (const s of sec) {
    if (!s.zone || seen.has(s.zone) || !Array.isArray(s.points) || s.points.length < 3) continue;
    seen.add(s.zone);
    named.push({ name: s.zone, pts: s.points, kind: "area" });
  }
  return named;
}

/** 地图本体(W44:值班页下半屏、地图页共用):底图、区域、机器人、报警圈、运行图层、点图「去这里 / 在这里设位置」。 */
const NO_AREAS: { zone: string; points: Pt[] }[] = [];

export function SiteMap(props: {
  units: UnitInfo[];
  /** 图上画报警圈的告警(带位置的)。 */
  p1: Alert[];
  now: number;
  selected: string | null;
  onSelect: (id: string) => void;
  role?: string;
  /** 标红的防区(报警的那几个,W44 值班页)。 */
  hot?: Set<string>;
  /** 画不画「近 7 天定位出过问题的地方」(保安页不画,设计 §4.3)。 */
  episodes?: boolean;
  /** 能不能点图「去这里 / 在这里设位置」(默认能;值班页只在「帮它找位置」时能)。 */
  pick?: boolean;
  /** 浮在图左上角的一句提示(值班页「帮它找位置」时说怎么做)。 */
  hint?: string | null;
  /** 这张图上有名字的区域(值班页拿来算每台机器人在哪个防区)。 */
  onAreas?: (areas: CanvasArea[]) => void;
  /** 点图交给页面(地图页:让最近的机器人过去看看、临时路线);给了就不进「去这里 / 设位置」。坐标是地图米。 */
  onPickMap?: (x: number, y: number, map: { map_id: string; version: string }) => void;
  /** 画不画禁行区、限速区、虚拟墙(地图页的禁行区图层,默认关)。默认画。 */
  navZones?: boolean;
  /** 画不画规划路径、轨迹(地图页的巡逻路线图层)。默认画。 */
  route?: boolean;
  /** 防区标签、颜色(地图页的防区和布防图层)。 */
  areaDecor?: (name: string) => { label: string; tone: "armed" | "off" } | null;
  /** 世界层(预览像素)、标记层(屏上)里页面自己多画的。 */
  extraWorld?: (meta: PreviewMeta, map: { map_id: string; version: string }) => ComponentChildren;
  extraMarks?: (s: Screen, meta: PreviewMeta, map: { map_id: string; version: string }) => ComponentChildren;
  /** 在巡逻的机器人下面写「下一个点约 N 分钟」(地图页的巡逻路线图层)。 */
  eta?: boolean;
  /** 现在画的是哪张图(页面按它去拉轨迹覆盖、待命点)。 */
  onMapShown?: (map: { map_id: string; version: string } | null, meta: PreviewMeta | null) => void;
}) {
  const sel = props.units.find((u) => u.robot.robot_id === props.selected) ?? props.units[0];
  // 底图跟着选中的狗(外审 W39-I2:原来总是第一只有图的狗,选中的狗可能在另一张图上);选中的狗没报图
  // 才退回第一只有图的
  const selMap = sel ? robotMap(sel.robot) : null;
  const which = selMap ?? props.units.map((u) => robotMap(u.robot)).find((m) => m) ?? null;
  const key = which ? `${which.map_id}/${which.version}` : "";
  // 预览、区域记着是哪张图的:换图以后才回来的旧回包不认
  const [got, setGot] = useState<{ key: string; meta: PreviewMeta | null; areas: { zone: string; points: Pt[] }[] }>({ key: "", meta: null, areas: [] });
  useEffect(() => {
    if (!which) return;
    let live = true;
    const base = `/api/maps/${enc(which.map_id)}/${enc(which.version)}`;
    api<PreviewMeta>("GET", `${base}/preview`).then((m) => live && setGot((g) => ({ key, meta: m, areas: g.key === key ? g.areas : [] })))
      .catch(() => live && setGot((g) => ({ key, meta: null, areas: g.key === key ? g.areas : [] })));
    api<{ areas: { zone: string; points: Pt[] }[] }>("GET", `${base}/areas`).then((d) => live && setGot((g) => ({ key, meta: g.key === key ? g.meta : null, areas: d.areas })))
      .catch(() => {});
    return () => { live = false; };
  }, [key]);
  const meta = got.key === key ? got.meta : null;
  // 没载入时也用同一个空列表:每次给新的 [] 会让回报区域的效果每次渲染都触发,页面跟着重画,停不下来
  const areas = got.key === key ? got.areas : NO_AREAS;
  const onAreas = props.onAreas;
  useEffect(() => {
    if (onAreas) onAreas(meta ? mapAreas(meta, areas) : []);
  }, [meta, areas]);
  const onMapShown = props.onMapShown;
  useEffect(() => {
    if (onMapShown) onMapShown(which && meta ? { map_id: which.map_id, version: which.version } : null, meta);
  }, [key, meta]);
  // 只画加载着这张图的狗(别的图上的坐标画在这张图上是错的)
  const onThisMap = (u: UnitInfo) => { const m = robotMap(u.robot); return !!(m && which && m.map_id === which.map_id && m.version === which.version); };
  // 点图 → 去这里 / 在这里设位置(posepick.tsx;只有管理员、保安)
  const pose = usePosePick({ meta, map: which ? { map_id: which.map_id, version: which.version } : null,
    robotId: sel?.robot.robot_id ?? null, robotMap: selMap,
    robotAt: sel && onThisMap(sel) ? sel.robot.loc?.pose ?? null : null, role: props.role ?? "",
    canFind: canFindPose(sel?.robot) });
  // 运行可视化:规划路径、轨迹、被挡位置(2 s 一拉),近 7 天定位出过问题的地方
  const viz = useRunViz(!!which);
  const vizRobots = which ? vizOnMap(viz, which.map_id, which.version) : [];
  const since = useMemo(() => Date.now() - 7 * 86_400_000, [key]);
  const { rows: episodes } = useEpisodes({ map_id: which?.map_id, map_version: which?.version, since_ms: since, limit: 300 }, !!which && props.episodes !== false);
  const eps = props.episodes === false ? [] : episodes ?? [];
  const etaOf = (u: UnitInfo): number | null => {
    if (u.state !== "patrolling") return null;
    const v = vizRobots.find(([id]) => id === u.robot.robot_id)?.[1];
    const p = u.robot.loc?.pose;
    return etaNext(v?.path, p ? [p.x, p.y] : null);
  };

  // 图上真画得出来的报警点、狗(图例只列有的)
  const pins = !which || !meta ? [] : props.p1.flatMap((a) => {
    const p = a.context?.pose;
    if (!p || p.map_id !== which.map_id || p.map_version !== which.version) return [];
    const { px, py } = toPx(meta, p.x, p.y);
    return onMap(meta, px, py) ? [{ a, px, py }] : [];
  });
  const bots = !meta ? [] : props.units.flatMap((u) => {
    const pose = u.robot.loc?.pose;
    if (!pose || !onThisMap(u)) return [];
    const { px, py } = toPx(meta, pose.x, pose.y);
    return onMap(meta, px, py, 20) ? [{ u, px, py, yaw: pose.yaw }] : [];
  });
  const legend: LegendKey[] = [...(bots.length ? ["robot" as const] : []), ...(pins.length ? ["alarm" as const] : [])];

  return (
    <div class="mapbox" {...pose.wrapProps}>
        {!which || !meta ? (
          <p class="empty">{t("noMap")}</p>
        ) : (
          <MapCanvas meta={meta} raster={`/api/maps/${enc(which.map_id)}/${enc(which.version)}/preview.png`} label={t("mapAria")}
            areas={mapAreas(meta, areas).map((a) => {
              const d = props.areaDecor?.(a.name);
              return { ...a, ...(d ? { label: d.label, tone: d.tone } : {}), ...(props.hot?.has(a.name) ? { hot: true } : {}) };
            })}
            navZones={props.navZones} legend={legend} drawing={!!(props.onPickMap ?? (props.pick === false ? undefined : pose.onPick))}
            onPick={props.onPickMap ? (px, py) => { const [x, y] = pxToMap(meta, px, py); props.onPickMap!(x, y, which); }
              : props.pick === false ? undefined : pose.onPick}
            world={<>{props.route !== false && <VizWorld meta={meta} robots={vizRobots} />}{props.extraWorld?.(meta, which)}</>}
            // 「去这里」浮在图的右上角(不在图下面另占一块);运行图层的图例并进底图图例那一行
            overlay={<>{props.hint && <div class="map-hint" role="status">{props.hint}</div>}{!props.onPickMap && props.pick !== false && pose.panel}</>}
            legendExtra={<RunLegendItems has={vizHas(vizRobots, eps)} />}
            // 区域名让开报警圈、狗(圆点 + 下面的编号)
            avoid={(s) => [
              ...vizAvoid(s, meta, vizRobots),
              ...pins.map(({ px, py }) => { const [x, y] = s.px(px, py); return { x, y, r: PIN_R + 2 }; }),
              ...bots.map(({ px, py }) => { const [x, y] = s.px(px, py); return { x, y: y + LABEL_PX / 2, r: BOT_R + LABEL_PX + 4 }; }),
            ]}
            marks={(s) => (
              <>
                {!props.onPickMap && props.pick !== false && pose.marks(s)}
                {props.route !== false && vizMarks(s, meta, vizRobots, eps)}
                {props.extraMarks?.(s, meta, which)}
                {pins.map(({ a, px, py }) => {
                  const [x, y] = s.px(px, py);
                  const h = PIN_R / 2.5;
                  return (
                    <g key={a.key} class={alarmState(a, props.now) === "unacked" ? "alarm-pin flash" : "alarm-pin"}>
                      <circle cx={x} cy={y} r={PIN_R} class="ring" />
                      <path d={`M${x} ${y - h}l${h} ${h}-${h} ${h}-${h}-${h}z`} class="diamond" />
                    </g>
                  );
                })}
                {bots.map(({ u, px, py, yaw }) => {
                  const [x, y] = s.px(px, py);
                  // 朝向:在预览上往 yaw 走一小段,跟着图一起变换,再在屏上取定长
                  const e = heading(px, py, yaw, 10);
                  const [ex, ey] = s.px(e.hx, e.hy);
                  const n = Math.hypot(ex - x, ey - y) || 1;
                  const len = BOT_R * 2.2;
                  const a = abnormal(u.state);
                  const on = u.robot.robot_id === sel?.robot.robot_id;
                  return (
                    <g key={u.robot.robot_id} class={`bot ${a ?? "ok"}${on ? " on" : ""}`} onClick={(e) => { e.stopPropagation(); props.onSelect(u.robot.robot_id); }}>
                      <line x1={x} y1={y} x2={x + ((ex - x) / n) * len} y2={y + ((ey - y) / n) * len} class="bot-head" />
                      <circle cx={x} cy={y} r={BOT_R} class="bot-dot" />
                      <text x={x} y={y + BOT_R + LABEL_PX + 2} class="bot-label" font-size={LABEL_PX}>{u.robot.robot_id}</text>
                      {props.eta && etaOf(u) != null && (
                        <text x={x} y={y + BOT_R + LABEL_PX * 2 + 4} class="bot-label eta" font-size={LABEL_PX - 1}>
                          {t("etaNextMin", { n: Math.max(1, Math.round(etaOf(u)! / 60)) })}
                        </text>
                      )}
                    </g>
                  );
                })}
              </>
            )} />
        )}
      </div>
  );
}

export function MapPage(props: {
  units: UnitInfo[];
  p1: Alert[];
  now: number;
  selected: string | null;
  onSelect: (id: string) => void;
  onVideo: (id: string) => void;
  role?: string;
  mode: ModeView | null;
  /** 保安的「全部布防」(设计 §11 第 14 条):模式不是布防时才有。 */
  onArm: () => void;
  say?: (s: string) => void;
}) {
  const say = props.say ?? (() => {});
  const duty = useDutySettings();
  const [layers, setLayers] = useState<Set<Layer>>(loadLayers);
  const [shown, setShown] = useState<{ map: { map_id: string; version: string } | null; meta: PreviewMeta | null }>({ map: null, meta: null });
  const [look, setLook] = useState<{ at: Pt; map: { map_id: string; version: string } } | null>(null);
  const [route, setRoute] = useState<Pt[] | null>(null);
  const [cells, setCells] = useState<{ cell_m: number; cells: Cell[] } | null>(null);
  const [authz, setAuthz] = useState<AuthzRow[]>([]);
  const [recent, setRecent] = useState<Alert[]>([]);
  const [standby, setStandby] = useState<{ name: string; x: number; y: number }[]>([]);
  const on = (l: Layer) => layers.has(l);
  const flip = (l: Layer) => {
    const n = new Set(layers);
    if (n.has(l)) n.delete(l);
    else n.add(l);
    saveLayers(n);
    setLayers(n);
  };
  const mapKey = shown.map ? `${shown.map.map_id}/${shown.map.version}` : "";

  // 本班出的事(含已处理的):30 秒一拉
  useEffect(() => {
    let live = true;
    const pull = () => api<{ alerts: Alert[] }>("GET", "/api/alerts?all=1&limit=200").then((d) => live && setRecent(d.alerts ?? [])).catch(() => {});
    void pull();
    const id = window.setInterval(pull, 30_000);
    return () => { live = false; window.clearInterval(id); };
  }, []);
  useEffect(() => {
    let live = true;
    api<{ authorizations: AuthzRow[] }>("GET", "/api/lists/authorizations").then((d) => live && setAuthz(d.authorizations ?? [])).catch(() => {});
    return () => { live = false; };
  }, []);
  // 多久没巡过:最近 24 小时每格最后一次有机器人到过的时刻,一分钟一拉
  useEffect(() => {
    if (!shown.map || !on("stale")) return;
    let live = true;
    const m = shown.map;
    const pull = () => api<{ cell_m: number; cells: Cell[] }>("GET",
      `/api/coverage?map_id=${enc(m.map_id)}&map_version=${enc(m.version)}&since=${Date.now() - 86_400_000}`)
      .then((d) => live && setCells(d)).catch(() => live && setCells(null));
    void pull();
    const id = window.setInterval(pull, 60_000);
    return () => { live = false; window.clearInterval(id); };
  }, [mapKey, on("stale")]);
  // 待命点:这张图上各台机器人的
  useEffect(() => {
    if (!shown.map) return;
    let live = true;
    const m = shown.map;
    Promise.all(props.units.map((u) => api<{ points: { name: string; x: number; y: number; map_id: string; map_version: string }[] }>(
      "GET", `/api/robots/${enc(u.robot.robot_id)}/standby`).then((d) => d.points ?? []).catch(() => [])))
      .then((all) => live && setStandby(all.flat().filter((p) => p.map_id === m.map_id && p.map_version === m.version)));
    return () => { live = false; };
  }, [mapKey, props.units.length]);

  const since = shiftStart(props.now, duty.shift_hours);
  const events = tonightEvents(recent, since);
  const stale = cells ? staleCells(cells.cells, props.now, staleAfter(duty.stale_hours)) : [];
  const onThisMap = (u: UnitInfo) => { const m = robotMap(u.robot); return !!(m && shown.map && m.map_id === shown.map.map_id && m.version === shown.map.version); };
  const robotsHere = props.units.filter((u) => u.state !== "offline" && onThisMap(u)).map((u) => u.robot.robot_id);
  const toRec = (a: Alert) => { location.hash = `#/recordings?robot=${enc(a.robot)}&t=${a.first_ms}`; };
  const names: Record<Layer, Key> = { zones: "layerZones", cameras: "layerCameras", route: "layerRoute", standby: "layerStandby",
    events: "layerEvents", stale: "layerStale", nogo: "layerNoGo" };

  return (
    <div class="mappage">
      <SiteMap units={props.units} p1={props.p1} now={props.now} selected={props.selected} onSelect={props.onSelect} role={props.role}
        episodes={false} navZones={on("nogo")} route={on("route")} eta={on("route")}
        areaDecor={on("zones") ? (n) => zoneDecor(n, props.mode, authz) : (n) => ({ label: n, tone: "armed" })}
        onMapShown={(map, meta) => setShown({ map, meta })}
        onPickMap={canDispatch(props.role ?? "") ? (x, y, map) => {
          if (route) setRoute([...route, [x, y]]);
          else setLook({ at: [x, y], map });
        } : undefined}
        hint={route ? t("trHint", { n: route.length }) : null}
        extraWorld={(meta) => (
          <>
            <defs>
              <pattern id="stale-hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
                <rect width="8" height="8" class="stale-bg" /><line x1="0" y1="0" x2="0" y2="8" class="stale-line" />
              </pattern>
            </defs>
            {on("stale") && cells && stale.map((c) => {
              const tl = toPx(meta, c.i * cells.cell_m, (c.j + 1) * cells.cell_m);
              const w = cells.cell_m / meta.m_per_px;
              return <rect key={`${c.i}:${c.j}`} x={tl.px} y={tl.py} width={w} height={w} class="stale-cell" />;
            })}
            {route && route.length > 1 && (
              <polyline class="temp-route" points={route.map(([x, y]) => { const p = toPx(meta, x, y); return `${p.px},${p.py}`; }).join(" ")} />
            )}
          </>
        )}
        extraMarks={(s) => (
          <>
            {on("standby") && standby.map((p) => {
              const [x, y] = s.map(p.x, p.y);
              return <g key={`sb:${p.name}`} class="standby-mark"><rect x={x - 3} y={y - 3} width="6" height="6" /><text x={x + 6} y={y + 4}>{p.name}</text></g>;
            })}
            {on("events") && shown.map && events.flatMap((a) => {
              const p = a.context?.pose;
              if (!p || p.map_id !== shown.map!.map_id || p.map_version !== shown.map!.version) return [];
              const [x, y] = s.map(p.x, p.y);
              return [(
                <g key={`ev:${a.key}`} class={`event-pin ${a.audience ?? ""}`} onClick={(e) => { e.stopPropagation(); toRec(a); }}>
                  <line x1={x} y1={y} x2={x} y2={y - 10} />
                  <text x={x} y={y - 14}>{`${hm(a.first_ms)} ${alertTitle(lang.value, a.kind, a.title).slice(0, 12)}`}</text>
                </g>
              )];
            })}
            {route && route.map(([mx, my], i) => {
              const [x, y] = s.map(mx, my);
              return <g key={`tr:${i}`} class="temp-pt"><circle cx={x} cy={y} r="9" /><text x={x} y={y + 4}>{i + 1}</text></g>;
            })}
          </>
        )} />
      <aside class="sidebar maprp" aria-label={t("tabMap")}>
        <h3>{t("mapShow")}</h3>
        <div class="layers">
          {LAYERS.filter((l) => l !== "cameras").map((l) => (
            <label key={l}><input type="checkbox" checked={on(l)} onChange={() => flip(l)} />{t(names[l])}</label>
          ))}
        </div>
        {props.mode && props.mode.mode !== "armed" && canArm(props.role ?? "") && (
          <button type="button" class="btn primary arm-all" onClick={props.onArm}>{t("armAll")}</button>
        )}
        <h3>{t("tonightN", { n: events.length })}</h3>
        <ul class="evlist">
          {events.map((a) => (
            <li key={a.key}>
              <button type="button" class="evrow" onClick={() => toRec(a)}>
                <time class="mono">{hm(a.first_ms)}</time>
                <span class={`ev-what ${a.audience ?? ""}`}>{alertTitle(lang.value, a.kind, a.title)}</span>
                <small class="muted">{a.resolved_ms == null ? t("evOpen") : verdictText(a)} · {t("evToRec")}</small>
              </button>
            </li>
          ))}
        </ul>
        {route ? (
          shown.map && <TempRoutePanel map={shown.map} points={route} robots={robotsHere} say={say}
            onUndo={() => setRoute(route.slice(0, -1))} onCancel={() => setRoute(null)} onSent={() => setRoute(null)} />
        ) : canDispatch(props.role ?? "") && (
          <div class="tip">
            <p>{t("mapTip")}</p>
            <button type="button" class="btn sm" onClick={() => { setLook(null); setRoute([]); }}>{t("trStart")}</button>
          </div>
        )}
      </aside>
      {look && <GoLookCard map={look.map} at={look.at} units={props.units} say={say} onClose={() => setLook(null)} />}
    </div>
  );
}

/** 结论说成人话(报警记录页也用)。 */
export function verdictText(a: Alert): string {
  return a.verdict === "intruder" ? t("vIntruder") : a.verdict === "false_alarm" ? t("vFalse") : a.verdict === "handled" ? t("vHandled") : t("vDone");
}

/** 点图以后的小卡片:问站点最近的机器人是谁、大约几分钟,确认了才派(设计 §4.3)。 */
export function GoLookCard(props: { map: { map_id: string; version: string }; at: Pt; units: UnitInfo[]; onClose: () => void; say: (s: string) => void }) {
  const [got, setGot] = useState<{ robot_id: string; eta_s: number } | null | "none" | "error">(null);
  const [why, setWhy] = useState("");
  const [busy, setBusy] = useState(false);
  const [x, y] = props.at;
  useEffect(() => {
    let live = true;
    api<{ robot_id: string; eta_s: number }>("GET",
      `/api/robots/nearest?map_id=${enc(props.map.map_id)}&map_version=${enc(props.map.version)}&x=${x}&y=${y}`)
      .then((d) => live && setGot(d))
      .catch((e) => {
        if (!live) return;
        if (e instanceof ApiError && e.status === 404) setGot("none");
        else { setGot("error"); setWhy(e instanceof ApiError ? e.message : String(e)); }
      });
    return () => { live = false; };
  }, [x, y, props.map.map_id, props.map.version]);
  async function send(id: string) {
    setBusy(true);
    const p = props.units.find((u) => u.robot.robot_id === id)?.robot.loc?.pose;
    const yaw = (p ? yawToward(p.x, p.y, x, y) : null) ?? 0;
    try {
      const r = await api<CommandReply>("POST", `/api/robots/${enc(id)}/goto`,
        { target: { map_id: props.map.map_id, map_version: props.map.version, frame_id: "map", x, y, yaw } });
      const no = notAccepted(r);
      props.say(no ?? t("lookSent", { id }));
      props.onClose();
    } catch (e) {
      setWhy(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div class="golook" role="dialog" aria-label={t("lookTitle")}>
      {got === null && <p class="muted">{t("lookAsking")}</p>}
      {got === "none" && <p>{t("lookNone")}</p>}
      {got === "error" && <p class="error">{why}</p>}
      {got && typeof got === "object" && (
        <p>{t("lookWho", { id: got.robot_id, n: Math.max(1, Math.round(got.eta_s / 60)) })}</p>
      )}
      {why && got !== "error" && <p class="error">{why}</p>}
      <div class="row end">
        <button type="button" class="btn" onClick={props.onClose}>{t("cancel")}</button>
        {got && typeof got === "object" && (
          <button type="button" class="btn primary" disabled={busy} onClick={() => send(got.robot_id)}>{t("lookSend")}</button>
        )}
      </div>
    </div>
  );
}

/** 保安的临时路线(设计 §4.3,用户选的):按顺序点几个点、选一台、开始;走一趟就没了,不存。 */
export function TempRoutePanel(props: {
  map: { map_id: string; version: string };
  points: Pt[];
  robots: string[];
  onUndo: () => void;
  onCancel: () => void;
  onSent: () => void;
  say: (s: string) => void;
}) {
  const [robot, setRobot] = useState<string | null>(props.robots[0] ?? null);
  const [errs, setErrs] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  async function go() {
    if (!robot) return;
    setBusy(true);
    setErrs([]);
    try {
      const r = await api<CommandReply>("POST", `/api/robots/${enc(robot)}/patrol`,
        { points: props.points.map(([x, y]) => ({ x, y })), map_id: props.map.map_id, map_version: props.map.version });
      const no = notAccepted(r);
      if (no) setErrs([no]);
      else { props.say(t("trSent", { id: robot })); props.onSent(); }
    } catch (e) {
      const list = e instanceof ApiError ? (e.data as { errors?: { index: number | null; problem: string }[] } | undefined)?.errors : undefined;
      setErrs(list && list.length
        ? list.map((x) => (x.index == null ? x.problem : t("trBadPoint", { n: x.index + 1, why: x.problem })))
        : [e instanceof ApiError ? e.message : String(e)]);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div class="temproute">
      <h3>{t("trTitle")}</h3>
      <p class="muted small">{t("trHint", { n: props.points.length })}</p>
      <div class="seg" role="group" aria-label={t("robots")}>
        {props.robots.map((id) => (
          <button key={id} type="button" aria-pressed={robot === id} onClick={() => setRobot(id)}>{id}</button>
        ))}
      </div>
      {errs.map((e) => <p key={e} class="error">{e}</p>)}
      <div class="row">
        <button type="button" class="btn sm" disabled={props.points.length === 0} onClick={props.onUndo}>{t("trUndo")}</button>
        <button type="button" class="btn sm" onClick={props.onCancel}>{t("cancel")}</button>
        <button type="button" class="btn sm primary push" disabled={busy || !robot || props.points.length === 0} onClick={go}>{t("trGo")}</button>
      </div>
    </div>
  );
}
