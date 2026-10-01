

def test_W13a_老库没有原点表_打开时按以前的挑法从待命点抄成原点(tmp_path):
    """以前下发地图拿「这张图上的待命点(默认的优先、再按名字)」当原点;迁移照同样的挑法,待命点原样留着。"""
    from d1max_site.db import SiteDB
    p = tmp_path / "site.db"
    db = SiteDB(p)
    with db.tx() as c:
        c.execute("DROP TABLE homes")
        for row in (("A", "zz", "m", "1", 1.0, 1), ("A", "aa", "m", "1", 2.0, 0),
                    ("A", "bb", "m", "2", 3.0, 0), ("A", "cc", "m", "2", 4.0, 0),
                    ("B", "x", "m", "1", 5.0, 0), ("B", "old", "m", "", 6.0, 1)):
            c.execute("INSERT INTO standby_points(robot_id, name, map_id, map_version, x, y, yaw, "
                      "is_default) VALUES (?,?,?,?,?,0,0,?)", row)
    db.close()
    db = SiteDB(p)
    got = {(r["robot_id"], r["map_version"]): (r["name"], r["x"])
           for r in db.query("SELECT * FROM homes")}
    assert got == {("A", "1"): ("zz", 1.0), ("A", "2"): ("bb", 3.0), ("B", "1"): ("x", 5.0)}
    assert len(db.query("SELECT * FROM standby_points")) == 6, "待命点原样留着"
    with db.tx() as c:
        c.execute("DELETE FROM homes")
    db.close()
    db = SiteDB(p)
    assert not db.query("SELECT * FROM homes"), "表已经在了:不再抄(删掉的原点不复活)"
    db.close()
