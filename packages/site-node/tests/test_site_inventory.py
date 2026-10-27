"""站点清点(RC 加固 H5):换主机前记一份、恢复后比。站点身份变了不过;不备份的少了是意料之中;
别的少了要人看。"""

# ruff: noqa: F811  (「站」是从 test_site_restore 借来的夹具,参数同名)
from __future__ import annotations

import json
import os
import sqlite3

import pytest
from test_site_restore import NOW, 站  # noqa: F401  (夹具)

from d1max_site import inventory
from d1max_site import main as site_main
from d1max_site.restore import restore


def _take(home, **over):
    got = inventory.take(home, fingerprint=site_main.cmd_fingerprint(home), site_id="estate-1",
                         now_ms=NOW)
    return got | over


def test_清点_数得对_只读_不升库结构(站):
    home, *_ = 站
    con = sqlite3.connect(home / "site.db")
    con.execute("UPDATE meta SET value='41' WHERE key='schema'")     # 假装是老库
    con.commit()
    con.close()
    before = (home / "site.db").stat().st_mtime_ns
    got = _take(home)
    assert got["kind"] == inventory.KIND and got["schema"] == 41, "只读:没把库升上去"
    assert (home / "site.db").stat().st_mtime_ns == before
    assert got["counts"]["robots"] == 1 and got["counts"]["runs"] == 1
    assert got["counts"]["run_photos"] == 1 and got["counts"]["accounts"] == 0
    assert len(got["fingerprint"]) == 64 and set(got["counts"]) == set(inventory.TABLES)


def test_真恢复一遍_指纹和数都一样_过(站, tmp_path, capsys):
    home, dest, key, db, _ = 站
    db.close()
    rec = tmp_path / "before.json"
    assert site_main.main(["--home", str(home), "inventory", "--out", str(rec)]) == 0
    new = tmp_path / "newhost"
    assert restore(dest, key, new).ok
    capsys.readouterr()
    assert site_main.main(["--home", str(new), "inventory", "--compare", str(rec)]) == 0
    shown = capsys.readouterr().out
    assert "[一样] 站点证书指纹" in shown and "[一样] 登记的机器人: 1" in shown
    assert shown.strip().endswith("结论:过。"), shown


def test_备份之后才加的_恢复后少了_要人看_不替人下结论(站, tmp_path, capsys):
    home, dest, key, db, _ = 站
    with db.tx() as c:                       # 备份已经做完了,这之后加一个账号
        c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, disabled) "
                  "VALUES ('late','guard',?,?,0,0)", (b"s", b"h"))
    db.close()
    rec = tmp_path / "before.json"
    assert site_main.main(["--home", str(home), "inventory", "--out", str(rec)]) == 0
    new = tmp_path / "newhost"
    assert restore(dest, key, new).ok
    capsys.readouterr()
    assert site_main.main(["--home", str(new), "inventory", "--compare", str(rec)]) == 0
    shown = capsys.readouterr().out
    assert "[要人看] 账号: 1 → 0(少了 1)" in shown
    assert "对一下是不是都在最后一次备份之后" in shown


def test_站点身份变了_库比原来老_不过_退出码1(站, tmp_path, capsys):
    home, *_ = 站
    now = _take(home)
    for over, why in (({"fingerprint": "ab" * 32}, "站点证书指纹"),
                      ({"site_id": "other"}, "站点编号"),
                      ({"schema": now["schema"] + 1}, "库结构")):
        r = inventory.compare(now | over, now)
        assert not r["ok"] and [x["what"] for x in r["lines"] if x["level"] == "fail"] == [why]
    rec = tmp_path / "before.json"
    rec.write_text(json.dumps(now | {"fingerprint": "ab" * 32}), "utf-8")
    assert site_main.main(["--home", str(home), "inventory", "--compare", str(rec)]) == 1
    assert "结论:不过。" in capsys.readouterr().out
    empty = inventory.compare(now | {"fingerprint": ""}, now | {"fingerprint": ""})
    assert not empty["ok"], "两边都没有指纹不算一样"


def test_连续录像少了是意料之中_多了的照实写_库升了不算错(站):
    home, *_ = 站
    now = _take(home)
    before = json.loads(json.dumps(now))
    before["counts"]["recordings"] = 40
    before["counts"]["alerts"] = 0
    before["schema"] = now["schema"] - 1
    after = json.loads(json.dumps(now))
    after["counts"]["alerts"] = 3
    r = inventory.compare(before, after)
    by = {x["what"]: x for x in r["lines"]}
    assert r["ok"] and not r["review"]
    assert by["连续录像"]["level"] == "expected" and "不备份" in by["连续录像"]["detail"]
    assert by["告警"]["level"] == "more" and by["库结构"]["level"] == "more"
    assert inventory.render(r).endswith("结论:过。")


def test_老库没有的表_两边都没有不提_一边没有照实写(站):
    home, *_ = 站
    now = _take(home)
    a, b = json.loads(json.dumps(now)), json.loads(json.dumps(now))
    a["counts"]["zone_areas"] = b["counts"]["zone_areas"] = None
    assert "防区范围" not in [x["what"] for x in inventory.compare(a, b)["lines"]]
    b["counts"]["zone_areas"] = 2
    line = next(x for x in inventory.compare(a, b)["lines"] if x["what"] == "防区范围")
    assert line["level"] == "more" and "没有这张表" in line["detail"]


def test_记录读不了_不是清点记录_没有库_说得出(站, tmp_path, capsys):
    home, *_ = 站
    assert site_main.main(["--home", str(home), "inventory", "--compare",
                           str(tmp_path / "无")]) != 0
    assert "读不了清点记录" in capsys.readouterr().err
    bad = tmp_path / "x.json"
    for text in ('{"kind": "else", "counts": {}}', '{"kind": "d1max-site-inventory"}', "[]"):
        bad.write_text(text, "utf-8")
        with pytest.raises(inventory.InventoryError, match="不是站点清点记录"):
            inventory.load(bad)
    with pytest.raises(inventory.InventoryError, match="没有站点库"):
        inventory.take(tmp_path / "空", fingerprint="x", site_id="y", now_ms=NOW)


def test_不给参数_打印出来_能当记录用(站, capsys):
    home, *_ = 站
    assert site_main.main(["--home", str(home), "inventory"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["kind"] == inventory.KIND and got["site_id"] == "estate-1"
    assert os.path.exists(home / "site.db")
