import socket, struct, time, math, binascii
targets = {"192.168.1.200":"FRONT", "192.168.2.200":"REAR"}
got={}
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003)); s.settimeout(20)
t0=time.time()
while time.time()-t0<20 and len(got)<2:
    try: raw,_=s.recvfrom(65535)
    except socket.timeout: break
    if len(raw)<42 or struct.unpack("!H",raw[12:14])[0]!=0x0800 or raw[23]!=17: continue
    src=".".join(str(b) for b in raw[26:30]); ihl=(raw[14]&0x0F)*4; udp=14+ihl
    if struct.unpack("!H",raw[udp+2:udp+4])[0]!=7788 or src not in targets or src in got: continue
    got[src]=raw[udp+8:]
def be(p,o): return struct.unpack(">f",p[o:o+4])[0]
def q_R_and_score(q):
    x,y,z,w=q; import numpy as np
    return None
for src,name in targets.items():
    if src not in got: print(f"{name}: 没抓到(狗可能关了)"); continue
    p=got[src]; print(f"\n===== {name} ({src}) len={len(p)} =====")
    # dump hex 1080..1124
    for base in (1080,1088,1096,1104,1112,1120):
        chunk=p[base:base+8]
        print(f"  [{base:4d}] {binascii.hexlify(chunk).decode()}")
    print(f"  -- interpret as BE float32 at offset 1084 (my orig) vs 1092 (reviewer/manual) --")
    for O,tag in ((1084,"1084"),(1092,"1092")):
        vals=[be(p,O+4*i) for i in range(7)]
        n=math.sqrt(sum(v*v for v in vals[:4]))
        print(f"  off {tag}: q=({vals[0]:+.6f},{vals[1]:+.6f},{vals[2]:+.6f},{vals[3]:+.6f}) |q|={n:.5f}  t=({vals[4]:+.6f},{vals[5]:+.6f},{vals[6]:+.6f})")
    # save raw 28-byte blocks
    open(f"/tmp/difop_{name}_1084.bin","wb").write(p[1084:1112])
    open(f"/tmp/difop_{name}_1092.bin","wb").write(p[1092:1120])
    open(f"/tmp/difop_{name}_full.bin","wb").write(p)
