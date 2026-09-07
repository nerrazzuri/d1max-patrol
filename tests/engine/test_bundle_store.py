"""盘上永远留两份:``current`` 和 ``previous``(§3.2)。

**这个文件里没有一个 sleep。** 时刻全是传进来的(§8.5 第 2 条)。
"""

from __future__ import annotations

import json
import shutil

import pytest

from d1max_patrol.engine import bundle as bundle_mod
from d1max_patrol.engine.bundle import (
    CURRENT_LINK,
    LANDED,
    MAX_ROLLBACKS,
    PREVIOUS_LINK,
    SCHEDULE_NAME,
    BundleError,
    BundleGuard,
    active_bundle,
    apply_bundle,
    build_bundle,
    guard_bundle,
    land,
    mark_proven,
    prune_bundles,
    read_bundle_schedule,
    read_state,
    rollback_bundle,
)

from .test_bundle_build import 打一个

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
    第 3 卷的备份盘就是这么用的。这一条钉住两件事:``read_state`` 只认链的
    basename,搬完还能读;但那条链本身还绝对指着老地方 —— 老地方一旦真的
    没了(不是复制一份留着原地,而是**真搬走**),``active_bundle`` 必须
    当场炸,不能把一个不存在的路径悄悄交给调用方。
    """
    槽 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 槽)
    另 = tmp_path / "elsewhere"
    shutil.copytree(root, 另, symlinks=True)
    shutil.rmtree(root)                     # 真搬:老地方彻底不在了
    assert read_state(另).current == 槽
    with pytest.raises(BundleError, match="悬空"):
        active_bundle(另)


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


def test_读得出包里那份排程(tmp_path):
    root = tmp_path / "bundles"
    land(root, 打一个(tmp_path))
    apply_bundle(root, "site-kl-1")
    got = read_bundle_schedule(root / "site-kl-1")
    assert got.timezone == "Asia/Kuala_Lumpur"


def test_排程不见了是BundleError(tmp_path):
    root = tmp_path / "bundles"
    land(root, 打一个(tmp_path))
    (root / "site-kl-1" / SCHEDULE_NAME).unlink()
    with pytest.raises(BundleError, match=SCHEDULE_NAME):
        read_bundle_schedule(root / "site-kl-1")


def test_排程不合规抛的也是BundleError而且带原话(tmp_path):
    """**不许把 yaml 的异常和 ScheduleError 漏给调用方。**

    调用方是 HTTP 层,它要把这件事翻成一个说得清的 409。漏上去就只能给
    500,而现场的人看到 500 只能猜 —— 这台狗此刻在一个没有网的地方。
    """
    root = tmp_path / "bundles"
    land(root, 打一个(tmp_path))
    (root / "site-kl-1" / SCHEDULE_NAME).write_text(
        "entries: []\n", encoding="utf-8")
    with pytest.raises(BundleError, match="timezone"):
        read_bundle_schedule(root / "site-kl-1")


# ---- 连着退两次(评审 F1) ------------------------------------------------

def test_连着退两次第二次要被拒(tmp_path, root):
    """**黑名单这道闸两扇门上都要装。**

    它本来只装在 ``apply_bundle`` 那一扇。v2 崩了退到 v1 之后再点一次回退,
    ``previous`` 正是刚拉黑的 v2 —— 那扇门开着,狗被送回刚崩掉的那一版,
    还被 ``proven_slot = prev`` 标成「跑成过」。手机双击、客户端超时重试、
    第 8 卷的自动回退判据抖一下,都够触发。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    mark_proven(root)                       # 一 是真跑成过的那一版
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    退了 = rollback_bundle(root, at=后来, reason="首次执行就崩")
    assert (退了.current, 退了.previous) == (一, 二)

    with pytest.raises(BundleError, match="退过的那一版"):
        rollback_bundle(root, at="2026-09-09T02:00:00+08:00", reason="再点一次")

    st = read_state(root)
    assert st.current == 一                  # 没被送回刚崩掉的那一版
    assert 二 in st.denied                   # 二 仍然在黑名单里
    assert [r.frm for r in st.rollbacks] == [二]      # 没多记一条
    assert st.proven is True                 # 一 本来就是证过的
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    assert 记["proven_slot"] == 一            # 二 没有凭空被标成「跑成过」
    with pytest.raises(BundleError, match="退过"):
        apply_bundle(root, 二)


def test_两条链指着同一版就退不了(tmp_path, root):
    """``current == previous`` 是「换链换到一半断电」留下的局面,不是可退的
    局面。退过去等于原地打转,还会记下一条 ``from == to`` 的荒唐审计,
    并且把这唯一一版拉黑 —— 盘上那份好包就此再也装不上去。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    bundle_mod.point_link(root / PREVIOUS_LINK, root / 一)
    with pytest.raises(BundleError, match="没有别的一版可退"):
        rollback_bundle(root, at=后来, reason="崩了")
    assert read_state(root).denied == ()


# ---- 换链换到一半断电(评审 F2) ------------------------------------------

def 断在两次换链之间(monkeypatch):
    """让下一次 ``apply_bundle`` 的**第二次** ``point_link`` 断电。

    §8.5 禁 sleep,也没法在开发机上真断电 —— 所以这儿直接把那个中间态造出来,
    不依赖任何时序。
    """
    真 = bundle_mod.point_link
    次 = []

    def 假(link, target):
        次.append(link.name)
        if len(次) >= 2:
            raise OSError("断电")
        真(link, target)

    monkeypatch.setattr(bundle_mod, "point_link", 假)


def 断电装二(tmp_path, root, monkeypatch):
    """一台装着 v1 的狗,装 v2 装到两次换链之间断了电。返回 ``(一, 二)``。"""
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    mark_proven(root)
    二 = 打包并落(tmp_path, root, 2)
    断在两次换链之间(monkeypatch)
    with pytest.raises(OSError):
        apply_bundle(root, 二)
    monkeypatch.undo()                      # 电来了:后面那一段是真的换链
    return 一, 二


def test_换链换到一半断电标记里有足够的信息(tmp_path, root, monkeypatch):
    """``point_link`` 的 docstring 承诺「那条退路上的窗口由调用方的标记文件
    兜着」。任务包这一侧原来写的是个 ``{}`` —— 兜不住任何东西。
    """
    一, 二 = 断电装二(tmp_path, root, monkeypatch)

    st = read_state(root)
    assert (st.current, st.previous) == (一, 一)      # 自相矛盾的中间态
    assert st.proven is False                        # 一 还凭空丢了 proven

    assert st.applying is not None
    assert (st.applying.to, st.applying.src, st.applying.prev) == (二, 一, "")
    # ``proven_slot`` 也在标记里 —— 换链之前 一 是被证过的那一版,
    # 撤销的时候得原样放回去(评审复评 finding 3)。
    assert st.applying.proven == 一
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    assert 记["applying"] == {"to": 二, "from": 一, "prev": "",
                              "proven_slot": 一}


def test_断电之后走一次guard状态回到可用的终态(tmp_path, root, monkeypatch):
    一, 二 = 断电装二(tmp_path, root, monkeypatch)
    assert guard_bundle(root) is BundleGuard.FINISHED

    st = read_state(root)
    assert (st.current, st.previous) == (二, 一)      # 两条链各指各的
    assert st.applying is None                       # 半成品收拾干净了
    assert active_bundle(root) == (root / 二).resolve()
    assert st.denied == ()                           # 谁都没被拉黑
    assert st.rollbacks == ()


def test_断电之后那份唯一的好包没有被误拉黑(tmp_path, root, monkeypatch):
    """原来的下场:``current == previous == v1`` 且 ``proven=False``,
    第 8 卷那条判据 ``崩了 and not proven and previous`` 随即成立,自动退一次,
    记下 ``from=v1 to=v1`` 并**把盘上唯一那份好包拉黑** —— 而路由不暴露
    ``force``,靠 API 再也装不回去。
    """
    一, 二 = 断电装二(tmp_path, root, monkeypatch)

    def 该不该退(state, 崩了: bool) -> bool:
        return 崩了 and not state.proven and bool(state.previous)

    assert 该不该退(read_state(root), 崩了=True) is True      # 判据照样成立
    with pytest.raises(BundleError, match="没有别的一版可退"):
        rollback_bundle(root, at=后来, reason="崩了")          # 但退不动
    assert read_state(root).denied == ()

    guard_bundle(root)
    assert apply_bundle(root, 一).current == 一               # 好包还装得回去


def test_目标那份包落坏了guard就把两条链放回换链之前(tmp_path, root, monkeypatch):
    """标记指着的那一版要是没落全(或者落下的那半份校验不过),就换不过去 ——
    两条链一起放回换链前的样子。只放回 ``current`` 的话会留下
    ``current == previous``,那正是这一条要消掉的局面。
    """
    一, 二 = 断电装二(tmp_path, root, monkeypatch)
    shutil.rmtree(root / 二)                          # 那份包没了
    assert guard_bundle(root) is BundleGuard.UNDONE

    st = read_state(root)
    assert (st.current, st.previous) == (一, "")      # 跟换链之前一模一样
    assert st.applying is None
    assert active_bundle(root) == (root / 一).resolve()


def test_两个方向都走不通就报broken而且标记留着(tmp_path, root, monkeypatch):
    """标记是现场取证唯一的线索,不能在「修不动」的那条路上被清掉;而且下一次
    guard 还要照它再试一遍 —— 那份包可能只是暂时读不到(盘没挂上)。
    """
    一, 二 = 断电装二(tmp_path, root, monkeypatch)
    shutil.rmtree(root / 二)
    shutil.rmtree(root / 一)
    assert guard_bundle(root) is BundleGuard.BROKEN
    assert read_state(root).applying is not None


def test_没有半成品的时候guard什么都不动(tmp_path, root):
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    assert guard_bundle(root) is BundleGuard.OK
    assert read_state(root).current == 一


def test_apply跑完了标记就没了(tmp_path, root):
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    assert "applying" not in 记


# ---- 回退历史有上限,拉黑事实没有(评审 F6) ------------------------------

def test_退很多次历史被截掉但最老那次拉黑仍然有效(tmp_path, root):
    """``reason`` 限的是长度、不是次数:实测 300 次回退把 ``landed.json``
    撑到 489KB、``GET /api/bundle`` 吐 177KB 用 3.1s。狗是长期在线的设备。

    **截断绝不能把拉黑事实一起截掉** —— 那样最老那几版就被放出来了,
    ``apply_bundle`` 那道闸拦的死循环会原样回来。所以拉黑单独存在 ``denied``,
    截掉的只是 ``at``/``reason``/``to`` 这些历史细节。
    """
    理由 = "崩" * 500                        # 每条都卡在路由那个 500 字上限上
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    退过的 = []
    大小 = []
    for v in range(2, MAX_ROLLBACKS + 12):   # 比上限多退十次
        槽 = 打包并落(tmp_path, root, v)
        apply_bundle(root, 槽)
        rollback_bundle(root, at=后来, reason=理由)
        退过的.append(槽)
        大小.append((root / LANDED).stat().st_size)

    st = read_state(root)
    assert len(st.rollbacks) == MAX_ROLLBACKS            # 历史有上限
    # 填满之后再退十次,文件几乎不再长 —— 多出来的只有 denied 里那几个槽名。
    assert 大小[-1] - 大小[MAX_ROLLBACKS - 1] < 1_000
    assert 大小[-1] < 60_000

    最老 = 退过的[0]
    assert 最老 not in [r.frm for r in st.rollbacks]     # 历史里已经没有它
    assert 最老 in st.denied                             # 但拉黑事实还在
    with pytest.raises(BundleError, match="退过的那一版"):
        apply_bundle(root, 最老)                         # 仍然装不上去


def test_老盘上只有rollbacks没有denied照样认拉黑(tmp_path, root):
    """升级上来的机器盘上那份 ``landed.json`` 只有 ``rollbacks``,没有
    ``denied``。那些拉黑事实不能凭空消失 —— 消失了正好是「一版崩过的包又被
    装回去」这条死循环。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    rollback_bundle(root, at=后来, reason="崩了")
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    del 记["denied"]                         # 装成老版本写下的那份
    (root / LANDED).write_text(json.dumps(记, ensure_ascii=False),
                               encoding="utf-8")

    assert read_state(root).denied == (二,)
    with pytest.raises(BundleError, match="退过的那一版"):
        apply_bundle(root, 二)


# ---- 老盘升上来那一次(评审复评 finding 1 / 3 / 6 / 7) --------------------

def 老盘(root, 条数: int, *, 真槽: str = "") -> list[str]:
    """把 ``landed.json`` 改写成**升级之前那台狗**的形状,返回退过哪些槽。

    老版本只写 ``rollbacks``,**没有 ``denied`` 这个键** —— 那些拉黑事实只
    存在于 ``rollbacks[].from`` 里。``条数`` 要超过 ``MAX_ROLLBACKS``,不然
    根本走不到截断那一行(上一轮那条测试正是栽在这儿)。
    """
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    退过 = [真槽 or "site-kl-900"] + [f"site-kl-{900 + i}" for i in range(1, 条数)]
    记["rollbacks"] = [{"at": 时刻, "from": s, "to": "site-kl-1",
                        "reason": "崩" * 500} for s in 退过]
    记.pop("denied", None)                   # 老版本压根没有这个键
    (root / LANDED).write_text(json.dumps(记, ensure_ascii=False),
                               encoding="utf-8")
    return 退过


def test_老盘升上来第一次寻常回退不会把老的拉黑事实截掉(tmp_path, root):
    """**评审复评 finding 1 —— 也是 F6 至今唯一一次被真正验收。**

    上一轮那条 ``test_老盘上只有rollbacks没有denied照样认拉黑`` 只造了**一条**
    rollback,走不到 ``del 退[:-MAX_ROLLBACKS]`` 那一行,钉住的是读路径不是
    截断路径。这儿造的是真老盘:条数超过上限、没有 ``denied`` 键。

    实跑序列:老盘 25 条 ``rollbacks`` → 升级(此刻 ``_黑名单()`` 从
    ``rollbacks[].from`` 算得出 25 条,兼容是好的)→ 做**一次寻常回退** →
    截断之前不固化的话,``rollbacks`` 截成 20、``denied`` 只有这一次的,
    最早那几版就此从黑名单里消失,``apply`` 它们 200 OK ——
    一个已知会崩的版本被装回去,正是 F1/F6 要堵的那个安静死循环。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    九 = 打包并落(tmp_path, root, 9)         # 这一版真在盘上,等下要拿它去 apply
    apply_bundle(root, 九)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)                   # current=二, previous=九
    最老 = 老盘(root, MAX_ROLLBACKS + 5, 真槽=九)[0]
    assert 最老 == 九
    assert 九 in read_state(root).denied      # 升上来这一刻兼容是好的

    # previous 此刻是 九,而 九 在黑名单里 —— 先把链摆成一个能退的样子(退回
    # 一),这是一次**寻常回退**,不是什么边角情形。
    bundle_mod.point_link(root / PREVIOUS_LINK, root / 一)

    rollback_bundle(root, at=后来, reason="又崩了")

    st = read_state(root)
    assert len(st.rollbacks) == MAX_ROLLBACKS                # 历史照样被截
    assert 最老 not in [r.frm for r in st.rollbacks]          # 已经不在历史里
    assert 最老 in st.denied                                  # (a) 仍然在黑名单里
    with pytest.raises(BundleError, match="退过的那一版"):     # (b) 仍然装不上去
        apply_bundle(root, 最老)


def test_老盘开一次机就被归一化(tmp_path, root):
    """**评审复评 finding 6。** 截断只发生在写路径上,老盘在下一次回退之前
    照样几百条(实测 489KB / ``GET /api/bundle`` 吐 177KB / 3.1s);它要是
    不再回退,就永远这样。开机守卫顺手把这件事做掉。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    退过的 = 老盘(root, 200)
    大 = (root / LANDED).stat().st_size
    assert 大 > 100_000                       # 老盘上真就是这么大一坨

    assert guard_bundle(root) is BundleGuard.OK

    st = read_state(root)
    assert len(st.rollbacks) == MAX_ROLLBACKS
    assert set(退过的) <= set(st.denied)      # 一条拉黑事实都没丢
    assert (root / LANDED).stat().st_size < 大 // 3


def test_归一化是幂等的(tmp_path, root, monkeypatch):
    """开十次机跟开一次机盘上得是同一份文件,**而且后面九次根本不写盘**。

    **断"字节相同"是不够的**(评审复评第 3 轮 N7):``json.dump(sort_keys=True)``
    本来就保证字节稳定,所以把 ``_归一化`` 里那道「逐项比、一样就 return」的
    短路整段删掉,老版本的这条测试照样绿 —— 复评实跑过。真正要钉住的风险是
    **每次开机重写一遍 489KB**:磨闪存,而且每一次开机都平白多开一个掉电窗口
    (盘上那份 ``landed.json`` 里存着 ``denied`` 这个安全判据)。
    所以这儿数的是 ``_写记`` 被调了几次。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    老盘(root, MAX_ROLLBACKS + 10)

    写了几次: list[str] = []
    真写 = bundle_mod._写记

    def 记一笔(root_, 记):
        写了几次.append("写")
        真写(root_, 记)

    monkeypatch.setattr(bundle_mod, "_写记", 记一笔)

    assert guard_bundle(root) is BundleGuard.OK
    assert len(写了几次) == 1                 # 第一次:老盘要收拾,写这一次
    第一次 = (root / LANDED).read_bytes()

    for _ in range(9):
        assert guard_bundle(root) is BundleGuard.OK
    assert len(写了几次) == 1                 # **后面九次一次都没写**
    assert bundle_mod._归一化(root) is False   # 它自己也说"没改"
    assert (root / LANDED).read_bytes() == 第一次


def test_归一化断在改名之前下次开机照样修得回来(tmp_path, root, monkeypatch):
    """``_写记`` 是「先写临时文件再改名」。真断在改名之前,盘上留着的还是
    归一化**之前**那一份 —— 不是半份 —— 下次开机照原样再归一化一遍就是了。

    **结论是 ``OK`` 而不是 ``BROKEN``**(评审复评第 3 轮 N1):这台机器盘上
    根本没有半成品标记,链好好的,归一化那点家务活没干成不该改变这个结论。
    上一轮这儿断的是 ``BROKEN`` —— 那正是把「家务活失败」当成「链坏了」上报,
    而第 8 卷的回退判据听的就是这个。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    退过的 = 老盘(root, MAX_ROLLBACKS + 10)
    原样 = (root / LANDED).read_bytes()

    def 断电(src, dst):
        raise OSError("断电")

    monkeypatch.setattr(bundle_mod.os, "replace", 断电)
    assert guard_bundle(root) is BundleGuard.OK         # 守卫自己没炸
    assert read_state(root).current == 一               # 链本来就是好的
    assert (root / LANDED).read_bytes() == 原样         # 盘上没有半份文件
    monkeypatch.undo()                                  # 电来了

    assert guard_bundle(root) is BundleGuard.OK
    st = read_state(root)
    assert len(st.rollbacks) == MAX_ROLLBACKS
    assert set(退过的) <= set(st.denied)


def test_归一化写盘失败也绝不许挡住修链(tmp_path, root, monkeypatch):
    """**评审复评第 3 轮 N1。** 归一化是家务活,修链是安全网。

    上一轮把 ``_归一化(root)`` 放成了 ``_guard_bundle`` 的第一句,而它**自己
    会写盘**:ENOSPC、EIO、只读挂载、掉电正好落在 ``os.replace`` 上 —— 一抛,
    整个 ``_guard_bundle`` 就抛出去,外面兜成 ``BROKEN``,**两条链一个字没修**。
    而这台机器接下来会怎么样:链停在 ``current == previous`` 且
    ``proven=False``,第 8 卷那条 ``崩了 and not proven and previous`` 随即成立,
    自动退一次,**把盘上唯一那份好包拉黑**。F2 那条修复路径被家务活挡在了外面。

    验收场景写的就是「回退演练 + 装到一半断电」,而盘快满的老盘正是最需要
    修链的那一台 —— 所以这条必须红得起来。
    """
    一, 二 = 断电装二(tmp_path, root, monkeypatch)
    老盘(root, MAX_ROLLBACKS + 10)             # 而且这是一台老盘,归一化有活干
    assert read_state(root).applying is not None

    真替 = bundle_mod.os.replace
    落到landed = []

    def 盘快满了(src, dst):
        """**只打断 ``landed.json`` 的第一次改名**,也就是归一化那一次。

        后面修链自己那一次写盘照常 —— 要断言的是「归一化失败之后修链照跑、
        而且返回值反映的是修链的结果」,不是「什么都写不了会怎样」。
        两条符号链接的 ``os.replace``(``point_link``)不在这条闸里。
        """
        if str(dst).endswith(LANDED):
            落到landed.append(str(dst))
            if len(落到landed) == 1:
                raise OSError(28, "No space left on device")
        return 真替(src, dst)

    monkeypatch.setattr(bundle_mod.os, "replace", 盘快满了)

    结论 = guard_bundle(root)

    # (b) 返回值反映的是**修链**的结果,不是归一化那次写盘失败
    assert 结论 is BundleGuard.FINISHED
    # (a) **链确实被修了** —— 这才是重点
    st = read_state(root)
    assert (st.current, st.previous) == (二, 一)
    assert st.applying is None
    # 归一化那一次是真失败了(不然这条测试自己就是假的):历史一条没截
    assert len(st.rollbacks) == MAX_ROLLBACKS + 10

    monkeypatch.undo()                        # 电来了 / 盘腾出来了
    assert guard_bundle(root) is BundleGuard.OK
    assert len(read_state(root).rollbacks) == MAX_ROLLBACKS   # 家务活补上了


# ---- landed.json 里那几个键不是列表(评审复评第 3 轮 N2 / N6) --------------

@pytest.mark.parametrize("键", ["denied", "rollbacks", "forced"])
@pytest.mark.parametrize("坏值", [None, 7, "site-kl-1", {"a": 1}], ids=str)
def test_这三个键不是列表守卫也不许抛(tmp_path, root, 键, 坏值):
    """``TypeError`` 既不是 ``OSError`` 也不是 ``ValueError`` —— 它**直穿**
    ``guard_bundle`` 那句「任何一条路都不抛」的承诺(评审复评第 3 轮 N2)。

    上一轮在同一句话上已经翻过一次车(``UnicodeDecodeError``),这次换了个
    异常类型和位置:``landed.json`` 里 ``denied``/``rollbacks``/``forced``
    是 JSON ``null`` 或者一个数字,列表推导就抛。同一个文件一贯把
    ``landed.json`` 当「外面的东西」(到处 ``isinstance(r, dict)`` 过滤、
    槽名过 ``_SLOT_RE``),偏偏容器本身没过。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    记[键] = 坏值
    (root / LANDED).write_text(json.dumps(记, ensure_ascii=False),
                               encoding="utf-8")

    assert read_state(root).current == 一      # 读状态本身不抛
    assert guard_bundle(root) is BundleGuard.OK
    assert read_state(root).current == 一      # 链一个字没动


@pytest.mark.parametrize("坏值", [None, 7, "site-kl-9", {"a": 1}], ids=str)
def test_forced不是列表强推也不该炸成AttributeError(tmp_path, root, 坏值):
    """``记.setdefault("forced", []).append(...)`` 遇到非 list 抛的是
    ``AttributeError``,``_bundle_apply`` 只接 ``BundleError``,于是冒成一个
    500(评审复评第 3 轮 N6)。跟 N2 用同一个「取列表」的写法收掉。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    rollback_bundle(root, at=后来, reason="崩了")
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    记["forced"] = 坏值
    (root / LANDED).write_text(json.dumps(记, ensure_ascii=False),
                               encoding="utf-8")

    st = apply_bundle(root, 二, force=True, at=后来)
    assert st.current == 二
    assert [(f.at, f.slot) for f in st.forced] == [(后来, 二)]   # 留痕还在
    assert 二 in st.denied                                       # 没被洗白


@pytest.mark.parametrize("坏值", [None, 7, "site-kl-9", {"a": 1}], ids=str)
def test_rollbacks不是列表回退也不该炸成AttributeError(tmp_path, root, 坏值):
    """``退 = 记.setdefault("rollbacks", [])`` 是同一个洞的另一半(N6)。"""
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    记["rollbacks"] = 坏值
    (root / LANDED).write_text(json.dumps(记, ensure_ascii=False),
                               encoding="utf-8")

    st = rollback_bundle(root, at=后来, reason="崩了")
    assert (st.current, st.previous) == (一, 二)
    assert [(r.frm, r.to) for r in st.rollbacks] == [(二, 一)]
    assert 二 in st.denied                    # 拉黑事实照样记下来了


# ---- 标记里的 proven_slot 坏了只丢字段(评审复评第 3 轮 N5) ----------------

def test_标记里proven_slot是null不许赔掉整条修复标记(tmp_path, root, monkeypatch):
    """``str(raw.get("proven_slot", ""))`` 把 JSON ``null`` 变成 ``"None"``,
    一个过不了 ``_SLOT_RE`` 的假槽名 —— **整条标记就此作废**,断电留下的两条
    链再也没人修(评审复评第 3 轮 N5)。

    而 ``proven`` 从头到尾**没有被拼进过路径**(只在 ``read_state`` 里跟
    ``current`` 比一次字符串),为它赔掉修复路径不划算。``to``/``src``/``prev``
    会拼路径,那三个作废才是对的。跟 N1 同源:安全网不许被家务活赔掉。
    """
    一, 二 = 断电装二(tmp_path, root, monkeypatch)
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    记["applying"]["proven_slot"] = None
    (root / LANDED).write_text(json.dumps(记, ensure_ascii=False),
                               encoding="utf-8")

    半 = read_state(root).applying
    assert 半 is not None                     # 标记保住了
    assert (半.to, 半.src, 半.proven) == (二, 一, "")

    assert guard_bundle(root) is BundleGuard.FINISHED
    st = read_state(root)
    assert (st.current, st.previous) == (二, 一)      # 链真的修回来了
    assert st.applying is None


@pytest.mark.parametrize("坏证", ["../../etc", "site-kl-1\x00", "Site-KL-1", 7])
def test_标记里proven_slot不合规也只丢这个字段(tmp_path, root, monkeypatch, 坏证):
    """同 N5:坏的 ``proven_slot`` 只该丢掉它自己,不该把整条标记拖下水。"""
    一, 二 = 断电装二(tmp_path, root, monkeypatch)
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    记["applying"]["proven_slot"] = 坏证
    (root / LANDED).write_text(json.dumps(记, ensure_ascii=False),
                               encoding="utf-8")

    半 = read_state(root).applying
    assert 半 is not None and 半.proven == ""
    assert guard_bundle(root) is BundleGuard.FINISHED
    assert read_state(root).current == 二


def test_标记里的from是null也不该被读成None这个假槽名(tmp_path, root, monkeypatch):
    """N5 的另一半:``from_wire`` 里那四个 ``str()``。

    ``str(None)`` 是 ``"None"`` —— 一个**长得像槽名的假槽名**。「不是字符串」
    的正确读法是「没有这一项」(空串,一路上都有人认),不是「有一项叫 None」
    (谁都不认,只会在某道闸上把整件事作废)。这儿盯的是 ``from``:它一变成
    ``"None"``,整条标记就作废,两条链停在断电那一刻没人管;当成空串的话,
    守卫照 ``to`` 把链换完,局面回到能用的终态。
    """
    _一, 二 = 断电装二(tmp_path, root, monkeypatch)
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    记["applying"]["from"] = None
    (root / LANDED).write_text(json.dumps(记, ensure_ascii=False),
                               encoding="utf-8")

    半 = read_state(root).applying
    assert 半 is not None and (半.to, 半.src) == (二, "")

    assert guard_bundle(root) is BundleGuard.FINISHED
    st = read_state(root)
    assert st.current == 二
    assert st.applying is None


def test_标记里的to坏了照旧作废整条(tmp_path, root, monkeypatch):
    """N5 只松 ``proven`` 这一项。``to`` 会被 ``root / 半.to`` 拼进路径,
    坏了必须作废整条 —— 这条守的是「松的那一项没有顺手松掉别的」。
    """
    一, 二 = 断电装二(tmp_path, root, monkeypatch)
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    记["applying"]["to"] = None
    (root / LANDED).write_text(json.dumps(记, ensure_ascii=False),
                               encoding="utf-8")

    assert read_state(root).applying is None
    assert guard_bundle(root) is BundleGuard.OK
    assert read_state(root).current == 一      # 链一个字没动


def test_撤销的时候那一版跑成过的事实要放回去(tmp_path, root, monkeypatch):
    """**评审复评 finding 3。** ``UNDONE`` 把两条链放回换链之前的样子,
    却让那份**真跑成过**的包带着 ``proven=False`` 回到 ``current`` ——
    第 8 卷那条 ``崩了 and not proven and previous`` 随即成立,自动退一次,
    **把那份跑成过的好包拉黑**。F2 的伤口只是从「断电当场」挪到了
    「guard 修完之后」。

    既有的 ``test_目标那份包落坏了guard就把两条链放回换链之前`` 恰好构造成
    ``prev == ""``,判据里 ``bool(previous)`` 为假,所以看不见这个洞 ——
    这儿显式构造 ``prev != ""`` 那一路。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    mark_proven(root)                        # 二 真跑成过一次
    三 = 打包并落(tmp_path, root, 3)
    断在两次换链之间(monkeypatch)
    with pytest.raises(OSError):
        apply_bundle(root, 三)
    monkeypatch.undo()
    shutil.rmtree(root / 三)                 # 三 那份包没落全

    assert guard_bundle(root) is BundleGuard.UNDONE

    st = read_state(root)
    assert (st.current, st.previous) == (二, 一)          # prev != "" 那一路
    assert st.proven is True                             # 跑成过的事实回来了

    def 该不该退(state, 崩了: bool) -> bool:
        return 崩了 and not state.proven and bool(state.previous)

    assert 该不该退(st, 崩了=True) is False               # 判据不再成立
    assert st.denied == ()                               # 谁都没被拉黑


def test_自述不是合法UTF8抛的是BundleError(tmp_path, root):
    """**评审复评 finding 2 的根因。** ``UnicodeDecodeError`` 是 ``ValueError``
    的子类,**不是 ``OSError``** —— 只写 ``except OSError`` 的 ``read_manifest``
    罩不住它,它会直穿 ``guard_bundle`` 那句
    ``except (OSError, BundleError)``。
    """
    一 = 打包并落(tmp_path, root, 1)
    (root / 一 / "bundle.yaml").write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(BundleError, match="UTF-8"):
        bundle_mod.read_manifest(root / 一)


def test_目标包的自述是非UTF8守卫也不许炸(tmp_path, root, monkeypatch):
    """apply 到一半断电,**同一次断电**把目标包的 ``bundle.yaml`` 写成了非
    UTF-8 字节(拷了一半、闪存掉电损坏)—— 那正是 ``UNDONE`` 这条路存在的
    理由。守卫在这儿炸掉的话,两条链就停在断电时那个自相矛盾的状态上。
    """
    一, 二 = 断电装二(tmp_path, root, monkeypatch)
    (root / 二 / "bundle.yaml").write_bytes(b"\xff\xfe\x00\x01")

    assert guard_bundle(root) is BundleGuard.UNDONE       # 没抛,给了个结论
    st = read_state(root)
    assert (st.current, st.previous) == (一, "")
    assert st.applying is None


@pytest.mark.parametrize("坏标记", ["../../etc", "site-kl-1\x00", "", "Site-KL-1"])
def test_标记里的槽名不合规就当没有标记(tmp_path, root, 坏标记):
    """**评审复评 finding 7。** ``guard_bundle`` 把 ``landed.json`` 里的
    ``to``/``from``/``prev`` 直接拼进路径,而 ``apply_bundle`` 对同一件事写着
    「槽名是外面传进来的,而且会被拼进路径」并过 ``_SLOT_RE``。今天挡住
    ``../../etc`` 的是 ``_能用()`` 里 ``verify_bundle`` 的目录名一致性检查 ——
    那是**别处的副作用**,不是本地判据;而一个带 NUL 的名字连
    ``Path.is_dir()`` 都会抛 ``ValueError``,把「守卫不许抛」那条承诺也一起
    打掉。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    记["applying"] = {"to": 坏标记, "from": 一, "prev": "", "proven_slot": ""}
    (root / LANDED).write_text(json.dumps(记, ensure_ascii=False),
                               encoding="utf-8")

    assert read_state(root).applying is None              # 当没有这条标记
    assert guard_bundle(root) is BundleGuard.OK           # 而且没抛
    assert read_state(root).current == 一                 # 链一个字没动


# ---- 强推留痕(评审复评 finding 5) ---------------------------------------

def test_强推会留痕而且不把那一版洗白(tmp_path, root):
    """这扇门后面是「装一个已知会崩的版本」,所以它必须留痕。

    但**强推不是洗白**:那一版仍然在 ``denied`` 里,下一次不带 ``force``
    照样拒。「这一次我知道我在干什么」跟「这一版从此可以随便装」是两句话。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    rollback_bundle(root, at=后来, reason="崩了")

    st = apply_bundle(root, 二, force=True, at=后来)
    assert st.current == 二
    assert [(f.at, f.slot) for f in st.forced] == [(后来, 二)]   # 留痕
    assert 二 in st.denied                                       # 没被洗白
    assert st.to_wire()["forced"] == [{"at": 后来, "slot": 二}]

    with pytest.raises(BundleError, match="退过的那一版"):
        apply_bundle(root, 二)                                   # 下一次照样拒


def test_没被拉黑的时候传force什么也不记(tmp_path, root):
    """对一个本来就没被拉黑的槽传 ``force`` 什么也没顶开 —— 记下来只会把
    这份历史冲成噪音,而它是给人看「谁在什么时候硬来过」的。
    """
    一 = 打包并落(tmp_path, root, 1)
    assert apply_bundle(root, 一, force=True, at=后来).forced == ()


def test_强推历史也有上限(tmp_path, root):
    """跟 ``rollbacks`` 同一个上限、同一条理由:狗是长期在线的设备,
    一个只增不减的列表迟早把 ``landed.json`` 撑成一个吐不动的东西。
    **拉黑事实不跟着被截** —— 那是判据,不是历史。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    二 = 打包并落(tmp_path, root, 2)
    apply_bundle(root, 二)
    rollback_bundle(root, at=后来, reason="崩了")
    for _ in range(MAX_ROLLBACKS + 5):
        apply_bundle(root, 二, force=True, at=后来)

    st = read_state(root)
    assert len(st.forced) == MAX_ROLLBACKS
    assert 二 in st.denied
