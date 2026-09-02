#!/usr/bin/env python3
"""D1 Max 实时地图看板 —— 浏览器里看机器在自建地图中的实时位姿 + 移动按钮。

单文件、纯标准库 + rclpy：
  · rclpy 订 TF loc_map→base_link（实时定位器发的），拿机器在地图中的 x/y/yaw
  · http.server 提供网页 + /map.png + /events(SSE 实时推位姿) + /cmd(POST 转发移动指令)
  · 移动按钮把 walk/stand/lie/estop 经 TCP JSONL 发给 patrol_agent(127.0.0.1:8090)

跑法（要 source ROS + zenoh，且实时定位器已在跑）：
  source /opt/ros/humble/setup.bash
  export ROS_DOMAIN_ID=24 RMW_IMPLEMENTATION=rmw_zenoh_cpp
  python3 scripts/live_viewer.py --map runs/slam/d1max_spin_map --agent 127.0.0.1:8090 --port 8095
然后浏览器开 http://localhost:8095
"""
from __future__ import annotations

import argparse
import json
import math
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rclpy
from rclpy.time import Time
from tf2_ros import Buffer, TransformException, TransformListener

STATE = {"x": None, "y": None, "yaw": None, "ok": False, "t": 0.0}
STATE_LOCK = threading.Lock()
CFG = {}


def read_map(stem: str):
    """读 <stem>.yaml(分辨率/原点) 和 <stem>.pgm(栅格宽高)。"""
    res, ox, oy = 0.05, 0.0, 0.0
    with open(stem + ".yaml") as f:
        for line in f:
            line = line.strip()
            if line.startswith("resolution:"):
                res = float(line.split(":")[1])
            elif line.startswith("origin:"):
                nums = line.split("[")[1].split("]")[0].split(",")
                ox, oy = float(nums[0]), float(nums[1])
    # pgm 头拿宽高
    w = h = 0
    with open(stem + ".pgm", "rb") as f:
        data = f.read(64)
    toks = data.split(None, 4)  # P5 W H maxval ...
    if toks[0] == b"P5":
        w, h = int(toks[1]), int(toks[2])
    with open(stem + ".png", "rb") as f:
        png = f.read()
    return {"res": res, "ox": ox, "oy": oy, "w": w, "h": h, "png": png}


def quat_yaw(qx, qy, qz, qw):
    return math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))


def tf_thread(map_frame: str):
    rclpy.init()
    node = rclpy.create_node("live_viewer_tf")
    buf = Buffer()
    TransformListener(buf, node)
    spin = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    spin.start()
    while True:
        try:
            tr = buf.lookup_transform(map_frame, "base_link", Time())
            t = tr.transform.translation
            q = tr.transform.rotation
            with STATE_LOCK:
                STATE.update(x=t.x, y=t.y,
                             yaw=quat_yaw(q.x, q.y, q.z, q.w),
                             ok=True, t=time.time())
        except TransformException:
            with STATE_LOCK:
                if time.time() - STATE["t"] > 1.5:
                    STATE["ok"] = False
        time.sleep(0.1)


def send_agent(cmd: dict) -> str:
    host, port = CFG["agent_host"], CFG["agent_port"]
    try:
        with socket.create_connection((host, port), timeout=3) as s:
            s.sendall((json.dumps(cmd) + "\n").encode())
            s.settimeout(1.0)
            try:
                return s.recv(512).decode(errors="replace")
            except OSError:
                return "sent"
    except OSError as e:
        return f"agent错误: {e}"


PAGE = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>D1 Max 实时地图</title>
<style>
 :root{--bg:#0c1114;--panel:#141c21;--edge:#26333a;--ink:#e7edf0;--muted:#93a3ac;
  --accent:#2bc4b3;--free:#3fc0bc;--occ:#f0a05a;--warn:#e0873b;}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"IBM Plex Sans",system-ui,sans-serif;}
 .wrap{max-width:1180px;margin:0 auto;padding:16px;display:grid;
  grid-template-columns:1fr 300px;gap:16px}
 @media(max-width:820px){.wrap{grid-template-columns:1fr}}
 h1{font-size:16px;margin:0 0 4px;letter-spacing:.02em}
 .sub{color:var(--muted);font-size:12px;font-family:"IBM Plex Mono",monospace}
 .mapbox{position:relative;background:#0a0f12;border:1px solid var(--edge);
  border-radius:12px;overflow:hidden}
 .mapbox img{display:block;width:100%;image-rendering:pixelated}
 #robot{position:absolute;width:0;height:0;transform:translate(-50%,-50%);
  transition:left .15s linear,top .15s linear}
 #robot .body{width:16px;height:16px;border-radius:50%;background:var(--accent);
  border:2px solid #fff;box-shadow:0 0 0 4px rgba(43,196,179,.28);
  position:absolute;left:-8px;top:-8px}
 #robot .head{position:absolute;left:-2px;top:-2px;width:4px;height:22px;
  background:linear-gradient(var(--accent),transparent);transform-origin:2px 2px}
 .panel{background:var(--panel);border:1px solid var(--edge);border-radius:12px;padding:14px}
 .stat{display:flex;justify-content:space-between;padding:7px 0;
  border-bottom:1px dashed var(--edge);font-size:13px}
 .stat:last-child{border:0}
 .stat b{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}
 .pill{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;
  font-family:"IBM Plex Mono",monospace}
 .on{background:rgba(63,192,188,.16);color:var(--free)}
 .off{background:rgba(224,135,59,.18);color:var(--warn)}
 .pad{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}
 button{font:inherit;color:var(--ink);background:#1b2831;border:1px solid var(--edge);
  border-radius:10px;padding:12px 6px;cursor:pointer;font-size:13px}
 button:hover{border-color:var(--accent);color:var(--accent)}
 button:active{transform:translateY(1px)}
 button.wide{grid-column:1/4}
 button.stop{background:#3a1d1d;border-color:#6b3030;color:#f2b5b5}
 button.stop:hover{border-color:#e06a6a;color:#ff9a9a}
 .lab{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.12em;
  margin:14px 0 6px;font-family:"IBM Plex Mono",monospace}
 .step{display:flex;gap:8px;align-items:center;margin-bottom:8px;font-size:12px;color:var(--muted)}
 .step input{width:60px;background:#0a0f12;border:1px solid var(--edge);color:var(--ink);
  border-radius:6px;padding:4px;font-family:"IBM Plex Mono",monospace}
 #log{font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--muted);
  margin-top:10px;max-height:96px;overflow:auto;white-space:pre-wrap}
</style></head><body>
<div class=wrap>
 <div>
  <h1>D1 Max · 实时地图定位</h1>
  <div class=sub id=hdr>连接中…</div>
  <div class=mapbox style="margin-top:10px">
   <img id=map src="/map.png" alt=map>
   <div id=robot><div class=body></div><div class=head></div></div>
  </div>
 </div>
 <div class=panel>
  <div class=stat><span>定位</span><span id=fix class="pill off">等待</span></div>
  <div class=stat><span>X (m)</span><b id=px>—</b></div>
  <div class=stat><span>Y (m)</span><b id=py>—</b></div>
  <div class=stat><span>朝向 (°)</span><b id=pyaw>—</b></div>

  <div class=lab>移动（点一下走一小步）</div>
  <div class=step>步长
   <input id=sec type=number value=1.5 step=0.5 min=0.5 max=5> 秒 ·
   <input id=spd type=number value=0.35 step=0.05 min=0.1 max=0.5> 量</div>
  <div class=pad>
   <button onclick="turn(1)">↰ 左转</button>
   <button onclick="mv('fwd',1)">↑ 前进</button>
   <button onclick="turn(-1)">↱ 右转</button>
   <button onclick="cmd({cmd:'stand'})">🧍 站起</button>
   <button onclick="mv('fwd',-1)">↓ 后退</button>
   <button onclick="cmd({cmd:'lie'})">🛌 趴下</button>
   <button class="wide stop" onclick="cmd({cmd:'estop',on:true})">■ 急停</button>
   <button class="wide" onclick="cmd({cmd:'estop',on:false})">▶ 解除急停</button>
  </div>
  <div id=log></div>
 </div>
</div>
<script>
 let META=null;
 fetch('/meta').then(r=>r.json()).then(m=>{META=m;});
 function log(s){const l=document.getElementById('log');
   l.textContent=(new Date().toLocaleTimeString()+'  '+s+'\\n')+l.textContent;}
 function cmd(o){fetch('/cmd',{method:'POST',body:JSON.stringify(o)})
   .then(r=>r.text()).then(t=>log(JSON.stringify(o)+' → '+t.trim()));}
 function mv(kind,dir){const sec=+document.getElementById('sec').value;
   const spd=+document.getElementById('spd').value;
   cmd({cmd:'walk',seconds:sec,fwd:dir*spd,lat:0,yaw:0});}
 function turn(dir){const sec=+document.getElementById('sec').value;
   const spd=+document.getElementById('spd').value;
   cmd({cmd:'walk',seconds:sec,fwd:0,lat:0,yaw:dir*spd});}
 // 世界坐标 → 图上百分比
 function place(x,y,yaw){if(!META)return;
   const col=(x-META.ox)/META.res, row=META.h-1-(y-META.oy)/META.res;
   const rb=document.getElementById('robot');
   rb.style.left=(col/META.w*100)+'%'; rb.style.top=(row/META.h*100)+'%';
   // 屏幕 y 向下：世界 yaw 逆时针 → 屏幕顺时针，取 -yaw；箭头默认朝上，+90°校正
   document.querySelector('#robot .head').style.transform=
     'rotate('+(-yaw*180/Math.PI+90+180)+'deg)';}
 const es=new EventSource('/events');
 es.onmessage=e=>{const d=JSON.parse(e.data);
   const fix=document.getElementById('fix');
   document.getElementById('hdr').textContent='地图 '
     +(META?META.w+'×'+META.h+' @ '+META.res+'m':'')+' · loc_map 帧';
   if(d.ok){fix.textContent='已锁定';fix.className='pill on';
     document.getElementById('px').textContent=d.x.toFixed(3);
     document.getElementById('py').textContent=d.y.toFixed(3);
     document.getElementById('pyaw').textContent=(d.yaw*180/Math.PI).toFixed(1);
     document.getElementById('robot').style.display='block';
     place(d.x,d.y,d.yaw);
   }else{fix.textContent='等待定位';fix.className='pill off';
     document.getElementById('robot').style.display='none';}};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        elif self.path == "/map.png":
            self._send(200, "image/png", CFG["map"]["png"])
        elif self.path == "/meta":
            m = CFG["map"]
            body = json.dumps({"res": m["res"], "ox": m["ox"], "oy": m["oy"],
                               "w": m["w"], "h": m["h"]}).encode()
            self._send(200, "application/json", body)
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    with STATE_LOCK:
                        s = dict(STATE)
                    payload = json.dumps({"x": s["x"], "y": s["y"],
                                          "yaw": s["yaw"], "ok": s["ok"]})
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.15)
            except (BrokenPipeError, ConnectionResetError):
                return
        else:
            self._send(404, "text/plain", b"404")

    def do_POST(self):
        if self.path == "/cmd":
            n = int(self.headers.get("Content-Length", 0))
            try:
                cmd = json.loads(self.rfile.read(n).decode() or "{}")
            except json.JSONDecodeError:
                self._send(400, "text/plain", b"bad json")
                return
            reply = send_agent(cmd)
            self._send(200, "text/plain; charset=utf-8", reply.encode())
        else:
            self._send(404, "text/plain", b"404")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", default="runs/slam/d1max_spin_map")
    ap.add_argument("--agent", default="127.0.0.1:8090")
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--map-frame", default="loc_map")
    a = ap.parse_args()
    h, p = a.agent.split(":")
    CFG["agent_host"], CFG["agent_port"] = h, int(p)
    CFG["map"] = read_map(a.map)
    print(f"[viewer] 地图 {CFG['map']['w']}x{CFG['map']['h']} @ {CFG['map']['res']}m "
          f"origin=({CFG['map']['ox']},{CFG['map']['oy']})")
    threading.Thread(target=tf_thread, args=(a.map_frame,), daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    print(f"[viewer] http://localhost:{a.port}  (agent {a.agent}, map_frame {a.map_frame})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
