// 布置 → 防区(W44 第三期,设计 §5.2):在图上点几下圈一块、起名;选中一个,勾哪些摄像头归它管(存的是摄像头的防区);
// 布防时间跳「布防时间」页;删要确认。改的都先记在页面上,外框的「保存」一起存(防区多边形一条条存,摄像头改防区不动口令)。
import { useEffect, useState } from "preact/hooks";
import { api } from "./api";
import { Confirm, Modal } from "./dialogs";
import { t } from "./i18n";
import { polyPx, toMap, type Area, type CamRow, type TabProps } from "./layoutpage";
import type { Pt } from "./mapview";

const same = (a: Pt[], b: Pt[]) => JSON.stringify(a) === JSON.stringify(b);

export function ZonesTab(p: TabProps) {
  const [areas, setAreas] = useState<Area[]>(p.data.areas);
  const [camZone, setCamZone] = useState<Record<string, string>>(() => Object.fromEntries(p.data.cameras.map((c) => [c.name, c.zone])));
  const [draft, setDraft] = useState<Pt[] | null>(null);
  const [naming, setNaming] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);

  const changed = JSON.stringify(areas) !== JSON.stringify(p.data.areas)
    || p.data.cameras.some((c) => camZone[c.name] !== undefined && camZone[c.name] !== c.zone);
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
      await p.reload("areas");
      await p.reload("cameras");
    });
    return () => p.registerSave(null);
  }, [areas, camZone]);

  const cur = areas.find((a) => a.zone === sel) ?? null;
  return (
    <div class="ly-tab">
      <p.Map
        areas={areas.map((a) => ({ name: a.zone, pts: a.points, kind: "zone" as const, hot: a.zone === sel }))}
        drawing={!!draft}
        onPick={draft ? (px, py) => setDraft((d) => (d ? [...d, toMap(p.meta, px, py)] : d)) : undefined}
        world={draft && draft.length > 0 ? <polyline points={polyPx(p.meta, draft)} class="draft-line" /> : null}
        marks={(s) => draft?.map(([x, y], i) => { const [sx, sy] = s.map(x, y); return <circle key={i} cx={sx} cy={sy} r={4} class="draft-pt" />; })} />
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
            <li key={a.zone}><button type="button" class={a.zone === sel ? "linklike on" : "linklike"} onClick={() => setSel(a.zone)}>{a.zone}</button></li>
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
