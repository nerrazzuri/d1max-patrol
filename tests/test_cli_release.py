"""装机的人手上那几条命令。"""

from __future__ import annotations

import json
from pathlib import Path

from d1max_patrol.cli import main
from d1max_patrol.engine.release import (
    MANIFEST_NAME,
    MAX_BOOT_ATTEMPTS,
    Layout,
    activate,
    commit,
    current_name,
    read_pending,
    stage,
    tree_sha256,
)


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


def test_切一个没装的版本退非零(tmp_path):
    root = tmp_path / "opt"
    assert main(["release", "activate", "根本没这一版", "--root", str(root)]) != 0


def test_守卫在没有在途标记时放行(tmp_path):
    """绝大多数开机走这条。它必须便宜、安静、退 0。"""
    root = tmp_path / "opt"
    main(["release", "install", str(_pkg(tmp_path / "p", "2026-09-20-77b2de")),
          "--root", str(root)])
    assert main(["release", "boot-guard", "--root", str(root)]) == 0


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
