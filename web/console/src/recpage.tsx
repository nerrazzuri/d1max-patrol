// 录像页(C-B3;W44 改版,设计 §4.4)。上面:日期、跳到几点、选机器人(按钮)、选哪一路(前 / 后 / 热像)。
// 左边播放器:大画面 + 另一路小窗(同一刻),左上角写机器人、哪一路、日期时间;播放、倍速、±10 秒、截图、
// 导出这一段(能导出的人)、留作证据(值班的人)。右边地图:那一刻机器人在哪,之前实线、之后虚线,点轨迹跳到
// 它经过那里的时刻。底下时间轴每台一行(有录像的段、报警、需要帮忙),共用一根播放线,滚轮拉宽拉窄。
// 画面上写的是**录像里的时刻**;放完一段自动接下一段。「留着」的回包只改问的那一段(外审 I1)。
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { api, enc } from "./api";
import { camLabel, type Cam } from "./camera";
import { hms, nameOf } from "./data";
import { t } from "./i18n";
import { MapCanvas } from "./mapcanvas";
import { toPx, type PreviewMeta } from "./mapview";
import { duration, type Alert } from "./model";
import {
  applyKeep,
  ascending,
  belongs,
  clockSeconds,
  covered,
  dateInput,
  dayRange,
  firstAfter,
  lanes,
  nearestTime,
  nextSegment,
  poseAt,
  prevSegment,
  segmentAt,
  spans,
  viewSpan,
  type Recording,
  type TrackPt,
} from "./recordings";
import { pxToMap } from "./routes";

const CAMS: Cam[] = ["front", "back"];
const SPEEDS = [1, 2, 4];

export function canReview(role: string): boolean {
  return role === "admin" || role === "engineer" || role === "guard";
}

/** 导出证据只给管理员(最高权限)(站点 permissions.EXPORT)。 */
export function canExport(role: string): boolean {
  return role === "admin" || role === "engineer";
}

export function RecordingsPage(props: { robots: string[]; role: string; now: number; robot: string | null; at: number | null; say: (s: string) => void; camsOf?: (robot: string) => Cam[] }) {
  const [robot, setRobot] = useState(props.robot && props.robots.includes(props.robot) ? props.robot : props.robots[0] ?? "");
  const [cam, setCam] = useState<Cam>("front");
  // 这台机器人有哪几路(W40:装了热像的多一路);换到没有这一路的就回到前面那路
  const cams = props.camsOf?.(robot) ?? CAMS;
  useEffect(() => {
    if (!cams.includes(cam)) setCam("front");
  }, [robot, cams.join(",")]);
  const pipCam: Cam = cam === "front" ? "back" : "front";
  const [date, setDate] = useState(dateInput(props.at ?? props.now));
  const [rows, setRows] = useState<Recording[] | null>(null);
  const [pipRows, setPipRows] = useState<Recording[]>([]);
  const [allRows, setAllRows] = useState<Recording[]>([]);
  const [segS, setSegS] = useState(60);
  const [state, setState] = useState<"loading" | "ok" | "off" | "failed">("loading");
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [track, setTrack] = useState<TrackPt[]>([]);
  const [cur, setCur] = useState<Recording | null>(null);
  const [head, setHead] = useState<number | null>(props.at);
  const [speed, setSpeed] = useState(1);
  const [broken, setBroken] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const pip = useRef<HTMLVideoElement>(null);
  const seek = useRef<number>(0);
  const want = useRef<number | null>(props.at);
  /** 选择(机器人、哪一路、哪一天)换了几次:在途的请求回来时对不上就不许动页面(C-B3 外审 I1)。 */
  const sel = useRef(0);
  const range = dayRange(date);
  const [view, setView] = useState<{ since: number; until: number } | null>(range);
  useEffect(() => setView(dayRange(date)), [date]);

  useEffect(() => {
    if (!robot && props.robots.length) setRobot(props.robots[0]);
  }, [props.robots.join(",")]);

  useEffect(() => {
    sel.current++;
    setRows(null);
    setPipRows([]);
    setCur(null);
    setState("loading");
    if (!robot || !range) return;
    let live = true;
    const span = `since=${range.since}&until=${range.until - 1}`;
    api<{ recordings: Recording[]; segment_s: number }>("GET", `/api/recordings?robot=${enc(robot)}&camera=${cam}&${span}&limit=2000`)
      .then((d) => {
        if (!live) return;
        const asc = ascending(d.recordings ?? []);
        setSegS(d.segment_s ?? 60);
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
    api<{ recordings: Recording[] }>("GET", `/api/recordings?robot=${enc(robot)}&camera=${pipCam}&${span}&limit=2000`)
      .then((d) => live && setPipRows(ascending(d.recordings ?? []))).catch(() => live && setPipRows([]));
    api<{ points: TrackPt[] }>("GET", `/api/robots/${enc(robot)}/track?since=${range.since}&until=${Math.min(range.until, range.since + 86_400_000)}`)
      .then((d) => live && setTrack(Array.isArray(d.points) ? d.points : [])).catch(() => live && setTrack([]));
    return () => { live = false; };
  }, [robot, cam, date]);

  // 时间轴:这一天所有机器人这一路的段、给保安看的报警(换机器人不重拉)
  useEffect(() => {
    if (!range) return;
    let live = true;
    api<{ recordings: Recording[] }>("GET", `/api/recordings?camera=${cam}&since=${range.since}&until=${range.until - 1}&limit=5000`)
      .then((d) => live && setAllRows(d.recordings ?? [])).catch(() => live && setAllRows([]));
    api<{ alerts: Alert[] }>("GET", "/api/alerts?all=1&limit=500")
      .then((d) => live && setAlerts((d.alerts ?? []).filter((a) => a.level !== "P3" && a.first_ms >= range.since && a.first_ms < range.until)))
      .catch(() => live && setAlerts([]));
    return () => { live = false; };
  }, [cam, date]);

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

  /** 换一台机器人,接着同一刻放。 */
  const switchTo = (id: string, at: number | null) => {
    if (id === robot) {
      if (at != null && rows) play(rows, at);
      return;
    }
    want.current = at;
    setRobot(id);
  };

  const keep = async () => {
    if (!cur) return;
    const gen = sel.current, id = cur.id;
    try {
      const d = await api<{ recording: Recording }>("POST", `/api/recordings/${id}/keep`, { keep: !cur.keep });
      // 只改问的那一段;回来时选择换了就丢掉。正在放的换成了别的段:不许被这个回包换回去
      const current = gen === sel.current;
      setRows((rs) => applyKeep({ rows: rs, cur: null }, id, d.recording, current).rows);
      setCur((c) => applyKeep({ rows: null, cur: c }, id, d.recording, current).cur);
    } catch (e) {
      if (gen === sel.current) props.say(t("failed", { why: e instanceof Error ? e.message : String(e) }));
    }
  };

  const snap = () => {
    const v = video.current;
    if (!v || !v.videoWidth) return;
    try {
      const c = document.createElement("canvas");
      c.width = v.videoWidth;
      c.height = v.videoHeight;
      c.getContext("2d")?.drawImage(v, 0, 0);
      const a = document.createElement("a");
      a.href = c.toDataURL("image/png");
      a.download = `${robot}-${cam}-${date}-${head != null ? hms(head).replace(/:/g, "") : ""}.png`;
      a.click();
    } catch {
      props.say(t("recSnapFailed"));
    }
  };

  // 画面只放、只标**这台机器人这一路**的段(段自己带着是谁的)
  const shown = belongs(cur, robot, cam) ? cur : null;
  const pipSeg = head != null ? segmentAt(pipRows, head, segS) : null;
  // 小窗跟着大画面走:差 2 秒以上就对一下
  useEffect(() => {
    const p = pip.current;
    if (!p || !pipSeg) return;
    if (Math.abs(p.currentTime - pipSeg.offsetS) > 2) {
      try { p.currentTime = pipSeg.offsetS; } catch { /* 还没载入 */ }
    }
  }, [head, pipSeg?.rec.id]);

  const total = range ? covered(sp, range.since, range.until) : 0;
  const kept = (rows ?? []).filter((r) => r.keep).length;
  const ls = range ? lanes(props.robots, allRows, alerts, range, segS) : [];
  return (
    <div class="recpage">
      <div class="recbar">
        <label>{t("recDate")}
          <input type="date" value={date} max={dateInput(props.now)} onChange={(e) => setDate((e.target as HTMLInputElement).value)} />
        </label>
        <label>{t("recJump")}
          <input type="text" class="mono hhmm wide" inputMode="numeric" maxLength={8} placeholder="HH:MM:SS" disabled={!rows || !range}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              const s = clockSeconds((e.target as HTMLInputElement).value);
              if (s == null) props.say(t("recBadTime"));
              else if (range && rows) play(rows, range.since + s * 1000);
            }} />
        </label>
        <div class="seg recrobots" role="group" aria-label={t("recRobot")}>
          {props.robots.map((r) => (
            <button key={r} type="button" aria-pressed={r === robot} onClick={() => switchTo(r, cur && head != null ? head : null)}>{nameOf(r)}</button>
          ))}
        </div>
        <div class="seg" role="group" aria-label={t("recCamera")}>
          {cams.map((c) => <button key={c} type="button" aria-pressed={c === cam} onClick={() => { if (cur && head != null) want.current = head; setCam(c); }}>{t(c)}</button>)}
        </div>
        <span class="muted push">
          {state === "ok" && rows && t("recSummary", { d: duration(total), n: rows.length, k: kept })}
        </span>
      </div>

      <div class="recbody">
        <div class="recview">
          <div class={shown && !broken ? "well rec" : "well rec idle"}>
            {shown && !broken && (
              <video ref={video} key={shown.id} class="feed" src={`/api/recordings/${shown.id}/video`} autoplay playsInline
                aria-label={`${camLabel(shown.robot_id, shown.camera as Cam)} ${t("recorded")}`}
                onLoadedMetadata={(e) => {
                  const v = e.currentTarget as HTMLVideoElement;
                  v.playbackRate = speed;
                  if (seek.current > 0) v.currentTime = Math.min(seek.current, Math.max(0, v.duration - 0.1));
                }}
                onPlay={() => void pip.current?.play().catch(() => {})}
                onPause={() => pip.current?.pause()}
                onTimeUpdate={(e) => setHead(shown.start_ms + (e.currentTarget as HTMLVideoElement).currentTime * 1000)}
                onEnded={() => step(nextSegment(rows ?? [], shown))}
                onError={() => setBroken(true)} />
            )}
            {(!shown || broken) && (
              <p class="empty">
                {broken ? t("recBroken") : state === "loading" ? t("loading") : state === "off" ? t("recOff") : state === "failed" ? t("recFailed")
                  : rows && rows.length === 0 ? t("recNone") : t("recPick")}
              </p>
            )}
            {shown && (
              <div class="osd top">
                <span>{camLabel(shown.robot_id, shown.camera as Cam)}</span>
                <span>{date} {head != null ? hms(head) : ""}</span>
              </div>
            )}
            {shown && !broken && pipSeg && (
              <div class="recpip">
                <video ref={pip} key={pipSeg.rec.id} src={`/api/recordings/${pipSeg.rec.id}/video`} muted autoplay playsInline
                  aria-label={camLabel(robot, pipCam)}
                  onLoadedMetadata={(e) => { (e.currentTarget as HTMLVideoElement).currentTime = pipSeg.offsetS; }} />
              </div>
            )}
            {shown && !broken && <div class="osd bottom recosd"><span class="osd-status">{t("recorded")}{shown.keep ? ` · ${t("recKept")}` : ""}</span></div>}
          </div>
          <div class="recctl">
            <button type="button" class="btn sm" disabled={!shown}
              onClick={() => { const v = video.current; if (v) { if (v.paused) void v.play().catch(() => {}); else v.pause(); } }}>{t("recPlayPause")}</button>
            <div class="seg" role="group" aria-label={t("recSpeed")}>
              {SPEEDS.map((s) => (
                <button key={s} type="button" aria-pressed={s === speed}
                  onClick={() => { setSpeed(s); if (video.current) video.current.playbackRate = s; }}>{s}×</button>
              ))}
            </div>
            <button type="button" class="btn sm" disabled={!rows || head == null} onClick={() => rows && head != null && play(rows, head - 10_000)}>⏪ {t("recTenS")}</button>
            <button type="button" class="btn sm" disabled={!rows || head == null} onClick={() => rows && head != null && play(rows, head + 10_000)}>{t("recTenS")} ⏩</button>
            <button type="button" class="btn sm" disabled={!shown || !prevSegment(rows ?? [], shown)} onClick={() => shown && step(prevSegment(rows ?? [], shown))}>{t("recPrev")}</button>
            <button type="button" class="btn sm" disabled={!shown || !nextSegment(rows ?? [], shown)} onClick={() => shown && step(nextSegment(rows ?? [], shown))}>{t("recNext")}</button>
            <span class="push" />
            <button type="button" class="btn sm" disabled={!shown} onClick={snap}>{t("recSnap")}</button>
            {canExport(props.role) && shown && (
              <a class="btn sm" href={`/api/recordings/${shown.id}/video`} download={`${robot}-${cam}-${shown.start_ms}.mp4`}>{t("recExport")}</a>
            )}
            {canExport(props.role) && !shown && <button type="button" class="btn sm" disabled>{t("recExport")}</button>}
            {canReview(props.role) && (
              <button type="button" class="btn sm" aria-pressed={!!shown?.keep} disabled={!shown} onClick={keep} title={t("recKeepHint")}>
                {shown?.keep ? t("recUnkeep") : t("recKeep")}
              </button>
            )}
          </div>
        </div>
        <RecMap robot={robot} track={track} head={head} onSeek={(ms) => rows && play(rows, ms)} />
      </div>

      {range && view && (
        <Timeline lanes={ls} robot={robot} day={range} view={view} head={head}
          onView={setView} onPick={(id, ms) => switchTo(id, ms)} />
      )}
    </div>
  );
}

/** 右边的地图:那一刻在哪(之前实线、之后虚线),点轨迹跳到经过那里的时刻。 */
function RecMap(props: { robot: string; track: TrackPt[]; head: number | null; onSeek: (ms: number) => void }) {
  const here = props.head != null ? poseAt(props.track, props.head) : null;
  const ref = here ?? props.track[0] ?? null;
  const key = ref ? `${ref.map_id}/${ref.map_version}` : "";
  const [meta, setMeta] = useState<{ key: string; meta: PreviewMeta | null }>({ key: "", meta: null });
  useEffect(() => {
    if (!ref) return;
    let live = true;
    api<PreviewMeta>("GET", `/api/maps/${enc(ref.map_id)}/${enc(ref.map_version)}/preview`)
      .then((m) => live && setMeta({ key, meta: m && typeof m.width === "number" && m.width > 0 ? m : null }))
      .catch(() => live && setMeta({ key, meta: null }));
    return () => { live = false; };
  }, [key]);
  const m = meta.key === key ? meta.meta : null;
  const onThis = ref ? props.track.filter((p) => p.map_id === ref.map_id && p.map_version === ref.map_version) : [];
  const before = props.head != null ? onThis.filter((p) => p.t <= props.head!) : onThis;
  const after = props.head != null ? onThis.filter((p) => p.t >= props.head!) : [];
  const line = (pts: TrackPt[]) => (m ? pts.map((p) => { const q = toPx(m, p.x, p.y); return `${q.px},${q.py}`; }).join(" ") : "");
  return (
    <div class="rmap">
      {m && ref ? (
        <MapCanvas meta={m} raster={`/api/maps/${enc(ref.map_id)}/${enc(ref.map_version)}/preview.png`} label={t("recMapAria")}
          drawing onPick={(px, py) => {
            const [x, y] = pxToMap(m, px, py);
            const at = nearestTime(props.track, x, y, { map_id: ref.map_id, version: ref.map_version });
            if (at != null) props.onSeek(at);
          }}
          world={<>
            {before.length > 1 && <polyline class="rec-trail before" points={line(before)} />}
            {after.length > 1 && <polyline class="rec-trail after" points={line(after)} />}
          </>}
          marks={(s) => {
            if (!here) return null;
            const [x, y] = s.map(here.x, here.y);
            return <g class="rec-here"><circle cx={x} cy={y} r="8" /><text x={x + 12} y={y + 4}>{nameOf(props.robot)}</text></g>;
          }} />
      ) : <div class="rmap-empty muted">{t("recMapNone")}</div>}
      <span class="rmap-hint">
        {props.head == null ? t("recMapPick") : here ? t("recMapHere", { id: nameOf(props.robot), t: hms(props.head) }) : t("recMapUnknown", { id: nameOf(props.robot), t: hms(props.head) })}
      </span>
    </div>
  );
}

/** 底下的时间轴:每台一行,共用一根播放线;滚轮拉宽拉窄(一整天 ↔ 5 分钟)。 */
function Timeline(props: {
  lanes: ReturnType<typeof lanes>;
  robot: string;
  day: { since: number; until: number };
  view: { since: number; until: number };
  head: number | null;
  onView: (v: { since: number; until: number }) => void;
  onPick: (robot: string, ms: number) => void;
}) {
  const { view } = props;
  const w = view.until - view.since;
  const pct = (ms: number) => ((ms - view.since) / w) * 100;
  const timeAt = (e: MouseEvent | WheelEvent, el: Element) => {
    const box = el.getBoundingClientRect();
    const f = box.width > 0 ? Math.min(1, Math.max(0, (e.clientX - box.left) / box.width)) : 0;
    return view.since + f * w;
  };
  const ticks: number[] = [];
  const stepMs = w > 6 * 3_600_000 ? 3_600_000 : w > 3_600_000 ? 900_000 : w > 900_000 ? 300_000 : 60_000;
  for (let x = Math.ceil(view.since / stepMs) * stepMs; x <= view.until; x += stepMs) ticks.push(x);
  return (
    <div class="tl" data-span={String(w)}>
      <div class="tl-tracks" onWheel={(e) => {
        e.preventDefault();
        const at = timeAt(e, e.currentTarget as Element);
        props.onView(viewSpan(props.day, view, at, e.deltaY < 0 ? 0.5 : 2));
      }}>
        {props.lanes.map((l) => (
          <div key={l.robot} class={l.robot === props.robot ? "lane on" : "lane"}>
            <b role="button" tabIndex={0} onClick={() => props.onPick(l.robot, props.head ?? props.day.since)}>{nameOf(l.robot)}</b>
            <div class="track" onClick={(e) => props.onPick(l.robot, timeAt(e, e.currentTarget as Element))}>
              {l.segs.filter(([a, b]) => b > view.since && a < view.until).map(([a, b]) => (
                <span key={a} class="seg-rec" style={{ left: `${Math.max(0, pct(a))}%`, width: `${Math.max(0.2, pct(Math.min(b, view.until)) - Math.max(0, pct(a)))}%` }} />
              ))}
              {l.marks.filter((k) => k.t >= view.since && k.t < view.until).map((k) => (
                <span key={k.t} class={`mk ${k.kind}`} style={{ left: `${pct(k.t)}%` }} />
              ))}
            </div>
          </div>
        ))}
        {props.head != null && props.head >= view.since && props.head < view.until && (
          <div class="tl-head" style={{ left: `calc(70px + (100% - 70px) * ${pct(props.head) / 100})` }}><span>{hms(props.head)}</span></div>
        )}
      </div>
      <div class="ticks mono" aria-hidden="true">
        {ticks.map((x) => <span key={x} style={{ left: `calc(70px + (100% - 70px) * ${pct(x) / 100})` }}>{hms(x).slice(0, 5)}</span>)}
      </div>
      <div class="lg muted small">
        <span><i class="k rec" />{t("recLegendRec")}</span>
        <span><i class="k alarm" />{t("recLegendAlarm")}</span>
        <span><i class="k help" />{t("recLegendHelp")}</span>
        <span>{t("recZoomHint")}</span>
      </div>
    </div>
  );
}
