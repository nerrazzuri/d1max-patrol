// 布防时间页(W44 第二期,设计 §5.1「布防时间」):一周七行、0–24 点,色块 = 布防,拖着画、点了删、拖两边改;
// 全部防区一样,或者每个防区单独设;例外日期(节假日、出远门);交班时间、多久没巡过;一个「保存」存整份。
// 星期、日子、时区都由站点算(站点时区),网页只画站点回来的。
import { useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { addBlock, copyDay, fromWire, minutesText, moveEdge, removeBlock, toWire, type Slot, type WireSlot } from "./armgrid";
import { hm, type DutySettings } from "./data";
import { t, type Key } from "./i18n";

type Say = (s: string) => void;
interface Exception { id: number; first_day: string; last_day: string; state: "armed" | "disarmed"; note: string }
interface Schedule { enabled: boolean; slots: WireSlot[]; exceptions: Exception[] }
interface ModeView { mode: string; label: string; source?: "schedule" | "manual"; next_change_ms?: number | null; manual_until_ms?: number | null; zones?: { zone: string }[] }

const DAY_KEYS: Key[] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const HOURS = Array.from({ length: 24 }, (_, i) => i);

function errText(e: unknown): string {
  return t("failed", { why: e instanceof ApiError ? e.message : String(e) });
}

/** 现在谁说了算:按时间表 / 手动到几点回时间表 / 时间表没开。 */
export function whoText(m: ModeView | null, enabled: boolean): string {
  if (!m) return "";
  if (m.source === "schedule") {
    return m.next_change_ms ? t("armWhoSchedule", { s: m.label, t: hm(m.next_change_ms) }) : t("armWhoScheduleNoNext", { s: m.label });
  }
  if (enabled && m.manual_until_ms) return t("armWhoManual", { s: m.label, t: hm(m.manual_until_ms) });
  return t("armWhoOff", { s: m.label });
}

export function ArmingPage({ say, now }: { say: Say; now: number }) {
  const [loaded, setLoaded] = useState<{ enabled: boolean; slots: Slot[]; duty: DutySettings } | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [slots, setSlots] = useState<Slot[]>([]);
  const [duty, setDuty] = useState<DutySettings>({ shift_hours: [7, 19], stale_hours: 2 });
  const [exceptions, setExceptions] = useState<Exception[]>([]);
  const [mode, setMode] = useState<ModeView | null>(null);
  const [perZone, setPerZone] = useState(false);
  const [zone, setZone] = useState("");
  const [sel, setSel] = useState<{ day: number; start: number } | null>(null);
  const [copyFrom, setCopyFrom] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  void now;

  const loadMode = () => api<ModeView>("GET", "/api/mode").then(setMode).catch(() => setMode(null));
  useEffect(() => {
    Promise.all([api<Schedule>("GET", "/api/arming/schedule"), api<DutySettings>("GET", "/api/duty-settings").catch(() => duty)])
      .then(([s, d]) => {
        const sl = fromWire(s.slots ?? []);
        setEnabled(!!s.enabled);
        setSlots(sl);
        setExceptions(s.exceptions ?? []);
        setDuty(d);
        setLoaded({ enabled: !!s.enabled, slots: sl, duty: d });
        setPerZone(sl.some((x) => x.zone !== ""));
      })
      .catch((e) => say(errText(e)));
    void loadMode();
  }, []);

  const dirty = loaded != null && (loaded.enabled !== enabled || JSON.stringify(loaded.slots) !== JSON.stringify(slots)
    || JSON.stringify(loaded.duty) !== JSON.stringify(duty));
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: Event) => { e.preventDefault(); (e as BeforeUnloadEvent).returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const zones = Array.from(new Set([...(mode?.zones ?? []).map((z) => z.zone), ...slots.filter((x) => x.zone).map((x) => x.zone)])).sort();
  const editing = perZone ? zone : "";
  const ownSlots = slots.some((x) => x.zone === editing);
  const followsAll = perZone && zone !== "" && !ownSlots;

  async function save() {
    setBusy(true);
    try {
      await api("POST", "/api/arming/schedule", { enabled, slots: toWire(slots) });
      await api("POST", "/api/duty-settings", duty);
      setLoaded({ enabled, slots, duty });
      say(t("saved"));
      void loadMode();
    } catch (e) {
      say(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section class="arming">
      <div class="panel-head">
        <h1>{t("tabArming")}</h1>
        <button type="button" class="btn primary push arm-save" disabled={busy || !dirty} onClick={save}>{t("save")}</button>
      </div>
      <p class="muted lead">{t("armLead")}</p>
      {dirty && <p class="arm-dirty" role="status">{t("armDirty")}</p>}
      <p class="arm-who">{whoText(mode, loaded?.enabled ?? enabled)}</p>

      <label class="switch">
        <input id="arm-enabled" type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.currentTarget.checked)} />
        {t("armEnabled")}
      </label>

      <div class="seg arm-scope" role="group" aria-label={t("armScope")}>
        <button type="button" aria-pressed={!perZone} onClick={() => { setPerZone(false); setZone(""); setSel(null); }}>{t("armAllSame")}</button>
        <button type="button" aria-pressed={perZone} onClick={() => { setPerZone(true); setSel(null); }}>{t("armPerZone")}</button>
      </div>
      {perZone && (
        <div class="arm-zones chips">
          {zones.length === 0 && <span class="muted small">{t("armNoZones")}</span>}
          {zones.map((z) => (
            <button key={z} type="button" class="chip" aria-pressed={zone === z} onClick={() => { setZone(z); setSel(null); }}>{z}</button>
          ))}
        </div>
      )}
      {followsAll ? (
        <div class="arm-follow">
          <p>{t("armFollowsAll", { z: zone })}</p>
          <button type="button" class="btn" onClick={() => setSlots(copyZone(slots, "", zone))}>{t("armOwnZone")}</button>
        </div>
      ) : (perZone && zone === "") ? <p class="muted">{t("armPickZone")}</p> : (
        <>
          <WeekGrid slots={slots.filter((x) => x.zone === editing)} sel={sel}
            onAdd={(day, a, b) => setSlots(addBlock(slots, editing, day, a, b))}
            onPick={(day, start) => setSel({ day, start })}
            onEdge={(day, start, edge, to) => { const n = moveEdge(slots, editing, day, start, edge, to); setSlots(n); }}
            onCopy={(day) => setCopyFrom(day)} />
          {sel && (
            <div class="arm-selected row">
              <span>{t(DAY_KEYS[sel.day])} {minutesText(sel.start)}–{minutesText(slots.find((x) => x.zone === editing && x.day === sel.day && x.start === sel.start)?.end ?? sel.start)}</span>
              <button type="button" class="btn sm" onClick={() => { setSlots(removeBlock(slots, editing, sel.day, sel.start)); setSel(null); }}>{t("armDeleteBlock")}</button>
              {perZone && zone && ownSlots && (
                <button type="button" class="btn sm" onClick={() => setSlots(slots.filter((x) => x.zone !== zone))}>{t("armBackToAll")}</button>
              )}
            </div>
          )}
          {copyFrom != null && (
            <CopyTo from={copyFrom} onClose={() => setCopyFrom(null)}
              onCopy={(days) => { setSlots(copyDay(slots, editing, copyFrom, days)); setCopyFrom(null); }} />
          )}
        </>
      )}

      <Exceptions list={exceptions} say={say} onChange={setExceptions} />

      <section class="panel arm-duty">
        <h2>{t("armDutyTitle")}</h2>
        <div class="row wrap">
          <label class="field">{t("armShiftA")}
            <select name="shiftA" value={String(duty.shift_hours[0])} onChange={(e) => setDuty({ ...duty, shift_hours: [Number(e.currentTarget.value), duty.shift_hours[1]] })}>
              {HOURS.map((h) => <option key={h} value={String(h)}>{`${String(h).padStart(2, "0")}:00`}</option>)}
            </select>
          </label>
          <label class="field">{t("armShiftB")}
            <select name="shiftB" value={String(duty.shift_hours[1])} onChange={(e) => setDuty({ ...duty, shift_hours: [duty.shift_hours[0], Number(e.currentTarget.value)] })}>
              {HOURS.map((h) => <option key={h} value={String(h)}>{`${String(h).padStart(2, "0")}:00`}</option>)}
            </select>
          </label>
          <label class="field">{t("armStale")}
            <input name="stale" type="number" min={1} max={48} value={duty.stale_hours}
              onInput={(e) => setDuty({ ...duty, stale_hours: Number(e.currentTarget.value) })} />
          </label>
        </div>
        <p class="muted small">{t("armDutyHint")}</p>
      </section>
    </section>
  );
}

function copyZone(slots: Slot[], from: string, to: string): Slot[] {
  return [...slots.filter((x) => x.zone !== to), ...slots.filter((x) => x.zone === from).map((x) => ({ ...x, zone: to }))];
}

function WeekGrid(props: {
  slots: Slot[];
  sel: { day: number; start: number } | null;
  onAdd: (day: number, a: number, b: number) => void;
  onPick: (day: number, start: number) => void;
  onEdge: (day: number, start: number, edge: "start" | "end", to: number) => void;
  onCopy: (day: number) => void;
}) {
  const drag = useRef<{ day: number; from: number; edge?: "start" | "end"; start?: number } | null>(null);
  const [ghost, setGhost] = useState<{ day: number; a: number; b: number } | null>(null);
  const minuteAt = (el: Element, x: number) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 ? Math.max(0, Math.min(1440, ((x - r.left) / r.width) * 1440)) : 0;
  };
  const pct = (m: number) => `${(m / 1440) * 100}%`;
  return (
    <div class="armgrid" role="grid" aria-label={t("tabArming")}>
      <div class="armhours" aria-hidden="true">
        {[0, 6, 12, 18, 24].map((h) => <span key={h} style={{ left: pct(h * 60) }}>{`${h}:00`}</span>)}
      </div>
      {DAY_KEYS.map((k, day) => (
        <div key={k} class="armrow" data-day={day} role="row">
          <span class="armday">{t(k)}</span>
          <div class="armtrack" role="gridcell"
            onPointerDown={(e) => {
              const el = e.currentTarget as HTMLElement;
              const tgt = e.target as HTMLElement;
              if (tgt.classList.contains("armedge")) {
                drag.current = { day, from: 0, edge: tgt.dataset.edge as "start" | "end", start: Number(tgt.dataset.start) };
              } else if (tgt.classList.contains("armblock")) {
                return;
              } else {
                const m = minuteAt(el, e.clientX);
                drag.current = { day, from: m };
                setGhost({ day, a: m, b: m });
              }
              try { el.setPointerCapture?.(e.pointerId); } catch { /* 测试环境没有 */ }
            }}
            onPointerMove={(e) => {
              const d = drag.current;
              if (!d || d.day !== day) return;
              const m = minuteAt(e.currentTarget as HTMLElement, e.clientX);
              if (d.edge && d.start != null) {
                props.onEdge(day, d.start, d.edge, m);
                if (d.edge === "start") d.start = Math.min(Math.round(m / 30) * 30, d.start);
              } else setGhost({ day, a: d.from, b: m });
            }}
            onPointerUp={(e) => {
              const d = drag.current;
              drag.current = null;
              setGhost(null);
              if (!d || d.day !== day || d.edge) return;
              props.onAdd(day, d.from, minuteAt(e.currentTarget as HTMLElement, e.clientX));
            }}>
            {props.slots.filter((s) => s.day === day).map((s) => (
              <div key={s.start} class={props.sel && props.sel.day === day && props.sel.start === s.start ? "armblock on" : "armblock"}
                style={{ left: pct(s.start), width: pct(s.end - s.start) }}
                title={`${minutesText(s.start)}–${minutesText(s.end)}`}
                onClick={() => props.onPick(day, s.start)}>
                <i class="armedge" data-edge="start" data-start={s.start} />
                <span class="armlabel">{minutesText(s.start)}–{minutesText(s.end)}</span>
                <i class="armedge end" data-edge="end" data-start={s.start} />
              </div>
            ))}
            {ghost && ghost.day === day && (
              <div class="armblock ghost" style={{ left: pct(Math.min(ghost.a, ghost.b)), width: pct(Math.abs(ghost.b - ghost.a)) }} />
            )}
          </div>
          <button type="button" class="btn sm arm-copy" onClick={() => props.onCopy(day)}>{t("armCopyTo")}</button>
        </div>
      ))}
    </div>
  );
}

function CopyTo({ from, onCopy, onClose }: { from: number; onCopy: (days: number[]) => void; onClose: () => void }) {
  const [days, setDays] = useState<number[]>([]);
  return (
    <div class="arm-copyto panel">
      <p>{t("armCopyFrom", { d: t(DAY_KEYS[from]) })}</p>
      <div class="row wrap">
        {DAY_KEYS.map((k, i) => i === from ? null : (
          <label key={k}><input type="checkbox" value={String(i)} checked={days.includes(i)}
            onChange={() => setDays(days.includes(i) ? days.filter((x) => x !== i) : [...days, i])} /> {t(k)}</label>
        ))}
      </div>
      <div class="row end">
        <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
        <button type="button" class="btn primary" disabled={days.length === 0} onClick={() => onCopy(days)}>{t("armCopy")}</button>
      </div>
    </div>
  );
}

function Exceptions({ list, say, onChange }: { list: Exception[]; say: Say; onChange: (l: Exception[]) => void }) {
  const [first, setFirst] = useState("");
  const [last, setLast] = useState("");
  const [state, setState] = useState<"armed" | "disarmed">("armed");
  const [note, setNote] = useState("");
  async function add() {
    try {
      const d = await api<Schedule>("POST", "/api/arming/exceptions", { first_day: first, last_day: last || first, state, note });
      onChange(d.exceptions ?? []);
      setFirst(""); setLast(""); setNote("");
    } catch (e) {
      say(errText(e));
    }
  }
  async function remove(id: number) {
    try {
      const d = await api<Schedule>("POST", `/api/arming/exceptions/${enc(String(id))}/remove`, {});
      onChange(d.exceptions ?? []);
    } catch (e) {
      say(errText(e));
    }
  }
  return (
    <section class="panel arm-ex">
      <h2>{t("armExTitle")}</h2>
      <p class="muted small">{t("armExHint")}</p>
      {list.length > 0 && (
        <ul class="plain">
          {list.map((x) => (
            <li key={x.id} class="row">
              <span class="mono">{x.first_day === x.last_day ? x.first_day : `${x.first_day} – ${x.last_day}`}</span>
              <b>{t(x.state === "armed" ? "armExArmed" : "armExDisarmed")}</b>
              {x.note && <span class="muted">{x.note}</span>}
              <button type="button" class="btn sm push" onClick={() => void remove(x.id)}>{t("remove")}</button>
            </li>
          ))}
        </ul>
      )}
      <div class="row wrap">
        <label class="field">{t("from")}<input name="first" type="date" value={first} onInput={(e) => setFirst(e.currentTarget.value)} /></label>
        <label class="field">{t("to")}<input name="last" type="date" value={last} onInput={(e) => setLast(e.currentTarget.value)} /></label>
        <label class="field">{t("armExState")}
          <select name="state" value={state} onChange={(e) => setState(e.currentTarget.value as "armed" | "disarmed")}>
            <option value="armed">{t("armExArmed")}</option>
            <option value="disarmed">{t("armExDisarmed")}</option>
          </select>
        </label>
        <label class="field">{t("note")}<input name="note" maxLength={200} value={note} onInput={(e) => setNote(e.currentTarget.value)} /></label>
        <button type="button" class="btn" disabled={!first} onClick={() => void add()}>{t("armExAdd")}</button>
      </div>
    </section>
  );
}
