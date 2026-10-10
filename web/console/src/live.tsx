// Live 页 = 视频墙(B0 专业版 2.3)。每只狗一个外壳:铭牌条 + 前后两路画面;两只一行,按编号固定;
// 有没解除 P1 的狗置顶成「报警画面」(整行宽、3px 报警框、操作条)。离线的狗收成一条窄条,不拉流。
import { useState } from "preact/hooks";
import { CameraWell, camLabel, type Cam } from "./camera";
import { hm, hms } from "./data";
import { Modal } from "./dialogs";
import { alertTitle, lang, t, type Key } from "./i18n";
import { Shape } from "./icons";
import {
  abnormal,
  alarmState,
  canDispatch,
  duration,
  type Alert,
  type DeterSession,
  type Robot,
  type RobotState,
} from "./model";

export interface UnitInfo {
  robot: Robot;
  state: RobotState;
  battery: number | null;
  deter?: DeterSession;
  task: string;
}

const CAMS: Cam[] = ["front", "back"];

export function StateText({ s }: { s: RobotState }) {
  const a = abnormal(s);
  return (
    <span class={a ? `state ${a}` : "state"}>
      {a && <Shape kind={a === "p1" ? "P1" : "P2"} />}
      {t(`st_${s}` as Key)}
    </span>
  );
}

function Strip({ u, onMap }: { u: UnitInfo; onMap: () => void }) {
  return (
    <div class="unit-strip">
      <span class="unit-id">{u.robot.robot_id}</span>
      <StateText s={u.state} />
      {u.battery != null && <span class={u.battery < 20 ? "batt low" : "batt"}>{Math.round(u.battery)}%</span>}
      <span class="muted">{u.task}</span>
      <button type="button" class="link push" onClick={onMap}>{t("map")}</button>
    </div>
  );
}

export function RobotUnit({ u, now, onMap, onOpen }: { u: UnitInfo; now: number; onMap: () => void; onOpen: (c: Cam) => void }) {
  return (
    <section class="unit" id={`unit-${u.robot.robot_id}`} aria-label={u.robot.robot_id}>
      <Strip u={u} onMap={onMap} />
      <div class="pair">
        {CAMS.map((c) => <CameraWell key={c} robot={u.robot.robot_id} cam={c} now={now} onOpen={() => onOpen(c)} />)}
      </div>
    </section>
  );
}

export function OfflineStrip({ u, onMap }: { u: UnitInfo; onMap: () => void }) {
  const last = u.robot.status?.last_seen;
  return (
    <section class="unit offline" id={`unit-${u.robot.robot_id}`} aria-label={u.robot.robot_id}>
      <span class="unit-id">{u.robot.robot_id}</span>
      <span class="state p2"><Shape kind="P2" />{last ? t("offlineSince", { t: hm(last) }) : t("st_offline")}</span>
      <span class="muted">{t("notStreaming")}</span>
      <button type="button" class="link push" onClick={onMap}>{t("map")}</button>
    </section>
  );
}

export function AlarmTile(props: {
  a: Alert;
  u: UnitInfo;
  role: string;
  now: number;
  onAck: () => void;
  onSiren: () => void;
  onFalse: () => void;
  onMap: () => void;
  onOpen: (c: Cam) => void;
}) {
  const { a, u } = props;
  const st = alarmState(a, props.now);
  const p = a.context?.persons ?? u.deter?.persons;
  const where = [
    a.context?.zone,
    p?.nearest_m != null ? t("aheadOf", { m: p.nearest_m, id: a.robot }) : a.robot,
  ].filter(Boolean).join(", ");
  const sirenOn = (u.deter?.level ?? 0) >= 2;
  return (
    <section class={st === "unacked" ? "alarm-tile unacked" : "alarm-tile"} id={`unit-${u.robot.robot_id}`}
      aria-label={`P1 ${alertTitle(lang.value, a.kind, a.title)} ${a.robot}`}>
      <div class="alarm-head">
        <span class="pri"><Shape kind="P1" size={14} />P1</span>
        <span class="alarm-title">{alertTitle(lang.value, a.kind, a.title)}</span>
        <span>{where}</span>
        <span class="push strong">
          {st === "unacked"
            ? t("unackedFor", { d: duration(props.now - a.first_ms) })
            : t("ackedBy", { who: a.acked_by, t: hms(a.acked_ms ?? props.now) })}
        </span>
        <time class="mono">{hms(a.first_ms)}</time>
      </div>
      <div class="pair">
        {CAMS.map((c) => <CameraWell key={c} robot={a.robot} cam={c} now={props.now} forceLive onOpen={() => props.onOpen(c)} />)}
      </div>
      <div class="alarm-actions">
        <button type="button" class="btn primary" disabled={st !== "unacked"} onClick={props.onAck}>{t("take")}</button>
        {u.deter && canDispatch(props.role) && (
          <button type="button" class="btn onalarm" aria-pressed={sirenOn} disabled={sirenOn} onClick={props.onSiren}>
            {sirenOn ? t("sirenOn") : t("siren")}
          </button>
        )}
        <button type="button" class="btn onalarm" onClick={props.onFalse}>{t("falseAlarm")}</button>
        <button type="button" class="btn onalarm" onClick={props.onMap}>{t("showOnMap")}</button>
        <span class="soft small">{t("talkNA")}</span>
        <span class="soft small push">
          {u.deter ? t("deterOn", { n: u.deter.level }) : ""}
          {p?.count ? ` ${t("persons", { n: p.count, m: p.nearest_m ?? "?" })}` : ""}
        </span>
      </div>
    </section>
  );
}

export function JumpStrip({ units, p1, watching, total }: { units: UnitInfo[]; p1: Set<string>; watching: number; total: number }) {
  return (
    <nav class="jump" aria-label="Robots">
      {units.map((u) => {
        const a = abnormal(u.state);
        const cls = p1.has(u.robot.robot_id) ? "jumpbtn p1" : "jumpbtn";
        return (
          <a key={u.robot.robot_id} class={cls} href={`#unit-${u.robot.robot_id}`}>
            {a && !p1.has(u.robot.robot_id) && <Shape kind={a === "p1" ? "P1" : "P2"} />}
            {u.robot.robot_id}
          </a>
        );
      })}
      <span class="muted push">{t("watching", { n: watching, total })}</span>
    </nav>
  );
}

export function FullCamera({ robot, cam, now, onClose }: { robot: string; cam: Cam; now: number; onClose: () => void }) {
  const [c, setC] = useState<Cam>(cam);
  return (
    <Modal label={camLabel(robot, c)} wide onClose={onClose}>
      <div class="seg" role="group" aria-label={robot}>
        {CAMS.map((x) => (
          <button key={x} type="button" aria-pressed={x === c} onClick={() => setC(x)}>{t(x)}</button>
        ))}
      </div>
      <div class="full">
        <CameraWell key={c} robot={robot} cam={c} now={now} forceLive />
      </div>
    </Modal>
  );
}
