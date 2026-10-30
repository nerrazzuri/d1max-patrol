// 授权和临时撤防页(W44 第二期,设计 §5.1「授权和临时撤防」;原来的「名单」页):授权期间机器人在这些防区看到人不报警,只记一笔。
// 上面:现在生效的授权(谁、在哪、到几点),能提前结束;大按钮「加一个授权」(每周重复,或只这一天);全部授权;
// 临时撤防单独一块(选防区和结束时间,到点自动恢复)。人脸、车牌名单不在这一轮(设计 §2)。
import { useEffect, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { hm } from "./data";
import { Confirm, Modal } from "./dialogs";
import { t, type Key } from "./i18n";

type Say = (s: string) => void;
export interface Authz {
  id: number; name: string; zones: string[]; days: number; start: string; end: string; until_ms: number | null; note: string;
  created_by: string; active_now?: boolean; on_today?: boolean;
}
interface ModeView { mode: string; label?: string; prev_mode?: string; until_ms?: number | null; visitor_zones?: string[]; zones?: { zone: string }[] }

const DAY_KEYS: Key[] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const VISITOR_HOURS = [1, 2, 4, 8, 24];

export function daysText(days: number): string {
  if (days === 127) return t("everyDay");
  if (days === 0b0011111) return t("weekdays");
  if (days === 0b1100000) return t("weekends");
  return DAY_KEYS.filter((_, i) => days >> i & 1).map((k) => t(k)).join(", ");
}

function errText(e: unknown): string {
  return t("failed", { why: e instanceof ApiError ? e.message : String(e) });
}

const where = (a: Authz) => (a.zones.length ? a.zones.join("、") : t("wholeSite"));

export function AccessPage({ say, now }: { say: Say; now: number }) {
  const [rows, setRows] = useState<Authz[]>([]);
  const [mode, setMode] = useState<ModeView | null>(null);
  const [adding, setAdding] = useState(false);
  const [ending, setEnding] = useState<Authz | null>(null);
  const [removing, setRemoving] = useState<Authz | null>(null);
  void now;
  const load = () => api<{ authorizations: Authz[] }>("GET", "/api/lists/authorizations").then((d) => setRows(d.authorizations ?? [])).catch((e) => say(errText(e)));
  const loadMode = () => api<ModeView>("GET", "/api/mode").then(setMode).catch(() => setMode(null));
  useEffect(() => { void load(); void loadMode(); }, []);
  const zones = (mode?.zones ?? []).map((z) => z.zone);
  const live = rows.filter((r) => r.active_now);

  async function post(path: string, body: unknown) {
    try {
      const d = await api<{ authorizations?: Authz[] }>("POST", path, body);
      if (d.authorizations) setRows(d.authorizations);
      else void load();
    } catch (e) {
      say(errText(e));
    }
  }

  return (
    <section class="access">
      <div class="panel-head">
        <h1>{t("tabAccess")}</h1>
        <button type="button" class="btn primary push" onClick={() => setAdding(true)}>{t("addAuthorization")}</button>
      </div>
      <p class="muted lead">{t("authorizationsHint")}</p>

      <section class="panel acc-now">
        <h2>{t("accNow")}</h2>
        {live.length === 0 ? <p class="muted">{t("accNowNone")}</p> : (
          <ul class="plain">
            {live.map((a) => (
              <li key={a.id} class="row">
                <b>{a.name}</b><span>{where(a)}</span><span class="muted">{t("accUntil", { t: a.end })}</span>
                <button type="button" class="btn sm push" onClick={() => setEnding(a)}>{t("accEndNow")}</button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <Visitor mode={mode} zones={zones} say={say} onDone={loadMode} />

      <section class="panel acc-all">
        <h2>{t("accAll")}</h2>
        <table class="grid">
          <thead><tr><th>{t("who")}</th><th>{t("where")}</th><th>{t("days")}</th><th>{t("timeRange")}</th><th>{t("validUntil")}</th><th>{t("colActions")}</th></tr></thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={6} class="muted">{t("noAuthorizations")}</td></tr>}
            {rows.map((r) => (
              <tr key={r.id}>
                <td class="strong">{r.name}{r.note ? <span class="muted small"> · {r.note}</span> : null}</td>
                <td>{where(r)}</td>
                <td>{daysText(r.days)}</td>
                <td class="mono">{r.start}–{r.end}</td>
                <td class="mono">{r.until_ms ? new Date(r.until_ms).toLocaleDateString("sv-SE") : "—"}</td>
                <td class="actions"><button type="button" class="btn sm" onClick={() => setRemoving(r)}>{t("remove")}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {adding && <AddAuthz zones={zones} onClose={() => setAdding(false)} say={say} onDone={() => { setAdding(false); void load(); say(t("saved")); }} />}
      {ending && (
        <Confirm text={t("accEndConfirm", { name: ending.name })} yes={t("accEndYes")} onNo={() => setEnding(null)}
          onYes={() => { const a = ending; setEnding(null); void post(`/api/lists/authorizations/${enc(String(a.id))}/end`, {}); }} />
      )}
      {removing && (
        <Confirm text={t("removeAuthzConfirm", { name: removing.name })} yes={t("remove")} onNo={() => setRemoving(null)}
          onYes={() => { const r = removing; setRemoving(null); void post(`/api/lists/authorizations/${enc(String(r.id))}/remove`, {}); }} />
      )}
    </section>
  );
}

function Visitor({ mode, zones, say, onDone }: { mode: ModeView | null; zones: string[]; say: Say; onDone: () => void }) {
  const [picked, setPicked] = useState<string[]>([]);
  const [hours, setHours] = useState(4);
  if (!mode) return null;
  const on = mode.mode === "visitor";
  async function set(body: Record<string, unknown>) {
    try {
      await api("POST", "/api/mode", body);
      say(t("saved"));
      onDone();
    } catch (e) {
      say(errText(e));
    }
  }
  return (
    <section class="panel acc-visitor">
      <h2>{t("modeVisitor")}</h2>
      <p class="muted small">{t("accVisitorHint")}</p>
      {on ? (
        <div class="row">
          <b>{t("accVisitorOn", { t: mode.until_ms ? hm(mode.until_ms) : "—" })}</b>
          {(mode.visitor_zones ?? []).length > 0 && <span class="muted">{(mode.visitor_zones ?? []).join("、")}</span>}
          <button type="button" class="btn push" onClick={() => void set({ mode: mode.prev_mode || "armed" })}>{t("accVisitorEnd")}</button>
        </div>
      ) : (
        <>
          <fieldset class="checks row-wrap">
            <legend>{t("accVisitorZones")}</legend>
            {zones.length === 0 && <span class="muted small">{t("armNoZones")}</span>}
            {zones.map((z) => (
              <label key={z}><input type="checkbox" value={z} checked={picked.includes(z)}
                onChange={() => setPicked(picked.includes(z) ? picked.filter((x) => x !== z) : [...picked, z])} /> {z}</label>
            ))}
          </fieldset>
          <div class="row">
            <label class="field">{t("accVisitorFor")}
              <select name="hours" value={String(hours)} onChange={(e) => setHours(Number(e.currentTarget.value))}>
                {VISITOR_HOURS.map((h) => <option key={h} value={String(h)}>{t("accHours", { n: h })}</option>)}
              </select>
            </label>
            <button type="button" class="btn primary" onClick={() => void set({ mode: "visitor", zones: picked, minutes: hours * 60 })}>{t("accVisitorStart")}</button>
          </div>
        </>
      )}
    </section>
  );
}

function AddAuthz({ zones, onClose, onDone, say }: { zones: string[]; onClose: () => void; onDone: () => void; say: Say }) {
  const [name, setName] = useState("");
  const [site, setSite] = useState(zones.length === 0);
  const [picked, setPicked] = useState<string[]>([]);
  const [repeat, setRepeat] = useState(true);
  const [days, setDays] = useState(0b0011111);
  const [date, setDate] = useState("");
  const [start, setStart] = useState("09:00");
  const [end, setEnd] = useState("18:00");
  const [until, setUntil] = useState("");
  const [note, setNote] = useState("");
  async function submit(e: Event) {
    e.preventDefault();
    let d = days;
    let untilMs: number | null = until ? new Date(`${until}T23:59:59`).getTime() : null;
    if (!repeat) {
      if (!date) return;
      const at = new Date(`${date}T00:00:00`);
      d = 1 << ((at.getDay() + 6) % 7);                  // 星期一 = 位 0(站点同一个规矩)
      untilMs = new Date(`${date}T23:59:59`).getTime();
    }
    try {
      await api("POST", "/api/lists/authorizations", { name, zones: site ? [] : picked, days: d, start, end, note, until_ms: untilMs });
      onDone();
    } catch (x) {
      say(errText(x));
    }
  }
  return (
    <Modal label={t("addAuthorization")} onClose={onClose}>
      <form class="form acc-add" onSubmit={submit}>
        <label class="field">{t("who")}<input name="who" value={name} maxLength={64} required placeholder={t("whoPlaceholder")} onInput={(e) => setName(e.currentTarget.value)} /></label>
        <fieldset class="checks">
          <legend>{t("where")}</legend>
          <label><input type="radio" name="scope" checked={site} onChange={() => setSite(true)} /> {t("wholeSite")}</label>
          <label><input type="radio" name="scope" checked={!site} disabled={zones.length === 0} onChange={() => setSite(false)} /> {t("someZones")}</label>
          {!site && zones.map((z) => (
            <label key={z} class="indent"><input type="checkbox" checked={picked.includes(z)}
              onChange={(e) => setPicked(e.currentTarget.checked ? [...picked, z] : picked.filter((x) => x !== z))} /> {z}</label>
          ))}
        </fieldset>
        <label class="switch"><input type="checkbox" name="repeat" checked={repeat} onChange={(e) => setRepeat(e.currentTarget.checked)} /> {t("accRepeat")}</label>
        {repeat ? (
          <fieldset class="checks row-wrap">
            <legend>{t("days")}</legend>
            {DAY_KEYS.map((k, i) => (
              <label key={k}><input type="checkbox" name={`day${i}`} checked={!!(days >> i & 1)} onChange={() => setDays(days ^ (1 << i))} /> {t(k)}</label>
            ))}
          </fieldset>
        ) : (
          <label class="field">{t("accOnDay")}<input name="date" type="date" value={date} required onInput={(e) => setDate(e.currentTarget.value)} /></label>
        )}
        <div class="row">
          <label class="field">{t("from")}<input type="time" value={start} required onInput={(e) => setStart(e.currentTarget.value)} /></label>
          <label class="field">{t("to")}<input type="time" value={end} required onInput={(e) => setEnd(e.currentTarget.value)} /></label>
        </div>
        <p class="muted small">{t("overnightHint")}</p>
        {repeat && <label class="field">{t("validUntil")} ({t("optional")})<input type="date" value={until} onInput={(e) => setUntil(e.currentTarget.value)} /></label>}
        <label class="field">{t("note")} ({t("optional")})<input value={note} maxLength={200} onInput={(e) => setNote(e.currentTarget.value)} /></label>
        <div class="row end">
          <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="submit" class="btn primary" disabled={!name.trim() || (repeat && days === 0) || (!repeat && !date) || (!site && picked.length === 0)}>{t("addAuthorization")}</button>
        </div>
      </form>
    </Modal>
  );
}
