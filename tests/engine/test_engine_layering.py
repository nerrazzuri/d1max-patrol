"""引擎不许知道 HTTP、不许依赖老的 app 层(原 ``tests/app/test_server.py`` 里那一条,W00c5e 老服务
退役时挪到这儿)。反过来是允许的。这一条要是破了,引擎就不能在代理、命令行、仿真里单独跑了。"""

from __future__ import annotations

from pathlib import Path


def test_引擎不import_http():
    import d1max_agent.engine  # 引擎的真身(W00b 起住在 robot-agent 包里)

    for py in Path(d1max_agent.engine.__file__).parent.glob("*.py"):
        src = py.read_text(encoding="utf-8")
        for banned in ("import http", "socketserver", "d1max_patrol.app"):
            assert banned not in src, f"{py.name} 里出现了 {banned}"
