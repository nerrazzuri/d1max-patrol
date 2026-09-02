"""VLM 判读与人工复核。

**一个真 API 都不打。** 假客户端记下每次调用的提示词和图片,判读逻辑本身
——提示词怎么拼、历史照片送不送、失败怎么降级、两层结论怎么分开存 ——
全在这一层,跟模型是谁没关系。

真客户端(:class:`AnthropicClient`)只测请求体的形状:发出去的是不是
base64 图 + 一段文字,密钥在不在请求头里。发出去之后是 Anthropic 的事。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from d1max_patrol.inspect.judge import (
    AnthropicClient,
    Finding,
    build_prompt,
    default_client,
    judge_run,
    parse_reply,
    read_findings,
    read_reviews,
    save_review,
)

#: 两张照片。第一张有 check,第二张没有。
PHOTO = "P1_transformer__front__20260901T101500Z.jpg"
PHOTO2 = "P2_gauge__front__20260901T101600Z.jpg"

#: 假的 JPEG。判读这一层从不解码图片,有几个字节能区分开就够了。
_JPG = b"\xff\xd8fake-jpeg\xff\xd9"


class FakeClient:
    """记下每次调用,回一个固定结论。"""

    def __init__(self, reply: dict[str, Any] | None = None) -> None:
        self.reply = reply if reply is not None else {
            "verdict": "normal", "confidence": 0.9,
            "reason": "门是关着的", "evidence": "画面中央"}
        self.prompts: list[str] = []
        self.images: list[list[bytes]] = []

    def judge(self, prompt: str, images) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.images.append(list(images))
        return dict(self.reply)


class FlakyClient(FakeClient):
    """第一次炸,之后正常。"""

    def judge(self, prompt: str, images) -> dict[str, Any]:
        first = not self.prompts
        super().judge(prompt, images)
        if first:
            raise RuntimeError("529 overloaded")
        return dict(self.reply)


class GarbageClient(FakeClient):
    """回一段根本不是结论的东西。"""

    def judge(self, prompt: str, images) -> Any:
        super().judge(prompt, images)
        return "好的,我看看……"


def _write_run(root: Path, mission: str, stamp: str, *,
               photos=(PHOTO, PHOTO2), checks=("配电柜门是否关闭", "")) -> Path:
    """手搭一个跑完的归档目录。"""
    run = root / mission / stamp
    (run / "photos").mkdir(parents=True)
    for name in photos:
        (run / "photos" / name).write_bytes(_JPG + name.encode("utf-8"))
    waypoints = []
    for name, check in zip([n.split("__")[0] for n in photos], checks, strict=True):
        wp: dict[str, Any] = {"name": name, "pose": {}}
        if check:
            wp["check"] = check
        waypoints.append(wp)
    (run / "manifest.json").write_text(json.dumps({
        "mission": {"mission": mission, "map_id": "map_test",
                    "waypoints": waypoints},
        "started_at": stamp, "fingerprint": {}, "summary": {},
    }, ensure_ascii=False), encoding="utf-8")
    return run


@pytest.fixture
def run_dir(tmp_path) -> Path:
    return _write_run(tmp_path / "runs", "巡检一号", "20260902T101500Z")


@pytest.fixture
def run_dir_no_check(tmp_path) -> Path:
    return _write_run(tmp_path / "other", "巡检二号", "20260902T101500Z",
                      checks=("", ""))


@pytest.fixture
def history_root(tmp_path) -> Path:
    """runs 根目录,里面已经有**上一次**同点位拍的照片。"""
    _write_run(tmp_path / "runs", "巡检一号", "20260901T101500Z")
    return tmp_path / "runs"


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def flaky_client() -> FlakyClient:
    return FlakyClient()


@pytest.fixture
def garbage_client() -> GarbageClient:
    return GarbageClient()


@pytest.fixture(autouse=True)
def 没有密钥(monkeypatch):
    """默认把密钥抹掉。

    不抹的话,开发机上真配了 ``ANTHROPIC_API_KEY`` 的那台就会在跑
    ``judge_run(run_dir)`` 时打真 API —— 花钱、要网、还慢。
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# --------------------------------------------------------------------- 判读


def test_每张照片判一次(run_dir, fake_client):
    findings = judge_run(run_dir, client=fake_client)
    assert len(findings) == len(list((run_dir / "photos").iterdir()))


def test_点位的check进了提示词(run_dir, fake_client):
    judge_run(run_dir, client=fake_client)
    assert "配电柜门是否关闭" in fake_client.prompts[0]


def test_提示词里带着点位名(run_dir, fake_client):
    """判读依据是一句话,点位名是它的上下文 —— "门"指哪个门要靠它。"""
    judge_run(run_dir, client=fake_client)
    assert "P1_transformer" in fake_client.prompts[0]


def test_没写check时退化成找异常(run_dir_no_check, fake_client):
    judge_run(run_dir_no_check, client=fake_client)
    assert "异常" in fake_client.prompts[0]


def test_上一次同点位的照片会一起送进去比对(run_dir, history_root, fake_client):
    judge_run(run_dir, client=fake_client, history_root=history_root)
    assert len(fake_client.images[0]) == 2


def test_比对时提示词里说清楚哪张是新的(run_dir, history_root, fake_client):
    """不说的话模型会把两张当成同一时刻的两个角度,比出来的"变化"是假的。"""
    judge_run(run_dir, client=fake_client, history_root=history_root)
    assert "上一次" in fake_client.prompts[0]


def test_送进去的第一张是这次拍的(run_dir, history_root, fake_client):
    judge_run(run_dir, client=fake_client, history_root=history_root)
    assert fake_client.images[0][0] == (run_dir / "photos" / PHOTO).read_bytes()


def test_不会把自己这趟的照片当成历史(run_dir, fake_client):
    """``history_root`` 里包含当前这趟。拿它自己比对等于什么都没比。"""
    judge_run(run_dir, client=fake_client, history_root=run_dir.parent.parent)
    assert len(fake_client.images[0]) == 1


def test_没有历史照片时只送一张(run_dir, tmp_path, fake_client):
    judge_run(run_dir, client=fake_client, history_root=tmp_path)
    assert len(fake_client.images[0]) == 1


def test_结论落到findings_json(run_dir, fake_client):
    judge_run(run_dir, client=fake_client)
    assert read_findings(run_dir)[0].verdict in {"normal", "abnormal", "unclear"}


def test_落盘的结论带着点位名(run_dir, fake_client):
    judge_run(run_dir, client=fake_client)
    assert read_findings(run_dir)[0].waypoint == "P1_transformer"


def test_置信度和理由都留下来了(run_dir, fake_client):
    judge_run(run_dir, client=fake_client)
    got = read_findings(run_dir)[0]
    assert got.confidence == 0.9
    assert got.reason == "门是关着的"
    assert got.evidence == "画面中央"


def test_没有密钥时标成待判读而不是报错(run_dir, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert all(f.verdict == "pending" for f in judge_run(run_dir))


def test_没有密钥时理由里说得出为什么(run_dir):
    """"pending"三个字对现场没用,人要知道是没配密钥还是调用失败。"""
    assert "ANTHROPIC_API_KEY" in judge_run(run_dir)[0].reason


def test_调用失败的那张标待判读别的照常(run_dir, flaky_client):
    findings = judge_run(run_dir, client=flaky_client)
    assert sum(f.verdict == "pending" for f in findings) == 1
    assert sum(f.verdict != "pending" for f in findings) >= 1


def test_调用失败的理由里带着原始错误(run_dir, flaky_client):
    findings = judge_run(run_dir, client=flaky_client)
    pending = next(f for f in findings if f.verdict == "pending")
    assert "529 overloaded" in pending.reason


def test_模型返回的不是json就标unclear而不是炸掉(run_dir, garbage_client):
    assert all(f.verdict in {"unclear", "pending"}
               for f in judge_run(run_dir, client=garbage_client))


def test_模型胡说时原文进理由(run_dir, garbage_client):
    """人翻报告时看得见模型到底说了什么,比"解析失败"四个字有用。"""
    assert "好的,我看看" in judge_run(run_dir, client=garbage_client)[0].reason


def test_不认识的结论降级成unclear而不是照抄(run_dir):
    """照抄一个"大概正常"进报告,统计"异常几张"时就漏了。"""
    client = FakeClient({"verdict": "大概正常", "confidence": 1.0, "reason": "嗯"})
    assert judge_run(run_dir, client=client)[0].verdict == "unclear"


def test_判不了的那张置信度是零(run_dir, garbage_client):
    """模型没给结论却留着一个高置信度,排序时会把它压到最后。"""
    assert judge_run(run_dir, client=garbage_client)[0].confidence == 0.0


def test_置信度给成百分数也收得回零到一之间(run_dir):
    client = FakeClient({"verdict": "normal", "confidence": 95, "reason": ""})
    assert judge_run(run_dir, client=client)[0].confidence == 1.0


def test_置信度给了句话也不炸(run_dir):
    client = FakeClient({"verdict": "normal", "confidence": "高", "reason": ""})
    assert judge_run(run_dir, client=client)[0].confidence == 0.0


def test_重跑会覆盖上一次的模型结论(run_dir):
    judge_run(run_dir, client=FakeClient({"verdict": "abnormal",
                                          "confidence": 0.8, "reason": "漏油"}))
    judge_run(run_dir, client=FakeClient({"verdict": "normal",
                                          "confidence": 0.9, "reason": "好了"}))
    findings = read_findings(run_dir)
    assert [f.verdict for f in findings] == ["normal", "normal"]
    assert len(findings) == 2, "覆盖不是追加"


def test_没有照片时给一份空结论而不是报错(tmp_path, fake_client):
    run = tmp_path / "runs" / "空跑" / "20260902T101500Z"
    run.mkdir(parents=True)
    assert judge_run(run, client=fake_client) == []
    assert (run / "findings.json").is_file()


def test_findings坏了当没判过而不是炸掉(run_dir):
    (run_dir / "findings.json").write_text("{不是 JSON", encoding="utf-8")
    assert read_findings(run_dir) == []


# ----------------------------------------------------------------- 人工复核


def test_人工复核写的是另一个文件(run_dir, fake_client):
    judge_run(run_dir, client=fake_client)
    before = (run_dir / "findings.json").read_text(encoding="utf-8")
    save_review(run_dir, PHOTO, "normal", "看过了")
    assert (run_dir / "review.json").exists()
    assert (run_dir / "findings.json").read_text(encoding="utf-8") == before, \
        "模型的原始结论绝不能被人工复核覆盖"


def test_重跑判读不会抹掉人工复核(run_dir, fake_client):
    judge_run(run_dir, client=fake_client)
    save_review(run_dir, PHOTO, "normal", "看过了")
    judge_run(run_dir, client=fake_client)
    assert read_reviews(run_dir)[PHOTO]["note"] == "看过了"


def test_复核同一张会改掉上一次的(run_dir):
    save_review(run_dir, PHOTO, "abnormal", "有油渍")
    save_review(run_dir, PHOTO, "normal", "擦干净了")
    assert read_reviews(run_dir)[PHOTO] == {"verdict": "normal",
                                            "note": "擦干净了"}


def test_复核两张互不影响(run_dir):
    save_review(run_dir, PHOTO, "abnormal", "有油渍")
    save_review(run_dir, PHOTO2, "normal", "")
    assert set(read_reviews(run_dir)) == {PHOTO, PHOTO2}


def test_复核一张不存在的照片被拒(run_dir):
    with pytest.raises(ValueError):
        save_review(run_dir, "根本没有这张.jpg", "normal", "")


def test_复核路径里带穿越被拒(run_dir):
    with pytest.raises(ValueError):
        save_review(run_dir, "../manifest.json", "normal", "")


def test_复核结论只能是那几个值(run_dir):
    with pytest.raises(ValueError):
        save_review(run_dir, PHOTO, "大概吧", "")


def test_被拒的复核一个字都不落盘(run_dir):
    with pytest.raises(ValueError):
        save_review(run_dir, PHOTO, "大概吧", "")
    assert not (run_dir / "review.json").exists()


def test_没复核过时读回来是空的(run_dir):
    assert read_reviews(run_dir) == {}


def test_review坏了当没复核过(run_dir):
    (run_dir / "review.json").write_text("[]", encoding="utf-8")
    assert read_reviews(run_dir) == {}


# --------------------------------------------------------------------- 密钥


def test_密钥不会进任何落盘文件(run_dir, fake_client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-绝密")
    judge_run(run_dir, client=fake_client)
    save_review(run_dir, PHOTO, "normal", "看过了")
    for p in run_dir.rglob("*"):
        if p.is_file() and p.suffix in {".json", ".jsonl", ".md", ".html"}:
            assert "sk-绝密" not in p.read_text(encoding="utf-8", errors="ignore")


def test_没配密钥时造不出客户端():
    assert default_client() is None


def test_配了密钥就造得出客户端(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    client = default_client()
    assert isinstance(client, AnthropicClient)


def test_模型名可以用环境变量换掉(monkeypatch):
    """现场想省钱换个小模型,不该改代码。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("D1MAX_VLM_MODEL", "claude-haiku-4-5-20251001")
    assert default_client().model == "claude-haiku-4-5-20251001"


# ----------------------------------------------------------------- 真客户端


def test_请求体里图片是base64的图块():
    body = json.loads(AnthropicClient("sk-x")._body("看看", [_JPG]))
    blocks = body["messages"][0]["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["type"] == "base64"


def test_请求体里图片在文字前面():
    """图在前、问题在后:模型先看见证据,再看见要它回答什么。"""
    body = json.loads(AnthropicClient("sk-x")._body("看看", [_JPG, _JPG]))
    types = [b["type"] for b in body["messages"][0]["content"]]
    assert types == ["image", "image", "text"]


def test_请求体里带的是给定的模型名():
    body = json.loads(AnthropicClient("sk-x", "claude-sonnet-5")._body("看看", []))
    assert body["model"] == "claude-sonnet-5"


def test_请求体里没有密钥():
    """密钥只该在请求头里。进了请求体就会跟着日志、跟着抓包到处跑。"""
    assert "sk-绝密" not in AnthropicClient("sk-绝密")._body("看看", [_JPG]).decode()


# ------------------------------------------------------------------ 解析零件


def test_代码块包着的json也抠得出来():
    got = parse_reply({"verdict": "normal", "confidence": 0.5, "reason": "行"},
                      photo="a.jpg", waypoint="P1")
    assert got.verdict == "normal"


def test_提示词里不带历史时不提上一次():
    assert "上一次" not in build_prompt("P1", "看门", with_history=False)


def test_结论能原样转成字典再读回来():
    one = Finding(photo="a.jpg", waypoint="P1", verdict="abnormal",
                  confidence=0.7, reason="漏油", evidence="左下")
    assert Finding.from_wire(one.to_wire()) == one
