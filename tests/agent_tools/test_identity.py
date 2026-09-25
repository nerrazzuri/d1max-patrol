"""机身身份 —— 这台机器是哪只狗。

**这一层为什么重要。** 每只 D1 Max 的内网地址都是同一套(Orin 永远
``192.168.168.100``),靠地址分不出谁是谁。归档里那句"这是哪只狗跑的"全靠
这个模块;它认错了,两只狗的巡检记录就会悄悄归成一堆 —— 而且是**事后无从
分辨**的那种混,因为照片和事件流本身不带机器信息。

所以这里盯得最紧的是"宁可说不知道,也不要说错":出厂占位串必须当成没有,
兜底出来的 SN 必须自己标明是兜底的。
"""

from __future__ import annotations

import pytest

from d1max_patrol.app.identity import (
    UNKNOWN_SN,
    Identity,
    clean_sn,
    read_macs,
    read_sn,
    resolve,
)

PIN = "704311"

#: 一份能过 ``parse_mission`` 的最小任务。点位上只 dwell,不拍照 ——
#: 这里测的是身份有没有落进 manifest,不是拍照那条路。
_MISSION = {
    "mission": "身份测试",
    "map_id": "map_test",
    "waypoints": [{
        "name": "P1",
        "pose": {"position": {"x": 1.0, "y": 0.0, "z": 0.0},
                 "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}},
        "check": "随便看看",
        "actions": [{"type": "dwell", "seconds": 0.01}],
    }],
}


# ------------------------------------------------------------------ 造假文件系统


def _sn_file(tmp_path, name: str, text: str):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _net(tmp_path, **ifaces: str):
    """造一份假的 ``/sys/class/net``。"""
    root = tmp_path / "net"
    root.mkdir()
    for name, mac in ifaces.items():
        d = root / name
        d.mkdir()
        (d / "address").write_text(mac + "\n", encoding="utf-8")
    return root


# ------------------------------------------------------------------ SN 清洗


def test_设备树读出来的nul结尾会被剥掉():
    """设备树里的字符串是 NUL 结尾的,直接读会带一个 NUL 在末尾。

    不剥的话 SN 里混着不可见字符,存进 JSON 是 ``\\u0000``,肉眼看不出来,
    而字符串比较又永远对不上。
    """
    assert clean_sn("1421521012345\x00") == "1421521012345"


def test_前后空白会被剥掉():
    assert clean_sn("  C40221 \n") == "C40221"


@pytest.mark.parametrize("junk", [
    "To be filled by O.E.M.",
    "Default string",
    "System Serial Number",
    "None",
    "0",
    "0123456789",
])
def test_出厂占位串一律当成没有(junk):
    """**这条是这个文件里最要紧的。**

    主板厂出货时会把 DMI 序列号填成这类占位串,而它在**每一台**机器上都是
    同一个值。真拿它当 SN,一整批机器会被认成同一只狗:归档全归到一处,
    照片对不上机器,而且事后没法分开 —— 谁也不知道哪张是哪只拍的。
    """
    assert clean_sn(junk) == ""


def test_一个字母数字都没有的值也当没有():
    """"---" "..." 这类是分隔符不是序列号。"""
    assert clean_sn("---") == ""
    assert clean_sn("  ...  ") == ""


def test_真序列号原样留着():
    assert clean_sn("C40221") == "C40221"
    assert clean_sn("1421521012345") == "1421521012345"


# ------------------------------------------------------------------ 按顺序找


def test_从第一个读得到的文件里取(tmp_path):
    a = _sn_file(tmp_path, "a", "AAA111")
    b = _sn_file(tmp_path, "b", "BBB222")
    sn, src = read_sn((a, b))
    assert sn == "AAA111"
    assert src == str(a)


def test_前面的文件不存在就往后找(tmp_path):
    b = _sn_file(tmp_path, "b", "BBB222")
    sn, _ = read_sn((tmp_path / "没有这个文件", b))
    assert sn == "BBB222"


def test_前面的文件是占位串也往后找(tmp_path):
    """读得到但是垃圾,跟读不到是一回事 —— 都得继续往下找。"""
    a = _sn_file(tmp_path, "a", "Default string")
    b = _sn_file(tmp_path, "b", "BBB222")
    sn, src = read_sn((a, b))
    assert sn == "BBB222"
    assert src == str(b)


def test_一个都没有就明说没有(tmp_path):
    assert read_sn((tmp_path / "无", tmp_path / "也无")) == ("", "")


def test_来源要指到具体哪个文件(tmp_path):
    """出了事要能顺着这条线回去看:值是从哪儿读的。"""
    a = _sn_file(tmp_path, "a", "AAA111")
    _, src = read_sn((a,))
    assert str(a) in src


# ------------------------------------------------------------------ MAC


def test_读得到全部网卡的mac(tmp_path):
    root = _net(tmp_path, eth0="aa:bb:cc:dd:ee:01", wlan0="aa:bb:cc:dd:ee:02")
    assert read_macs(root) == ("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02")


def test_环回口不算(tmp_path):
    """lo 在每台机器上都是全零,拿它认机器等于没认。"""
    root = _net(tmp_path, lo="00:00:00:00:00:00", eth0="aa:bb:cc:dd:ee:01")
    assert read_macs(root) == ("aa:bb:cc:dd:ee:01",)


def test_全零的mac不算(tmp_path):
    """有些虚拟口叫别的名字,但 MAC 一样是全零。按值挡,不只按名字挡。"""
    root = _net(tmp_path, dummy0="00:00:00:00:00:00", eth0="aa:bb:cc:dd:ee:01")
    assert read_macs(root) == ("aa:bb:cc:dd:ee:01",)


def test_大写的mac归一成小写(tmp_path):
    """同一块网卡在不同内核上大小写不一样,不归一的话会被当成两块。"""
    root = _net(tmp_path, eth0="AA:BB:CC:DD:EE:01")
    assert read_macs(root) == ("aa:bb:cc:dd:ee:01",)


def test_同一个mac只记一次(tmp_path):
    """桥接口跟物理口常常共用一个 MAC。"""
    root = _net(tmp_path, eth0="aa:bb:cc:dd:ee:01", br0="aa:bb:cc:dd:ee:01")
    assert read_macs(root) == ("aa:bb:cc:dd:ee:01",)


def test_排过序才输出(tmp_path):
    """顺序不稳的话,兜底 SN(取第一个 MAC)会在两次启动之间变来变去。"""
    root = _net(tmp_path, zzz="aa:bb:cc:dd:ee:09", aaa="aa:bb:cc:dd:ee:01")
    assert read_macs(root) == ("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:09")


def test_没有sys目录就是空不是报错(tmp_path):
    """开发机上(Windows)根本没有 /sys。那不是错误,是这台机器没有而已。"""
    assert read_macs(tmp_path / "根本不存在") == ()


# ------------------------------------------------------------------ 定身份


def test_人填的赢过文件里读的(tmp_path):
    """厂商接口不报 SN,现场贴的标签才是权威;文件里那个标识的是模组。"""
    a = _sn_file(tmp_path, "a", "模组序列号")
    who = resolve("C40221", files=(a,), net_root=tmp_path / "无")
    assert who.sn == "C40221"
    assert who.source == "配置"
    assert who.provisional is False


def test_人没填就用文件里的(tmp_path):
    a = _sn_file(tmp_path, "a", "AAA111")
    who = resolve(None, files=(a,), net_root=tmp_path / "无")
    assert who.sn == "AAA111"
    assert who.provisional is False


def test_人填的是空白等于没填(tmp_path):
    """``--sn ""`` 和 ``--sn "   "`` 都该往下找,而不是把空串当序列号。"""
    a = _sn_file(tmp_path, "a", "AAA111")
    assert resolve("   ", files=(a,), net_root=tmp_path / "无").sn == "AAA111"


def test_人填的是占位串也不认(tmp_path):
    """人也会图省事直接粘一个 unknown 进去。"""
    a = _sn_file(tmp_path, "a", "AAA111")
    assert resolve("unknown", files=(a,), net_root=tmp_path / "无").sn == "AAA111"


def test_都查不到就拿mac兜底而且标明是兜底(tmp_path):
    """兜底的值必须自己承认是兜底的。

    它是稳定的(重启不变、刷机不变),够用;但它标识的是网卡不是整机,
    换块网卡就变。人得看得见这一点,才知道该去补一个真的 SN。
    """
    root = _net(tmp_path, eth0="aa:bb:cc:dd:ee:01")
    who = resolve(None, files=(tmp_path / "无",), net_root=root)
    assert who.sn == "mac:aabbccddee01"
    assert who.source == "mac"
    assert who.provisional is True


def test_兜底取的是排序后的第一块网卡(tmp_path):
    """取哪一块必须是确定的,否则每次启动 SN 都不一样,归档会碎成好几堆。"""
    root = _net(tmp_path, zzz="aa:bb:cc:dd:ee:09", aaa="aa:bb:cc:dd:ee:01")
    a = resolve(None, files=(tmp_path / "无",), net_root=root)
    b = resolve(None, files=(tmp_path / "无",), net_root=root)
    assert a.sn == b.sn == "mac:aabbccddee01"


def test_连mac都没有就是unknown(tmp_path):
    """走投无路也要给个确定的答案,不能抛异常 —— app 得起得来。"""
    who = resolve(None, files=(tmp_path / "无",), net_root=tmp_path / "也无")
    assert who.sn == UNKNOWN_SN
    assert who.provisional is True


def test_mac不管有没有sn都记着(tmp_path):
    """SN 查到了 MAC 照样记:手机连热点时看得见 BSSID,不用登录就能认狗。"""
    root = _net(tmp_path, eth0="aa:bb:cc:dd:ee:01")
    a = _sn_file(tmp_path, "a", "AAA111")
    who = resolve(None, files=(a,), net_root=root)
    assert who.sn == "AAA111"
    assert who.macs == ("aa:bb:cc:dd:ee:01",)


def test_起的名字前后空白剥掉(tmp_path):
    who = resolve("AAA", "  三号  ", files=(tmp_path / "无",),
                  net_root=tmp_path / "无")
    assert who.nickname == "三号"


def test_没起名字就是空串(tmp_path):
    who = resolve("AAA", None, files=(tmp_path / "无",), net_root=tmp_path / "无")
    assert who.nickname == ""


def test_主机名也记下来(tmp_path):
    who = resolve("AAA", files=(tmp_path / "无",), net_root=tmp_path / "无",
                  host="orin-nx")
    assert who.host == "orin-nx"


# ------------------------------------------------------------------ 进指纹


def test_指纹里有sn():
    assert Identity("C40221", "配置", (), "", "h").fingerprint() == {
        "robot_sn": "C40221"}


def test_起了名字指纹里才有名字():
    """空名字不占位置 —— 指纹是给人看报告的,空字段只会挤走有用的信息。"""
    assert "robot_name" not in Identity("C40221", "配置", (), "", "h").fingerprint()
    named = Identity("C40221", "配置", (), "三号", "h").fingerprint()
    assert named["robot_name"] == "三号"


def test_兜底的sn在指纹里要标出来():
    """报告上得看得出这个 SN 靠不靠得住,不然日后按它归堆会归错。"""
    fp = Identity("mac:aabb", "mac", (), "", "h").fingerprint()
    assert fp["robot_sn_source"] == "mac"


def test_权威的sn不带来源字段():
    """真 SN 就是真的,不用再解释一句 —— 指纹上每一格都很贵。"""
    fp = Identity("C40221", "配置", (), "", "h").fingerprint()
    assert "robot_sn_source" not in fp


def test_指纹里不放mac列表():
    """一串网卡地址在报告上没人看,只会把 SDK 版本、地图 ID 挤下去。"""
    fp = Identity("C40221", "配置", ("aa:bb:cc:dd:ee:01",), "", "h").fingerprint()
    assert not any("aa:bb" in str(v) for v in fp.values())


# ------------------------------------------------ 身份真的进了归档(最要紧的一条)


# ---------------------------------------------------- §6.2:SN 必须手写


def test_人填的SN才算手写的(tmp_path):
    who = resolve("C40221", files=(tmp_path / "无",), net_root=tmp_path / "无")
    assert who.hand_written is True


def test_从设备树读出来的不算手写(tmp_path):
    """§6.2:设备树里那个标识的是**模组**,换一次主板就变;客户认的是机身
    上贴的那张标签。它够稳定(所以 ``provisional`` 是 ``False``),但它不是
    机身编号。
    """
    a = _sn_file(tmp_path, "a", "AAA111")
    who = resolve(None, files=(a,), net_root=tmp_path / "无")
    assert who.provisional is False
    assert who.hand_written is False


def test_mac兜底的更不算手写(tmp_path):
    root = _net(tmp_path, eth0="aa:bb:cc:dd:ee:01")
    who = resolve(None, files=(tmp_path / "无",), net_root=root)
    assert who.hand_written is False


def test_手写这件事发得出去(tmp_path):
    who = resolve("C40221", files=(tmp_path / "无",), net_root=tmp_path / "无")
    assert who.to_wire()["hand_written"] is True
