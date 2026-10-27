// 巡检路线页(2026-10-14,管理员):选地图版本 → 图上看这一版上的路线(折线 + 带号的点)→ 点图加点、
// 拖点改位置、拖圆形把手转朝向、调顺序、删点、设点上的动作 → 存(站点逐点校验,错的点标红)。
// 每条路线下面是它的排程。站点存的路线、排程是唯一真理源;存的时候带 base_revision,别人改过的不会被盖掉。
import type { FunctionComponent } from "preact";
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { Confirm } from "./dialogs";
import { startDrag, clickGuard, swallowClick } from "./dragpt";
import { t } from "./i18n";
import type { OwnLayer } from "./layoutpage";
import { MapCanvas } from "./mapcanvas";
import { PoseMarker } from "./posemarker";
import { toPx, type PreviewMeta } from "./mapview";
import "./routes.css";
import {
  addPoint,
  clientToMap,
  degOf,
  deletePoint,
  dwellOf,
  handleAt,
  hasAction,
  movePoint,
  patchPoint,
  radOf,
  reorder,
  setDwell,
  toggleAction,
  yawToward,
  type Draft,
  type PointProblem,
  type Route,
  type RoutePoint,
  type RouteSchedule,
  type RouteView,
} from "./routes";
import { rt } from "./routes_i18n";

type Say = (s: string) => void;
interface MapRow { map_id: string; version: string; note: string }

export const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
const PT_R = 11;
const HANDLE_PX = 30;

function errText(e: unknown): string {
  return t("failed", { why: e instanceof ApiError ? e.message : String(e) });
}

const same = (a: Draft | null, r: Route | undefined): boolean =>
  !!a && !!r && !a.isNew && JSON.stringify(a.points) === JSON.stringify(r.waypoints);

/** 嵌进「布置」页(W44 第三期):图、地图版本、保存按钮都用外框的;不显示坐标数字(设计 §1:改路线不用看到坐标)。 */
export interface RouteEmbed {
  map: MapRow;
  meta: PreviewMeta;
  Map: FunctionComponent<OwnLayer>;
  setDirty: (b: boolean) => void;
  registerSave: (fn: (() => Promise<void>) | null) => void;
}

export function RoutesPage({ say, embed }: { say: Say; embed?: RouteEmbed }) {
  const [maps, setMaps] = useState<MapRow[]>(embed ? [embed.map] : []);
  const [selState, setSel] = useState<MapRow | null>(embed?.map ?? null);
  const sel = embed ? embed.map : selState;
  const [metaState, setMeta] = useState<PreviewMeta | null>(embed?.meta ?? null);
  const meta = embed ? embed.meta : metaState;
  const [view, setView] = useState<RouteView | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [orig, setOrig] = useState<Route | undefined>(undefined);
  const [pi, setPi] = useState<number | null>(null);
  const [problems, setProblems] = useState<PointProblem[]>([]);
  const [conflict, setConflict] = useState(false);
  const [removing, setRemoving] = useState(false);
  // 2026-10-15 用户:所有路线一起画在图上太乱 —— 图上只画列表里选中的那一条;要对照别的路线再打开
  const [showOthers, setShowOthers] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const guard = useRef(clickGuard());
  // 外审 R2(2026-10-15 下午):读、存、删、重新拉的回包都可能晚到 —— 出发时记下「哪张图、第几次编辑」,
  // 回来时对不上就不碰页面(服务端已经存下的不撤,只是不把它放进现在这一页)。换图、进编辑、新建、回列表、
  // 删掉都算新的一次编辑。
  const mapKey = sel ? `${sel.map_id}\u0000${sel.version}` : "";
  const live = useRef({ map: mapKey, gen: 0 });
  if (live.current.map !== mapKey) live.current = { map: mapKey, gen: live.current.gen + 1 };
  const ticket = () => ({ ...live.current });
  const still = (tk: { map: string; gen: number }) => live.current.map === tk.map && live.current.gen === tk.gen;
  const sameMap = (tk: { map: string }) => live.current.map === tk.map;
  const nextEdit = () => { live.current = { ...live.current, gen: live.current.gen + 1 }; };

  useEffect(() => {
    if (embed) return;
    api<{ maps: MapRow[] }>("GET", "/api/maps").then((d) => {
      setMaps(d.maps);
      setSel((s) => s ?? d.maps[d.maps.length - 1] ?? null);
    }).catch((e) => say(errText(e)));
  }, []);
  const base = sel ? `/api/maps/${enc(sel.map_id)}/${enc(sel.version)}` : "";
  const load = useCallback(() => {
    if (!sel) return Promise.resolve();
    const tk = ticket();
    return api<RouteView>("GET", `/api/routes?map_id=${enc(sel.map_id)}&map_version=${enc(sel.version)}`)
      .then((v) => { if (sameMap(tk)) setView(v); })
      .catch((e) => { if (sameMap(tk)) { setView(null); say(errText(e)); } });
  }, [sel?.map_id, sel?.version]);
  useEffect(() => {
    setMeta(null);
    setDraft(null);
    setOrig(undefined);
    setPi(null);
    setProblems([]);
    setConflict(false);
    setView(null);
    if (!base) return;
    const tk = ticket();
    if (!embed) {
      api<PreviewMeta>("GET", `${base}/preview`).then((m) => { if (sameMap(tk)) setMeta(m); })
        .catch(() => { if (sameMap(tk)) setMeta(null); });
    }
    void load();
  }, [base]);

  const routes = view?.routes ?? [];
  const dirty = !!draft && (draft.isNew ? draft.points.length > 0 : !same(draft, orig));
  const bad = new Map<number, string>();
  for (const p of problems) if (p.index !== null) bad.set(p.index, p.problem);
  const wholeProblems = problems.filter((p) => p.index === null);
  // W44 第三期:两点之间走不通(站点逐段查,``segment: [i, i+1]``)
  const badSegs = problems.filter((p) => Array.isArray(p.segment)).map((p) => p.segment as [number, number]);

  function edit(r: Route) {
    nextEdit();
    setDraft({ id: r.id, isNew: false, base: r.revision, points: r.waypoints, policy: r.policy, map: mapKey });
    setOrig(r);
    setPi(null);
    setProblems([]);
    setConflict(false);
  }
  function startNew() {
    // 新建:右侧栏换成编辑页,名字在编辑页顶上填(2026-10-15 用户:原来名字框夹在路线列表和点位列表中间)
    nextEdit();
    setDraft({ id: "", isNew: true, base: 0, points: [], policy: {}, map: mapKey });
    setOrig(undefined);
    setPi(null);
    setProblems([]);
    setConflict(false);
  }
  function change(points: RoutePoint[], keepProblems = false) {
    setDraft((d) => (d ? { ...d, points } : d));
    if (!keepProblems) setProblems([]);
  }

  function pick(px: number, py: number) {
    if (!meta || !draft) return;
    const [x, y] = [Math.round((meta.left_x + px * meta.m_per_px) * 100) / 100, Math.round((meta.top_y - py * meta.m_per_px) * 100) / 100];
    change(addPoint(draft.points, x, y));
    setPi(draft.points.length);
  }

  /** ``strict``:失败了抛出去(嵌进布置页时外框按 409 说「刚才有人改过」)。 */
  async function save(strict = false) {
    if (!draft || !sel) return;
    if (draft.map !== mapKey) {
      // 草稿不是在现在选的这张图上编的(外审 R2):不发,不然点会改绑到别的图上
      say(rt("routeWrongMap"));
      return;
    }
    const tk = ticket();
    setProblems([]);
    setConflict(false);
    try {
      const r = await api<Route>("POST", `/api/routes/${enc(draft.id)}`, {
        map_id: sel.map_id, map_version: sel.version, waypoints: draft.points, policy: draft.policy, base_revision: draft.base,
      });
      if (!still(tk)) return;                 // 已经离开这条路线 / 这张图:存下了,但不放进现在这一页
      say(rt("routeSaved", { rev: r.revision }));
      setDraft({ id: r.id, isNew: false, base: r.revision, points: r.waypoints, policy: r.policy, map: tk.map });
      setOrig(r);
      await load();
    } catch (e) {
      if (!still(tk)) return;
      if (e instanceof ApiError && e.status === 409) setConflict(true);
      const errs = e instanceof ApiError ? (e.data as { errors?: PointProblem[] } | undefined)?.errors : undefined;
      if (Array.isArray(errs) && errs.length) {
        setProblems(errs);
        const first = errs.find((x) => x.index !== null);
        if (first) setPi(first.index);
      }
      say(errText(e));
      if (strict) throw e;
    }
  }
  async function reloadRoute() {
    if (!draft) return;
    const tk = ticket();
    try {
      const r = await api<Route>("GET", `/api/routes/${enc(draft.id)}`);
      if (!still(tk)) return;
      if (sel && (r.map_id !== sel.map_id || r.map_version !== sel.version)) {
        // 别人把这条路线改到了别的图上:不放进这张图的编辑页
        say(rt("routeMovedMap", { map: `${r.map_id} · v${r.map_version}` }));
        setDraft(null);
        await load();
        return;
      }
      edit(r);
      await load();
    } catch (e) {
      if (!still(tk)) return;
      say(errText(e));
      setDraft(null);
      await load();
    }
  }
  async function del() {
    if (!draft) return;
    setRemoving(false);
    const tk = ticket();
    try {
      await api("POST", `/api/routes/${enc(draft.id)}/delete`, { base_revision: draft.base });
      if (!still(tk)) return;
      say(rt("routeDeleted"));
      nextEdit();
      setDraft(null);
      setOrig(undefined);
      await load();
    } catch (e) {
      if (!still(tk)) return;
      say(errText(e));
    }
  }

  // ---- 图上
  const pts = draft?.points ?? [];
  const others = showOthers ? routes.filter((r) => r.id !== draft?.id) : [];
  const schedCount = (id: string) => (view?.schedules ?? []).filter((s) => s.mission === id).length;
  const line = (ps: { x: number; y: number }[]) => ps.map((p) => { const q = toPx(meta!, p.x, p.y); return `${q.px},${q.py}`; }).join(" ");

  function dragPoint(i: number) {
    return (e: PointerEvent) => {
      setPi(i);
      startDrag(e, guard.current, (cx, cy) => {
        const m = meta && clientToMap(root.current, meta, cx, cy);
        if (m) setDraft((d) => (d ? { ...d, points: movePoint(d.points, i, m[0], m[1]) } : d));
      });
    };
  }
  function dragHandle(i: number) {
    return (e: PointerEvent) => {
      startDrag(e, guard.current, (cx, cy) => {
        const m = meta && clientToMap(root.current, meta, cx, cy);
        if (!m) return;
        setDraft((d) => {
          if (!d) return d;
          const p = d.points[i];
          const yaw = yawToward(p.x, p.y, m[0], m[1]);
          return yaw === null ? d : { ...d, points: patchPoint(d.points, i, { yaw }) };
        });
      });
    };
  }

  const nameTaken = !!draft?.isNew && routes.some((r) => r.id === draft.id);
  const canSave = !!draft && dirty && pts.length > 0 && NAME_RE.test(draft.id) && !nameTaken;
  useEffect(() => { embed?.setDirty(dirty); }, [dirty]);
  useEffect(() => {
    if (!embed) return;
    embed.registerSave(canSave ? () => save(true) : null);
    return () => embed.registerSave(null);
  });
  function back() {
    if (dirty && !window.confirm(rt("routeUnsaved"))) return;
    nextEdit();
    setDraft(null);
    setOrig(undefined);
    setPi(null);
    setProblems([]);
    setConflict(false);
  }

  const selPoint = draft && pi !== null ? draft.points[pi] : undefined;
  const mySchedules = (view?.schedules ?? []).filter((s) => draft && !draft.isNew && s.mission === draft.id);

  const MapLike = (embed ? embed.Map : MapCanvas) as FunctionComponent<Parameters<typeof MapCanvas>[0]>;
  return (
    <div class={embed ? "routes-embed" : "admin"}>
      <section class={embed ? undefined : "panel"}>
        <div class="panel-head" hidden={!!embed}>
          <h1>{rt("routesTitle")}</h1>
          <label class="field-inline">{rt("routesMap")}
            <select value={sel ? `${sel.map_id}\u0000${sel.version}` : ""} onChange={(e) => {
              if (dirty && !window.confirm(rt("routeUnsaved"))) return;
              const [m, v] = e.currentTarget.value.split("\u0000");
              setSel(maps.find((x) => x.map_id === m && x.version === v) ?? null);
            }}>
              {maps.map((m) => <option key={`${m.map_id}:${m.version}`} value={`${m.map_id}\u0000${m.version}`}>{m.map_id} · v{m.version}{m.note ? ` · ${m.note}` : ""}</option>)}
            </select>
          </label>
          <span class="push" />
          {dirty && <span class="muted small" role="status">{rt("routeUnsaved")}</span>}
        </div>
        <div class="routesplit">
          <div class="routemap" ref={root} onClickCapture={(e) => swallowClick(guard.current, e)}>
            {!sel || !meta ? <p class="empty">{t("noMap")}</p> : (
              <MapLike meta={meta} raster={`${base}/preview.png`} label={rt("routesTitle")}
                drawing={!!draft} onPick={draft ? pick : undefined}
                overlay={!draft && routes.length > 0 ? <p class="routepick-hint small" data-route-hint="1">{rt("routePickHint")}</p> : undefined}
                world={<>
                  {others.map((r) => <polyline key={r.id} points={line(r.waypoints)} class="route-line other" data-route={r.id} />)}
                  {draft && pts.length > 1 && <polyline points={line(pts)} class="route-line" data-route={draft.id} />}
                  {badSegs.map(([a, b]) => pts[a] && pts[b] && <polyline key={`seg${a}`} points={line([pts[a], pts[b]])} class="route-line bad-seg" data-bad-seg={a} />)}
                </>}
                marks={(s) => (
                  <>
                    {/* 路线的点:定位箭头(posemarker.tsx),中间是序号,三角尖指着到点后的朝向;选中的那一个三角尖能拖 */}
                    {others.map((r) => r.waypoints.map((p, i) => {
                      const h = handleAt(meta, s, p.x, p.y, p.yaw, HANDLE_PX);
                      return <PoseMarker key={`${r.id}:${i}`} x={h.x} y={h.y} hx={h.hx} hy={h.hy} r={8} label={i + 1}
                        class="route-pt other" />;
                    }))}
                    {pts.map((p, i) => {
                      const h = handleAt(meta, s, p.x, p.y, p.yaw, HANDLE_PX);
                      return (
                        <PoseMarker key={i} x={h.x} y={h.y} hx={h.hx} hy={h.hy} r={PT_R} label={i + 1} selected={i === pi}
                          class={`route-pt${i === pi ? " on" : ""}${bad.has(i) ? " bad" : ""}`}
                          attrs={{ "data-point": String(i), onPointerDown: dragPoint(i),
                            onClick: (e: MouseEvent) => { e.stopPropagation(); setPi(i); } } as Record<string, unknown>}
                          handle={i === pi ? { data: i, onPointerDown: dragHandle(i) } : undefined}>
                          <title>{`${i + 1}. ${p.name}${bad.has(i) ? ` — ${bad.get(i)}` : ""}`}</title>
                        </PoseMarker>
                      );
                    })}
                  </>
                )} />
            )}
          </div>
          <div class="routeside">
            {!draft ? (
              <>
            {/* 列表页:只有路线列表;点一条或「新建路线」换成编辑页(跟手机一样) */}
            <div class="routeside-head">
              <h2>{rt("routesTitle")}</h2>
              <button type="button" class="btn sm primary" onClick={startNew} disabled={!sel || !meta}>{rt("routeNewBtn")}</button>
            </div>
            {routes.length === 0 && <p class="muted small">{rt("routesNone")}</p>}
            <ul class="routelist" aria-label={rt("routesTitle")}>
              {routes.map((r) => {
                const n = schedCount(r.id);
                return (
                  <li key={r.id}>
                    <button type="button" class="routerow" data-route-row={r.id} onClick={() => edit(r)}>
                      <span class="routerow-name">{r.id}</span>
                      <span class="muted small">
                        {rt("routePointsN", { n: r.waypoints.length })}
                        {n > 0 && ` · ${rt("routeSchedN", { n })}`}
                        {!r.bound && <span class="bad"> · {rt("routeUnboundTag")}</span>}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
            {routes.length > 1 && (
              <label class="small field-check">
                <input type="checkbox" checked={showOthers} onChange={(e) => setShowOthers(e.currentTarget.checked)} />
                {rt("routesShowOthers")}
              </label>
            )}
              </>
            ) : (
              <>
                {/* 编辑页 */}
                <button type="button" class="link routeback" onClick={back}>{rt("routeBack")}</button>
                {draft.isNew ? (
                  <label class="routename">
                    <span class="small">{rt("routeName")}</span>
                    <input aria-label={rt("routeName")} placeholder={rt("routeName")} value={draft.id} maxLength={64} autoFocus
                      onInput={(e) => { const id = e.currentTarget.value.trim(); setDraft((d) => (d ? { ...d, id } : d)); }} />
                    <span class={`small${nameTaken || (draft.id && !NAME_RE.test(draft.id)) ? " bad" : " muted"}`}>
                      {nameTaken ? rt("routeNameTaken") : rt("routeNameHint")}
                    </span>
                  </label>
                ) : <h2>{draft.id}</h2>}
                {sel && <p class="muted small">{rt("routeBind", { v: sel.version })}</p>}
                {orig && !orig.bound && <p class="muted small">{rt("routeUnbound")}</p>}
                <p class="muted small">{rt("routeAddHint")}</p>
                {conflict && (
                  <p class="small" role="alert">{rt("routeConflict")} <button type="button" class="link" onClick={() => void reloadRoute()}>{rt("routeReload")}</button></p>
                )}
                {problems.length > 0 && <p class="small bad" role="alert">{rt("routeProblems", { n: problems.length })}</p>}
                {wholeProblems.map((p, i) => <p key={i} class="small bad">{p.problem}</p>)}
                {pts.length === 0 && <p class="muted small">{rt("routeNoPoints")}</p>}
                <ul class="pointlist" aria-label={rt("routePoints")}>
                  {pts.map((p, i) => (
                    <li key={i} aria-current={i === pi ? "true" : undefined}>
                      <span class="num">{i + 1}</span>
                      <button type="button" class="link" onClick={() => setPi(i)}>{p.name}</button>
                      {bad.has(i) && <span class="bad" title={bad.get(i)}>!</span>}
                      <span class="push" />
                      <button type="button" class="btn sm" aria-label={`${rt("pointUp")} ${i + 1}`} disabled={i === 0}
                        onClick={() => { change(reorder(pts, i, i - 1)); setPi(i - 1); }}>▲</button>
                      <button type="button" class="btn sm" aria-label={`${rt("pointDown")} ${i + 1}`} disabled={i === pts.length - 1}
                        onClick={() => { change(reorder(pts, i, i + 1)); setPi(i + 1); }}>▼</button>
                      <button type="button" class="btn sm" aria-label={`${rt("pointRemove")} ${i + 1}`}
                        onClick={() => { change(deletePoint(pts, i)); setPi(null); }}>✕</button>
                    </li>
                  ))}
                </ul>
                {bad.size > 0 && (
                  <ul class="small bad">{[...bad].map(([i, why]) => <li key={i}>{i + 1}. {pts[i]?.name}: {why}</li>)}</ul>
                )}
                {selPoint && pi !== null ? (
                  <PointForm p={selPoint} names={pts.map((x) => x.name)} idx={pi} plain={!!embed} onChange={(patch) => change(patchPoint(pts, pi, patch), true)}
                    onActions={(p2) => change(pts.map((x, k) => (k === pi ? p2 : x)), true)} />
                ) : pts.length > 0 && <p class="muted small">{rt("pointSelectHint")}</p>}
                <div class="row">
                  {!embed && <button type="button" class="btn primary" disabled={!canSave} onClick={() => void save()}>{rt("routeSave")}</button>}
                  <button type="button" class="btn" disabled={!dirty} onClick={() => (draft.isNew ? setDraft(null) : orig && edit(orig))}>{rt("routeDiscard")}</button>
                  {!draft.isNew && <button type="button" class="btn stop" onClick={() => setRemoving(true)}>{rt("routeDelete")}</button>}
                </div>
                {!draft.isNew && (
                  <SchedulePanel route={draft.id} view={view} schedules={mySchedules} say={say} reload={load} />
                )}
              </>
            )}
          </div>
        </div>
      </section>
      {removing && draft && (
        <Confirm text={rt("routeDeleteConfirm", { name: draft.id })} yes={rt("routeDelete")} danger onNo={() => setRemoving(false)} onYes={() => void del()} />
      )}
    </div>
  );
}

function PointForm({ p, idx, names, plain, onChange, onActions }: {
  p: RoutePoint; idx: number; names: string[]; plain?: boolean;
  onChange: (patch: Partial<RoutePoint>) => void; onActions: (p: RoutePoint) => void;
}) {
  const dup = names.some((n, k) => k !== idx && n === p.name);
  const num = (v: string) => (v.trim() === "" || !Number.isFinite(Number(v)) ? null : Number(v));
  return (
    <div class="pointform" aria-label={`${idx + 1}`}>
      <label class="field wide">{rt("pointName")}
        <input value={p.name} maxLength={64} aria-invalid={dup || !p.name.trim()} onInput={(e) => onChange({ name: e.currentTarget.value })} />
      </label>
      {!plain && <><label class="field">{rt("pointX")}
        <input type="number" step="0.01" value={p.x} onInput={(e) => { const v = num(e.currentTarget.value); if (v !== null) onChange({ x: v }); }} />
      </label>
      <label class="field">{rt("pointY")}
        <input type="number" step="0.01" value={p.y} onInput={(e) => { const v = num(e.currentTarget.value); if (v !== null) onChange({ y: v }); }} />
      </label>
      <label class="field">{rt("pointYaw")}
        <input type="number" step="1" value={degOf(p.yaw)} onInput={(e) => { const v = num(e.currentTarget.value); if (v !== null) onChange({ yaw: radOf(v) }); }} />
      </label></>}
      {plain && <p class="muted small wide">{rt("pointDragHint")}</p>}
      <label class="field">{rt("pointDwell")}
        <input type="number" min="0" step="1" value={dwellOf(p)} onInput={(e) => { const v = num(e.currentTarget.value); if (v !== null && v >= 0) onActions(setDwell(p, v)); }} />
      </label>
      <label class="wide"><input type="checkbox" checked={hasAction(p, "photo", "front")} onChange={() => onActions(toggleAction(p, { type: "photo", camera: "front" }))} /> {rt("pointPhotoFront")}</label>
      <label class="wide"><input type="checkbox" checked={hasAction(p, "photo", "back")} onChange={() => onActions(toggleAction(p, { type: "photo", camera: "back" }))} /> {rt("pointPhotoBack")}</label>
      <label class="wide"><input type="checkbox" checked={hasAction(p, "light")} onChange={() => onActions(toggleAction(p, { type: "light", on: true }))} /> {rt("pointLight")}</label>
      {/* 设计 §2.4:拍热像、量温度要等热像实物接上(契约的拍照现在只认前、后两路) */}
      <label class="wide muted" data-thermal="photo"><input type="checkbox" disabled /> {rt("pointThermal")} <span class="small">· {rt("thermalLater")}</span></label>
      <label class="wide muted" data-thermal="temp"><input type="checkbox" disabled /> {rt("pointTemp")} <span class="small">· {rt("thermalLater")}</span></label>
      <label class="field wide">{rt("pointCheck")}
        <input value={p.check ?? ""} maxLength={500} onInput={(e) => { const v = e.currentTarget.value; const { check: _c, ...rest } = p; onActions(v ? { ...rest, check: v } : rest); }} />
      </label>
    </div>
  );
}

function SchedulePanel({ route, view, schedules, say, reload }: {
  route: string; view: RouteView | null; schedules: RouteSchedule[]; say: Say; reload: () => Promise<void>;
}) {
  const [tz, setTz] = useState("");
  const [id, setId] = useState("");
  const [at, setAt] = useState("22:00");
  const [days, setDays] = useState<string[]>([...DAYS]);
  const [win, setWin] = useState(30);
  const [missed, setMissed] = useState<"skip" | "run_late" | "alarm">("skip");
  const [removing, setRemoving] = useState<RouteSchedule | null>(null);
  const call = async (path: string, body: unknown, done: string) => {
    try {
      await api("POST", path, body);
      say(done);
      await reload();
    } catch (e) {
      say(errText(e));
    }
  };
  return (
    <>
      <h2>{rt("routeSchedules")}</h2>
      {view && view.timezone === null ? (
        <form class="row" onSubmit={(e) => { e.preventDefault(); void call("/api/route-timezone", { timezone: tz.trim() }, t("saved")); }}>
          <label class="field">{rt("schedTimezone")}
            <input value={tz} placeholder="Asia/Kuala_Lumpur" onInput={(e) => setTz(e.currentTarget.value)} />
          </label>
          <button type="submit" class="btn sm" disabled={!tz.trim()}>{rt("schedSetTimezone")}</button>
          <p class="muted small">{rt("schedTimezoneHint")}</p>
        </form>
      ) : (
        <>
          {view && <p class="muted small">{rt("schedTimezone")}: <span class="mono">{view.timezone}</span></p>}
          {schedules.length === 0 && <p class="muted small">{rt("routeNoSchedules")}</p>}
          <ul>
            {schedules.map((s) => (
              <li key={s.id}>
                <span class="strong">{s.id}</span>
                <span class="mono small">{s.at}</span>
                <span class="muted small">{s.days.join(" ")}</span>
                <button type="button" class="link push" aria-label={`${rt("schedRemove")} ${s.id}`} onClick={() => setRemoving(s)}>{t("remove")}</button>
              </li>
            ))}
          </ul>
          <form class="form" onSubmit={(e) => {
            e.preventDefault();
            void call(`/api/route-schedules/${enc(id.trim())}`, { mission: route, at, days, window_min: win, on_missed: missed, base_revision: 0 }, rt("schedSaved"));
          }}>
            <label class="field">{rt("schedId")}<input value={id} maxLength={64} onInput={(e) => setId(e.currentTarget.value)} /></label>
            <label class="field">{rt("schedAt")}<input type="time" value={at} onInput={(e) => setAt(e.currentTarget.value)} /></label>
            <fieldset class="checks row-wrap">
              <legend>{rt("schedDays")}</legend>
              {DAYS.map((d) => (
                <label key={d}><input type="checkbox" checked={days.includes(d)}
                  onChange={() => setDays(days.includes(d) ? days.filter((x) => x !== d) : [...days, d])} /> {t(d)}</label>
              ))}
            </fieldset>
            <label class="field">{rt("schedWindow")}<input type="number" min="1" max="1440" value={win} onInput={(e) => setWin(Number(e.currentTarget.value))} /></label>
            <label class="field">{rt("schedMissed")}
              <select value={missed} onChange={(e) => setMissed(e.currentTarget.value as typeof missed)}>
                <option value="skip">{rt("schedSkip")}</option>
                <option value="run_late">{rt("schedRunLate")}</option>
                <option value="alarm">{rt("schedAlarm")}</option>
              </select>
            </label>
            <p class="muted small">{rt("schedNote")}</p>
            <button type="submit" class="btn sm" disabled={!NAME_RE.test(id.trim()) || days.length === 0 || !(win >= 1)}>{rt("schedAdd")}</button>
          </form>
        </>
      )}
      {removing && (
        <Confirm text={`${rt("schedRemove")}: ${removing.id}`} yes={rt("schedRemove")} danger onNo={() => setRemoving(null)}
          onYes={() => { const s = removing; setRemoving(null); void call(`/api/route-schedules/${enc(s.id)}/delete`, { base_revision: s.revision }, t("saved")); }} />
      )}
    </>
  );
}
