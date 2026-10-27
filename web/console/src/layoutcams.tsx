// 布置 → 摄像头(W44 第三期,设计 §5.2):把固定摄像头放到图上 —— 点图放图标,拖箭头调朝向,张角填度数;旁边看实时画面对照朝向。
// 连接信息(地址、账号口令、防区)在「改连接信息」里填(口令照旧不回显,空着不改);删摄像头当场删。
// 位置先记在页面上,外框「保存」一起存(不带口令,站点保留原来的)。
import { useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { CameraForm, warningText, type CamRow as AdminCam } from "./camadmin";
import { Confirm } from "./dialogs";
import { clickGuard, startDrag, swallowClick } from "./dragpt";
import { t } from "./i18n";
import { camsOn, fanPath, toMap, type CamRow, type Placement, type TabProps } from "./layoutpage";
import { PoseMarker } from "./posemarker";
import { clientToMap, handleAt, yawToward } from "./routes";

export function CamerasTab(p: TabProps) {
  const here = (pl: Placement | null | undefined) => !!pl && pl.map_id === p.map.map_id && pl.map_version === p.map.version;
  const [place, setPlace] = useState<Record<string, Placement | null>>(() =>
    Object.fromEntries(p.data.cameras.map((c) => [c.name, c.placement ?? null])));
  const [sel, setSel] = useState<string | null>(null);
  const [placing, setPlacing] = useState(false);
  const [form, setForm] = useState<CamRow | "new" | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const guard = useRef(clickGuard());

  const changed = p.data.cameras.some((c) => JSON.stringify(place[c.name] ?? null) !== JSON.stringify(c.placement ?? null));
  useEffect(() => p.setDirty(changed), [changed]);
  useEffect(() => {
    p.registerSave(async () => {
      for (const c of p.data.cameras) {
        const now = place[c.name] ?? null;
        if (JSON.stringify(now) === JSON.stringify(c.placement ?? null)) continue;
        await api("POST", "/api/admin/cameras", {
          name: c.name, zone: c.zone, onvif_url: c.onvif_url, rtsp_url: c.rtsp_url ?? "", username: c.username ?? "", motion: !!c.motion,
          edit: true, placement: now,
        });
      }
      await p.reload("cameras");
    });
    return () => p.registerSave(null);
  }, [place]);

  const cur = p.data.cameras.find((c) => c.name === sel) ?? null;
  const curPl = cur ? place[cur.name] : null;
  const setPl = (name: string, f: Partial<Placement> | null) => setPlace((all) => {
    if (f === null) return { ...all, [name]: null };
    const old = all[name];
    return { ...all, [name]: { map_id: p.map.map_id, map_version: p.map.version, x: 0, y: 0, yaw: 0, fov_deg: 90, ...(here(old) ? old : {}), ...f } as Placement };
  });
  const shown = camsOn(p.data.cameras.map((c) => ({ ...c, placement: place[c.name] ?? null })), p.map);
  const turn = (name: string) => (e: PointerEvent) => {
    startDrag(e, guard.current, (cx, cy) => {
      const m = clientToMap(root.current, p.meta, cx, cy);
      const pl = place[name];
      if (!m || !pl) return;
      const yaw = yawToward(pl.x, pl.y, m[0], m[1]);
      if (yaw !== null) setPl(name, { yaw });
    });
  };

  return (
    <div class="ly-tab">
      <div ref={root} onClickCapture={(e) => swallowClick(guard.current, e)}>
        <p.Map
          drawing={placing}
          onPick={cur && (placing || here(curPl)) ? (px, py) => { const [x, y] = toMap(p.meta, px, py); setPl(cur.name, { x, y }); setPlacing(false); } : undefined}
          world={<>{shown.map((c) => <path key={c.name} class={c.name === sel ? "cam-fan on" : "cam-fan"} d={fanPath(p.meta, c.placement as Placement)} />)}</>}
          marks={(s) => shown.map((c) => {
            const pl = c.placement as Placement;
            const h = handleAt(p.meta, s, pl.x, pl.y, pl.yaw, 28);
            return (
              <PoseMarker key={c.name} x={h.x} y={h.y} hx={h.hx} hy={h.hy} r={8} label="" selected={c.name === sel} class="cam-pt"
                attrs={{ "data-cam": c.name, onClick: (e: MouseEvent) => { e.stopPropagation(); setSel(c.name); } } as Record<string, unknown>}
                handle={c.name === sel ? { data: c.name, onPointerDown: turn(c.name) } : undefined}>
                <title>{c.name}</title>
              </PoseMarker>
            );
          })} />
      </div>
      <div class="ly-side">
        <button type="button" class="btn primary" onClick={() => setForm("new")}>{t("lyAddCam")}</button>
        <p class="muted small">{t("lyCamsHint")}</p>
        <ul class="plain ly-list">
          {p.data.cameras.length === 0 && <li class="muted small">{t("lyNoCams")}</li>}
          {p.data.cameras.map((c) => (
            <li key={c.name}>
              <button type="button" class={c.name === sel ? "linklike on" : "linklike"} onClick={() => { setSel(c.name); setPlacing(false); }}>{c.name}</button>
              <span class="muted small"> {c.zone}{here(place[c.name]) ? "" : place[c.name] ? ` · ${t("lyCamOtherMap")}` : ` · ${t("lyCamNotPlaced")}`}</span>
              {(c.warnings ?? []).map((w) => <div key={w.code} class="warncell small" data-warning={w.code}>{warningText(w, c.zone)}</div>)}
            </li>
          ))}
        </ul>
        {cur && (
          <section class="panel ly-detail">
            <h3>{cur.name}</h3>
            {here(curPl) ? (
              <>
                <p class="muted small">{t("lyCamMoveHint")}</p>
                <label class="field">{t("lyCamFov")}
                  <input name="fov" type="number" min={10} max={180} step={5} value={curPl!.fov_deg}
                    onInput={(e) => { const v = Number(e.currentTarget.value); if (v >= 10 && v <= 180) setPl(cur.name, { fov_deg: v }); }} />
                </label>
                <button type="button" class="btn sm" onClick={() => setPl(cur.name, null)}>{t("lyCamUnplace")}</button>
              </>
            ) : (
              <button type="button" class="btn sm primary" onClick={() => setPlacing(true)}>{t("lyCamPlace")}</button>
            )}
            {placing && <p class="muted small">{t("lyCamPlaceHint")}</p>}
            {cur.rtsp_url ? <img class="cam-live" alt={cur.name} src={`/api/cameras/${enc(cur.name)}/live`} /> : <p class="muted small">{t("lyCamNoLive")}</p>}
            <div class="row wrap">
              <button type="button" class="btn sm" onClick={() => setForm(cur)}>{t("lyCamEdit")}</button>
              <button type="button" class="btn sm" onClick={() => setRemoving(cur.name)}>{t("remove")}</button>
            </div>
          </section>
        )}
      </div>
      {form && (
        <CameraForm initial={form === "new" ? null : (form as unknown as AdminCam)} onClose={() => setForm(null)} say={p.say}
          onSaved={(warnings, zone) => {
            // 站点回的警告(防区没映射到拦截点、没画多边形)照样说出来(演示站点实测:原来只说「已保存」)
            setForm(null);
            p.say(warnings.length ? `${t("saved")} ${warnings.map((w) => warningText(w, zone)).join(" ")}` : t("saved"));
            void p.reload("cameras");
          }} />
      )}
      {removing && (
        <Confirm text={t("lyCamRemove", { name: removing })} yes={t("remove")} danger onNo={() => setRemoving(null)}
          onYes={() => {
            const n = removing;
            setRemoving(null);
            api("POST", `/api/admin/cameras/${enc(n)}/remove`, {}).then(() => { setSel(null); void p.reload("cameras"); })
              .catch((e) => p.say(t("failed", { why: e instanceof ApiError ? e.message : String(e) })));
          }} />
      )}
    </div>
  );
}
