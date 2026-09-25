"""W00c5d:狗往站点搬文件的契约。"""

from __future__ import annotations

from d1max_contract.intake import dog_may_upload, split_run


def test_狗只能传清单事件遥测照片():
    for ok in ("manifest.json", "events.jsonl", "telemetry.jsonl", "photos/P1__front__x.jpg"):
        assert dog_may_upload(ok), ok
    for bad in ("findings.json", "review.json", "report.md", "state.json", "photos/a/b.jpg",
                "photos/x.exe", "../manifest.json", "photos/"):
        assert not dog_may_upload(bad), bad


def test_一趟的目录名():
    assert split_run("巡检一/20260925T010000Z") == ("巡检一", "20260925T010000Z")
    for bad in ("巡检一", "巡检一/x", "a/b/20260925T010000Z", "/20260925T010000Z"):
        assert split_run(bad) is None, bad
