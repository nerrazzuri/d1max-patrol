// 画面位(W44 第一期,设计 §4.1):一台机器人一个位 —— 前摄像头大画面 + 后摄像头小窗,点小窗两路互换;
// 装了热像的多一个「热像」按钮(大画面换热像、前摄像头进小窗,设计 §2.4);报警时大画面换成现场(固定摄像头)。
// 顶上一条状态栏:名字 + 状态、速度、电量、钉住、放大。值班页、实况页共用状态栏。
import { useState } from "preact/hooks";
import { CameraWell, type Cam } from "./camera";
import { t } from "./i18n";
import { StateText, type UnitInfo } from "./live";
import { robotCams } from "./model";
import { nameOf } from "./data";

export interface Scene {
  /** 现场叫什么(「3 号 后花园」)。 */
  label: string;
  /** 现场画面的地址(固定摄像头直播)。 */
  src: string;
  /** 画面健康(最后一帧多久了,外审 R6)。 */
  health: string;
}

export function SlotBar(props: {
  u: UnitInfo;
  pinned?: boolean;
  onPin?: () => void;
  onZoom?: () => void;
  thermal?: { on: boolean; toggle: () => void } | null;
}) {
  const { u } = props;
  return (
    <div class="slot-bar">
      <span class="slot-name">{nameOf(u.robot.robot_id)}</span>
      <StateText s={u.state} stale={u.stale} />
      <span class="slot-mid">
        {u.speed != null && <span class="mono">{t("speedMps", { v: u.speed.toFixed(1) })}</span>}
        {u.battery != null && <span>{t("battery", { n: Math.round(u.battery) })}</span>}
      </span>
      {props.thermal && (
        <button type="button" class="slot-btn slot-thermal" aria-pressed={props.thermal.on} onClick={props.thermal.toggle}>{t("thermal")}</button>
      )}
      {props.onPin && (
        <button type="button" class="slot-btn slot-pin" aria-pressed={!!props.pinned} onClick={props.onPin}>{t("pinSlot")}</button>
      )}
      {props.onZoom && <button type="button" class="slot-btn slot-zoom" onClick={props.onZoom}>{t("enlarge")} ⤢</button>}
    </div>
  );
}

export function VideoSlot(props: {
  u: UnitInfo;
  now: number;
  pinned: boolean;
  onPin: () => void;
  onZoom: () => void;
  forceLive?: boolean;
  scene?: Scene;
  /** 报警现场的那一位:外框标红。 */
  alarm?: boolean;
}) {
  const [swapped, setSwapped] = useState(false);
  const [thermal, setThermal] = useState(false);
  const id = props.u.robot.robot_id;
  const hasThermal = robotCams(props.u.robot).includes("thermal");
  const useThermal = thermal && hasThermal && !props.scene;
  const main: Cam = useThermal ? "thermal" : swapped ? "back" : "front";
  const pip: Cam = useThermal ? "front" : props.scene ? "front" : swapped ? "front" : "back";
  const off = props.u.state === "offline";
  return (
    <section class={props.alarm ? "slot alarm" : "slot"} id={`unit-${id}`} aria-label={id}>
      <div class="slot-main">
        {off ? (
          <div class="slot-off">{t("st_offline")}</div>
        ) : props.scene ? (
          // 跟机器人画面同一个组件(断流提示、重连);换现场整个换掉,上一个的回调碰不到新的
          <CameraWell key={`s-${props.scene.src}`} robot={id} cam="front" now={props.now} forceLive={props.forceLive}
            variant="main" feed={props.scene} />
        ) : (
          <CameraWell key={`m-${main}`} robot={id} cam={main} now={props.now} forceLive={props.forceLive} variant="main" />
        )}
      </div>
      {!off && (
        <button type="button" class="slot-pip" aria-label={t(pip)} disabled={!!props.scene || useThermal}
          onClick={() => setSwapped(!swapped)}>
          <CameraWell key={`p-${pip}`} robot={id} cam={pip} now={props.now} forceLive={props.forceLive} variant="pip" />
        </button>
      )}
      <SlotBar u={props.u} pinned={props.pinned} onPin={props.onPin} onZoom={props.onZoom}
        thermal={hasThermal && !props.scene ? { on: thermal, toggle: () => setThermal(!thermal) } : null} />
    </section>
  );
}
