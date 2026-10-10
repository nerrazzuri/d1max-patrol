// 一路摄像头画面(B0 专业版 2.3 / VMS 惯例):画面上沿是「D1-01 Front 02:15:08」,下沿是 Live / Paused at … /
// Reconnecting…。**看得见才拉流**:可见面积 ≥25% 才拉;离开视口 2 秒后停(防滚动抖动),停之前把最后一帧
// 画到 canvas 上压暗显示;浏览器页签藏起来也停。P1 的狗(forceLive)一直拉。
import { useEffect, useRef, useState } from "preact/hooks";
import { enc } from "./api";
import { hms, usePageVisible } from "./data";
import { t } from "./i18n";

export type Cam = "front" | "back";

const PAUSE_AFTER_MS = 2000;
const RETRY_MS = 3000;

export function camLabel(robot: string, cam: Cam): string {
  return `${robot} ${t(cam)}`;
}

export function CameraWell(props: { robot: string; cam: Cam; forceLive?: boolean; now: number; onOpen?: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  const img = useRef<HTMLImageElement>(null);
  const frozen = useRef<HTMLCanvasElement>(null);
  const [visible, setVisible] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [pausedAt, setPausedAt] = useState<number | null>(null);
  const [broken, setBroken] = useState(false);
  const [nonce, setNonce] = useState(0);
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

  useEffect(() => {
    if (!broken) return;
    const id = window.setTimeout(() => {
      setBroken(false);
      setNonce((n) => n + 1);
    }, RETRY_MS);
    return () => window.clearTimeout(id);
  }, [broken]);

  const label = camLabel(props.robot, props.cam);
  const src = `/api/robots/${enc(props.robot)}/video/${props.cam}?v=${nonce}`;
  const status = broken ? t("reconnecting") : streaming ? t("live") : pausedAt ? t("pausedAt", { t: hms(pausedAt) }) : t("noFeed");
  return (
    <div ref={box} class={streaming ? "well" : "well paused"} title={!streaming ? t("pausedHint") : undefined}>
      <canvas ref={frozen} class="frozen" aria-hidden="true" />
      {streaming && !broken && (
        <img ref={img} class="feed" src={src} alt={label} onError={() => setBroken(true)} />
      )}
      <div class="osd top">
        <span>{label}</span>
        <span>{hms(streaming ? props.now : pausedAt ?? props.now)}</span>
      </div>
      <div class="osd bottom">
        <span class="osd-status">{streaming && !broken && <span class="livedot" aria-hidden="true" />}{status}</span>
        {props.onOpen && (
          <button type="button" class="osd-btn" onClick={props.onOpen} aria-label={`${t("fullScreen")}: ${label}`}>{t("fullScreen")}</button>
        )}
      </div>
    </div>
  );
}
