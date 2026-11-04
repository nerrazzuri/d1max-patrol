from nexwar_contract import oldenv


def test_改名_老的环境变量照抄成新的_新的已有不动_值不进日志(caplog):
    env = {"D1MAX_HAL": "d1max", "D1MAX_DEVICE_PIN": "123456", "NEXWAR_MAP": "m:1", "D1MAX_MAP": "old",
           "PATH": "/bin"}
    got = oldenv.adopt(env)
    assert sorted(got) == ["NEXWAR_DEVICE_PIN", "NEXWAR_HAL"]
    assert env["NEXWAR_HAL"] == "d1max" and env["NEXWAR_MAP"] == "m:1", "新键已有的不动"
    assert "123456" not in caplog.text
