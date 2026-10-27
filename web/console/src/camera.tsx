// 一路摄像头画面(B0 专业版 2.3 / VMS 惯例):画面上沿是「D1-01 Front 02:15:08」,下沿是 Live / Paused at … /
// Reconnecting…。**看得见才拉流**:可见面积 ≥25% 才拉;离开视口 2 秒后停(防滚动抖动),停之前把最后一帧
// 画到 canvas 上压暗显示;浏览器页签藏起来也停。P1 的狗(forceLive)一直拉。
// 断流(W44、外审 R6):出错停在最后一帧、写「画面断了 · 最后画面 几点 · 已断几秒」,3 秒后换个地址重连。MJPEG 放在
// <img> 里浏览器不报每一帧,静悄悄停帧看不出来 —— 拉着的时候每 2 秒问站点「最后一帧多久了」(站点收到帧的时刻,
// 不是浏览器重载图片的时刻):停了就按断了处理;重连以后站点说又有新帧了才把提示收掉。报警现场的固定摄像头
// (``feed``)走同一套。
import { useEffect, useRef, useState } from "preact/hooks";
import { api, enc } from "./api";
import { hms, nameOf, usePageVisible } from "./data";
import { t } from "./i18n";

/** 相机:狗自带前后两路;装了上装 V1.2 热像的多一路 thermal(W40)。 */
export type Cam = "front" | "back" | "thermal";

const PAUSE_AFTER_MS = 2000;
const RETRY_MS = 3000;
const RECOVERED_MS = 2500;
const HEALTH_MS = 2000;

/** 不是机器人的那一路(报警现场的固定摄像头):地址、名字、健康地址。 */
export interface Feed { src: string; label: string; health: string }
interface Health { online?: boolean; last_frame_age_ms?: number | null }

export function camLabel(robot: string, cam: Cam): string {
  return `${nameOf(robot)} ${t(cam)}`;
}

export function CameraWell(props: { robot: string; cam: Cam; forceLive?: boolean; now: number; onOpen?: () => void; variant?: "main" | "pip"; feed?: Feed }) {
  const box = useRef<HTMLDivElement>(null);
  const img = useRef<HTMLImageElement>(null);
  const frozen = useRef<HTMLCanvasElement>(null);
  const [visible, setVisible] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [pausedAt, setPausedAt] = useState<number | null>(null);
  const [broken, setBroken] = useState(false);
  /** W44:断流的那一刻(停在最后一帧、写已断几秒);重连上、2.5 秒没再断才清掉。 */
  const [lostAt, setLostAt] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);
  /** 这一次连上以后出过画面(``load``)。 */
  const [started, setStarted] = useState(false);
  /** 站点没有健康接口(老站点、没开视频):退回按时间收提示。 */
  const [noHealth, setNoHealth] = useState(false);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const pageVisible = usePageVisible();

  useEffect(() => {
    const el = box.current;
    if (!el || typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const io = new IntersectionObserver(([e]) => setVisible(e.intersectionRatio >= 0.25), { threshold: [0, 0.25, 0.5] });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  const want = !!props.forceLive || (visible && pageVisible);
  useEffect(() => {
    if (want) {
      setStreaming(true);
      setPausedAt(null);
      return;
    }
    const id = window.setTimeout(() => {
      freeze();
      setStreaming(false);
      setPausedAt(Date.now());
    }, PAUSE_AFTER_MS);
    return () => window.clearTimeout(id);
  }, [want]);

  function freeze() {
    const c = frozen.current;
    const i = img.current;
    if (!c || !i || !i.naturalWidth) return;
    try {
      c.width = i.naturalWidth;
      c.height = i.naturalHeight;
      c.getContext("2d")?.drawImage(i, 0, 0);
    } catch {
      /* 画不了就只显示暂停字样 */
    }
  }

  function lose(at: number) {
    freeze();
    setLostAt((x) => x ?? at);
    setStarted(false);
    setBroken(true);
  }

  useEffect(() => {
    if (!noHealth || broken || lostAt == null || !started) return;
    const id = window.setTimeout(() => setLostAt(null), RECOVERED_MS);
    return () => window.clearTimeout(id);
  }, [noHealth, broken, lostAt, started]);

  const healthUrl = props.feed ? props.feed.health : `/api/robots/${enc(props.robot)}/video/health`;
  useEffect(() => {
    if (!streaming || broken || noHealth) return;
    let stop = false;
    const id = window.setInterval(() => {
      api<Health & { cameras?: Record<string, Health> }>("GET", healthUrl)
        .then((d) => {
          if (stop || !alive.current) return;
          const h = props.feed ? d : d.cameras?.[props.cam];
          if (!h) return;
          const age = typeof h.last_frame_age_ms === "number" ? h.last_frame_age_ms : null;
          if (h.online) {
            if (started) setLostAt(null);               // 重连上了,站点也收到新帧了
          } else if (age != null) {
            lose(Date.now() - age);                     // 停帧:断的时刻是最后一帧的时刻
          } else if (started) {
            lose(Date.now());                           // 出过画面,站点那头已经没在拉了
          }
        })
        .catch(() => { if (!stop && alive.current) setNoHealth(true); });
    }, HEALTH_MS);
    return () => { stop = true; window.clearInterval(id); };
  }, [streaming, broken, noHealth, started, healthUrl]);

  useEffect(() => {
    if (!broken) return;
    const id = window.setTimeout(() => {
      setBroken(false);
      setNonce((n) => n + 1);
    }, RETRY_MS);
    return () => window.clearTimeout(id);
  }, [broken]);

  const label = props.feed ? props.feed.label : camLabel(props.robot, props.cam);
  const src = props.feed
    ? (nonce ? `${props.feed.src}${props.feed.src.includes("?") ? "&" : "?"}v=${nonce}` : props.feed.src)
    : `/api/robots/${enc(props.robot)}/video/${props.cam}?v=${nonce}`;
  // 断过、重连了但第一帧还没到:还是「正在重连」,不写「实时」(真浏览器上查出来的)
  const retrying = broken || (lostAt != null && !started);
  const status = retrying ? t("reconnecting") : streaming ? t("live") : pausedAt ? t("pausedAt", { t: hms(pausedAt) }) : t("noFeed");
  const cls = `${streaming ? "well" : "well paused"}${lostAt != null ? " lostfeed" : ""}${props.variant ? ` ${props.variant}` : ""}`;
  return (
    <div ref={box} class={cls} title={!streaming ? t("pausedHint") : undefined}>
      <canvas ref={frozen} class="frozen" aria-hidden="true" />
      {streaming && !broken && (
        <img ref={img} class={props.feed ? "feed scene" : "feed"} src={src} alt={label}
          onLoad={() => setStarted(true)}
          onError={() => lose(Date.now())} />
      )}
      {lostAt != null && (
        <div class="lost" role="status">
          {t("feedLost", { t: hms(lostAt), s: Math.max(0, Math.round((props.now - lostAt) / 1000)) })}
        </div>
      )}
      <div class="osd top">
        <span>{label}</span>
        <span>{hms(streaming ? props.now : pausedAt ?? props.now)}</span>
      </div>
      <div class="osd bottom">
        <span class="osd-status">{streaming && !retrying && <span class="livedot" aria-hidden="true" />}{props.feed ? `${label} · ` : ""}{status}</span>
        {props.onOpen && (
          <button type="button" class="osd-btn" onClick={props.onOpen} aria-label={`${t("fullScreen")}: ${label}`}>{t("fullScreen")}</button>
        )}
      </div>
    </div>
  );
}
