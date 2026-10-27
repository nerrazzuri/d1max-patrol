// 值班页(W44 第一期,设计 §4.1;默认首页)。上半屏两个画面位(按紧急程度自动挑,可以钉住);下半屏左边每台
// 机器人一条状态,右边一张地图。出事时顶上红色横幅:什么事、在哪、几点,三个按钮;第一个画面位换成现场。
// 保安只看四类事(有人闯入、起火或过热、机器人需要帮忙、机器人掉线);技术类不在这里。
import { useEffect, useMemo, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { hm } from "./data";
import { Confirm } from "./dialogs";
import { guardAlarms, pickSlots, rankUnits, type SlotPick } from "./dutyrank";
import { alertTitle, lang, t } from "./i18n";
import { StateText, type UnitInfo } from "./live";
import type { CanvasArea } from "./mapcanvas";
import { SiteMap } from "./mappage";
import { pointInPolygon } from "./mapview";
import type { Alert } from "./model";
import { VideoSlot, type Scene } from "./slot";

/** 迷路了的两种(站点 alerts.LEVEL_OF):横幅上多一个「帮它找位置」。 */
const LOST = new Set(["loc_lost_paused", "loc_lost_idle"]);

interface FixedCam { name: string; zone: string }

export function DutyPage(props: {
  units: UnitInfo[];
  alerts: Alert[];
  now: number;
  role: string;
  say: (s: string) => void;
  refresh: () => Promise<void>;
  onZoom: (robot: string) => void;
}) {
  const [pins, setPins] = useState<[string | null, string | null]>([null, null]);
  const [confirmFalse, setConfirmFalse] = useState<Alert | null>(null);
  const [finding, setFinding] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [areas, setAreas] = useState<CanvasArea[]>([]);
  const [cams, setCams] = useState<FixedCam[]>([]);

  useEffect(() => {
    let live = true;
    api<{ cameras: FixedCam[] }>("GET", "/api/cameras").then((d) => live && setCams(d.cameras ?? [])).catch(() => {});
    return () => { live = false; };
  }, []);

  const alarms = useMemo(() => guardAlarms(props.alerts), [props.alerts]);
  const ranked = useMemo(() => rankUnits(props.units, props.alerts), [props.units, props.alerts]);
  const slots = pickSlots(ranked, pins, alarms);
  const top = alarms[0];
  const hot = new Set(alarms.map((a) => a.context?.zone).filter((z): z is string => !!z));
  const unitOf = (id: string) => props.units.find((u) => u.robot.robot_id === id);

  /** 有人闯入的现场:那个防区的固定摄像头。 */
  function sceneOf(a: Alert | undefined): Scene | undefined {
    if (!a || a.audience !== "intrusion" || !a.context?.zone) return undefined;
    const c = cams.find((x) => x.zone === a.context?.zone);
    return c ? { label: c.name, src: `/api/cameras/${enc(c.name)}/live` } : undefined;
  }

  async function post(path: string, body: unknown) {
    try {
      await api("POST", path, body);
    } catch (e) {
      // 别人已经处理掉了(解决了、删了):说一次,刷新,横幅自己就没了
      if (e instanceof ApiError && e.status === 404) props.say(t("dutyGone"));
      else props.say(t("failed", { why: e instanceof ApiError ? e.message : String(e) }));
    }
    await props.refresh();
  }
  const ack = (a: Alert) => post(`/api/alerts/${enc(a.key)}/ack`, {});
  const resolve = (a: Alert, verdict: string) => post(`/api/alerts/${enc(a.key)}/resolve`, { verdict });

  const pin = (i: 0 | 1, robot: string) => setPins((p) => {
    const n: [string | null, string | null] = [p[0], p[1]];
    n[i] = n[i] === robot ? null : robot;
    return n;
  });
  /** 点状态条:放进没钉住的那个位(两个都钉住了就换掉第二个)。 */
  const put = (robot: string) => setPins((p) => (p[0] == null || p[1] != null && p[0] !== robot ? [robot, p[1]] : [p[0], robot]));

  const zoneOf = (u: UnitInfo): string => {
    const p = u.robot.loc?.pose;
    if (!p) return "";
    return areas.find((a) => pointInPolygon([p.x, p.y], a.pts))?.name ?? "";
  };

  const slot = (s: SlotPick | null, i: 0 | 1) => {
    const u = s ? unitOf(s.robot) : undefined;
    if (!s || !u) return <div class="slot empty-slot" />;
    return (
      <VideoSlot key={`${i}:${s.robot}:${s.alert?.key ?? ""}`} u={u} now={props.now} pinned={pins[i] === s.robot}
        onPin={() => pin(i, s.robot)} onZoom={() => props.onZoom(s.robot)}
        forceLive={!!s.alert} scene={sceneOf(s.alert)} alarm={!!s.alert} />
    );
  };

  return (
    <div class="duty">
      {top && (
        <div class="banner" role="alert">
          <div class="banner-text">
            <b>{alertTitle(lang.value, top.kind, top.title)}</b>
            <span>{[top.context?.zone, hm(top.first_ms)].filter(Boolean).join(" · ")}</span>
            {top.acked_ms != null && top.acked_by && <span class="banner-who">{t("dutyTakenBy", { who: top.acked_by })}</span>}
            {alarms.length > 1 && <span class="banner-more">{t("dutyMore", { n: alarms.length - 1 })}</span>}
          </div>
          <div class="banner-acts">
            {top.acked_ms == null && <button type="button" class="btn onsolid" onClick={() => ack(top)}>{t("dutyTake")}</button>}
            {LOST.has(top.kind) && (
              <button type="button" class="btn onsolid" onClick={() => { setFinding(top.robot); setSelected(top.robot); }}>{t("dutyFindPose")}</button>
            )}
            {top.audience === "intrusion" ? (
              <>
                <button type="button" class="btn onsolid" onClick={() => resolve(top, "intruder")}>{t("dutyIntruder")}</button>
                <button type="button" class="btn onsolid" onClick={() => setConfirmFalse(top)}>{t("dutyFalse")}</button>
              </>
            ) : (
              <button type="button" class="btn onsolid" onClick={() => resolve(top, "handled")}>{t("dutyHandled")}</button>
            )}
          </div>
        </div>
      )}
      <div class="duty-slots">
        {slot(slots[0], 0)}
        {slot(slots[1], 1)}
      </div>
      <div class="duty-bottom">
        <ul class="duty-list" aria-label={t("robots")}>
          {ranked.length === 0 && <li class="muted">{t("dutyNoRobots")}</li>}
          {ranked.map((u) => (
            <li key={u.robot.robot_id}>
              <button type="button" class={selected === u.robot.robot_id ? "duty-row on" : "duty-row"}
                onClick={() => { put(u.robot.robot_id); setSelected(u.robot.robot_id); }}>
                <span class="unit-id">{u.robot.robot_id}</span>
                <StateText s={u.state} stale={u.stale} />
                <span class="muted small">{zoneOf(u)}</span>
                {u.battery != null && <span class="batt push">{Math.round(u.battery)}%</span>}
              </button>
            </li>
          ))}
        </ul>
        <div class="duty-map">
          <SiteMap units={props.units} p1={alarms.filter((a) => a.level === "P1")} now={props.now} role={props.role}
            selected={finding ?? selected ?? slots[0]?.robot ?? null} onSelect={setSelected} hot={hot} episodes={false}
            hint={finding ? t("dutyFindHint", { id: finding }) : null} onAreas={setAreas} />
          {finding && <p class="sr-only">{t("dutyFindHint", { id: finding })}</p>}
        </div>
      </div>
      {confirmFalse && (
        <Confirm text={t("dutyFalseConfirm")} yes={t("dutyFalseYes")} onNo={() => setConfirmFalse(null)}
          onYes={() => { const a = confirmFalse; setConfirmFalse(null); void resolve(a, "false_alarm"); }} />
      )}
    </div>
  );
}
