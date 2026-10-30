// 所有画面走一条 WebSocket(W51)。原来每路 MJPEG 占一条 HTTP/1.1 连接,浏览器对一个站点最多 6 条,只敢开
// 3 路长连、别的隔 2 秒取一张;WebSocket 不算在那 6 条里,一条装下所有画面(站点那头见 d1max_site/video_mux.py)。
//
// 用法:``subscribe(画面地址, 每秒几帧, {onFrame, onError})`` 回一个退订函数。同一路几个人看只订一次(fps 取大的)。
// 每来一帧给一个 blob: 地址(上一帧的地址收回);这一路出错(站点回的原因)或者连接断了叫 onError —— 调用方
// 按断了处理、过一会儿重新订阅。连接断了、还有人在看就自己重连(1 秒起,最多 5 秒)。
import { useEffect, useRef, useState } from "preact/hooks";

export const WS_PATH = "/api/video/ws";
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 5000;
const REVOKE_AFTER_MS = 1000;

export interface FrameListener {
  fps: number;
  onFrame(url: string): void;
  onError(status: number, message: string): void;
}

/** 测试换掉 WebSocket(跟 ``setFetch`` 一样)。 */
export interface SocketLike {
  binaryType: string;
  readyState: number;
  onopen: ((e: unknown) => void) | null;
  onmessage: ((e: { data: unknown }) => void) | null;
  onclose: ((e: unknown) => void) | null;
  onerror: ((e: unknown) => void) | null;
  send(data: string): void;
  close(): void;
}
type Factory = (url: string) => SocketLike;

const OPEN = 1;
const realFactory: Factory | null = typeof WebSocket !== "undefined" && typeof location !== "undefined"
  ? (url) => new WebSocket(url) as unknown as SocketLike
  : null;
/** 单元测试里默认不连(没有站点);要测的自己 ``setSocketFactory``。 */
let factory: Factory | null = import.meta.env?.MODE === "test" ? null : realFactory;
export function setSocketFactory(f: Factory | null): void {
  factory = f;
}

interface Sub { listeners: Set<FrameListener>; url: string | null }
const subs = new Map<string, Sub>();
let sock: SocketLike | null = null;
let backoff = RECONNECT_MIN_MS;
let reconnectTimer: number | null = null;
let sendQueued = false;

function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${WS_PATH}`;
}

function wanted(): { id: string; fps: number }[] {
  return [...subs].map(([id, s]) => ({ id, fps: Math.max(...[...s.listeners].map((l) => l.fps)) }));
}

function sendSubs(): void {
  if (sendQueued) return;
  sendQueued = true;
  queueMicrotask(() => {
    sendQueued = false;
    if (sock && sock.readyState === OPEN) sock.send(JSON.stringify({ sub: wanted() }));
  });
}

function decode(buf: ArrayBuffer): { id: string; jpeg: Uint8Array } | null {
  const u8 = new Uint8Array(buf);
  if (u8.length < 1) return null;
  const n = u8[0];
  if (u8.length < 1 + n) return null;
  return { id: new TextDecoder().decode(u8.subarray(1, 1 + n)), jpeg: u8.subarray(1 + n) };
}

function onFrame(id: string, jpeg: Uint8Array): void {
  const s = subs.get(id);
  if (!s) return;
  const url = URL.createObjectURL(new Blob([jpeg as BlobPart], { type: "image/jpeg" }));
  const old = s.url;
  s.url = url;
  for (const l of s.listeners) l.onFrame(url);
  // 上一帧过一会儿再收:<img> 可能还在解它,马上收会报加载失败(被当成断流)
  if (old) window.setTimeout(() => URL.revokeObjectURL(old), REVOKE_AFTER_MS);
}

function failAll(message: string): void {
  for (const s of subs.values()) for (const l of [...s.listeners]) l.onError(0, message);
}

function connect(): void {
  if (sock || !factory || reconnectTimer != null) return;
  const ws = factory(wsUrl());
  sock = ws;
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    backoff = RECONNECT_MIN_MS;
    sendSubs();
  };
  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) {
      const f = decode(e.data);
      if (f) onFrame(f.id, f.jpeg);
      return;
    }
    try {
      const m = JSON.parse(String(e.data)) as { kind?: string; id?: string | null; status?: number; message?: string };
      if (m.kind !== "error") return;
      const s = m.id ? subs.get(m.id) : undefined;
      for (const l of s ? [...s.listeners] : []) l.onError(m.status ?? 502, m.message ?? "");
    } catch {
      /* 看不懂的不管 */
    }
  };
  ws.onerror = () => { /* 跟着会有 onclose */ };
  ws.onclose = () => {
    if (sock !== ws) return;
    sock = null;
    if (subs.size === 0) return;
    failAll("与站点的画面连接断了");
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      if (subs.size) connect();
    }, backoff);
    backoff = Math.min(RECONNECT_MAX_MS, backoff * 2);
  };
}

/** 订阅一路画面;回退订函数。 */
export function subscribe(id: string, l: FrameListener): () => void {
  let s = subs.get(id);
  if (!s) subs.set(id, (s = { listeners: new Set(), url: null }));
  s.listeners.add(l);
  connect();
  sendSubs();
  return () => {
    const cur = subs.get(id);
    if (!cur) return;
    cur.listeners.delete(l);
    if (cur.listeners.size === 0) {
      subs.delete(id);
      const last = cur.url;
      if (last) window.setTimeout(() => URL.revokeObjectURL(last), REVOKE_AFTER_MS);
    }
    sendSubs();
    if (subs.size === 0 && sock) {
      const ws = sock;
      sock = null;
      ws.close();                                  // 没人看了:连接也收掉
    }
  };
}

/** 现在订着哪几路(测试、诊断用)。 */
export function subscribed(): string[] {
  return [...subs.keys()];
}

/** 一路画面的最新一帧(blob: 地址);``on`` 为假、还没来帧是 null。出错叫 ``onError``(只叫最新给的那个)。
 *  ``epoch`` 变了就重新订阅(调用方断流重连时加一)。 */
export function useVideoFrame(id: string, on: boolean, fps: number, epoch: number,
                              onError: (status: number, message: string) => void): string | null {
  const [url, setUrl] = useState<string | null>(null);
  const err = useRef(onError);
  err.current = onError;
  useEffect(() => {
    if (!on) {
      setUrl(null);
      return;
    }
    let live = true;
    const off = subscribe(id, {
      fps,
      onFrame: (u) => { if (live) setUrl(u); },
      onError: (status, message) => { if (live) err.current(status, message); },
    });
    return () => { live = false; off(); };
  }, [id, on, fps, epoch]);
  return on ? url : null;
}
