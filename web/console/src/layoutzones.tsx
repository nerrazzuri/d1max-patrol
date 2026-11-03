// 布置 → 防区(W44 第三期,设计 §5.2):在图上点几下圈一块、起名;选中一个,勾哪些摄像头归它管(存的是摄像头的防区);
// 布防时间跳「布防时间」页;删要确认。改的都先记在页面上,外框的「保存」一起存(防区多边形一条条存,摄像头改防区不动口令)。
// W73:每个防区的查看点(出事去哪看)、值守位(布防空档站哪儿盯)画在图上;站点自动选,选中防区能挪、能恢复自动。
import { useEffect, useRef, useState } from "preact/hooks";
import { api } from "./api";
import { Confirm, Modal } from "./dialogs";
import { clickGuard, startDrag, swallowClick } from "./dragpt";
import { t } from "./i18n";
import { polyPx, toMap, type Area, type CamRow, type TabProps, type ZonePost } from "./layoutpage";
import type { Pt } from "./mapview";
import { PoseMarker } from "./posemarker";
import { clientToMap, handleAt, yawToward } from "./routes";

type PostPair = { view: { x: number; y: number }; watch: { x: number; y: number; yaw: number } };

const same = (a: Pt[], b: Pt[]) => JSON.stringify(a) === JSON.stringify(b);

export function ZonesTab(p: TabProps) {
  const [areas, setAreas] = useState<Area[]>(p.data.areas);
  const [camZone, setCamZone] = useState<Record<string, string>>(() => Object.fromEntries(p.data.cameras.map((c) => [c.name, c.zone])));
  const [draft, setDraft] = useState<Pt[] | null>(null);
  const [naming, setNaming] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  // W73:挪过的查看点、值守位(没存);null = 恢复自动
  const [postEdit, setPostEdit] = useState<Record<string, PostPair | null>>({});
  const [placing, setPlacing] = useState<"view" | "watch" | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const guard = useRef(clickGuard());

  const changed = JSON.stringify(areas) !== JSON.stringify(p.data.areas)
    || p.data.cameras.some((c) => camZone[c.name] !== undefined && camZone[c.name] !== c.zone)
    || Object.keys(postEdit).length > 0;
  useEffect(() => p.setDirty(changed), [changed]);

  useEffect(() => {
    p.registerSave(async () => {
      const old = new Map(p.data.areas.map((a) => [a.zone, a.points]));
      const now = new Map(areas.map((a) => [a.zone, a.points]));
      for (const [z] of old) if (!now.has(z)) await api("POST", `${p.base}/areas`, { zone: z, remove: true });
      for (const [z, pts] of now) if (!old.has(z) || !same(old.get(z)!, pts)) await api("POST", `${p.base}/areas`, { zone: z, points: pts });
      for (const c of p.data.cameras) {
        const z = camZone[c.name];
        if (z !== undefined && z !== c.zone) await api("POST", "/api/admin/cameras", camBody(c, z));
      }
      for (const [z, pair] of Object.entries(postEdit)) {
        if (now.has(z)) await api("POST", `${p.base}/areas`, { zone: z, posts: pair });
      }
      await p.reload("areas");
      await p.reload("cameras");
    });
    return () => p.registerSave(null);
  }, [areas, camZone, postEdit]);

  const cur = areas.find((a) => a.zone === sel) ?? null;
  const serverPost = (z: string) => p.data.posts.find((x) => x.zone === z);
  /** 图上、详情里显示的那一份:挪过没存的优先;恢复自动(null)的先按原来的画,存了以后站点重算。 */
  const shown = (z: string): ZonePost | undefined => {
    const e = postEdit[z];
    const s0 = serverPost(z);
    if (e) return { zone: z, auto: false, note: "", view: e.view, watch: e.watch };
    return s0;
  };
  const pairOf = (z: string): PostPair | null => {
    const x = shown(z);
    return x?.view && x.watch ? { view: x.view, watch: x.watch } : null;
  };
  function pick(px: number, py: number) {
    if (draft) { setDraft((d) => (d ? [...d, toMap(p.meta, px, py)] : d)); return; }
    if (!cur || !placing) return;
    const [x, y] = toMap(p.meta, px, py);
    const base = pairOf(cur.zone) ?? { view: { x, y }, watch: { x, y, yaw: 0 } };
    setPostEdit({ ...postEdit, [cur.zone]: placing === "view" ? { ...base, view: { x, y } } : { ...base, watch: { ...base.watch, x, y } } });
    setPlacing(null);
  }
  const turn = (z: string) => (e: PointerEvent) => {
    startDrag(e, guard.current, (cx, cy) => {
      const m = clientToMap(root.current, p.meta, cx, cy);
      const pair = pairOf(z);
      if (!m || !pair) return;
      const yaw = yawToward(pair.watch.x, pair.watch.y, m[0], m[1]);
      if (yaw !== null) setPostEdit((all) => ({ ...all, [z]: { ...pair, watch: { ...pair.watch, yaw } } }));
    });
  };
  return (
    <div class="ly-tab">
      <div ref={root} onClickCapture={(e) => swallowClick(guard.current, e)}>
      <p.Map
        areas={areas.map((a) => ({ name: a.zone, pts: a.points, kind: "zone" as const, hot: a.zone === sel }))}
        drawing={!!draft || !!placing}
        onPick={draft || placing ? pick : undefined}
        world={draft && draft.length > 0 ? <polyline points={polyPx(p.meta, draft)} class="draft-line" /> : null}
        marks={(s) => <>
          {!draft && areas.map((a) => {
            const x = shown(a.zone);
            if (!x?.view || !x.watch) return null;
            const [vx, vy] = s.map(x.view.x, x.view.y);
            const h = handleAt(p.meta, s, x.watch.x, x.watch.y, x.watch.yaw, 22);
            const on = a.zone === sel;
            return (
              <g key={a.zone} class={`zone-posts${on ? " on" : ""}`}>
                <PoseMarker x={h.x} y={h.y} hx={h.hx} hy={h.hy} r={8} label="" selected={on} class="zone-watch"
                  attrs={{ "data-watch": a.zone } as Record<string, unknown>}
                  handle={on ? { data: a.zone, onPointerDown: turn(a.zone) } : undefined}>
                  <title>{`${a.zone} · ${t("lyWatchMark")}`}</title>
                </PoseMarker>
                <circle cx={vx} cy={vy} r={5} class="zone-view" data-view={a.zone}><title>{`${a.zone} · ${t("lyViewMark")}`}</title></circle>
              </g>
            );
          })}
          {draft?.map(([x, y], i) => { const [sx, sy] = s.map(x, y); return <circle key={i} cx={sx} cy={sy} r={5} class="draft-pt" />; })}
        </>} />
      </div>
      <div class="ly-side">
        {draft ? (
          <div class="row wrap">
            <span class="muted small">{t("drawHint", { n: draft.length })}</span>
            <button type="button" class="btn sm" disabled={!draft.length} onClick={() => setDraft(draft.slice(0, -1))}>{t("undoPoint")}</button>
            <button type="button" class="btn sm" onClick={() => setDraft(null)}>{t("cancel")}</button>
            <button type="button" class="btn sm primary" disabled={draft.length < 3} onClick={() => setNaming(true)}>{t("lyDrawDone")}</button>
          </div>
        ) : <button type="button" class="btn primary" onClick={() => { setDraft([]); setSel(null); }}>{t("lyDrawZone")}</button>}
        <p class="muted small">{t("lyZonesHint")}</p>
        <ul class="plain ly-list">
          {areas.length === 0 && <li class="muted small">{t("noZones")}</li>}
          {areas.map((a) => (
            <li key={a.zone}><button type="button" class={a.zone === sel ? "linklike on" : "linklike"} onClick={() => { setSel(a.zone); setPlacing(null); }}>{a.zone}</button></li>
          ))}
        </ul>
        {cur && (
          <section class="panel ly-detail">
            <h3>{cur.zone}</h3>
            <fieldset class="checks zone-cams">
              <legend>{t("lyZoneCams")}</legend>
              {p.data.cameras.length === 0 && <span class="muted small">{t("lyNoCams")}</span>}
              {p.data.cameras.map((c) => (
                // 摄像头一定归一个防区:原本就归这里的不能取消(去别的防区勾它);挪过来的取消就回原来的防区
                <label key={c.name}><input type="checkbox" value={c.name} checked={camZone[c.name] === cur.zone}
                  disabled={c.zone === cur.zone && camZone[c.name] === cur.zone}
                  onChange={(e) => setCamZone({ ...camZone, [c.name]: e.currentTarget.checked ? cur.zone : c.zone })} /> {c.name}
                  {camZone[c.name] && camZone[c.name] !== cur.zone && <span class="muted small"> ({camZone[c.name]})</span>}
                </label>
              ))}
            </fieldset>
            <ZonePostsBox post={shown(cur.zone)} saved={!!serverPost(cur.zone)} placing={placing} edited={cur.zone in postEdit}
              onPlace={(w) => setPlacing(placing === w ? null : w)}
              onReset={() => {
                const s0 = serverPost(cur.zone);
                const next = { ...postEdit };
                if (s0 && !s0.auto) next[cur.zone] = null; else delete next[cur.zone];
                setPostEdit(next);
              }} />
            <a href="#/admin/arming" class="small">{t("lyZoneArming")}</a>
            <div class="row end"><button type="button" class="btn sm" onClick={() => setRemoving(cur.zone)}>{t("remove")}</button></div>
          </section>
        )}
      </div>
      {naming && draft && (
        <NameZone taken={areas.map((a) => a.zone)} onClose={() => setNaming(false)}
          onSave={(name) => { setAreas([...areas.filter((a) => a.zone !== name), { zone: name, points: draft }]); setDraft(null); setNaming(false); setSel(name); }} />
      )}
      {removing && (
        <Confirm text={t("removeZoneConfirm", { zone: removing })} yes={t("remove")} danger onNo={() => setRemoving(null)}
          onYes={() => { const z = removing; setRemoving(null); setAreas(areas.filter((a) => a.zone !== z)); setSel(null); }} />
      )}
    </div>
  );
}

function ZonePostsBox({ post, saved, placing, edited, onPlace, onReset }: {
  post: ZonePost | undefined; saved: boolean; placing: "view" | "watch" | null; edited: boolean;
  onPlace: (w: "view" | "watch") => void; onReset: () => void;
}) {
  if (!saved) return null;           // 新圈的防区:存了以后站点才选得出来
  return (
    <fieldset class="zone-posts-box">
      <legend>{t("lyPosts")}</legend>
      <p class="muted small" data-posts-state="1">
        {post?.auto ? t("lyPostsAuto") : t("lyPostsManual", { by: post?.updated_by || "—" })}
      </p>
      {post?.auto && !post.view && post.note && <p class="small bad">{t("lyPostsNone", { why: post.note })}</p>}
      {placing && <p class="muted small" role="status">{placing === "view" ? t("lyPickView") : t("lyPickWatch")}</p>}
      <div class="row wrap">
        <button type="button" class="btn sm" aria-pressed={placing === "view"} onClick={() => onPlace("view")}>{t("lyMoveView")}</button>
        <button type="button" class="btn sm" aria-pressed={placing === "watch"} onClick={() => onPlace("watch")}>{t("lyMoveWatch")}</button>
        <button type="button" class="btn sm" disabled={!!post?.auto && !edited} onClick={onReset}>{t("lyPostsReset")}</button>
      </div>
    </fieldset>
  );
}

/** 只改摄像头的防区:别的照抄,不带口令(站点就保留原来的)。 */
function camBody(c: CamRow, zone: string): Record<string, unknown> {
  return { name: c.name, zone, edit: true, onvif_url: c.onvif_url, rtsp_url: c.rtsp_url ?? "", username: c.username ?? "", motion: !!c.motion };
}

function NameZone({ taken, onClose, onSave }: { taken: string[]; onClose: () => void; onSave: (name: string) => void }) {
  const [name, setName] = useState("");
  const dup = taken.includes(name.trim());
  return (
    <Modal label={t("lyDrawDone")} onClose={onClose}>
      <form class="form" onSubmit={(e) => { e.preventDefault(); if (name.trim()) onSave(name.trim()); }}>
        <label class="field">{t("zoneName")}<input name="zone" value={name} maxLength={128} required onInput={(e) => setName(e.currentTarget.value)} /></label>
        <p class="muted small">{dup ? t("lyZoneReplace") : t("zoneNameHint")}</p>
        <div class="row end">
          <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="submit" class="btn primary">{t("save")}</button>
        </div>
      </form>
    </Modal>
  );
}
