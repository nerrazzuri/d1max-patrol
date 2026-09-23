"""装机的人手上那几条命令。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from d1max_patrol.cli import _release_root, build_parser, main
from d1max_patrol.engine.release import (
    MANIFEST_NAME,
    MAX_BOOT_ATTEMPTS,
    Layout,
    activate,
    commit,
    current_name,
    read_manifest,
    read_pending,
    stage,
    tree_sha256,
    write_pending,
)

NOW = 1_700_000_000_000


def _pkg(root: Path, name: str, *, 弄坏: bool = False) -> Path:
    where = root / name
    (where / "bin").mkdir(parents=True, exist_ok=True)
    (where / "bin" / "run.py").write_text("print(1)\n", encoding="utf-8")
    sha = tree_sha256(where)
    if 弄坏:
        (where / "bin" / "run.py").write_text("偷偷改了\n", encoding="utf-8")
    (where / MANIFEST_NAME).write_text(json.dumps({
        "name": name, "version": "0.2.0", "content_sha256": sha,
        "requires_mission_schema": 1, "built_at": "2026-09-20T03:11:00Z",
    }), encoding="utf-8")
    return where


def test_装第一版(tmp_path, capsys):
    root = tmp_path / "opt"
    pkg = _pkg(tmp_path / "pkg", "2026-09-20-77b2de")
    assert main(["release", "install", str(pkg), "--root", str(root)]) == 0
    assert "2026-09-20-77b2de" in capsys.readouterr().out


def test_装坏包退非零而且不留半个目录(tmp_path):
    root = tmp_path / "opt"
    pkg = _pkg(tmp_path / "pkg", "2026-09-20-77b2de", 弄坏=True)
    assert main(["release", "install", str(pkg), "--root", str(root)]) != 0
    assert list((root / "releases").glob("*")) == []


def test_列出装了哪几版(tmp_path, capsys):
    root = tmp_path / "opt"
    for name in ("2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        main(["release", "install", str(_pkg(tmp_path / name, name)),
              "--root", str(root)])
    capsys.readouterr()
    assert main(["release", "list", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "2026-09-06-a3f9c1" in out and "2026-09-20-77b2de" in out


def test_切版本会换链并留下在途标记(tmp_path):
    root = tmp_path / "opt"
    main(["release", "install", str(_pkg(tmp_path / "p", "2026-09-20-77b2de")),
          "--root", str(root)])
    assert main(["release", "activate", "2026-09-20-77b2de",
                 "--root", str(root)]) == 0
    layout = Layout(root=root)
    assert current_name(layout) == "2026-09-20-77b2de"
    assert read_pending(layout) is not None


def test_切版本记的sn跟环境变量对齐(tmp_path, monkeypatch):
    """跟 HTTP 服务侧(app/server.py 的 resolve(args.sn, ...))对齐同一个来源,

    否则命令行这条路记进 pending.sn 的是 MAC 兜底值,重启后自检拿真 SN 一比
    对不上,把一版好的自动回滚掉(见 fix1 brief 第 1 条)。
    """
    root = tmp_path / "opt"
    monkeypatch.setenv("D1MAX_SN", "D1M-XYZ-9")
    main(["release", "install", str(_pkg(tmp_path / "p", "2026-09-20-77b2de")),
          "--root", str(root)])
    assert main(["release", "activate", "2026-09-20-77b2de",
                 "--root", str(root)]) == 0
    layout = Layout(root=root)
    assert read_pending(layout).sn == "D1M-XYZ-9"


def test_切版本提示升级前检查在这条路上没跑(tmp_path, capsys):
    root = tmp_path / "opt"
    main(["release", "install", str(_pkg(tmp_path / "p", "2026-09-20-77b2de")),
          "--root", str(root)])
    capsys.readouterr()
    assert main(["release", "activate", "2026-09-20-77b2de",
                 "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "没跑" in out


def test_切一个没装的版本退非零(tmp_path, capsys):
    root = tmp_path / "opt"
    layout = Layout(root=root)
    before = current_name(layout)
    assert main(["release", "activate", "根本没这一版", "--root", str(root)]) != 0
    err = capsys.readouterr().err
    assert "切不了" in err
    assert current_name(layout) == before


def test_守卫在没有在途标记时放行(tmp_path):
    """绝大多数开机走这条。它必须便宜、安静、退 0,而且不能碰链。"""
    root = tmp_path / "opt"
    layout = Layout(root=root)
    main(["release", "install", str(_pkg(tmp_path / "p", "2026-09-20-77b2de")),
          "--root", str(root)])
    main(["release", "activate", "2026-09-20-77b2de", "--root", str(root)])
    commit(layout)
    before = current_name(layout)
    assert main(["release", "boot-guard", "--root", str(root)]) == 0
    assert current_name(layout) == before


def test_守卫第一次开机只记一笔就放行(tmp_path):
    """第一次没起来可能只是慢。第一次不回滚。"""
    root = tmp_path / "opt"
    layout = Layout(root=root)
    for name in ("2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        stage(layout, _pkg(tmp_path / name, name), now_ms=1)
    activate(layout, "2026-09-06-a3f9c1", now_ms=1, auto=False)
    commit(layout)
    activate(layout, "2026-09-20-77b2de", now_ms=3, auto=False)
    assert main(["release", "boot-guard", "--root", str(root)]) == 0
    assert current_name(layout) == "2026-09-20-77b2de"
    assert read_pending(layout).attempts == 1


def test_守卫数够次数就退回去(tmp_path, capsys):
    """连着 MAX_BOOT_ATTEMPTS 次开机都还挂着在途标记 —— 说明它连自检都跑不到。

    跟 ``tests/engine/test_release_guard.py`` 里同一条判据(数满
    ``MAX_BOOT_ATTEMPTS`` 次都是 COUNTED,再多一次才 ROLLED_BACK)对齐 ——
    ``boot_guard`` 的开机计数是「数到这个数还没坐实才退」,不是「第二次开机
    就退」,所以这里按常量算次数,不硬编成 2。
    """
    root = tmp_path / "opt"
    layout = Layout(root=root)
    for name in ("2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        stage(layout, _pkg(tmp_path / name, name), now_ms=1)
    activate(layout, "2026-09-06-a3f9c1", now_ms=1, auto=False)
    commit(layout)
    activate(layout, "2026-09-20-77b2de", now_ms=3, auto=False)
    for _ in range(MAX_BOOT_ATTEMPTS):
        main(["release", "boot-guard", "--root", str(root)])
    assert main(["release", "boot-guard", "--root", str(root)]) == 0
    assert current_name(layout) == "2026-09-06-a3f9c1"
    assert read_pending(layout) is None
    assert "退回" in capsys.readouterr().out


def test_守卫放弃时提示落在stderr(tmp_path, capsys):
    """GAVE_UP:装机那一次就没起来,没有上一版可退。退出码仍是 0,

    但"请人来看"这句必须落在 stderr,不能被日常开机收 stdout 的脚本吞掉。
    造标记用 activate + write_pending,不手写 JSON。
    """
    root = tmp_path / "opt"
    layout = Layout(root=root)
    stage(layout, _pkg(tmp_path / "p", "2026-09-20-77b2de"), now_ms=1)
    pending = activate(layout, "2026-09-20-77b2de", now_ms=1, auto=False)
    write_pending(layout, replace(pending, attempts=MAX_BOOT_ATTEMPTS))
    capsys.readouterr()
    assert main(["release", "boot-guard", "--root", str(root)]) == 0
    out = capsys.readouterr()
    assert "请人来看" in out.err
    assert "请人来看" not in out.out


def test_守卫盘上一版都没有时提示落在stderr(tmp_path, capsys):
    """BROKEN:根目录下什么版本都没有,也没有在途标记 —— 这台机器要重装。"""
    root = tmp_path / "opt"
    capsys.readouterr()
    assert main(["release", "boot-guard", "--root", str(root)]) == 0
    out = capsys.readouterr()
    assert "重装" in out.err
    assert "重装" not in out.out


def test_守卫看的是根不是自己装在哪(tmp_path):
    """守卫**必须**跟版本无关 —— 它装在 <root>/bin 里,不在任何一版目录里。

    这条测试盯的是这一点的可观察后果:守卫只认 --root/环境变量,
    绝不去读 current 指向的目录里的任何东西。
    """
    root = tmp_path / "opt"
    layout = Layout(root=root)
    stage(layout, _pkg(tmp_path / "p", "2026-09-20-77b2de"), now_ms=1)
    activate(layout, "2026-09-20-77b2de", now_ms=1, auto=False)
    # 把 current 指向的目录整个搬走 —— 一个坏到不能再坏的版本。
    import shutil
    shutil.rmtree(layout.releases / "2026-09-20-77b2de")
    assert main(["release", "boot-guard", "--root", str(root)]) == 0


def test_根路径也能从环境变量来(tmp_path, monkeypatch, capsys):
    """systemd 单元里写环境变量比写一长串参数干净。"""
    root = tmp_path / "opt"
    monkeypatch.setenv("D1MAX_RELEASE_ROOT", str(root))
    main(["release", "install", str(_pkg(tmp_path / "p", "2026-09-20-77b2de"))])
    capsys.readouterr()
    assert main(["release", "list"]) == 0
    assert "2026-09-20-77b2de" in capsys.readouterr().out


def test_rollback成功退回上一版(tmp_path):
    root = tmp_path / "opt"
    layout = Layout(root=root)
    for name in ("2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        main(["release", "install", str(_pkg(tmp_path / name, name)),
              "--root", str(root)])
    main(["release", "activate", "2026-09-06-a3f9c1", "--root", str(root)])
    commit(layout)
    main(["release", "activate", "2026-09-20-77b2de", "--root", str(root)])
    assert main(["release", "rollback", "--root", str(root)]) == 0
    assert current_name(layout) == "2026-09-06-a3f9c1"


def test_rollback没有上一版可退时退非零(tmp_path, capsys):
    root = tmp_path / "opt"
    main(["release", "install", str(_pkg(tmp_path / "p", "2026-09-20-77b2de")),
          "--root", str(root)])
    capsys.readouterr()
    assert main(["release", "rollback", "--root", str(root)]) == 2
    assert "退不了" in capsys.readouterr().err


def test_migrate_data把槽里的数据搬到数据根(tmp_path, capsys):
    root = tmp_path / "opt"
    data = tmp_path / "var"
    layout = Layout(root=root)
    layout.releases.mkdir(parents=True)
    stage(layout, _pkg(tmp_path / "pkg", "2026-09-20-77b2de"), now_ms=NOW)
    slot = layout.releases / "2026-09-20-77b2de"
    (slot / "runs" / "r1").mkdir(parents=True)
    (slot / "runs" / "r1" / "manifest.json").write_text("{}", encoding="utf-8")

    assert main(["release", "migrate-data", "--root", str(root),
                 "--data-root", str(data)]) == 0

    out = capsys.readouterr().out
    assert (data / "runs" / "r1" / "manifest.json").exists()
    assert "2026-09-20-77b2de" in out


def test_migrate_data默认数据根读环境变量(tmp_path, monkeypatch):
    root = tmp_path / "opt"
    data = tmp_path / "var"
    layout = Layout(root=root)
    layout.releases.mkdir(parents=True)
    stage(layout, _pkg(tmp_path / "pkg", "2026-09-20-77b2de"), now_ms=NOW)
    (layout.releases / "2026-09-20-77b2de" / "queue.jsonl").write_text(
        json.dumps({"key": "r/x/events.jsonl", "priority": 2}) + "\n", encoding="utf-8")
    monkeypatch.setenv("D1MAX_DATA_ROOT", str(data))

    assert main(["release", "migrate-data", "--root", str(root)]) == 0
    assert '"key": "r/x/events.jsonl"' in (data / "queue.jsonl").read_text(encoding="utf-8")


def test_migrate_data合并出错时退非零而且提示搬迁失败(tmp_path, capsys):
    """数据根撞上类型不对的东西(这里让它本身就是个文件)—— 合并没法进行,
    要退非零、错误落 stderr,不能裸抛一坨 traceback。"""
    root = tmp_path / "opt"
    data = tmp_path / "var"
    data.write_text("我是个文件,不是目录", encoding="utf-8")
    layout = Layout(root=root)
    layout.releases.mkdir(parents=True)
    stage(layout, _pkg(tmp_path / "pkg", "2026-09-20-77b2de"), now_ms=NOW)
    slot = layout.releases / "2026-09-20-77b2de"
    (slot / "queue.jsonl").write_text("x\n", encoding="utf-8")

    assert main(["release", "migrate-data", "--root", str(root),
                 "--data-root", str(data)]) == 2

    err = capsys.readouterr().err
    assert "搬迁失败" in err


def test_release_root默认取opt_d1max(monkeypatch):
    """三级优先级(--root > D1MAX_RELEASE_ROOT > /opt/d1max)只测过前两级。

    这里只调纯函数,不经过 main(),绝不能真的去碰 /opt/d1max。
    """
    monkeypatch.delenv("D1MAX_RELEASE_ROOT", raising=False)
    assert _release_root(None) == Path("/opt/d1max")


# ------------------------------------------------------- pack(在笔记本上跑)


def _源码树(root):
    """一棵最小的、能被 pack 收下的源码树。字段都跟真仓库一个形状。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "d1max-patrol"\nversion = "0.4.2"\n',
        encoding="utf-8")
    (root / "src" / "d1max_patrol").mkdir(parents=True, exist_ok=True)
    (root / "src" / "d1max_patrol" / "__init__.py").write_text(
        "", encoding="utf-8")
    (root / "config" / "params").mkdir(parents=True, exist_ok=True)
    (root / "config" / "params" / "mapper_3d.yaml").write_text(
        "resolution: 0.05\n", encoding="utf-8")
    (root / "deploy").mkdir(parents=True, exist_ok=True)          # W01b:deploy/ 进包
    (root / "deploy" / "d1max-patrol.service").write_text("[Unit]\n", encoding="utf-8")
    # 一个不该进包的东西,验一下 CLI 这条路上排除规则也是活的。
    (root / "refs").mkdir(parents=True, exist_ok=True)
    (root / "refs" / "厂商协议.md").write_text("私有\n", encoding="utf-8")
    return root


def test_pack打出包目录和指纹(tmp_path, capsys):
    """**屏幕上必须有这两行** —— 现场记录栏要抄它们。"""
    出 = tmp_path / "出"
    assert main(["release", "pack", str(_源码树(tmp_path / "树")), str(出)]) == 0
    out = capsys.readouterr().out
    包 = next(p for p in 出.iterdir() if p.is_dir())
    manifest = read_manifest(包)
    assert str(包) in out
    assert manifest.content_sha256 in out
    assert "0.4.2" in out


def test_pack打出来的包能被install真的收下(tmp_path, capsys):
    """端到端:这正是明天装机 install.sh 3/7 走的那条路。"""
    出 = tmp_path / "出"
    root = tmp_path / "opt"
    assert main(["release", "pack", str(_源码树(tmp_path / "树")), str(出)]) == 0
    包 = next(p for p in 出.iterdir() if p.is_dir())
    capsys.readouterr()
    assert main(["release", "install", str(包), "--root", str(root)]) == 0
    assert "落槽了" in capsys.readouterr().out
    assert (root / "releases" / 包.name / MANIFEST_NAME).is_file()
    # 排除规则在 CLI 这条路上也得是活的。
    assert not (root / "releases" / 包.name / "refs").exists()


def test_pack名字不合规退非零(tmp_path, capsys):
    出 = tmp_path / "出"
    assert main(["release", "pack", str(_源码树(tmp_path / "树")), str(出),
                 "--name", "d1max-2026-09-20-77b2de"]) == 2
    assert "打不了包" in capsys.readouterr().err
    assert not 出.exists() or list(出.iterdir()) == []


def test_pack不覆盖已有的包除非给force(tmp_path, capsys):
    出 = tmp_path / "出"
    树 = _源码树(tmp_path / "树")
    assert main(["release", "pack", str(树), str(出)]) == 0
    包 = next(p for p in 出.iterdir() if p.is_dir())
    capsys.readouterr()
    assert main(["release", "pack", str(树), str(出), "--name", 包.name]) == 2
    assert "--force" in capsys.readouterr().err
    assert main(["release", "pack", str(树), str(出), "--name", 包.name,
                 "--force"]) == 0


def test_pack的帮助说清了它跑在笔记本上(capsys):
    """现场最容易犯的错是拿着这条命令上狗敲。--help 里必须挡住它。"""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["release", "pack", "--help"])
    帮助 = capsys.readouterr().out
    assert "笔记本" in 帮助 and "狗" in 帮助


# ------------------------------------------------------------ W01b:单元随包装

def _两版(tmp_path, root):
    for name in ("2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        main(["release", "install", str(_pkg(tmp_path / name, name)), "--root", str(root)])


class _假助手:
    def __init__(self, 在: bool = True, 拒: str | None = None) -> None:
        self.在, self.拒, self.装过 = 在, 拒, []

    def present(self) -> bool:
        return self.在

    def install_unit(self, name: str) -> str:
        from d1max_patrol.engine.privileged import PrivilegedError
        if self.拒:
            raise PrivilegedError(self.拒)
        self.装过.append(name)
        return "installed"


def test_activate在有助手时先装单元(tmp_path, monkeypatch, capsys):
    import d1max_patrol.cli as cli
    助手 = _假助手()
    monkeypatch.setattr(cli, "Privileged", lambda: 助手)
    root = tmp_path / "opt"
    monkeypatch.setattr(cli, "_HELPER_RELEASE_ROOT", root)
    _两版(tmp_path, root)
    assert main(["release", "activate", "2026-09-20-77b2de", "--root", str(root)]) == 0
    assert 助手.装过 == ["2026-09-20-77b2de"]
    assert "单元文件: installed" in capsys.readouterr().out


def test_activate没助手就提示并照常切(tmp_path, monkeypatch, capsys):
    import d1max_patrol.cli as cli
    助手 = _假助手(在=False)
    monkeypatch.setattr(cli, "Privileged", lambda: 助手)
    root = tmp_path / "opt"
    monkeypatch.setattr(cli, "_HELPER_RELEASE_ROOT", root)
    _两版(tmp_path, root)
    assert main(["release", "activate", "2026-09-20-77b2de", "--root", str(root)]) == 0
    assert 助手.装过 == []
    assert "单元文件没更新" in capsys.readouterr().out
    assert current_name(Layout(root=root)) == "2026-09-20-77b2de"


def test_activate单元装不上退非零且不切链(tmp_path, monkeypatch, capsys):
    import d1max_patrol.cli as cli
    monkeypatch.setattr(cli, "Privileged", lambda: _假助手(拒="第 8 行:User 只能是 robot"))
    root = tmp_path / "opt"
    monkeypatch.setattr(cli, "_HELPER_RELEASE_ROOT", root)
    _两版(tmp_path, root)
    assert main(["release", "activate", "2026-09-20-77b2de", "--root", str(root)]) == 2
    assert current_name(Layout(root=root)) == ""
    assert read_pending(Layout(root=root)) is None
    assert "User 只能是 robot" in capsys.readouterr().err


def test_rollback也装回上一版单元(tmp_path, monkeypatch):
    import d1max_patrol.cli as cli
    助手 = _假助手()
    monkeypatch.setattr(cli, "Privileged", lambda: 助手)
    root = tmp_path / "opt"
    monkeypatch.setattr(cli, "_HELPER_RELEASE_ROOT", root)
    layout = Layout(root=root)
    _两版(tmp_path, root)
    main(["release", "activate", "2026-09-06-a3f9c1", "--root", str(root)])
    commit(layout)
    main(["release", "activate", "2026-09-20-77b2de", "--root", str(root)])
    助手.装过.clear()
    assert main(["release", "rollback", "--root", str(root)]) == 0
    assert 助手.装过 == ["2026-09-06-a3f9c1"]


def test_rollback单元装不回去也照样退回去(tmp_path, monkeypatch, capsys):
    import d1max_patrol.cli as cli
    root = tmp_path / "opt"
    layout = Layout(root=root)
    monkeypatch.setattr(cli, "_HELPER_RELEASE_ROOT", root)
    monkeypatch.setattr(cli, "Privileged", lambda: _假助手(在=False))
    _两版(tmp_path, root)
    main(["release", "activate", "2026-09-06-a3f9c1", "--root", str(root)])
    commit(layout)
    main(["release", "activate", "2026-09-20-77b2de", "--root", str(root)])
    monkeypatch.setattr(cli, "Privileged", lambda: _假助手(拒="源不存在"))
    assert main(["release", "rollback", "--root", str(root)]) == 0
    assert current_name(layout) == "2026-09-06-a3f9c1"
    assert "源不存在" in capsys.readouterr().err


def test_版本根不是opt_d1max时不走助手(tmp_path, monkeypatch, capsys):
    """助手只认真机上写死的 /opt/d1max;--root 指到别处时装的会是另一棵树的单元。"""
    import d1max_patrol.cli as cli
    助手 = _假助手()
    monkeypatch.setattr(cli, "Privileged", lambda: 助手)      # 助手在,但根不对
    root = tmp_path / "opt"
    _两版(tmp_path, root)
    assert main(["release", "activate", "2026-09-20-77b2de", "--root", str(root)]) == 0
    assert 助手.装过 == []
    assert "特权助手只认" in capsys.readouterr().out
    assert current_name(Layout(root=root)) == "2026-09-20-77b2de"
