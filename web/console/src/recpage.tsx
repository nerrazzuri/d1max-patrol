// 录像页(C-B3,视觉规范 VMS 惯例):选狗、前后、哪一天 → 一条 24 小时的时间轴(有录像的地方是实的,
// 告警在轴上有记号)→ 点哪儿放哪儿,一段放完自动接下一段。画面上写的是**录像里的时刻**,标 Recorded,
// 跟实时画面分得清。值班的人能把一段标成「留着」(过了留存期也不删)。
// 正在放的时候换前后:接着同一刻放另一路(看同一件事的另一个角度)。
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { api, enc } from "./api";
import { camLabel, type Cam } from "./camera";
import { hms } from "./data";
import { alertTitle, lang, t } from "./i18n";
import { duration, type Alert } from "./model";
import {
  ascending,
  covered,
  dateInput,
  dayRange,
  firstAfter,
  minuteOf,
  nextSegment,
  prevSegment,
  segmentAt,
  spans,
  type Recording,
} from "./recordings";

const CAMS: Cam[] = ["front", "back"];
const SPEEDS = [1, 2, 4];

export function canReview(role: string): boolean {
  return role === "admin" || role === "guard";
}

export function RecordingsPage(props: { robots: string[]; role: string; now: number; robot: string | null; at: number | null; say: (s: string) => void }) {
  const [robot, setRobot] = useState(props.robot && props.robots.includes(props.robot) ? props.robot : props.robots[0] ?? "");
  const [cam, setCam] = useState<Cam>("front");
  const [date, setDate] = useState(dateInput(props.at ?? props.now));
  const [rows, setRows] = useState<Recording[] | null>(null);
  const [segS, setSegS] = useState(60);
  const [state, setState] = useState<"loading" | "ok" | "off" | "failed">("loading");
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [cur, setCur] = useState<Recording | null>(null);
  const [head, setHead] = useState<number | null>(props.at);
  const [speed, setSpeed] = useState(1);
  const [broken, setBroken] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const seek = useRef<number>(0);
  const want = useRef<number | null>(props.at);
  const range = dayRange(date);

  useEffect(() => {
    if (!robot && props.robots.length) setRobot(props.robots[0]);
  }, [props.robots.join(",")]);

  useEffect(() => {
    setRows(null);
    setCur(null);
    setState("loading");
    if (!robot || !range) return;
    let live = true;
    api<{ recordings: Recording[]; segment_s: number }>("GET",
      `/api/recordings?robot=${enc(robot)}&camera=${cam}&since=${range.since}&until=${range.until - 1}&limit=2000`)
      .then((d) => {
        if (!live) return;
        const asc = ascending(d.recordings);
        setSegS(d.segment_s);
        setRows(asc);
        setState("ok");
        const at = want.current;
        want.current = null;
        if (at != null) play(asc, at, d.segment_s);
      })
      .catch((e) => {
        if (!live) return;
        setRows([]);
        setState(e && e.status === 404 ? "off" : "failed");
      });
    api<{ alerts: Alert[] }>("GET", "/api/alerts?all=1&limit=500")
      .then((d) => live && setAlerts(d.alerts.filter((a) => a.robot === robot && a.level !== "P3" && a.first_ms >= range.since && a.first_ms < range.until)))
      .catch(() => live && setAlerts([]));
    return () => { live = false; };
  }, [robot, cam, date]);

  const sp = useMemo(() => spans(rows ?? [], segS), [rows, segS]);

  function play(list: Recording[], ms: number, seg = segS) {
    const hit = segmentAt(list, ms, seg);
    const rec = hit?.rec ?? firstAfter(list, ms);
    if (!rec) {
      props.say(t("recNothingAfter", { t: hms(ms) }));
      return;
    }
    seek.current = hit ? hit.offsetS : 0;
    setBroken(false);
    setHead(hit ? ms : rec.start_ms);
    if (cur?.id === rec.id && video.current) {
      video.current.currentTime = seek.current;
      void video.current.play().catch(() => {});
    } else setCur(rec);
  }

  const step = (rec: Recording | null) => {
    if (!rec) return;
    seek.current = 0;
    setBroken(false);
    setHead(rec.start_ms);
    setCur(rec);
  };

  const onAxis = (e: MouseEvent) => {
    if (!range || !rows) return;
    const box = (e.currentTarget as SVGElement).getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (e.clientX - box.left) / box.width));
    play(rows, range.since + f * (range.until - range.since));
  };
  const onAxisKey = (e: KeyboardEvent) => {
    if (!rows || head == null) return;
    const d = e.key === "ArrowLeft" ? -60_000 : e.key === "ArrowRight" ? 60_000 : 0;
    if (!d) return;
    e.preventDefault();
    play(rows, head + d);
  };

  const keep = async () => {
    if (!cur) return;
    try {
      const d = await api<{ recording: Recording }>("POST", `/api/recordings/${cur.id}/keep`, { keep: !cur.keep });
      setCur(d.recording);
      setRows((rs) => rs && rs.map((r) => (r.id === d.recording.id ? d.recording : r)));
    } catch (e) {
      props.say(t("failed", { why: e instanceof Error ? e.message : String(e) }));
    }
  };

  const total = range ? covered(sp, range.since, range.until) : 0;
  const kept = (rows ?? []).filter((r) => r.keep).length;

  return (
    <div class="recpage">
      <div class="recbar">
        <label>{t("recRobot")}
          <select value={robot} onChange={(e) => setRobot((e.target as HTMLSelectElement).value)}>
            {props.robots.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <div class="seg" role="group" aria-label={t("recCamera")}>
          {CAMS.map((c) => <button key={c} type="button" aria-pressed={c === cam} onClick={() => { if (cur && head != null) want.current = head; setCam(c); }}>{t(c)}</button>)}
        </div>
        <label>{t("recDate")}
          <input type="date" value={date} max={dateInput(props.now)} onChange={(e) => setDate((e.target as HTMLInputElement).value)} />
        </label>
        <label>{t("recJump")}
          <input type="time" step="1" disabled={!rows || !range}
            onChange={(e) => {
              const v = (e.target as HTMLInputElement).value.split(":").map(Number);
              if (range && rows && v.length >= 2 && v.every((n) => !Number.isNaN(n))) play(rows, range.since + ((v[0] * 60 + v[1]) * 60 + (v[2] ?? 0)) * 1000);
            }} />
        </label>
        <span class="muted push">
          {state === "ok" && rows && t("recSummary", { d: duration(total), n: rows.length, k: kept })}
        </span>
      </div>

      <div class="recview">
        <div class={cur && !broken ? "well rec" : "well rec idle"}>
          {cur && !broken && (
            <video ref={video} key={cur.id} class="feed" src={`/api/recordings/${cur.id}/video`} autoplay playsInline controls
              aria-label={`${camLabel(robot, cam)} ${t("recorded")}`}
              onLoadedMetadata={(e) => {
                const v = e.currentTarget as HTMLVideoElement;
                v.playbackRate = speed;
                if (seek.current > 0) v.currentTime = Math.min(seek.current, Math.max(0, v.duration - 0.1));
              }}
              onTimeUpdate={(e) => setHead(cur.start_ms + (e.currentTarget as HTMLVideoElement).currentTime * 1000)}
              onEnded={() => step(nextSegment(rows ?? [], cur))}
              onError={() => setBroken(true)} />
          )}
          {(!cur || broken) && (
            <p class="empty">
              {broken ? t("recBroken") : state === "loading" ? t("loading") : state === "off" ? t("recOff") : state === "failed" ? t("recFailed")
                : rows && rows.length === 0 ? t("recNone") : t("recPick")}
            </p>
          )}
          {cur && (
            <div class="osd top">
              <span>{camLabel(robot, cam)}</span>
              <span>{date} {head != null ? hms(head) : ""}</span>
            </div>
          )}
          {cur && !broken && <div class="osd bottom recosd"><span class="osd-status">{t("recorded")}{cur.keep ? ` · ${t("recKept")}` : ""}</span></div>}
        </div>
        <div class="recctl">
          <button type="button" class="btn sm" disabled={!cur || !prevSegment(rows ?? [], cur)} onClick={() => cur && step(prevSegment(rows ?? [], cur))}>{t("recPrev")}</button>
          <button type="button" class="btn sm" disabled={!cur || !nextSegment(rows ?? [], cur)} onClick={() => cur && step(nextSegment(rows ?? [], cur))}>{t("recNext")}</button>
          <div class="seg" role="group" aria-label={t("recSpeed")}>
            {SPEEDS.map((s) => (
              <button key={s} type="button" aria-pressed={s === speed}
                onClick={() => { setSpeed(s); if (video.current) video.current.playbackRate = s; }}>{s}×</button>
            ))}
          </div>
          {canReview(props.role) && (
            <button type="button" class="btn sm" aria-pressed={!!cur?.keep} disabled={!cur} onClick={keep}>
              {cur?.keep ? t("recUnkeep") : t("recKeep")}
            </button>
          )}
          <span class="muted small">{t("recKeepHint")}</span>
        </div>
      </div>

      {range && (
        <div class="timeline">
          <svg class="axis" viewBox="0 0 1440 44" preserveAspectRatio="none" role="slider" tabIndex={0}
            aria-label={t("recAxis")} aria-valuemin={0} aria-valuemax={1440}
            aria-valuenow={head != null ? Math.round(minuteOf(head, range.since)) : 0}
            aria-valuetext={head != null ? hms(head) : ""} onClick={onAxis} onKeyDown={onAxisKey}>
            <rect x="0" y="12" width="1440" height="20" class="axis-bg" />
            {Array.from({ length: 23 }, (_, i) => <line key={i} x1={(i + 1) * 60} x2={(i + 1) * 60} y1="8" y2="36" class="axis-tick" />)}
            {sp.map((s) => {
              const a = minuteOf(s.from, range.since), b = minuteOf(s.to, range.since);
              return <rect key={s.from} x={a} y="12" width={Math.max(0.5, b - a)} height="20" class="axis-rec" />;
            })}
            {(rows ?? []).filter((r) => r.keep).map((r) => (
              <rect key={r.id} x={minuteOf(r.start_ms, range.since)} y="32" width={Math.max(1, segS / 60)} height="4" class="axis-keep" />
            ))}
            {alerts.map((a) => {
              const x = minuteOf(a.first_ms, range.since);
              return <line key={a.key} x1={x} x2={x} y1="2" y2="42" class={a.level === "P1" ? "axis-p1" : "axis-p2"} />;
            })}
            {head != null && head >= range.since && head < range.until && (
              <line x1={minuteOf(head, range.since)} x2={minuteOf(head, range.since)} y1="0" y2="44" class="axis-head" />
            )}
          </svg>
          <div class="hours mono" aria-hidden="true">
            {Array.from({ length: 12 }, (_, i) => <span key={i}>{String(i * 2).padStart(2, "0")}:00</span>)}
          </div>
          <div class="legend-row muted small">
            <span class="key rec" />{t("recLegendRec")}
            <span class="key gap" />{t("recLegendGap")}
            <span class="key p1" />P1
            <span class="key p2" />P2
            <span class="key keep" />{t("recKept")}
          </div>
        </div>
      )}

      {alerts.length > 0 && (
        <table class="rectable">
          <caption>{t("recAlarms")}</caption>
          <tbody>
            {alerts.slice().sort((a, b) => a.first_ms - b.first_ms).map((a) => (
              <tr key={a.key}>
                <td class="mono">{hms(a.first_ms)}</td>
                <td class={`pri ${a.level}`}>{a.level}</td>
                <td>{alertTitle(lang.value, a.kind, a.title)}</td>
                <td><button type="button" class="link" disabled={!rows} onClick={() => rows && play(rows, a.first_ms - 10_000)}>{t("recPlayFrom")}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
