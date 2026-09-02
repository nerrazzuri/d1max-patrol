"""VLM 判读:让模型先看一遍照片,人再复核。

一趟巡检回来几十上百张照片,人从头翻一遍要半小时,而绝大多数是正常的。
模型的作用是**把注意力排序** —— 它给每张照片一个结论和一句理由,人只需要
认真看它标成异常和存疑的那几张。

**两层结论,分开存,谁也不覆盖谁。**

- ``findings.json``:模型说的。重跑判读会整个换掉。
- ``review.json``:人说的。判读重跑多少次都不动它。

分开是硬要求。合在一个文件里,重跑一次判读就把人昨天一张张看过的复核结果
抹了 —— 而那正是整份报告里最贵的东西。报告合成最终结论时以人的为准
(见 ``report.PhotoRow.final_verdict``)。

**判不了不等于判正常。** 没配密钥、调用失败、模型回了一段不是 JSON 的东西
—— 这几种情况分别落到 ``pending`` 和 ``unclear``,绝不落到 ``normal``。
把"没判"写成"正常"会让人跳过那张照片,而那恰恰是最该看的一张。

**一张照片失败不拖垮一趟。** 每张单独判、单独兜错,失败的那张标 ``pending``
等人来看,别的照常出结论。

**历史比对。** 同一个点位上一次拍的照片会跟这次的一起送进去。变电站里绝大
多数异常是"跟上次不一样"(多了一摊油、指示灯变了色),单看一张判不出来。
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from d1max_patrol.engine.archive import list_runs, read_manifest

#: 默认用的模型。判读要看图、要讲道理,给最能打的那个。
DEFAULT_MODEL = "claude-opus-5"

#: Anthropic 的消息接口。
API_URL = "https://api.anthropic.com/v1/messages"

#: 模型能给的结论。``pending`` 不在里面 —— 那是"还没判",不是模型的输出。
MODEL_VERDICTS = frozenset({"normal", "abnormal", "unclear"})

#: 人工复核能给的结论。和模型的一样:人也有"看不清"的时候。
REVIEW_VERDICTS = MODEL_VERDICTS

#: 落盘的理由最多留这么长。模型偶尔会回一整段,而 ``reason`` 是给人扫一眼的。
_REASON_MAX = 600

#: 没写 ``check`` 的点位用这句话兜底。
_DEFAULT_CHECK = (
    "照片里有没有异常:漏油、锈蚀、破损、异物、积水、门窗没关好、"
    "指示灯颜色不对、仪表读数超出正常范围")

_PROMPT = """你是变电站巡检的判读员。这是巡检机器人在点位「{waypoint}」拍到的照片。

判读依据:{check}

{history}只输出一个 JSON 对象,前后不要有别的字,也不要用代码块包起来。字段:

- verdict:normal(正常) / abnormal(异常) / unclear(看不清、判不了)
- confidence:0 到 1 的小数
- reason:一句话中文说明,讲清楚你依据照片里的什么下的结论
- evidence:照片里支持这个结论的位置或读数,没有就给空字符串

看不清、拍歪了、光线不够导致判不了,就给 unclear,别硬判 —— 判错一张比
说一句"看不清"贵得多。
"""

_HISTORY_HINT = ("第一张是这次拍的,第二张是上一次同一个点位拍的。"
                 "重点看**变化**:多了什么、少了什么、读数变了没有。\n\n")


@dataclass(frozen=True, slots=True)
class Finding:
    """模型对一张照片的结论。"""

    photo: str
    waypoint: str
    verdict: str
    confidence: float
    reason: str
    evidence: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "photo": self.photo,
            "waypoint": self.waypoint,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "reason": self.reason,
            "evidence": self.evidence,
        }

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> Finding:
        return cls(
            photo=str(raw.get("photo", "")),
            waypoint=str(raw.get("waypoint", "")),
            verdict=str(raw.get("verdict", "pending")),
            confidence=_as_confidence(raw.get("confidence")),
            reason=str(raw.get("reason", "")),
            evidence=str(raw.get("evidence", "")),
        )


class VlmClient(Protocol):
    """判读一张(或两张)照片,回一个 dict。

    故意收得这么窄:判读逻辑只需要"把提示词和图片送出去,拿回一个 dict"。
    换个模型、换个厂家,甚至换成本地跑的模型,都只是换一个实现。
    """

    def judge(self, prompt: str, images: Sequence[bytes]) -> dict[str, Any]: ...


# --------------------------------------------------------------------- 工具


def _as_confidence(value: Any) -> float:
    """把模型给的置信度收进 0..1。

    模型偶尔会给 95 而不是 0.95,也偶尔给一句话。收不进来就当 0 —— 置信度
    只用来排序,给错一个数比让整张照片判不出来轻。
    """
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    if conf != conf:            # NaN:任何比较都是假的,单独挡掉
        return 0.0
    return min(1.0, max(0.0, conf))


def _atomic_write(path: Path, text: str) -> None:
    """写临时文件再换过去。理由同 ``engine.archive``:断电时不留半个 JSON。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _split_photo_name(name: str) -> tuple[str, str]:
    """``<点位>__<相机>__<时间戳>.jpg`` -> (点位, 相机)。

    和 ``report`` 里那份是同一套规则:拆不开就把整个文件名当点位名,宁可
    判读的提示词里点位名难看一点,也不能因为多了个不认识的文件就漏判。
    """
    stem = Path(name).stem
    parts = stem.split("__")
    if len(parts) >= 3:
        return parts[0], parts[1]
    return stem, ""


def _checks(run_dir: Path) -> dict[str, str]:
    """从 manifest 的任务快照里取每个点位的判读依据。

    读快照而不是读 ``missions/*.yaml``:任务定义随时会被人改,而历史照片要
    按**当时**写的依据判读。
    """
    manifest = read_manifest(run_dir) or {}
    mission = manifest.get("mission") or {}
    out: dict[str, str] = {}
    for wp in mission.get("waypoints") or []:
        if isinstance(wp, dict) and wp.get("name"):
            out[str(wp["name"])] = str(wp.get("check", "") or "")
    return out


def _photos(run_dir: Path) -> list[str]:
    photos = Path(run_dir) / "photos"
    if not photos.is_dir():
        return []
    return sorted(p.name for p in photos.iterdir() if p.is_file())


def _previous_photo(run_dir: Path, history_root: Path | None,
                    waypoint: str, camera: str) -> bytes | None:
    """同一点位同一相机上一次拍的那张。没有就 ``None``。

    ``list_runs`` 已经是新的在前,所以第一个命中的就是"上一次"。当前这趟要
    跳过 —— 不然比对的是它自己。
    """
    if history_root is None:
        return None
    prefix = f"{waypoint}__{camera}__"
    here = Path(run_dir).resolve()
    for run in list_runs(history_root):
        try:
            if run.resolve() == here:
                continue
        except OSError:
            continue
        folder = run / "photos"
        if not folder.is_dir():
            continue
        candidates = sorted((p for p in folder.iterdir()
                             if p.is_file() and p.name.startswith(prefix)),
                            reverse=True)
        for path in candidates:
            try:
                return path.read_bytes()
            except OSError:
                continue
    return None


def build_prompt(waypoint: str, check: str, *, with_history: bool) -> str:
    """拼提示词。``check`` 为空时退化成"找常见异常"。"""
    return _PROMPT.format(waypoint=waypoint or "未知点位",
                          check=check.strip() or _DEFAULT_CHECK,
                          history=_HISTORY_HINT if with_history else "")


def parse_reply(raw: Any, *, photo: str, waypoint: str) -> Finding:
    """把模型回的东西变成一条结论。**怎么都不抛。**

    回的不是 dict、``verdict`` 不认识、``confidence`` 是句话 —— 一律降级成
    ``unclear``,并把原文放进 ``reason``。人翻报告时看得见模型到底说了什么,
    比看见一句"解析失败"有用。
    """
    if not isinstance(raw, dict):
        return Finding(photo=photo, waypoint=waypoint, verdict="unclear",
                       confidence=0.0,
                       reason=f"模型回的不是 JSON 对象: {str(raw)[:_REASON_MAX]}")
    verdict = str(raw.get("verdict", "")).strip().lower()
    reason = str(raw.get("reason", "") or "")[:_REASON_MAX]
    if verdict not in MODEL_VERDICTS:
        return Finding(
            photo=photo, waypoint=waypoint, verdict="unclear",
            confidence=0.0,
            reason=f"模型给的结论不认识({verdict or '空'}): {reason}"[:_REASON_MAX],
            evidence=str(raw.get("evidence", "") or "")[:_REASON_MAX])
    return Finding(photo=photo, waypoint=waypoint, verdict=verdict,
                   confidence=_as_confidence(raw.get("confidence")),
                   reason=reason,
                   evidence=str(raw.get("evidence", "") or "")[:_REASON_MAX])


# ------------------------------------------------------------------- 真客户端


def _extract_json(text: str) -> Any:
    """从模型回的一段文字里抠出那个 JSON 对象。

    要求了"只输出 JSON",但模型偶尔还是会包一层 ```json,或者前面加一句
    "好的"。找第一个 ``{`` 到最后一个 ``}``,比正则稳。抠不出来就把原文
    交出去,由 :func:`parse_reply` 降级成 ``unclear``。
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return text
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return text


class AnthropicClient:
    """打 Anthropic 消息接口的判读客户端。

    用 ``urllib`` 而不是 SDK:全局约束是"运行时不引第三方依赖",而这里要发
    的就是一个 POST。密钥只活在请求头里,不进日志、不进任何落盘文件。
    """

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, *,
                 url: str = API_URL, timeout_s: float = 90.0) -> None:
        self._key = api_key
        self._model = model
        self._url = url
        self._timeout = timeout_s

    @property
    def model(self) -> str:
        return self._model

    def _body(self, prompt: str, images: Sequence[bytes]) -> bytes:
        content: list[dict[str, Any]] = [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/jpeg",
                        "data": base64.b64encode(img).decode("ascii")}}
            for img in images
        ]
        content.append({"type": "text", "text": prompt})
        return json.dumps({
            "model": self._model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": content}],
        }, ensure_ascii=False).encode("utf-8")

    def judge(self, prompt: str, images: Sequence[bytes]) -> dict[str, Any]:
        req = urllib.request.Request(     # 地址是本模块的常量,不是外来输入
            self._url, data=self._body(prompt, images), method="POST",
            headers={"content-type": "application/json",
                     "x-api-key": self._key,
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        text = "".join(str(block.get("text", ""))
                       for block in payload.get("content") or []
                       if isinstance(block, dict) and block.get("type") == "text")
        parsed = _extract_json(text)
        return parsed if isinstance(parsed, dict) else {"verdict": "", "reason": text}


def default_client() -> VlmClient | None:
    """按环境变量造一个客户端。没配密钥就 ``None``。

    没密钥不是错误:模拟器上、现场没网时都跑得起来,只是照片全标"待判读"。
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    return AnthropicClient(key, os.environ.get("D1MAX_VLM_MODEL", DEFAULT_MODEL))


# --------------------------------------------------------------------- 判读


def judge_run(run_dir: Path, *, client: VlmClient | None = None,
              history_root: Path | None = None) -> list[Finding]:
    """判读一整趟的照片,结论写进 ``findings.json``,并返回。

    ``client`` 留空就按环境变量造一个;造不出来(没密钥)全部标 ``pending``。
    ``history_root`` 给的是 runs 根目录,用来找同点位上一次的照片做比对。

    **重跑是覆盖式的**:模型的结论整个换掉,``review.json`` 一个字不动。
    """
    run_dir = Path(run_dir)
    vlm = client if client is not None else default_client()
    checks = _checks(run_dir)
    findings: list[Finding] = []

    for name in _photos(run_dir):
        waypoint, camera = _split_photo_name(name)
        if vlm is None:
            findings.append(Finding(
                photo=name, waypoint=waypoint, verdict="pending",
                confidence=0.0,
                reason="没有 ANTHROPIC_API_KEY,这张还没判读"))
            continue
        try:
            data = (run_dir / "photos" / name).read_bytes()
        except OSError as exc:
            findings.append(Finding(photo=name, waypoint=waypoint,
                                    verdict="pending", confidence=0.0,
                                    reason=f"照片读不了: {exc}"))
            continue
        previous = _previous_photo(run_dir, history_root, waypoint, camera)
        images = [data] if previous is None else [data, previous]
        prompt = build_prompt(waypoint, checks.get(waypoint, ""),
                              with_history=previous is not None)
        try:
            raw = vlm.judge(prompt, images)
        except Exception as exc:    # noqa: BLE001
            # 判读一张失败不能拖垮整趟:超时、限流、网断、模型换了返回格式
            # ——每一种都只该让**这一张**变成"待判读",别的照常出结论。
            findings.append(Finding(
                photo=name, waypoint=waypoint, verdict="pending",
                confidence=0.0,
                reason=f"判读没调通({type(exc).__name__}): {exc}"[:_REASON_MAX]))
            continue
        findings.append(parse_reply(raw, photo=name, waypoint=waypoint))

    _atomic_write(run_dir / "findings.json",
                  json.dumps([f.to_wire() for f in findings],
                             ensure_ascii=False, indent=2))
    return findings


def read_findings(run_dir: Path) -> list[Finding]:
    """读回模型的结论。文件没有或坏了都当空 —— 判读是可选的一步。"""
    path = Path(run_dir) / "findings.json"
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [Finding.from_wire(item) for item in raw if isinstance(item, dict)]


# ----------------------------------------------------------------- 人工复核


def read_reviews(run_dir: Path) -> dict[str, dict[str, Any]]:
    """读回人工复核。key 是照片文件名。"""
    path = Path(run_dir) / "review.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def save_review(run_dir: Path, photo: str, verdict: str, note: str) -> None:
    """记一条人工复核。**写的是 ``review.json``,碰不到 ``findings.json``。**

    照片必须真的存在:复核一张不在这趟里的照片,多半是页面上点错了行,
    静默存下来只会让报告里凭空多出一条对不上号的结论。
    """
    run_dir = Path(run_dir)
    if verdict not in REVIEW_VERDICTS:
        raise ValueError(
            f"复核结论只能是 {'/'.join(sorted(REVIEW_VERDICTS))},不是 {verdict!r}")
    if ".." in photo or "/" in photo or "\\" in photo:
        raise ValueError(f"照片名不合法: {photo!r}")
    if not (run_dir / "photos" / photo).is_file():
        raise ValueError(f"这趟里没有这张照片: {photo}")
    reviews = read_reviews(run_dir)
    reviews[photo] = {"verdict": verdict, "note": str(note)}
    _atomic_write(run_dir / "review.json",
                  json.dumps(reviews, ensure_ascii=False, indent=2))


__all__ = [
    "API_URL",
    "DEFAULT_MODEL",
    "MODEL_VERDICTS",
    "REVIEW_VERDICTS",
    "AnthropicClient",
    "Finding",
    "VlmClient",
    "build_prompt",
    "default_client",
    "judge_run",
    "parse_reply",
    "read_findings",
    "read_reviews",
    "save_review",
]
