"""盘上永远留两份:``current`` 和 ``previous``(§3.2)。

**这个文件里没有一个 sleep。** 时刻全是传进来的(§8.5 第 2 条)。
"""

from __future__ import annotations

import json
import shutil

import pytest

from d1max_patrol.engine.bundle import (
    CURRENT_LINK,
    LANDED,
    PREVIOUS_LINK,
    BundleError,
    active_bundle,
    apply_bundle,
    build_bundle,
    land,
    mark_proven,
    prune_bundles,
    read_state,
    rollback_bundle,
)

时刻 = "2026-09-07T14:03:00+08:00"
后来 = "2026-09-08T02:11:00+08:00"


def 源(tmp_path, 记号: str):
    src = tmp_path / f"src-{记号}"
    (src / "missions").mkdir(parents=True, exist_ok=True)
    (src / "schedule.yaml").write_text(
        f"timezone: Asia/Kuala_Lumpur\nentries: []\n# {记号}\n", encoding="utf-8")
    return src


def 打包并落(tmp_path, root, version: int, *, targets=()):
    """打一个包,落到 ``root``。返回槽名。**只落盘,不换链。**"""
    staged = build_bundle(源(tmp_path, f"v{version}"), tmp_path / "staging",
                          bundle_id="site-kl", version=version, built_at=时刻,
                          targets=targets)
    return land(root, staged).slot_name


@pytest.fixture
def root(tmp_path):
    d = tmp_path / "bundles"
    d.mkdir()
    return d


# ---- land:只落盘 ---------------------------------------------------------

def test_落盘之后包在但链还没换(tmp_path, root):
    """**定夺 11。** 落好了但还没生效,是一个必须能被看到的状态。"""
    槽 = 打包并落(tmp_path, root, 1)
    assert (root / 槽).is_dir()
    assert not (root / CURRENT_LINK).exists()
    assert active_bundle(root) is None
    assert read_state(root).current == ""


def test_落一个坏包会被拦住(tmp_path, root):
    staged = build_bundle(源(tmp_path, "x"), tmp_path / "staging",
                          bundle_id="site-kl", version=1, built_at=时刻)
    (staged / "schedule.yaml").write_text("改过了\n", encoding="utf-8")
    with pytest.raises(BundleError, match="content_sha256"):
        land(root, staged)
    assert not (root / "site-kl-1").exists()


def test_落两遍同一个包不炸(tmp_path, root):
    """网络会重传。**同一个包落第二遍必须是空操作**,不是报错 ——
    报错会让重传逻辑变成一个要处理特例的东西。
    """
    staged = build_bundle(源(tmp_path, "a"), tmp_path / "s1",
                          bundle_id="site-kl", version=1, built_at=时刻)
    land(root, staged)
    再 = build_bundle(源(tmp_path, "a"), tmp_path / "s2",
                      bundle_id="site-kl", version=1, built_at=时刻)
    assert land(root, 再).slot_name == "site-kl-1"


def test_同一个版本号换了内容就拒(tmp_path, root):
    """§3.2:版本号是单调递增的。同号不同内容,说明上游改了包没升版号 ——
    收下的话,「狗上是哪一版」这句话就再也对不上服务器那句了。
    """
    staged = build_bundle(源(tmp_path, "a"), tmp_path / "s1",
                          bundle_id="site-kl", version=1, built_at=时刻)
    land(root, staged)
    别的 = build_bundle(源(tmp_path, "b"), tmp_path / "s2",
                        bundle_id="site-kl", version=1, built_at=时刻)
    with pytest.raises(BundleError, match="site-kl-1"):
        land(root, 别的)


# ---- apply:才换链 --------------------------------------------------------

def test_第一次生效只有current没有previous(tmp_path, root):
    槽 = 打包并落(tmp_path, root, 1)
    st = apply_bundle(root, 槽)
    assert st.current == 槽
    assert st.previous == ""
    assert st.proven is False
    assert active_bundle(root) == (root / 槽).resolve()


def test_第二次生效把上一版推成previous(tmp_path, root):
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    st = apply_bundle(root, 二)
    assert st.current == 二
    assert st.previous == 一
    assert (root / PREVIOUS_LINK).exists()


def test_生效一个不在盘上的槽会炸(tmp_path, root):
    with pytest.raises(BundleError, match="site-kl-9"):
        apply_bundle(root, "site-kl-9")


@pytest.mark.parametrize("坏槽名", [
    "../../etc", "site-kl-1/..", "/etc", "Site-KL-1", "site-kl", "",
])
def test_槽名不合规当场就拒(tmp_path, root, 坏槽名):
    """**槽名是外面传进来的,而且会被拼进路径。**

    只靠 ``is_dir()`` 拦不住:``root / "../../etc"`` 在 Linux 上解出来就是
    ``/etc``,而 ``/etc`` 真的是个目录 —— 然后 ``verify_bundle`` 会去遍历它。
    开发机是 Windows,那条路径不存在,所以这道闸在本机是**看不出**必要性的;
    这正是它必须有一条自己的测试的原因。
    """
    打包并落(tmp_path, root, 1)
    with pytest.raises(BundleError, match="槽名不合规"):
        apply_bundle(root, 坏槽名)


def test_生效一个坏掉的槽会炸(tmp_path, root):
    """**换链之前先校验。** 换完再校验的话,坏包已经在跑了。"""
    槽 = 打包并落(tmp_path, root, 1)
    (root / 槽 / "schedule.yaml").write_text("改过了\n", encoding="utf-8")
    with pytest.raises(BundleError, match="content_sha256"):
        apply_bundle(root, 槽)
    assert not (root / CURRENT_LINK).exists()


# ---- 目标 SN:闸在换链这一步(定夺 13) --------------------------------------

def test_没写targets的包哪台机器都能换上(tmp_path, root):
    """空 ``targets`` 是「不限」。这是绝大多数包的样子,别让它变麻烦。"""
    槽 = 打包并落(tmp_path, root, 1)
    assert apply_bundle(root, 槽, sn="D1M-9999").current == 槽


def test_写了targets的包名单上的机器能换上(tmp_path, root):
    槽 = 打包并落(tmp_path, root, 1, targets=["D1M-0001", "D1M-0002"])
    assert apply_bundle(root, 槽, sn="D1M-0002").current == 槽


def test_不在名单上的机器换不上去(tmp_path, root):
    """**这份包落在盘上没问题,不能发生的是它在这台机器上生效。**

    现场拿一块备份盘给五台狗分发同一堆目录是正常操作,五台里只有三台在
    ``targets`` 里也是正常的(定夺 13)。
    """
    槽 = 打包并落(tmp_path, root, 1, targets=["D1M-0001"])
    with pytest.raises(BundleError, match="targets"):
        apply_bundle(root, 槽, sn="D1M-0003")
    assert not (root / CURRENT_LINK).exists()


def test_写了targets却没传sn也换不上去(tmp_path, root):
    """``sn`` 的默认值是空串。**忘了传等于换不上去,而不是等于放行** ——
    默认值选错方向的话,这道闸会在第一个忘记传参的调用点静静消失。
    """
    槽 = 打包并落(tmp_path, root, 1, targets=["D1M-0001"])
    with pytest.raises(BundleError, match="targets"):
        apply_bundle(root, 槽)


def test_force不跳SN这道闸(tmp_path, root):
    """``force`` 是给「退过的那一版」开的口子,**不是给「这份包不是给这台狗的」
    开的**。两件事混在一个开关里,现场只会剩下一个「加 force 就好了」的口诀。
    """
    槽 = 打包并落(tmp_path, root, 1, targets=["D1M-0001"])
    with pytest.raises(BundleError, match="targets"):
        apply_bundle(root, 槽, sn="D1M-0003", force=True)


def test_不在名单上的包照样落得下来(tmp_path, root):
    """**``land()`` 不查 SN**(定夺 11 + 13):落盘和换链是两件事。"""
    槽 = 打包并落(tmp_path, root, 1, targets=["D1M-0001"])
    assert (root / 槽).is_dir()


def test_新生效的一版还没被证过(tmp_path, root):
    """``proven`` 就是「这一版有没有真跑成过一次」。§3.2 的自动回退
    判据全压在它上头。
    """
    槽 = 打包并落(tmp_path, root, 1)
    assert apply_bundle(root, 槽).proven is False
    assert mark_proven(root).proven is True


def test_换了一版之后proven要归零(tmp_path, root):
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    mark_proven(root)
    二 = 打包并落(tmp_path, root, 2)
    assert apply_bundle(root, 二).proven is False


def test_手动把链改到别处proven就不算数(tmp_path, root):
    """``proven`` 记的是**哪一个槽**被证过,不是一个光秃秃的布尔。

    存布尔的话,有人手工把 ``current`` 挪到另一版上,那一版就凭空继承了
    「跑成过」这个结论 —— 而它一次都没跑过。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    mark_proven(root)
    二 = 打包并落(tmp_path, root, 2)
    from d1max_patrol.engine.release import point_link
    point_link(root / CURRENT_LINK, root / 二)
    assert read_state(root).proven is False


def test_没有current的时候标记不了(tmp_path, root):
    with pytest.raises(BundleError, match=CURRENT_LINK):
        mark_proven(root)


# ---- 回退 ----------------------------------------------------------------

def test_退回上一版(tmp_path, root):
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    mark_proven(root)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    st = rollback_bundle(root, at=后来, reason="首次执行就崩")
    assert st.current == 一
    assert active_bundle(root) == (root / 一).resolve()


def test_退回之后previous指着刚被退掉的那一版(tmp_path, root):
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    assert rollback_bundle(root, at=后来, reason="崩了").previous == 二


def test_退回要留记录(tmp_path, root):
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    st = rollback_bundle(root, at=后来, reason="首次执行就崩")
    assert len(st.rollbacks) == 1
    r = st.rollbacks[0]
    assert (r.frm, r.to, r.reason, r.at) == (二, 一, "首次执行就崩", 后来)


def test_退过的那一版不许再被生效(tmp_path, root):
    """**这一条是回退真正的价值所在。**

    退完之后什么都不拦的话,下一轮排程/下一次下发会原样再生效一次那个
    崩过的包,然后再退一次 —— 一个安静的死循环,现场看到的是「狗一直在重启」。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    rollback_bundle(root, at=后来, reason="崩了")
    with pytest.raises(BundleError, match="退过"):
        apply_bundle(root, 二)


def test_退过的那一版可以被明确地强推(tmp_path, root):
    """人看过日志、认定那次是别的原因,得有一条路走回去 ——
    但必须是**明确写出来**的那一条,不是默认行为。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    rollback_bundle(root, at=后来, reason="崩了")
    assert apply_bundle(root, 二, force=True).current == 二


def test_没有previous就退不了(tmp_path, root):
    槽 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 槽)
    with pytest.raises(BundleError, match=PREVIOUS_LINK):
        rollback_bundle(root, at=后来, reason="崩了")


def test_退回的时刻必须带时区(tmp_path, root):
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    with pytest.raises(BundleError, match="时区"):
        rollback_bundle(root, at="2026-09-08T02:11:00", reason="崩了")


def test_退两次记两条(tmp_path, root):
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    rollback_bundle(root, at=后来, reason="第一次")
    三 = 打包并落(tmp_path, root, 3)
    apply_bundle(root, 三)
    st = rollback_bundle(root, at="2026-09-09T02:00:00+08:00", reason="第二次")
    assert [r.reason for r in st.rollbacks] == ["第一次", "第二次"]


# ---- 状态与清理 ----------------------------------------------------------

def test_空目录的状态是空的(root):
    st = read_state(root)
    assert (st.current, st.previous, st.proven, st.rollbacks) == ("", "", False, ())


def test_landed_json坏了不至于让狗读不出current(tmp_path, root):
    """**链是「在跑哪一版」的唯一真理源**,``landed.json`` 只存链存不下的东西
    (证没证过、退过哪些)。一个坏掉的 json 不该让狗连自己在跑什么都说不出。
    """
    槽 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 槽)
    (root / LANDED).write_text("{坏的", encoding="utf-8")
    st = read_state(root)
    assert st.current == 槽
    assert st.proven is False
    assert st.rollbacks == ()


def test_状态能上线(tmp_path, root):
    槽 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 槽)
    w = read_state(root).to_wire()
    assert w["current"] == 槽
    assert w["previous"] == ""
    assert w["proven"] is False
    assert w["rollbacks"] == []


def test_清理只留两份(tmp_path, root):
    for v in (1, 2, 3, 4):
        apply_bundle(root, 打包并落(tmp_path, root, v))
    删了 = prune_bundles(root)
    assert sorted(删了) == ["site-kl-1", "site-kl-2"]
    assert (root / "site-kl-3").is_dir()
    assert (root / "site-kl-4").is_dir()


def test_清理不动current和previous(tmp_path, root):
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    assert prune_bundles(root) == ()
    assert active_bundle(root) == (root / 二).resolve()


def test_清理不碰landed_json和那两条链(tmp_path, root):
    for v in (1, 2, 3):
        apply_bundle(root, 打包并落(tmp_path, root, v))
    prune_bundles(root)
    assert (root / LANDED).exists()
    assert (root / CURRENT_LINK).exists()
    assert (root / PREVIOUS_LINK).exists()


def test_退过的记录留着不被清理(tmp_path, root):
    """退过哪些版本是**只增**的。清理磁盘不该清掉「这一版崩过」这条事实 ——
    清掉了,``test_退过的那一版不许再被生效`` 拦的那个死循环就回来了。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    rollback_bundle(root, at=后来, reason="崩了")
    for v in (3, 4):
        apply_bundle(root, 打包并落(tmp_path, root, v))
    prune_bundles(root)
    assert [r.frm for r in read_state(root).rollbacks] == [二]
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    assert 记["rollbacks"][0]["from"] == 二


def test_搬到别的路径去还认得出来(tmp_path, root):
    """链是相对的还是绝对的?**绝对的**,而且整棵树是可以被整体搬走的 ——
    第 3 卷的备份盘就是这么用的。这一条钉住「搬完还能读」。
    """
    槽 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 槽)
    另 = tmp_path / "elsewhere"
    shutil.copytree(root, 另, symlinks=True)
    assert read_state(另).current == 槽


def test_一版装进去跑崩了自己退回来这条路真走一遍(tmp_path, root):
    """§8.5 第 3 条:**回滚必须被真走过一次,而且要故意失败一次。**

    这一条不是拦在门外的那种失败 —— 包是好的、校验全过、链也换了,
    是**跑起来之后**崩的。整条路:落盘 -> 生效 -> 证过 -> 新版生效 ->
    没证成 -> 判据说该退 -> 退 -> 退过的不许再上。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    mark_proven(root)                       # 老版本跑成过

    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)                   # 换过去了,校验全过
    st = read_state(root)
    assert (st.current, st.proven) == (二, False)   # 但一次都没跑成

    # 判据是个纯函数:没证过 + 崩了 = 该退。这里就地写出来,是为了让
    # 「什么情况该退」这句话有一个能被单独看懂的形状。
    def 该不该退(state, 崩了: bool) -> bool:
        return 崩了 and not state.proven and bool(state.previous)

    assert 该不该退(st, 崩了=True) is True
    assert 该不该退(st, 崩了=False) is False

    退了 = rollback_bundle(root, at=后来, reason="首次执行就崩")
    assert 退了.current == 一
    assert active_bundle(root) == (root / 一).resolve()
    with pytest.raises(BundleError, match="退过"):
        apply_bundle(root, 二)


def test_证过的那一版崩了不走自动退(tmp_path, root):
    """自动退只对「新版没证过就崩」这一种。证过的那一版再崩,是别的毛病
    (环境变了、硬件坏了),退回去解决不了 —— 而退回去会让人以为解决了。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    mark_proven(root)

    def 该不该退(state, 崩了: bool) -> bool:
        return 崩了 and not state.proven and bool(state.previous)

    assert 该不该退(read_state(root), 崩了=True) is False
