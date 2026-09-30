"""W00c5d 第二部分:狗上正在用的那一张图。下载边下边核,对不上整张不装;
载入成功才换正在用的,别的删掉。"""

from __future__ import annotations

import hashlib
import json

import pytest

from d1max_agent.maps import (
    FetchRefused,
    MapInstallError,
    MapIntegrityError,
    MapKeeper,
    download_deadline_s,
)
from d1max_contract.maps import MapRef


def _ref(map_id, version, files):
    return MapRef.from_wire({"map_id": map_id, "version": version, "files": [
        {"name": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()}
        for n, d in files.items()]})


class 站点:
    def __init__(self):
        self.files: dict[tuple[str, str, str], bytes] = {}
        self.fail = False
        self.calls: list[tuple[str, int]] = []
        #: 文件名 → 这个文件前几次下到第几个字节就断
        self.drop: dict[str, list[int]] = {}

    def add(self, map_id, version, files):
        for n, d in files.items():
            self.files[(map_id, version, n)] = d
        return _ref(map_id, version, files)

    def fetch(self, map_id, version, name, offset=0):
        self.calls.append((name, offset))
        if self.fail:
            raise ConnectionError("站点连不上")
        data = self.files[(map_id, version, name)]
        cut = self.drop.get(name, [])
        stop = cut.pop(0) if cut else None
        for i in range(offset, len(data), 3):
            if stop is not None and i >= stop:
                raise ConnectionResetError("WiFi 断了")
            yield data[i:i + 3]


@pytest.fixture
def k(tmp_path):
    s = 站点()
    s.slept = []
    return MapKeeper(tmp_path / "maps", fetch=s.fetch, sleep=s.slept.append), s


def test_装好_提交_重启还认得_别的图删掉(k, tmp_path):
    keeper, site = k
    a = site.add("estate-1", "7", {"a.pgm": b"old map", "home.json": b'{"x":1,"y":2,"yaw":0.5}'})
    keeper.install(a)
    keeper.commit(a)
    assert keeper.active() == a and keeper.home_of(a) == (1.0, 2.0, 0.5)
    b = site.add("estate-1", "8", {"a.pgm": b"new map!"})
    d = keeper.install(b)
    assert (d / "a.pgm").read_bytes() == b"new map!"
    assert keeper.active() == a, "装好不等于换了:载入成功才提交"
    keeper.commit(b)
    again = MapKeeper(tmp_path / "maps", fetch=site.fetch)
    assert again.active() == b
    assert not (tmp_path / "maps" / "estate-1" / "7").exists(), "狗上只留正在用的那一张"
    assert again.home_of(b) is None


def test_哈希或大小对不上_整张不装_照旧用原来的(k, tmp_path):
    keeper, site = k
    a = site.add("m", "1", {"x.pgm": b"good"})
    keeper.install(a)
    keeper.commit(a)
    b = site.add("m", "2", {"x.pgm": b"good2", "y.yaml": b"y"})
    site.files[("m", "2", "y.yaml")] = b"z"               # 路上坏了一个字节
    with pytest.raises(MapInstallError):
        keeper.install(b)
    assert not (tmp_path / "maps" / "m" / "2").exists() and keeper.active() == a
    site.files[("m", "2", "y.yaml")] = b"yy"              # 比清单大
    with pytest.raises(MapInstallError):
        keeper.install(b)
    site.fail = True
    with pytest.raises(MapInstallError):
        keeper.install(site.add("m", "3", {"x.pgm": b"3"}))
    assert keeper.active() == a


def test_载不进去就扔掉_正在用的只读声明_坏没坏归完整校验(k, tmp_path):
    """W09g:``active()`` 只读 ``active.json`` 的声明(状态查询、周期里随便调,不碰文件);文件坏没坏由
    ``verify_active()`` 判(原来大小不对就回 None,起来时悄悄退回 ``--map``)。"""
    keeper, site = k
    a = site.add("m", "1", {"x.pgm": b"good"})
    keeper.install(a)
    keeper.commit(a)
    b = site.add("m", "2", {"x.pgm": b"bad"})
    keeper.install(b)
    keeper.discard(b)
    assert not (tmp_path / "maps" / "m" / "2").exists() and keeper.active() == a
    (tmp_path / "maps" / "m" / "1" / "x.pgm").write_bytes(b"goo")   # 大小不对了(写坏了一半)
    assert keeper.active() == a, "声明还是它"
    with pytest.raises(MapIntegrityError, match="x.pgm"):
        keeper.verify_active()
    (tmp_path / "maps" / "active.json").write_text(json.dumps({"map_id": "../x"}))
    assert keeper.active() is None
    with pytest.raises(MapIntegrityError, match="active.json"):
        keeper.declared()


def test_下载断了从断点接着下_哈希照样核(k):
    """W09c 决定 8:几百 MB 的先验走 WiFi,断一下就从 0 重下是永远下不完的。"""
    keeper, site = k
    big = bytes(range(256)) * 4
    a = site.add("m", "1", {"prior.mm": big, "floor.yaml": b"y"})
    site.drop["prior.mm"] = [150, 300, 450, 600, 750, 900]   # 断六次,每次都有进展:不算连着失败
    d = keeper.install(a)
    assert (d / "prior.mm").read_bytes() == big
    assert [c[1] for c in site.calls if c[0] == "prior.mm"] == [0, 150, 300, 450, 600, 750, 900]
    assert site.slept == [1.0] * 6, "有进展就重新数:退避从 1 s 起"


def test_一直连不上_试几次就放弃_不挂满期限(k):
    keeper, site = k
    a = site.add("m", "1", {"x.pgm": b"x"})
    site.fail = True
    with pytest.raises(MapInstallError, match="连不上"):
        keeper.install(a)
    assert len(site.calls) == 5 and site.slept == [1.0, 2.0, 4.0, 8.0], "退避,不是死循环"


def test_站点说没有这个_不重试(tmp_path):
    calls = []

    def fetch(*a, **k):
        calls.append(a)
        raise FetchRefused("404 没有这个")
        yield b""

    keeper = MapKeeper(tmp_path / "maps", fetch=fetch, sleep=lambda s: None)
    with pytest.raises(MapInstallError, match="没有这个"):
        keeper.install(_ref("m", "1", {"x.pgm": b"x"}))
    assert len(calls) == 1


def test_期限按大小算_至少半小时():
    assert download_deadline_s(10) == 1800.0
    assert download_deadline_s(530 * 10**6) == pytest.approx(530 * 8, rel=0.01), "按 1 Mbit/s"


def test_建好的图放进本地库_硬链接不多占盘_发件箱删了还在(k, tmp_path):
    """W09c 决定 7:建图的狗不再从站点下回来。"""
    import os
    keeper, site = k
    out = tmp_path / "outbox" / "m" / "1"
    out.mkdir(parents=True)
    files = {"prior.mm": b"prior", "floor.pgm": b"P5"}
    for n, d in files.items():
        (out / n).write_bytes(d)
    a = _ref("m", "1", files)
    d = keeper.adopt(out, a)
    assert os.stat(d / "prior.mm").st_ino == os.stat(out / "prior.mm").st_ino
    for n in files:
        (out / n).unlink()                                # 发件箱传完就删
    assert keeper.install(a) == d and site.calls == [], "本地有、核得上:不下载"
    assert (d / "prior.mm").read_bytes() == b"prior"


def test_本地那份核不上就照样下载(k, tmp_path):
    keeper, site = k
    a = site.add("m", "1", {"prior.mm": b"prior"})
    out = tmp_path / "outbox"
    out.mkdir()
    (out / "prior.mm").write_bytes(b"prior")
    d = keeper.adopt(out, a)
    (d / "prior.mm").write_bytes(b"PRIOR")                # 同样大小,内容坏了
    keeper.install(a)
    assert site.calls == [("prior.mm", 0)] and (d / "prior.mm").read_bytes() == b"prior"


def test_正在用的那张不被收养的覆盖(k, tmp_path):
    keeper, site = k
    a = site.add("m", "1", {"x.pgm": b"good"})
    keeper.install(a)
    keeper.commit(a)
    out = tmp_path / "outbox"
    out.mkdir()
    (out / "x.pgm").write_bytes(b"good")
    import os
    before = os.stat(keeper.dir_of(a) / "x.pgm").st_ino
    assert keeper.adopt(out, a) == keeper.dir_of(a)
    assert keeper.active() == a
    assert os.stat(keeper.dir_of(a) / "x.pgm").st_ino == before, "正在用的那份不删了重建"


def test_站点没说错却只给了一半_当断了接着下(tmp_path):
    data = bytes(range(200))
    calls = []

    def fetch(map_id, version, name, offset=0):
        calls.append(offset)
        yield data[offset:offset + 120]                      # 每次只给 120 字节就结束

    keeper = MapKeeper(tmp_path / "maps", fetch=fetch, sleep=lambda s: None)
    d = keeper.install(_ref("m", "1", {"x.pgm": data}))
    assert (d / "x.pgm").read_bytes() == data and calls == [0, 120]


def test_站点不认Range_整个给回来_前面那段扔掉(tmp_path):
    """老站点(W09c 之前)不认 ``Range``:回 200 整个文件。"""
    import http.server
    import threading

    from d1max_agent.maps import https_fetch
    data = bytes(range(256)) * 40

    class 老站点(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), 老站点)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        fetch = https_fetch(f"http://127.0.0.1:{srv.server_port}", None)
        assert b"".join(fetch("m", "1", "x", offset=3000)) == data[3000:]
        assert b"".join(fetch("m", "1", "x")) == data
    finally:
        srv.shutdown()


def test_换图提交不碰点开头的目录_清理出错不往外抛(k, tmp_path, monkeypatch):
    """内审应修 5:建图收尾(收养)跟换图同时做 —— 提交的清理把正在收养的临时目录删了、或者
    清理里 rmdir 抛了,``active.json`` 已经换了却当成「记不下」。"""
    from pathlib import Path
    keeper, site = k
    a = site.add("m", "1", {"x.pgm": b"1"})
    keeper.install(a)
    adopting = tmp_path / "maps" / ".incoming" / "m@9.adopt"
    adopting.mkdir(parents=True)
    (adopting / "prior.mm").write_bytes(b"p")
    b = site.add("n", "1", {"x.pgm": b"n"})
    keeper.install(b)

    def 不给删(self):
        raise OSError("目录不空")
    monkeypatch.setattr(Path, "rmdir", 不给删)
    keeper.commit(a)                                      # m/1 正在用:清掉 n/1,n/ 删不掉也不抛
    assert keeper.active() == a
    assert (adopting / "prior.mm").exists()


def test_收养来的留一份_等不到空闲扔掉时也不删_再收养一份才换掉(k, tmp_path):
    """内审再议 12:建图的狗那份很容易被删(等空闲超时的 discard、下一次随便什么 commit),发件箱那份
    传完也删了,再激活就要重下几百 MB。"""
    keeper, site = k
    a = site.add("m", "1", {"x.pgm": b"1"})
    keeper.install(a)
    keeper.commit(a)
    out = tmp_path / "outbox"

    def 建(v):
        d = out / v
        d.mkdir(parents=True)
        (d / "prior.mm").write_bytes(v.encode())
        return keeper.adopt(d, _ref("m", v, {"prior.mm": v.encode()})), _ref(
            "m", v, {"prior.mm": v.encode()})

    d2, r2 = 建("2")
    keeper.commit(a)                                      # 比如重发了一次原点
    assert keeper.local(r2) == d2
    keeper.discard(r2)                                    # 等不到空闲
    assert keeper.local(r2) == d2
    d3, r3 = 建("3")
    keeper.commit(a)
    assert keeper.local(r3) == d3 and keeper.local(r2) is None, "只留最近收养的那一份"


def test_站点回4xx_只有没有这个才算拒_证书一时认不出照样重试(tmp_path):
    """内审小 8:站点狗专用口回 403 的意思是「证书一时认不出,过一会儿再来」。"""
    import http.server
    import threading

    from d1max_agent.maps import https_fetch
    codes = {"/maps/m/1/gone": 404, "/maps/m/1/cert": 403, "/maps/m/1/range": 416}

    class 站(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(codes[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), 站)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        fetch = https_fetch(f"http://127.0.0.1:{srv.server_port}", None)
        for name, refused in (("gone", True), ("range", True), ("cert", False)):
            with pytest.raises(Exception) as e:
                list(fetch("m", "1", name))
            assert isinstance(e.value, FetchRefused) == refused, name
    finally:
        srv.shutdown()
