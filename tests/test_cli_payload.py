"""W56:``d1max payload relay-setup`` / ``relay-status``(硬件组 10-31:生产时把 0x0030 设成 50)。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _fake_board():
    p = ROOT / "packages" / "adapter-d1max" / "tests"
    sys.path.insert(0, str(p))
    try:
        spec = importlib.util.spec_from_file_location("_tp", p / "test_payload.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    finally:
        sys.path.remove(str(p))
    return m.假板子()


@pytest.fixture
def board(monkeypatch, tmp_path):
    b = _fake_board()
    import d1max_contract.serialport as sp
    monkeypatch.setattr(sp, "open_serial", lambda path, baud: b.open())
    raw = json.loads((ROOT / "deploy" / "payload.v2.example.json").read_text())
    raw["audio"]["clips_dir"] = str(tmp_path)
    (tmp_path / "payload.json").write_text(json.dumps(raw))
    yield b, tmp_path / "payload.json"
    b.close()


def test_生产设置断线全关(board, capsys):
    from d1max_patrol.cli import main
    b, cfg = board
    b.holding[0x30] = 0
    assert main(["payload", "relay-setup", "--config", str(cfg)]) == 0
    assert b.holding[0x30] == 50
    assert "重新上电" in capsys.readouterr().out


def test_看继电器板状态(board, capsys):
    from d1max_patrol.cli import main
    b, cfg = board
    b.coils[1] = True
    b.inputs = 0b0010
    b.holding[0x30] = 0
    assert main(["payload", "relay-status", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "CH2=开" in out and "IN2=1" in out and "IN1=0" in out
    assert "应为 50" in out
