/* 巡检 app 的全部前端逻辑。无框架、无构建、不引外部资源。
 *
 * 三条规矩，改这个文件之前先读一遍：
 *
 * 1. **状态只有一个来源：`/api/events` 那条 SSE。** 页面自己不轮询。
 *    轮询会让"机器人现在在哪"落后半秒到一秒，而遥控和急停这两件事上，
 *    半秒就是撞不撞得上墙的差别。只有"点了按钮之后"才主动去取一次列表
 *    这种非状态数据。
 *
 * 2. **世界坐标 ↔ 画布像素的换算只有 `worldToPx` / `pxToWorld` 两个函数。**
 *    别处再算一遍就一定会和它们不一致，而不一致的表现是"标的点差了几十
 *    厘米"——现场极难看出来，等发现时任务已经跑歪了。
 *
 * 3. **遥控只在这一页看得见且窗口有焦点时才发心跳。** 服务端 0.6 秒收不到
 *    心跳就停车（`app/teleop.py`）。页面切走了还在续命，等于没人看着机器
 *    还在跑。
 */

"use strict";

/* ------------------------------------------------------------------ 小工具 */

const $ = (id) => document.getElementById(id);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/* ------------------------------------------------------------------ 解锁 */

/* token 存 sessionStorage：关掉标签页就没了。
 *
 * 不用 localStorage，是因为那会把"能开这条狗"这件事一直留在设备上。重输一次
 * PIN 是五秒钟的事，换的是"手机借给别人看一眼"不等于把机器狗一起借出去。
 *
 * 每个访问都包 try：隐私模式下 sessionStorage 会直接抛，而那时候页面还得能用
 * ——只是每次刷新要重输一次 PIN。 */
const TOKEN_KEY = "d1max-token";

function token() {
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch (_) { return ""; }
}

function setToken(value) {
  try {
    if (value) sessionStorage.setItem(TOKEN_KEY, value);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch (_) { /* 存不下就存不下，这一次会话照样能用 */ }
}

/** 给浏览器原生加载的 URL 挂上 token。
 *
 * `<img>`、`<a href>` 和 `EventSource` 都没有"设请求头"的地方，只能走查询串。
 * 服务端只在几条**只读 GET** 上认这个参数（见 `app/auth.py`），别处一律不认
 * ——所以这个函数只该用在那几处，不要拿它去拼会改东西的请求。 */
function withToken(url) {
  const t = token();
  if (!t) return url;
  return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(t);
}

/** 一次 API 调用。失败时把服务端那句人话原样抛出来。 */
async function api(path, method = "GET", payload) {
  const opts = { method, headers: {} };
  if (payload !== undefined) {
    opts.headers["content-type"] = "application/json";
    opts.body = JSON.stringify(payload);
  }
  // token 走请求头而不是 cookie：cookie 是浏览器自动附上的，别的网页里的
  // 脚本一发请求就等于替你开狗。自定义头跨域要先过 CORS 预检，我们不放行。
  const t = token();
  if (t) opts.headers["authorization"] = "Bearer " + t;
  const resp = await fetch(path, opts);
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = null; }
  if (!resp.ok) {
    // 401 就是"这个 token 不作数了"。清掉再弹框，不然下一次点还是 401。
    if (resp.status === 401) { setToken(""); showLock(); }
    const why = (data && (data.error || data.detail)) || text || resp.status;
    const err = new Error(why);
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

/** 亮出输 PIN 的那一层。 */
function showLock(msg) {
  $("lock").hidden = false;
  $("lock-msg").textContent = msg || "";
  $("lock-pin").focus();
}

/** PIN 换 token。换到了就整页重来 —— 这样 SSE、画面、报告链接全都带上新
 *  token，比挨个去补要可靠得多，而且这个页面重来一次几乎不花时间。 */
async function unlock() {
  const pin = $("lock-pin").value.trim();
  if (!pin) { $("lock-msg").textContent = "先输 PIN。"; return; }
  let resp;
  try {
    resp = await fetch("/api/auth", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ pin: pin }),
    });
  } catch (err) {
    $("lock-msg").textContent = "连不上：" + err.message;
    return;
  }
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    $("lock-msg").textContent = (data && (data.error || "")) + " " +
                                (data && data.detail || "");
    $("lock-pin").select();
    return;
  }
  setToken(data.token);
  location.reload();
}

/** 顶上那条红条。传空字符串收起来。 */
function banner(msg) {
  const el = $("banner");
  el.textContent = msg || "";
  el.hidden = !msg;
}

/** 包一层：出错就把原因贴到红条上，而不是无声无息什么都没发生。 */
function guard(fn) {
  return async (...args) => {
    try { banner(""); await fn(...args); } catch (err) { banner(String(err.message || err)); }
  };
}

const on = (id, fn) => $(id).addEventListener("click", guard(fn));

/* -------------------------------------------------------------------- 页签 */

function showTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  $$(".tab-panel").forEach((p) => { p.hidden = p.dataset.panel !== name; });
  syncTeleop();
}

$$("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

/* -------------------------------------------------------- 状态：一条 SSE */

let state = {};

function led(id, level, text) {
  const el = $(id);
  el.className = "led " + level;
  el.textContent = text;
}

/** 快照 -> 三盏灯 + 电量 + 运行页。整页的"现在什么情况"都出自这里。 */
function render(snap) {
  state = snap;
  const nav = snap.nav || {};
  const dev = snap.device || {};

  // 定位：只有"定位好了"才是绿的。链路断了不算"定位不好"，那是另一盏灯的事。
  const loc = nav.loc_status || "";
  if (!nav.connected) led("led-pose", "", "定位 --");
  else if (loc === "ok" || loc === "localized") led("led-pose", "ok", "定位正常");
  else if (loc) led("led-pose", "bad", "定位 " + loc);
  else led("led-pose", "warn", "定位 未知");

  const links = [];
  if (!nav.connected) links.push("导航");
  if (!dev.connected) links.push("运控");
  if (links.length) led("led-agent", "bad", links.join("/") + "断了");
  else if (nav.link_down) led("led-agent", "warn", "链路抖动");
  else led("led-agent", "ok", "链路正常");

  // 控制权丢了不是一个红点能说清的事：人得知道现在该去做什么。
  // 厂家已明确没有交接机制（#46/#47），拿回控制权的唯一办法就是重启运控主机。
  if (dev.control_lost) {
    led("led-control", "bad", "控制权被抢");
    banner("控制权被上装拿走了。厂商侧没有交接机制，机器狗不会再响应这里发出的" +
           "任何指令。要拿回来只能重启 RK3588（运控主机）：断电重上电，" +
           "等它起来之后本页会自动重连。");
  } else {
    led("led-control", dev.connected ? "ok" : "", dev.connected ? "控制权在我" : "控制权 --");
  }

  const pct = dev.battery;
  $("battery").textContent = (pct === null || pct === undefined) ? "电量 --" : "电量 " + pct + "%";

  renderRun(snap.run || {});
  renderCaps(snap.caps || []);
  renderProcs((snap.backend || {}).procs || []);
  drawMap();
}

function connect() {
  const src = new EventSource(withToken("/api/events"));
  src.addEventListener("message", (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.kind === "state") render(msg);
  });
  // 断了就重连。EventSource 自己也会重试，但服务端主动 bye 之后不会 —— 而
  // 那正是"app 重启了"的情形，恰恰是最需要自动接回去的时候。
  src.addEventListener("error", () => { setTimeout(() => { src.close(); connect(); }, 2000); });
}

/* -------------------------------------------------------------- 急停 */

on("estop", async () => {
  await api("/api/estop", "POST");
  banner("已急停：遥控停了，正在跑的任务也打断了。");
});

/* -------------------------------------------------------------- 地图画布 */

const canvas = $("map");
const ctx2d = canvas.getContext("2d");
let grid = null;      // 当前底图（占据栅格）
let base = null;      // 底图渲染好的离屏画布
let view = { scale: 1, offX: 0, offY: 0 };
let marks = [];       // 页面上标出来的点位
let drag = null;      // 正在拖的那一笔

/** base64 游程 -> 逐格占据值。和 `protocol/map_frames.py` 的编码一一对应：
 *  每 5 字节一段 —— 1 字节有符号值 + 4 字节小端长度。 */
function decodeRle(blob, expect) {
  const bin = atob(blob);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const view8 = new DataView(bytes.buffer);
  const out = new Int8Array(expect);
  let at = 0;
  for (let o = 0; o + 5 <= bytes.length; o += 5) {
    const value = view8.getInt8(o);
    const count = view8.getUint32(o + 1, true);
    for (let k = 0; k < count && at < expect; k++) out[at++] = value;
  }
  return out;
}

/** 世界坐标 -> 画布像素。**全页只有这一个地方做这件事。** */
function worldToPx(x, y) {
  if (!grid) return { px: 0, py: 0 };
  const col = (x - grid.origin_x) / grid.resolution;
  const row = (y - grid.origin_y) / grid.resolution;
  return { px: view.offX + col * view.scale,
           py: view.offY + (grid.height - row) * view.scale };
}

/** 画布像素 -> 世界坐标。`worldToPx` 的逆。
 *  origin_yaw 一律当 0 处理：`map_saver_cli` 出的图从来不转，真碰上转过的
 *  图，这里会明显地画歪 —— 那比悄悄算错好。 */
function pxToWorld(px, py) {
  if (!grid) return { x: 0, y: 0 };
  const col = (px - view.offX) / view.scale;
  const row = grid.height - (py - view.offY) / view.scale;
  return { x: grid.origin_x + col * grid.resolution,
           y: grid.origin_y + row * grid.resolution };
}

function buildBase() {
  base = document.createElement("canvas");
  base.width = grid.width;
  base.height = grid.height;
  const bctx = base.getContext("2d");
  const img = bctx.createImageData(grid.width, grid.height);
  const cells = decodeRle(grid.rle, grid.width * grid.height);
  for (let row = 0; row < grid.height; row++) {
    // 栅格第 0 行是 y 最小的那行（图像的最下面），画上去要翻过来。
    const dst = (grid.height - 1 - row) * grid.width;
    for (let col = 0; col < grid.width; col++) {
      const v = cells[row * grid.width + col];
      const shade = v < 0 ? 154 : (v >= 65 ? 30 : 245);
      const o = (dst + col) * 4;
      img.data[o] = img.data[o + 1] = img.data[o + 2] = shade;
      img.data[o + 3] = 255;
    }
  }
  bctx.putImageData(img, 0, 0);
  view.scale = Math.min(canvas.width / grid.width, canvas.height / grid.height);
  view.offX = (canvas.width - grid.width * view.scale) / 2;
  view.offY = (canvas.height - grid.height * view.scale) / 2;
}

/** 底图画一次、叠加层每帧重画。 */
function drawMap() {
  ctx2d.clearRect(0, 0, canvas.width, canvas.height);
  if (!base) return;
  ctx2d.imageSmoothingEnabled = false;
  ctx2d.drawImage(base, view.offX, view.offY,
                  grid.width * view.scale, grid.height * view.scale);

  marks.forEach((m, i) => arrow(m.x, m.y, m.yaw, "#1d4ed8", String(i + 1)));

  const pose = (state.device || {}).pose;
  if (pose) arrow(pose.x, pose.y, pose.yaw, "#d1332e", "");

  if (drag && drag.to) {
    const a = worldToPx(drag.from.x, drag.from.y);
    const b = worldToPx(drag.to.x, drag.to.y);
    ctx2d.strokeStyle = "#1d4ed8";
    ctx2d.beginPath();
    ctx2d.moveTo(a.px, a.py);
    ctx2d.lineTo(b.px, b.py);
    ctx2d.stroke();
  }
}

function arrow(x, y, yaw, color, label) {
  const { px, py } = worldToPx(x, y);
  ctx2d.fillStyle = color;
  ctx2d.strokeStyle = color;
  ctx2d.lineWidth = 2;
  ctx2d.beginPath();
  ctx2d.arc(px, py, 5, 0, Math.PI * 2);
  ctx2d.fill();
  ctx2d.beginPath();
  ctx2d.moveTo(px, py);
  // y 轴在画布上是反的，所以朝向要取负。
  ctx2d.lineTo(px + Math.cos(yaw) * 16, py - Math.sin(yaw) * 16);
  ctx2d.stroke();
  if (label) {
    ctx2d.fillStyle = "#111";
    ctx2d.font = "12px sans-serif";
    ctx2d.fillText(label, px + 7, py - 7);
  }
}

function atCanvas(ev) {
  const box = canvas.getBoundingClientRect();
  return pxToWorld((ev.clientX - box.left) * canvas.width / box.width,
                   (ev.clientY - box.top) * canvas.height / box.height);
}

// 标点：按下的位置是坐标，拖出的方向是朝向。一次按下只出一个点。
canvas.addEventListener("mousedown", (ev) => {
  if (!grid) return;
  drag = { from: atCanvas(ev), to: null };
});
canvas.addEventListener("mousemove", (ev) => {
  if (!drag) return;
  drag.to = atCanvas(ev);
  drawMap();
});
canvas.addEventListener("mouseup", (ev) => {
  if (!drag) return;
  const to = atCanvas(ev);
  const dx = to.x - drag.from.x;
  const dy = to.y - drag.from.y;
  // 只点一下没拖：朝向保持 0，人可以在表格里改。
  const yaw = (Math.hypot(dx, dy) < 1e-3) ? 0 : Math.atan2(dy, dx);
  marks.push({ name: "P" + (marks.length + 1), x: drag.from.x, y: drag.from.y,
               yaw: yaw, check: "", photo: "front" });
  drag = null;
  renderMarks();
  drawMap();
});
canvas.addEventListener("mouseleave", () => { drag = null; drawMap(); });

const loadGrid = guard(async () => {
  const id = $("grid-map").value;
  if (!id) { grid = base = null; drawMap(); return; }
  grid = await api("/api/maps/" + encodeURIComponent(id) + "/grid");
  $("grid-source").textContent = grid.source === "live" ? "（还在建，未存盘）" : "";
  buildBase();
  drawMap();
});

$("grid-map").addEventListener("change", loadGrid);
on("grid-reload", loadGrid);

/* --------------------------------------------------------------- 实时画面 */

$("cam-pick").addEventListener("change", () => {
  const name = $("cam-pick").value;
  // 空 src 会让浏览器去重新请求当前页面，反倒开一条没用的连接。
  if (name) $("cam").src = withToken("/api/video/" + name + "?t=" + Date.now());
  else $("cam").removeAttribute("src");
});

/* ------------------------------------------------------------------ 遥控 */

const PULSE_S = 0.4;          // 和 app/teleop.py 的 DEFAULT_PULSE_S 对齐
const BEAT_MS = 200;          // 子规范 §5：每 200ms 一次；服务端 0.6s 不见就停车
const KEYS = {
  w: [1, 0, 0], s: [-1, 0, 0], a: [0, 1, 0],
  d: [0, -1, 0], q: [0, 0, 1], e: [0, 0, -1],
};
const held = new Set();
let beatTimer = null;

const gain = () => Number($("teleop-gain").value) / 100;

/** 走一拍。键盘和按钮走的是同一条路。 */
async function sendPulse(fwd, lat, yaw) {
  const g = gain();
  await api("/api/teleop", "POST",
            { fwd: fwd * g, lat: lat * g, yaw: yaw * g, seconds: PULSE_S });
}

/** 把当前按住的键合成一条指令。都松开了就发全零 —— 那就是"停"。 */
function pushHeld() {
  let f = 0, l = 0, y = 0;
  held.forEach((k) => { const v = KEYS[k]; f += v[0]; l += v[1]; y += v[2]; });
  sendPulse(Math.max(-1, Math.min(1, f)), Math.max(-1, Math.min(1, l)),
            Math.max(-1, Math.min(1, y))).catch((err) => banner(String(err.message)));
}

function teleopLive() {
  const panel = document.querySelector('[data-panel="mapping"]');
  return panel && !panel.hidden && document.hasFocus();
}

/** 只在遥控看得见且窗口有焦点时才跑那个 200ms 的定时器。 */
function syncTeleop() {
  if (teleopLive() && beatTimer === null) {
    beatTimer = setInterval(() => {
      api("/api/teleop/heartbeat", "POST").catch(() => {});
      if (held.size) pushHeld();
    }, BEAT_MS);
  } else if (!teleopLive() && beatTimer !== null) {
    clearInterval(beatTimer);
    beatTimer = null;
    if (held.size) { held.clear(); pushHeld(); }   // 走之前先停下来
  }
}

window.addEventListener("focus", syncTeleop);
window.addEventListener("blur", syncTeleop);

document.addEventListener("keydown", (ev) => {
  if (ev.repeat || !teleopLive()) return;
  const tag = (ev.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return;
  const key = ev.key.toLowerCase();
  if (!(key in KEYS)) return;
  ev.preventDefault();
  held.add(key);
  paintPad();
  pushHeld();
});

document.addEventListener("keyup", (ev) => {
  const key = ev.key.toLowerCase();
  if (!held.delete(key)) return;
  paintPad();
  pushHeld();          // 松手即停
});

function paintPad() {
  const live = new Set();
  held.forEach((k) => { const v = KEYS[k]; live.add(v.join(",")); });
  $$(".pad button").forEach((b) => {
    const sig = [b.dataset.fwd, b.dataset.lat, b.dataset.yaw].join(",");
    b.classList.toggle("on", live.has(sig));
  });
}

$$(".pad button").forEach((b) => {
  const go = guard(() => sendPulse(Number(b.dataset.fwd), Number(b.dataset.lat),
                                   Number(b.dataset.yaw)));
  b.addEventListener("mousedown", go);
  b.addEventListener("mouseup", guard(() => sendPulse(0, 0, 0)));
  b.addEventListener("mouseleave", () => { if (b.matches(":active")) sendPulse(0, 0, 0); });
});

$("teleop-gain").addEventListener("input", () => {
  $("teleop-gain-out").textContent = $("teleop-gain").value;
});

/* ------------------------------------------------------------------ 建图 */

async function refreshMapping() {
  const m = await api("/api/mapping");
  $("map-phase").textContent = { idle: "空闲", recording: "录包中",
                                 rebuilding: "重建中" }[m.phase] || m.phase;
  $("map-bag").textContent = m.bag ? "（" + m.bag + "）" : "";
  $("map-error").textContent = m.last_error || "";
  fill($("bag-pick"), m.bags || []);
  fill($("map-list"), m.maps || []);
  fill($("grid-map"), m.maps || [], true);
}

/** 往下拉框里填选项，尽量保住当前选中的那个。 */
function fill(sel, items, blank) {
  const keep = sel.value;
  sel.innerHTML = "";
  if (blank) sel.appendChild(new Option("（不画底图）", ""));
  items.forEach((it) => sel.appendChild(new Option(it, it)));
  if (items.includes(keep)) sel.value = keep;
}

on("rec-start", async () => {
  await api("/api/mapping/record/start", "POST", { name: $("bag-name").value.trim() });
  await refreshMapping();
});
on("rec-stop", async () => {
  await api("/api/mapping/record/stop", "POST");
  await refreshMapping();
});
on("rebuild", async () => {
  await api("/api/mapping/rebuild", "POST",
            { bag: $("bag-pick").value, map_id: $("rebuild-map-id").value.trim() });
  banner("重建已经在跑了。它要几分钟到几十分钟，进度看下面的日志。");
  await refreshMapping();
});

function renderProcs(names) {
  const sel = $("log-pick");
  const keep = sel.value;
  const all = Array.from(new Set(names.concat(["slam", "rebuild", "map_bridge",
                                               "pose_bridge", "patrol_agent"])));
  sel.innerHTML = "";
  all.forEach((n) => sel.appendChild(new Option(n, n)));
  if (all.includes(keep)) sel.value = keep;
}

on("log-reload", async () => {
  const name = $("log-pick").value;
  // 这条给的是纯文本不是 JSON，走不了 api()，所以请求头要自己带。
  // 它不在服务端 ?token= 的白名单里 —— 那个名单只给设不了请求头的标签。
  const t = token();
  const resp = await fetch("/api/procs/" + encodeURIComponent(name) + "/log",
                           t ? { headers: { authorization: "Bearer " + t } } : {});
  if (resp.status === 401) { setToken(""); showLock(); return; }
  $("log").textContent = await resp.text();
  $("log").scrollTop = $("log").scrollHeight;
});

/* ------------------------------------------------------------ 地图与点位 */

function renderCaps(caps) {
  // 能不能自动重定位由后端的能力集说了算，不由页面猜。
  $("pose-reset").disabled = !caps.includes("reloc");
}

on("map-load", async () => {
  await api("/api/maps/load", "POST", { map_id: $("map-list").value });
  banner("地图已载入：" + $("map-list").value);
});
on("pose-reset", async () => { await api("/api/pose/reset", "POST"); });
on("pose-initial", async () => {
  await api("/api/pose/initial", "POST", {
    x: Number($("pose-x").value), y: Number($("pose-y").value),
    yaw: Number($("pose-yaw").value),
  });
});

function renderMarks() {
  const body = $("wp-table").querySelector("tbody");
  body.innerHTML = "";
  marks.forEach((m, i) => {
    const tr = document.createElement("tr");
    tr.appendChild(cell(input(m.name, (v) => { m.name = v; })));
    tr.appendChild(cell(document.createTextNode(m.x.toFixed(2))));
    tr.appendChild(cell(document.createTextNode(m.y.toFixed(2))));
    tr.appendChild(cell(input(m.yaw.toFixed(3), (v) => { m.yaw = Number(v) || 0; })));
    tr.appendChild(cell(input(m.check, (v) => { m.check = v; }, "这个点看什么")));
    const pick = document.createElement("select");
    [["", "不拍"], ["front", "前"], ["back", "后"]].forEach(
      ([v, t]) => pick.appendChild(new Option(t, v)));
    pick.value = m.photo;
    pick.addEventListener("change", () => { m.photo = pick.value; });
    tr.appendChild(cell(pick));
    const del = document.createElement("button");
    del.textContent = "删";
    del.addEventListener("click", () => {
      marks.splice(i, 1); renderMarks(); drawMap();
    });
    tr.appendChild(cell(del));
    body.appendChild(tr);
  });
  $("mission-preview").textContent = JSON.stringify(missionBody(), null, 2);
}

function cell(node) { const td = document.createElement("td"); td.appendChild(node); return td; }

function input(value, onChange, placeholder) {
  const el = document.createElement("input");
  el.value = value;
  if (placeholder) el.placeholder = placeholder;
  el.addEventListener("change", () => { onChange(el.value); renderMarks(); });
  return el;
}

/* ------------------------------------------------------------------ 任务 */

/** 页面上的点位 -> 任务定义。字段名和 `engine/mission.py` 一一对应。 */
function missionBody() {
  return {
    mission: $("mission-name").value.trim(),
    map_id: $("mission-map").value.trim(),
    waypoints: marks.map((m) => {
      const wp = {
        name: m.name,
        pose: {
          position: { x: m.x, y: m.y, z: 0 },
          orientation: { x: 0, y: 0, z: Math.sin(m.yaw / 2), w: Math.cos(m.yaw / 2) },
        },
      };
      if (m.check) wp.check = m.check;
      if (m.photo) wp.actions = [{ type: "photo", camera: m.photo }];
      return wp;
    }),
  };
}

/** 任务定义 -> 页面上的点位。存过的任务打开之后还要能接着改。 */
function loadMission(m) {
  $("mission-name").value = m.mission || "";
  $("mission-map").value = m.map_id || "";
  marks = (m.waypoints || []).map((w) => {
    const o = (w.pose && w.pose.orientation) || { z: 0, w: 1 };
    const p = (w.pose && w.pose.position) || { x: 0, y: 0 };
    const photo = (w.actions || []).find((a) => a.type === "photo");
    return {
      name: w.name, x: p.x, y: p.y,
      yaw: Math.atan2(2 * o.w * o.z, 1 - 2 * o.z * o.z),
      check: w.check || "", photo: photo ? photo.camera : "",
    };
  });
  renderMarks();
  drawMap();
}

async function refreshMissions() {
  const got = await api("/api/missions");
  fill($("mission-list"), got.missions || []);
}

on("mission-open", async () => {
  loadMission(await api("/api/missions/" + encodeURIComponent($("mission-list").value)));
});

on("mission-save", async () => {
  const body = missionBody();
  if (!body.mission) throw new Error("先给任务起个名字");
  await api("/api/missions/" + encodeURIComponent(body.mission), "PUT", body);
  await refreshMissions();
  banner("已保存：" + body.mission);
});

on("mission-run", async () => {
  const name = $("mission-name").value.trim() || $("mission-list").value;
  try {
    const got = await api("/api/missions/" + encodeURIComponent(name) + "/run", "POST");
    renderChecks(got.checks || []);
    showTab("run");
  } catch (err) {
    // 起飞检查没过：五项各是什么样得原样摆出来，不能只说一句"起不了"。
    renderChecks((err.data && err.data.checks) || []);
    throw err;
  }
});

function renderChecks(checks) {
  const box = $("preflight");
  box.innerHTML = "";
  checks.forEach((c) => {
    const p = document.createElement("p");
    p.className = c.ok ? "hint" : "err";
    p.textContent = (c.ok ? "通过 " : "没过 ") + c.name + "：" + (c.detail || "");
    box.appendChild(p);
  });
}

/* ------------------------------------------------------------------ 运行 */

const RUN_STATE = {
  IDLE: "空闲", PREFLIGHT: "起飞检查", LOCALIZING: "重定位中", RUNNING: "跑着",
  PAUSED: "暂停", RETURNING: "返航中", ABORTING: "正在中止", ABORTED: "中止了",
  DONE: "跑完了",
};

function renderRun(run) {
  $("run-state").textContent = RUN_STATE[run.state] || run.state || "--";
  $("run-mission").textContent = run.mission || "--";
  $("run-progress").textContent = run.total
    ? (run.waypoint_index + " / " + run.total + "　" + (run.waypoint_name || ""))
    : "--";
  $("run-reason").textContent = run.reason || "";
  const body = $("run-table").querySelector("tbody");
  body.innerHTML = "";
  (run.results || []).forEach((r) => {
    const tr = document.createElement("tr");
    [r.name, r.ok ? "到了" : "没到", r.elapsed_s + " s",
     (r.photos || []).length, r.note || ""].forEach((v) => {
      const td = document.createElement("td");
      td.textContent = String(v);
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
}

on("run-pause", async () => { await api("/api/run/pause", "POST"); });
on("run-resume", async () => { await api("/api/run/resume", "POST"); });
on("run-abort", async () => {
  await api("/api/run/abort", "POST", { reason: "页面上点了中止" });
});

/* ------------------------------------------------------------ 判读与报告 */

const VERDICT = { normal: "正常", abnormal: "异常", unclear: "看不清", pending: "待判读" };

let openRun = "";

async function refreshRuns() {
  const got = await api("/api/runs");
  const sel = $("run-list");
  const keep = sel.value;
  sel.innerHTML = "";
  (got.runs || []).forEach((r) => {
    sel.appendChild(new Option(
      r.mission + "　" + r.started_at + "　" + (r.state || ""), r.id));
  });
  if (keep) sel.value = keep;
}

async function openRunDetail(id) {
  openRun = id;
  const got = await api("/api/runs/" + encodeURIComponent(id));
  const root = "/api/runs/" + encodeURIComponent(id);
  $("report-md").href = withToken(root + "/report.md");
  $("report-html").href = withToken(root + "/report.html");
  const byPhoto = {};
  (got.findings || []).forEach((f) => { byPhoto[f.photo] = f; });
  const box = $("photos");
  box.innerHTML = "";
  (got.photos || []).forEach((name) => {
    box.appendChild(photoRow(root, name, byPhoto[name], (got.reviews || {})[name]));
  });
  if (!got.photos || !got.photos.length) box.textContent = "这趟没有照片。";
}

/** 一张照片一行：左图，中间灰底是模型说的，右边白底是人说的。 */
function photoRow(root, name, finding, review) {
  const row = document.createElement("div");
  row.className = "photo";

  const img = document.createElement("img");
  img.src = withToken(root + "/photos/" + encodeURIComponent(name));
  img.alt = name;
  row.appendChild(img);

  const model = document.createElement("div");
  model.className = "verdict-model";
  const v = (finding && finding.verdict) || "pending";
  model.innerHTML = "<h4>模型判读</h4>";
  const tag = document.createElement("span");
  tag.className = "tag " + v;
  tag.textContent = VERDICT[v] || v;
  model.appendChild(tag);
  const why = document.createElement("p");
  why.textContent = (finding && finding.reason) || "还没判读。";
  model.appendChild(why);
  row.appendChild(model);

  const human = document.createElement("div");
  human.className = "verdict-human";
  human.innerHTML = "<h4>人工复核</h4>";
  const pick = document.createElement("select");
  [["", "（未复核）"], ["normal", "正常"], ["abnormal", "异常"],
   ["unclear", "看不清"]].forEach(([val, text]) => pick.appendChild(new Option(text, val)));
  pick.value = (review && review.verdict) || "";
  const note = document.createElement("input");
  note.placeholder = "说一句（可空）";
  note.value = (review && review.note) || "";
  const save = document.createElement("button");
  save.textContent = "记下";
  save.addEventListener("click", guard(async () => {
    if (!pick.value) throw new Error("先选一个结论");
    await api(root + "/review/" + encodeURIComponent(name), "POST",
              { verdict: pick.value, note: note.value });
    banner("复核已记下：" + name);
  }));
  human.appendChild(pick);
  human.appendChild(note);
  human.appendChild(save);
  row.appendChild(human);

  const label = document.createElement("div");
  label.className = "hint";
  label.textContent = name;
  row.appendChild(label);
  return row;
}

on("run-open", async () => {
  await refreshRuns();
  await openRunDetail($("run-list").value);
});

on("judge", async () => {
  if (!openRun) throw new Error("先打开一趟");
  banner("正在判读，几十张照片要等一会儿……");
  await api("/api/runs/" + encodeURIComponent(openRun) + "/judge", "POST");
  await openRunDetail(openRun);
  banner("判读完了。");
});

/* ------------------------------------------------------------------ 起步 */

on("lock-go", unlock);
$("lock-pin").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") unlock();
});

/* 第一个请求顺带就把"这台锁没锁"问出来了。
 *
 * 为什么不是靠 SSE 发现：`EventSource` 的 error 事件里没有状态码，401 和
 * "网线掉了"在它那儿长得一模一样，而它还会自己每两秒重连一次 —— 页面会一直
 * 空着，谁也不知道到底是要输 PIN 还是网断了。所以先打一个正常的请求。
 *
 * `refreshMapping` 不包 guard：guard 会把错误变成顶上一条红条，而 401 要的
 * 不是红条，是输 PIN 那一层。 */
(async () => {
  showTab("mapping");
  try {
    await refreshMapping();
  } catch (err) {
    if (err.status === 401) return;      // api() 已经把输 PIN 那层亮出来了
    banner(String(err.message || err));
  }
  connect();
  guard(refreshMissions)();
  guard(refreshRuns)();
})();
