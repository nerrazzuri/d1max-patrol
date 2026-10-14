// 管理组(商业化 B1c):地图与防区(在图上画防区多边形)、名单(时段 / 人员授权)。
import { useCallback, useEffect, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { Confirm, Modal } from "./dialogs";
import { t } from "./i18n";
import { MapCanvas } from "./mapcanvas";
import { toPx, type PreviewMeta, type Pt } from "./mapview";

type Say = (s: string) => void;

interface MapRow { map_id: string; version: string; note: string; created_ms: number; source: string }
interface Area { zone: string; points: Pt[] }

function errText(e: unknown): string {
  return t("failed", { why: e instanceof ApiError ? e.message : String(e) });
}

/** 预览图上的像素 → 地图坐标(米)。 */
export function toMap(meta: PreviewMeta, px: number, py: number): Pt {
  return [Math.round((meta.left_x + px * meta.m_per_px) * 100) / 100, Math.round((meta.top_y - py * meta.m_per_px) * 100) / 100];
}

function poly(meta: PreviewMeta, pts: Pt[]): string {
  return pts.map(([x, y]) => { const p = toPx(meta, x, y); return `${p.px},${p.py}`; }).join(" ");
}

// ------------------------------------------------------------------ 地图与防区

export function MapsZonesPage({ say }: { say: Say }) {
  const [maps, setMaps] = useState<MapRow[]>([]);
  const [sel, setSel] = useState<MapRow | null>(null);
  const [meta, setMeta] = useState<PreviewMeta | null>(null);
  const [areas, setAreas] = useState<Area[]>([]);
  const [draft, setDraft] = useState<Pt[] | null>(null);
  const [naming, setNaming] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);

  useEffect(() => {
    api<{ maps: MapRow[] }>("GET", "/api/maps").then((d) => {
      setMaps(d.maps);
      setSel((s) => s ?? d.maps[d.maps.length - 1] ?? null);
    }).catch((e) => say(errText(e)));
  }, []);
  const base = sel ? `/api/maps/${enc(sel.map_id)}/${enc(sel.version)}` : "";
  const loadAreas = useCallback(() => {
    if (!base) return;
    api<{ areas: Area[] }>("GET", `${base}/areas`).then((d) => setAreas(d.areas)).catch(() => setAreas([]));
  }, [base]);
  useEffect(() => {
    setMeta(null);
    setDraft(null);
    if (!base) return;
    api<PreviewMeta>("GET", `${base}/preview`).then(setMeta).catch(() => setMeta(null));
    loadAreas();
  }, [base]);

  // 点的那一处:地图组件给的是预览像素(世界层的 getScreenCTM 倒回去,转正、缩放、平移都算进去了)
  function pick(px: number, py: number) {
    if (!meta) return;
    setDraft((d) => (d ? [...d, toMap(meta, px, py)] : d));
  }

  async function save(name: string) {
    setNaming(false);
    try {
      const d = await api<{ areas: Area[] }>("POST", `${base}/areas`, { zone: name, points: draft });
      setAreas(d.areas);
      setDraft(null);
      say(t("saved"));
    } catch (e) {
      say(errText(e));
    }
  }

  return (
    <div class="admin">
      <section class="panel">
        <div class="panel-head">
          <h1>{t("tabMapsZones")}</h1>
          <label class="field-inline">{t("mapLabel")}
            <select value={sel ? `${sel.map_id}\u0000${sel.version}` : ""} onChange={(e) => {
              const [m, v] = e.currentTarget.value.split("\u0000");
              setSel(maps.find((x) => x.map_id === m && x.version === v) ?? null);
            }}>
              {maps.map((m) => <option key={`${m.map_id}:${m.version}`} value={`${m.map_id}\u0000${m.version}`}>{m.map_id} · v{m.version}{m.note ? ` · ${m.note}` : ""}</option>)}
            </select>
          </label>
          <span class="push" />
          {draft ? (
            <>
              <span class="muted small">{t("drawHint", { n: draft.length })}</span>
              <button type="button" class="btn sm" onClick={() => setDraft(draft.slice(0, -1))} disabled={!draft.length}>{t("undoPoint")}</button>
              <button type="button" class="btn sm" onClick={() => setDraft(null)}>{t("cancel")}</button>
              <button type="button" class="btn primary sm" disabled={draft.length < 3} onClick={() => setNaming(true)}>{t("finishZone")}</button>
            </>
          ) : (
            <button type="button" class="btn primary" disabled={!meta} onClick={() => setDraft([])}>{t("drawZone")}</button>
          )}
        </div>
        <div class="zonesplit">
          <div class="zonemap">
            {!sel || !meta ? <p class="empty">{t("noMap")}</p> : (
              // 防区页只画能改的防区(防区接口,改了当场变);站点预览里的 areas 在地图页上画
              <MapCanvas meta={meta} raster={`${base}/preview.png`} label={t("tabMapsZones")}
                areas={areas.map((a) => ({ name: a.zone, pts: a.points, kind: "zone" as const }))}
                drawing={!!draft} onPick={draft ? pick : undefined}
                world={draft && draft.length > 0 ? <polyline points={poly(meta, draft)} class="draft-line" /> : null}
                marks={(s) => draft?.map(([x, y], i) => {
                  const [sx, sy] = s.map(x, y);
                  return <circle key={i} cx={sx} cy={sy} r={4} class="draft-pt" />;
                })} />
            )}
          </div>
          <div class="zonelist">
            <h2>{t("securityZones")}</h2>
            <p class="muted small">{t("zonesHint")}</p>
            {(meta?.zones ?? []).some((z) => z.kind === "nogo") && (
              <p class="muted small nogo-legend">
                <span class="swatch nogo" aria-hidden="true" /> {t("noGoLegend")}
                {(meta?.zones ?? []).some((z) => z.kind === "nogo" && z.hard) && (
                  <><br /><span class="swatch nogo hard" aria-hidden="true" /> {t("hardNoGoLegend")}</>
                )}
              </p>
            )}
            {areas.length === 0 && <p class="muted small">{t("noZones")}</p>}
            <ul>
              {areas.map((a) => (
                <li key={a.zone}>
                  <span class="strong">{a.zone}</span>
                  <span class="muted small">{t("points", { n: a.points.length })}</span>
                  <button type="button" class="link push" onClick={() => setRemoving(a.zone)}>{t("remove")}</button>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>
      {naming && <NameZone onClose={() => setNaming(false)} onSave={save} />}
      {removing && (
        <Confirm text={t("removeZoneConfirm", { zone: removing })} yes={t("remove")} onNo={() => setRemoving(null)}
          onYes={() => {
            const z = removing;
            setRemoving(null);
            api<{ areas: Area[] }>("POST", `${base}/areas`, { zone: z, remove: true }).then((d) => setAreas(d.areas)).catch((e) => say(errText(e)));
          }} />
      )}
    </div>
  );
}

function NameZone({ onClose, onSave }: { onClose: () => void; onSave: (name: string) => void }) {
  const [name, setName] = useState("");
  return (
    <Modal label={t("finishZone")} onClose={onClose}>
      <form class="form" onSubmit={(e) => { e.preventDefault(); if (name.trim()) onSave(name.trim()); }}>
        <label class="field">{t("zoneName")}<input value={name} maxLength={128} required onInput={(e) => setName(e.currentTarget.value)} /></label>
        <p class="muted small">{t("zoneNameHint")}</p>
        <div class="row end">
          <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="submit" class="btn primary">{t("save")}</button>
        </div>
      </form>
    </Modal>
  );
}

// ------------------------------------------------------------------ 名单

interface Authz { id: number; name: string; zones: string[]; days: number; start: string; end: string; until_ms: number | null; note: string; created_by: string }

const DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;

export function daysText(days: number): string {
  if (days === 127) return t("everyDay");
  if (days === 0b0011111) return t("weekdays");
  if (days === 0b1100000) return t("weekends");
  return DAY_KEYS.filter((_, i) => days >> i & 1).map((k) => t(k)).join(", ");
}

export function ListsPage({ say }: { say: Say }) {
  const [rows, setRows] = useState<Authz[]>([]);
  const [zones, setZones] = useState<string[]>([]);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<Authz | null>(null);
  const load = () => api<{ authorizations: Authz[] }>("GET", "/api/lists/authorizations").then((d) => setRows(d.authorizations)).catch((e) => say(errText(e)));
  useEffect(() => {
    void load();
    api<{ zones: { zone: string }[] }>("GET", "/api/mode").then((d) => setZones(d.zones.map((z) => z.zone))).catch(() => setZones([]));
  }, []);
  return (
    <div class="admin">
      <section class="panel">
        <div class="panel-head">
          <h1>{t("authorizations")}</h1>
          <button type="button" class="btn primary push" onClick={() => setAdding(true)}>{t("addAuthorization")}</button>
        </div>
        <p class="muted small pad">{t("authorizationsHint")}</p>
        <table class="grid">
          <thead><tr><th>{t("who")}</th><th>{t("where")}</th><th>{t("days")}</th><th>{t("timeRange")}</th><th>{t("validUntil")}</th><th>{t("note")}</th><th>{t("colActions")}</th></tr></thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={7} class="muted">{t("noAuthorizations")}</td></tr>}
            {rows.map((r) => (
              <tr key={r.id}>
                <td class="strong">{r.name}</td>
                <td>{r.zones.length ? r.zones.join(", ") : t("wholeSite")}</td>
                <td>{daysText(r.days)}</td>
                <td class="mono">{r.start}–{r.end}</td>
                <td class="mono">{r.until_ms ? new Date(r.until_ms).toISOString().slice(0, 10) : "—"}</td>
                <td class="muted">{r.note || "—"}</td>
                <td class="actions"><button type="button" class="btn sm" onClick={() => setRemoving(r)}>{t("remove")}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>{t("faceLists")}</h2></div>
        <p class="muted small pad">{t("faceListsHint")}</p>
      </section>
      {adding && <AddAuthz zones={zones} onClose={() => setAdding(false)} say={say} onDone={() => { setAdding(false); void load(); say(t("saved")); }} />}
      {removing && (
        <Confirm text={t("removeAuthzConfirm", { name: removing.name })} yes={t("remove")} onNo={() => setRemoving(null)}
          onYes={() => {
            const r = removing;
            setRemoving(null);
            api("POST", `/api/lists/authorizations/${r.id}/remove`, {}).then(load).catch((e) => say(errText(e)));
          }} />
      )}
    </div>
  );
}

function AddAuthz({ zones, onClose, onDone, say }: { zones: string[]; onClose: () => void; onDone: () => void; say: Say }) {
  const [name, setName] = useState("");
  const [site, setSite] = useState(zones.length === 0);
  const [picked, setPicked] = useState<string[]>([]);
  const [days, setDays] = useState(0b0011111);
  const [start, setStart] = useState("09:00");
  const [end, setEnd] = useState("11:00");
  const [until, setUntil] = useState("");
  const [note, setNote] = useState("");
  async function submit(e: Event) {
    e.preventDefault();
    try {
      await api("POST", "/api/lists/authorizations", {
        name, zones: site ? [] : picked, days, start, end, note,
        until_ms: until ? new Date(`${until}T23:59:59`).getTime() : null,
      });
      onDone();
    } catch (x) {
      say(errText(x));
    }
  }
  return (
    <Modal label={t("addAuthorization")} onClose={onClose}>
      <form class="form" onSubmit={submit}>
        <label class="field">{t("who")}<input value={name} maxLength={64} required placeholder={t("whoPlaceholder")} onInput={(e) => setName(e.currentTarget.value)} /></label>
        <fieldset class="checks">
          <legend>{t("where")}</legend>
          <label><input type="radio" name="scope" checked={site} onChange={() => setSite(true)} /> {t("wholeSite")}</label>
          <label><input type="radio" name="scope" checked={!site} disabled={zones.length === 0} onChange={() => setSite(false)} /> {t("someZones")}</label>
          {!site && zones.map((z) => (
            <label key={z} class="indent"><input type="checkbox" checked={picked.includes(z)}
              onChange={(e) => setPicked(e.currentTarget.checked ? [...picked, z] : picked.filter((x) => x !== z))} /> {z}</label>
          ))}
        </fieldset>
        <fieldset class="checks row-wrap">
          <legend>{t("days")}</legend>
          {DAY_KEYS.map((k, i) => (
            <label key={k}><input type="checkbox" checked={!!(days >> i & 1)} onChange={() => setDays(days ^ (1 << i))} /> {t(k)}</label>
          ))}
        </fieldset>
        <div class="row">
          <label class="field">{t("from")}<input type="time" value={start} required onInput={(e) => setStart(e.currentTarget.value)} /></label>
          <label class="field">{t("to")}<input type="time" value={end} required onInput={(e) => setEnd(e.currentTarget.value)} /></label>
        </div>
        <p class="muted small">{t("overnightHint")}</p>
        <label class="field">{t("validUntil")} ({t("optional")})<input type="date" value={until} onInput={(e) => setUntil(e.currentTarget.value)} /></label>
        <label class="field">{t("note")} ({t("optional")})<input value={note} maxLength={200} onInput={(e) => setNote(e.currentTarget.value)} /></label>
        <div class="row end">
          <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="submit" class="btn primary" disabled={!name.trim() || days === 0 || (!site && picked.length === 0)}>{t("addAuthorization")}</button>
        </div>
      </form>
    </Modal>
  );
}
