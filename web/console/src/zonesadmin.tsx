// 管理组(商业化 B1c):地图与防区(在图上画防区多边形)、名单(时段 / 人员授权)。
// 地图台账 44(2026-10-14):在图上框一块「抹掉」(导航区域的种类 erase)—— 狗永远不去、站点画图时抹成没建图。
// 存的是整份导航区域(带改之前看到的修订号,跟手机一样);加一块是收紧、当场生效,删一块是放宽、要重新确认。
// 2026-10-15(用户「只能画防区感觉不太够」):网页也能画禁行区、限速区、**虚拟墙**(画一条线,站点存成约 0.3 m 宽的
// 禁行带,规则跟禁行区一样),列出狗守的区域、删、确认这一版(从上一版带过来的建议还是在手机上核)。
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { Confirm, Modal } from "./dialogs";
import { t } from "./i18n";
import { MapCanvas } from "./mapcanvas";
import { toPx, type NavZone, type PreviewMeta, type Pt } from "./mapview";

/** 虚拟墙的宽(米,跟契约 ``WALL_WIDTH_M`` 一样;只用来写提示,多边形是站点算的)。 */
const WALL_WIDTH_M = 0.3;

type Say = (s: string) => void;

interface MapRow { map_id: string; version: string; note: string; created_ms: number; source: string }
interface Area { zone: string; points: Pt[] }
/** 站点的导航区域(GET/POST …/zones):修订号 + 整份区域 + 确认情况。 */
interface NavZones {
  revision: number;
  zones: NavZone[];
  auto_confirmed?: boolean;
  confirmed?: { revision: number; by: string; at_ms: number; current: boolean; how: string } | null;
  confirm_text?: string;
  proposal?: unknown;
  /** 还没确认、狗上照旧守着的(外审 W38-I1)。 */
  held?: NavZone[];
}
/** 正在画什么:防区(摄像头用)、抹掉区,或者狗守的禁行区、限速区、虚拟墙。 */
type DrawKind = "zone" | "erase" | "nogo" | "slow" | "wall";
/** 画完填的那几项。 */
interface NavForm { label: string; hard: boolean; speed: number; temp: boolean; hours: number }

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
  // 外审 R3(2026-10-15 下午):预览、防区、导航区域都记着是哪张图的(``base``);换图后才回来的旧回包不写,
  // 渲染时也只认这张图的 —— 不然 A 图的区域会被拿去确认 B 图(两张图的修订号各数各的,同一个数说明不了什么)
  const [metaAt, setMetaAt] = useState<{ base: string; v: PreviewMeta | null }>({ base: "", v: null });
  const [areasAt, setAreasAt] = useState<{ base: string; v: Area[] }>({ base: "", v: [] });
  const [draft, setDraft] = useState<Pt[] | null>(null);
  const [drawKind, setDrawKind] = useState<DrawKind>("zone");
  const [navForm, setNavForm] = useState(false);
  const [removingNav, setRemovingNav] = useState<NavZone | null>(null);
  /** 弹确认框那一刻给人看的那一份(哪张图、第几版、内容):发之前再拉一次比,变了不确认。 */
  const [confirming, setConfirming] = useState<{ base: string; nav: NavZones } | null>(null);
  const [naming, setNaming] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [navAt, setNavAt] = useState<{ base: string; v: NavZones | null }>({ base: "", v: null });
  const [erasing, setErasing] = useState(false);
  const [unerasing, setUnerasing] = useState<NavZone | null>(null);

  useEffect(() => {
    api<{ maps: MapRow[] }>("GET", "/api/maps").then((d) => {
      setMaps(d.maps);
      setSel((s) => s ?? d.maps[d.maps.length - 1] ?? null);
    }).catch((e) => say(errText(e)));
  }, []);
  const base = sel ? `/api/maps/${enc(sel.map_id)}/${enc(sel.version)}` : "";
  const cur = useRef(base);
  cur.current = base;
  /** 回包回来时还是这张图吗。 */
  const here = (b: string) => cur.current === b;
  const meta = metaAt.base === base ? metaAt.v : null;
  const areas = areasAt.base === base ? areasAt.v : [];
  const nav = navAt.base === base ? navAt.v : null;
  const setMeta = (b: string, v: PreviewMeta | null) => { if (here(b)) setMetaAt({ base: b, v }); };
  const setAreas = (b: string, v: Area[]) => { if (here(b)) setAreasAt({ base: b, v }); };
  const setNav = (b: string, v: NavZones | null) => { if (here(b)) setNavAt({ base: b, v }); };
  const loadAreas = useCallback(() => {
    if (!base) return;
    const b = base;
    api<{ areas: Area[] }>("GET", `${b}/areas`).then((d) => setAreas(b, d.areas)).catch(() => setAreas(b, []));
  }, [base]);
  const loadMeta = useCallback(() => {
    if (!base) return;
    const b = base;
    api<PreviewMeta>("GET", `${b}/preview`).then((m) => setMeta(b, m)).catch(() => setMeta(b, null));
  }, [base]);
  const loadNav = useCallback(() => {
    if (!base) return;
    const b = base;
    api<NavZones>("GET", `${b}/zones`).then((d) => setNav(b, d)).catch(() => setNav(b, null));
  }, [base]);
  useEffect(() => {
    setDraft(null);
    // 换了图:上一张图的弹框、正在画的全作废
    setConfirming(null);
    setNavForm(false);
    setRemovingNav(null);
    setNaming(false);
    setRemoving(null);
    setErasing(false);
    setUnerasing(null);
    if (!base) return;
    loadMeta();
    loadAreas();
    loadNav();
  }, [base]);
  const erased = (nav?.zones ?? []).filter((z) => z.kind === "erase");
  const navList = (nav?.zones ?? []).filter((z) => z.kind === "nogo" || z.kind === "slow");
  const minPts = drawKind === "wall" ? 2 : 3;

  /** 整份导航区域存回去(改之前看到的修订号先拉一次最新的,别盖掉手机上刚改的);存完重新取预览(抹掉区变了,
   *  平面图地址跟着变)和区域。 */
  async function saveNav(change: (zones: NavZone[], rev: number) => NavZone[]): Promise<NavZones> {
    const b = base;
    const now = await api<NavZones>("GET", `${b}/zones`);
    const d = await api<NavZones>("POST", `${b}/zones`, { zones: change(now.zones, now.revision), base_revision: now.revision });
    setNav(b, d);
    if (here(b)) loadMeta();
    return d;
  }

  /** 存、删的结果:还在这张图上才说、才清草稿(换了图的,存是存下了,不在这一页说)。 */
  function after(b: string, f: () => void) {
    if (here(b)) f();
  }

  async function erase() {
    setErasing(false);
    const pts = draft ?? [];
    const b = base;
    try {
      const d = await saveNav((zones, rev) => {
        let id = `e${rev + 1}`;
        while (zones.some((z) => z.id === id)) id += "x";
        return [...zones, { id, kind: "erase", label: t("eraseLabel"), polygon: pts }];
      });
      after(b, () => {
        setDraft(null);
        say(d.auto_confirmed ? t("erasedOk", { rev: d.revision }) : t("erasedNeedsConfirm", { rev: d.revision }));
      });
    } catch (e) {
      after(b, () => say(errText(e)));
    }
  }

  async function unerase(z: NavZone) {
    setUnerasing(null);
    const b = base;
    try {
      const d = await saveNav((zones) => zones.filter((o) => o.id !== z.id));
      after(b, () => say(t("eraseRemoved", { rev: d.revision })));
    } catch (e) {
      after(b, () => say(errText(e)));
    }
  }

  /** 画完的禁行区、限速区、虚拟墙并进整份导航区域。虚拟墙只给线,多边形站点算。 */
  async function addNav(f: NavForm) {
    setNavForm(false);
    const pts = draft ?? [];
    const kind = drawKind;
    const b = base;
    try {
      const d = await saveNav((zones, rev) => {
        let id = `${kind === "wall" ? "w" : kind === "slow" ? "s" : "n"}${rev + 1}`;
        while (zones.some((z) => z.id === id)) id += "x";
        const z: Record<string, unknown> = { id, kind: kind === "slow" ? "slow" : "nogo", label: f.label.trim() };
        if (kind === "wall") z.wall = pts;
        else z.polygon = pts;
        if (kind === "slow") z.max_speed_mps = f.speed;
        else if (f.hard) z.hard = true;
        if (f.temp) z.for_s = Math.round(f.hours * 3600);
        return [...zones, z as unknown as NavZone];
      });
      after(b, () => {
        setDraft(null);
        say(d.auto_confirmed ? t("navSavedOk", { rev: d.revision }) : t("navSavedNeedsConfirm", { rev: d.revision }));
      });
    } catch (e) {
      after(b, () => say(errText(e)));
    }
  }

  async function removeNav(z: NavZone) {
    setRemovingNav(null);
    const b = base;
    try {
      const d = await saveNav((zones) => zones.filter((o) => o.id !== z.id));
      after(b, () => say(d.auto_confirmed ? t("navSavedOk", { rev: d.revision }) : t("navRemoved", { rev: d.revision })));
    } catch (e) {
      after(b, () => say(errText(e)));
    }
  }

  /** 确认这一版(那一句 = 站点给的 confirm_text);一个区域都没有的要明说 empty_ok。 */
  async function confirmNav() {
    const shown = confirming;
    setConfirming(null);
    if (!shown || !here(shown.base)) return;
    const b = shown.base;
    try {
      // 外审 R3:确认的必须是人刚看过的那一份 —— 发之前再拉一次,图、修订、内容、狗上照旧守着的有一样变了
      // 就不确认,把新的摆出来让人重看
      const now = await api<NavZones>("GET", `${b}/zones`);
      if (!here(b)) return;
      if (navFingerprint(now) !== navFingerprint(shown.nav)) {
        setNav(b, now);
        say(t("navChangedLookAgain", { rev: now.revision }));
        return;
      }
      const d = await api<NavZones>("POST", `${b}/zones/confirm`, {
        revision: shown.nav.revision, confirm: true, ...(shown.nav.zones.length === 0 ? { empty_ok: true } : {}),
      });
      setNav(b, d);
      after(b, () => {
        loadMeta();
        say(t("navConfirmedOk", { rev: d.revision }));
      });
    } catch (e) {
      after(b, () => {
        say(errText(e));
        loadNav();
      });
    }
  }

  function startDraw(k: DrawKind) {
    setDrawKind(k);
    setDraft([]);
  }

  // 点的那一处:地图组件给的是预览像素(世界层的 getScreenCTM 倒回去,转正、缩放、平移都算进去了)
  function pick(px: number, py: number) {
    if (!meta) return;
    setDraft((d) => (d ? [...d, toMap(meta, px, py)] : d));
  }

  async function save(name: string) {
    setNaming(false);
    const b = base;
    try {
      const d = await api<{ areas: Area[] }>("POST", `${b}/areas`, { zone: name, points: draft });
      setAreas(b, d.areas);
      after(b, () => {
        setDraft(null);
        say(t("saved"));
      });
    } catch (e) {
      after(b, () => say(errText(e)));
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
              <span class="muted small" data-draw-hint="1">{drawKind === "erase" ? t("eraseDrawHint", { n: draft.length })
                : drawKind === "wall" ? t("vwallDrawHint", { n: draft.length, w: WALL_WIDTH_M })
                : drawKind === "zone" ? t("drawHint", { n: draft.length }) : t("navDrawHint", { n: draft.length })}</span>
              <button type="button" class="btn sm" onClick={() => setDraft(draft.slice(0, -1))} disabled={!draft.length}>{t("undoPoint")}</button>
              <button type="button" class="btn sm" onClick={() => setDraft(null)}>{t("cancel")}</button>
              {drawKind === "erase"
                ? <button type="button" class="btn primary sm" disabled={draft.length < 3} onClick={() => setErasing(true)}>{t("finishErase")}</button>
                : drawKind === "zone"
                  ? <button type="button" class="btn primary sm" disabled={draft.length < 3} onClick={() => setNaming(true)}>{t("finishZone")}</button>
                  : <button type="button" class="btn primary sm" disabled={draft.length < minPts} onClick={() => setNavForm(true)}>{t("finishNav")}</button>}
            </>
          ) : (
            <div class="row draw-buttons">
              <button type="button" class="btn" disabled={!meta || !nav} onClick={() => startDraw("wall")}>{t("drawVWall")}</button>
              <button type="button" class="btn" disabled={!meta || !nav} onClick={() => startDraw("nogo")}>{t("drawNoGo")}</button>
              <button type="button" class="btn" disabled={!meta || !nav} onClick={() => startDraw("slow")}>{t("drawSlow")}</button>
              <button type="button" class="btn" disabled={!meta || !nav} onClick={() => startDraw("erase")}>{t("drawErase")}</button>
              <button type="button" class="btn primary" disabled={!meta} onClick={() => startDraw("zone")}>{t("drawZone")}</button>
            </div>
          )}
        </div>
        <div class="zonesplit">
          <div class="zonemap">
            {!sel || !meta ? <p class="empty">{t("noMap")}</p> : (
              // 防区页只画能改的防区(防区接口,改了当场变);站点预览里的 areas 在地图页上画
              <MapCanvas meta={meta} raster={`${base}/preview.png`} label={t("tabMapsZones")}
                areas={areas.map((a) => ({ name: a.zone, pts: a.points, kind: "zone" as const }))}
                drawing={!!draft} onPick={draft ? pick : undefined}
                world={draft && draft.length > 0 ? <polyline points={poly(meta, draft)}
                  class={drawKind === "erase" ? "draft-line erase" : drawKind === "wall" ? "draft-line vwall" : "draft-line"} /> : null}
                marks={(s) => draft?.map(([x, y], i) => {
                  const [sx, sy] = s.map(x, y);
                  return <circle key={i} cx={sx} cy={sy} r={4} class="draft-pt" />;
                })} />
            )}
          </div>
          <div class="zonelist">
            {nav && <NavPanel nav={nav} list={navList} onRemove={setRemovingNav} onConfirm={() => setConfirming({ base, nav })} />}
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
            <h2>{t("erasedRegions")}</h2>
            <p class="muted small">{t("erasedHint")}</p>
            {erased.length === 0 && <p class="muted small">{t("noErased")}</p>}
            <ul class="erased-list">
              {erased.map((z) => (
                <li key={z.id}>
                  <span class="strong">{z.label || z.id}</span>
                  <span class="muted small">{t("points", { n: z.polygon.length })}</span>
                  <button type="button" class="link push" onClick={() => setUnerasing(z)}>{t("remove")}</button>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>
      {naming && <NameZone onClose={() => setNaming(false)} onSave={save} />}
      {navForm && (drawKind === "nogo" || drawKind === "slow" || drawKind === "wall") && (
        <NavZoneForm kind={drawKind} onClose={() => setNavForm(false)} onSave={(f) => void addNav(f)} />
      )}
      {removingNav && (
        <Confirm text={t("removeNavConfirm", { label: removingNav.label || removingNav.id })} yes={t("remove")} danger
          onNo={() => setRemovingNav(null)} onYes={() => void removeNav(removingNav)} />
      )}
      {confirming && confirming.base === base && (
        <Confirm text={confirming.nav.zones.length === 0 ? t("navConfirmEmpty")
          : t("navConfirmText", { rev: confirming.nav.revision, text: confirming.nav.confirm_text ?? "" })}
          yes={t("navConfirmBtn")} onNo={() => setConfirming(null)} onYes={() => void confirmNav()} />
      )}
      {erasing && <Confirm text={t("eraseConfirm")} yes={t("finishErase")} onNo={() => setErasing(false)} onYes={() => void erase()} />}
      {unerasing && (
        <Confirm text={t("removeEraseConfirm", { label: unerasing.label || unerasing.id })} yes={t("remove")} danger
          onNo={() => setUnerasing(null)} onYes={() => void unerase(unerasing)} />
      )}
      {removing && (
        <Confirm text={t("removeZoneConfirm", { zone: removing })} yes={t("remove")} onNo={() => setRemoving(null)}
          onYes={() => {
            const z = removing;
            setRemoving(null);
            const b = base;
            api<{ areas: Area[] }>("POST", `${b}/areas`, { zone: z, remove: true }).then((d) => setAreas(b, d.areas))
              .catch((e) => after(b, () => say(errText(e))));
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

/** 一个导航区域给人看的一行字:种类(虚拟墙、禁行区、限速区多少)、硬、临时到什么时候。 */
export function navZoneLine(z: NavZone): string {
  const kind = z.wall ? t("kindVWall") : z.kind === "slow" ? t("kindSlow", { v: z.max_speed_mps ?? "?" }) : t("kindNoGo");
  const bits = [kind];
  if (z.hard) bits.push(t("kindHard"));
  if (typeof z.until_ms === "number") {
    const d = new Date(z.until_ms);
    const two = (v: number) => String(v).padStart(2, "0");
    bits.push(t("kindTemp", { t: `${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}` }));
  }
  return bits.join(" · ");
}

/** 右边「狗守的区域」:这一版确认了没有、确认之前狗照旧守着的、列表(删)、确认按钮。 */
function NavPanel({ nav, list, onRemove, onConfirm }: {
  nav: NavZones; list: NavZone[]; onRemove: (z: NavZone) => void; onConfirm: () => void;
}) {
  const current = !!nav.confirmed?.current;
  const held = nav.held ?? [];
  return (
    <section class="navzones" data-navzones="1">
      <h2>{t("navZones")}</h2>
      <p class="muted small">{t("navZonesHint")}</p>
      {current
        ? <p class="small" data-nav-status="ok">{t("navConfirmed", { rev: nav.revision, by: nav.confirmed!.by })}</p>
        : <p class="small warn" data-nav-status="unconfirmed">{t("navUnconfirmed", { rev: nav.revision })}</p>}
      {held.length > 0 && <p class="small warn" data-nav-held="1">{t("navHeld", { names: held.map((z) => z.label || z.id).join(", ") })}</p>}
      {!current && (nav.proposal
        ? <p class="small">{t("navProposalOpen")}</p>
        : <button type="button" class="btn sm primary" onClick={onConfirm}>{t("navConfirmBtn")}</button>)}
      {list.length === 0 && <p class="muted small">{t("noNavZones")}</p>}
      <ul class="navzone-list">
        {list.map((z) => (
          <li key={z.id} data-navzone={z.id}>
            <span class="strong">{z.label || z.id}</span>
            <span class="muted small">{navZoneLine(z)}</span>
            <button type="button" class="link push" onClick={() => onRemove(z)}>{t("remove")}</button>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** 画完禁行区、限速区、虚拟墙填的那几项:名字;硬(禁行、虚拟墙);限速;临时几小时。 */
function NavZoneForm({ kind, onClose, onSave }: { kind: "nogo" | "slow" | "wall"; onClose: () => void; onSave: (f: NavForm) => void }) {
  const [f, setF] = useState<NavForm>({
    label: kind === "wall" ? t("legendVWall") : kind === "slow" ? t("legendSlow") : t("legendNoGo"),
    hard: false, speed: 0.3, temp: false, hours: 2,
  });
  const title = kind === "wall" ? t("navFormTitleVWall") : kind === "slow" ? t("navFormTitleSlow") : t("navFormTitleNoGo");
  const ok = f.label.trim().length > 0 && f.label.trim().length <= 64 && (!f.temp || (f.hours > 0 && f.hours <= 8760))
    && (kind !== "slow" || (f.speed > 0 && f.speed <= 5));
  return (
    <Modal label={title} onClose={onClose}>
      <form class="form" onSubmit={(e) => { e.preventDefault(); if (ok) onSave(f); }}>
        <h2>{title}</h2>
        <label class="field">{t("navName")}<input value={f.label} maxLength={64} autoFocus onInput={(e) => setF({ ...f, label: e.currentTarget.value })} /></label>
        {kind === "slow" ? (
          <label class="field">{t("navSpeed")}<input type="number" min={0.1} max={5} step={0.1} value={f.speed}
            onInput={(e) => setF({ ...f, speed: Number(e.currentTarget.value) })} /></label>
        ) : (
          <label class="field-check"><input type="checkbox" checked={f.hard} onChange={(e) => setF({ ...f, hard: e.currentTarget.checked })} />{t("navHard")}</label>
        )}
        <label class="field-check"><input type="checkbox" checked={f.temp} onChange={(e) => setF({ ...f, temp: e.currentTarget.checked })} />{t("navTemp")}</label>
        {f.temp && (
          <label class="field">{t("navTempHours")}<input type="number" min={1} max={8760} step={1} value={f.hours}
            onInput={(e) => setF({ ...f, hours: Number(e.currentTarget.value) })} /><span class="muted small">{t("navTempHint")}</span></label>
        )}
        <div class="row end">
          <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="submit" class="btn primary" disabled={!ok}>{t("navSave")}</button>
        </div>
      </form>
    </Modal>
  );
}

/** 一份导航区域「给人看的内容」:修订号、整份区域、狗上照旧守着的、确认那一句(确认前后比,外审 R3)。 */
export function navFingerprint(n: NavZones): string {
  return JSON.stringify([n.revision, n.zones, n.held ?? [], n.confirm_text ?? "", !!n.proposal]);
}
