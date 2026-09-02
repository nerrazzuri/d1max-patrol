"""巡检报告:Markdown 与自包含 HTML。"""

from __future__ import annotations

import json
import sys

import pytest

from d1max_patrol.inspect.report import build_html, build_markdown, write_reports

P1 = "P1_transformer__front__20260901T101500Z.jpg"
P2 = "P2_panel__back__20260901T101530Z.jpg"

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"

MANIFEST = {
    "mission": {"mission": "substation_night_patrol", "map_id": "map_1"},
    "started_at": "20260901T101500Z",
    "fingerprint": {"sdk": "0.1.1"},
    "summary": {
        "state": "DONE",
        "reason": "",
        "succeeded": 1,
        "failed": 1,
        "total": 2,
        "results": [
            {"name": "P1_transformer", "ok": True, "arrived_ms": 1,
             "elapsed_s": 5.1, "photos": [P1], "note": ""},
            {"name": "P2_panel", "ok": False, "arrived_ms": 0,
             "elapsed_s": 7.2, "photos": [], "note": "到点超时"},
        ],
    },
}


def _make_run(tmp_path, *, manifest=MANIFEST, photos=(P1, P2)):
    run = tmp_path / "substation_night_patrol" / "20260901T101500Z"
    (run / "photos").mkdir(parents=True)
    if manifest is not None:
        (run / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for name in photos:
        (run / "photos" / name).write_bytes(JPEG)
    return run


@pytest.fixture
def run_dir(tmp_path):
    return _make_run(tmp_path)


@pytest.fixture
def empty_run_dir(tmp_path):
    run = tmp_path / "空跑" / "20260901T120000Z"
    run.mkdir(parents=True)
    return run


@pytest.fixture
def run_dir_with_findings(run_dir):
    (run_dir / "findings.json").write_text(json.dumps([
        {"photo": P1, "waypoint": "P1_transformer", "verdict": "abnormal",
         "confidence": 0.8, "reason": "柜门看着是开的"},
        {"photo": P2, "waypoint": "P2_panel", "verdict": "normal",
         "confidence": 0.9, "reason": "读数正常"},
    ], ensure_ascii=False), encoding="utf-8")
    (run_dir / "review.json").write_text(json.dumps(
        {P1: {"verdict": "normal", "note": "现场看过了,是反光"}},
        ensure_ascii=False), encoding="utf-8")
    return run_dir


# ------------------------------------------------------------------- Markdown


def test_点位表格每个点一行(run_dir):
    md = build_markdown(run_dir)
    for name in ("P1_transformer", "P2_panel"):
        assert name in md


def test_汇总里有总时长和成功失败数(run_dir):
    md = build_markdown(run_dir)
    assert "成功" in md and "失败" in md
    assert "12.3s" in md, "总时长要把每个点的用时加起来"


def test_失败点位的原因会写出来(run_dir):
    assert "到点超时" in build_markdown(run_dir)


def test_环境指纹进报告(run_dir):
    """报告要能追溯到当时的软件版本 —— 半年后回头查全靠它。"""
    assert "sdk=0.1.1" in build_markdown(run_dir)


def test_md里的中文不会被转义(run_dir_with_findings):
    md = build_markdown(run_dir_with_findings)
    assert "柜门看着是开的" in md and "\\u" not in md


def test_没有照片的运行也出得了报告(empty_run_dir):
    """跑到一半崩掉的运行最需要报告,这时候恰恰什么都没有。"""
    md = build_markdown(empty_run_dir)
    assert "巡检报告" in md and "没有点位记录" in md


def test_没走到收尾时退回state读结果(tmp_path):
    """manifest 里没有汇总,说明这趟是崩掉的 —— 别把结果一起丢了。"""
    run = _make_run(tmp_path, manifest={"mission": {"mission": "x"},
                                        "started_at": "20260901T101500Z"})
    (run / "state.json").write_text(json.dumps({
        "state": "ABORTED", "reason": "雷达掉线",
        "results": [{"name": "P1_transformer", "ok": True, "elapsed_s": 3.0,
                     "photos": [P1], "note": ""}],
    }, ensure_ascii=False), encoding="utf-8")
    md = build_markdown(run)
    assert "ABORTED" in md and "P1_transformer" in md


# --------------------------------------------------------------------- 判读


def test_有判读结论时报告里模型和人的分开写(run_dir_with_findings):
    md = build_markdown(run_dir_with_findings)
    assert "模型判读" in md and "人工复核" in md


def test_人改过的结论以人的为准(run_dir_with_findings):
    md = build_markdown(run_dir_with_findings)
    idx_final = md.index("最终结论")
    assert "正常" in md[idx_final:idx_final + 200]


def test_模型的原始结论不会被复核抹掉(run_dir_with_findings):
    """复核是加一层,不是改一层 —— 谁判的必须分得清。"""
    md = build_markdown(run_dir_with_findings)
    assert "柜门看着是开的" in md


def test_没复核过的会写明尚未复核(run_dir_with_findings):
    md = build_markdown(run_dir_with_findings)
    assert "尚未复核" in md, "P2 没人看过,报告里不能装作看过了"


def test_没判读过的报告照样出得来并注明待判读(run_dir):
    assert "待判读" in build_markdown(run_dir)


def test_判读文件坏了会写进报告而不是静默丢(run_dir):
    """悄悄少一段判读结果,比报告里写一行"读不了"危险得多。"""
    (run_dir / "findings.json").write_text("这不是 json", encoding="utf-8")
    md = build_markdown(run_dir)
    assert "findings.json 读不了" in md
    assert "P1_transformer" in md, "判读挂了不该把整份报告拖下水"


def test_照片名拆不开也不会被漏掉(run_dir):
    (run_dir / "photos" / "怪名字.jpg").write_bytes(JPEG)
    assert "怪名字" in build_markdown(run_dir)


# ---------------------------------------------------------------------- HTML


def test_html是自包含的(run_dir):
    html = build_html(run_dir)
    assert "data:image/jpeg;base64," in html
    assert "http://" not in html and "https://" not in html, "自包含意味着断网也能看"


def test_html里的中文不会乱码(run_dir):
    assert 'charset="utf-8"' in build_html(run_dir).lower()


def test_照片文件名里的与号会被转义(run_dir):
    """点位名进 HTML 前必须转义 —— Task 1 已经挡了斜杠,这里是第二道。

    这条用 ``&`` 而不是尖括号:Windows 上根本建不出带 ``<`` 的文件名,而
    开发是在 Windows 上做的。尖括号那条单独放在下面,只在 Linux 上跑。
    """
    (run_dir / "photos" / "a&b__front__20260901T101500Z.jpg").write_bytes(JPEG)
    html = build_html(run_dir)
    assert "a&amp;b" in html
    assert ">a&b<" not in html


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows 的文件名不许带尖括号,建不出这个用例")
def test_照片文件名里的尖括号不会破坏html(run_dir):
    (run_dir / "photos" / "a<script>__front__20260901T101500Z.jpg").write_bytes(JPEG)
    assert "<script>" not in build_html(run_dir)


def test_判读理由里的标签不会破坏html(run_dir):
    """模型回的是自由文本,里面带标签完全可能 —— 这条在哪个平台都跑得了。"""
    (run_dir / "findings.json").write_text(json.dumps([
        {"photo": P1, "waypoint": "P1_transformer", "verdict": "abnormal",
         "confidence": 0.5, "reason": "<script>alert(1)</script>"}],
        ensure_ascii=False), encoding="utf-8")
    assert "<script>" not in build_html(run_dir)


def test_点位说明里的尖括号也会转义(run_dir):
    manifest = json.loads(json.dumps(MANIFEST))
    manifest["summary"]["results"][1]["note"] = "<b>坏了</b>"
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                           encoding="utf-8")
    assert "<b>坏了</b>" not in build_html(run_dir)


def test_html里每张照片都内嵌一次(run_dir):
    assert build_html(run_dir).count("data:image/jpeg;base64,") == 2


def test_照片读不了时报告照样出得来(run_dir, monkeypatch):
    """一张照片坏了不该让整份报告出不来 —— 现场就靠它交差。"""
    from d1max_patrol.inspect import report

    def boom(path):
        raise OSError("磁盘坏道")

    monkeypatch.setattr(report, "_data_uri", boom)
    html = build_html(run_dir)
    assert "读不了" in html and "P2_panel" in html


def test_空运行的html也是完整的(empty_run_dir):
    html = build_html(empty_run_dir)
    assert html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")


# -------------------------------------------------------------------- 写文件


def test_写报告会同时落md和html(run_dir):
    md, html = write_reports(run_dir)
    assert md.name == "report.md" and html.name == "report.html"
    assert md.exists() and html.exists()


def test_写报告不动归档里的别的东西(run_dir):
    before = (run_dir / "manifest.json").read_text(encoding="utf-8")
    write_reports(run_dir)
    assert (run_dir / "manifest.json").read_text(encoding="utf-8") == before
    assert len(list((run_dir / "photos").iterdir())) == 2


def test_重出报告会覆盖上一次的(run_dir):
    """现场会先出一版看看,判读完再出一版 —— 不能留两份互相矛盾的。"""
    md, _ = write_reports(run_dir)
    (run_dir / "findings.json").write_text(json.dumps([
        {"photo": P1, "waypoint": "P1_transformer", "verdict": "abnormal",
         "confidence": 0.7, "reason": "门开着"}], ensure_ascii=False),
        encoding="utf-8")
    write_reports(run_dir)
    assert "门开着" in md.read_text(encoding="utf-8")


def test_报告模块不认识HTTP服务端():
    """``inspect`` 层和 ``engine`` 层一样,必须能脱离 app 单独用。"""
    import inspect as _inspect

    from d1max_patrol.inspect import report

    src = _inspect.getsource(report)
    for banned in ("http.server", "socketserver", "d1max_patrol.app"):
        assert banned not in src, f"报告里不该出现 {banned}"
