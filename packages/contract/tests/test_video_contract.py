"""W00c5b:``video`` 命令的载荷。站点发、代理收,两头用同一份校验:相机名只认 front/back,
地址只认不带参数的 ``srt://主机:端口``(参数由代理自己拼,站点塞不进 ``mode=listener`` 之类),
口令是 SRT 要求的 10–79 位,有效期有上下限。"""

from __future__ import annotations

import pytest

from d1max_contract.errors import ContractError
from d1max_contract.video import (
    CAMERAS,
    VIDEO_TTL_MAX_MS,
    VIDEO_TTL_MIN_MS,
    VideoRequest,
    parse_video_payload,
    srt_push_url,
)


def _ok(**kw):
    d = {"camera": "front", "url": "srt://10.0.0.5:8890", "passphrase": "a" * 32,
         "ttl_ms": 10_000} | kw
    return d


def test_往返():
    r = parse_video_payload(_ok())
    assert r == VideoRequest(camera="front", url="srt://10.0.0.5:8890", passphrase="a" * 32,
                             ttl_ms=10_000)
    assert parse_video_payload(r.to_payload()) == r
    assert CAMERAS == ("front", "back")


@pytest.mark.parametrize("bad", [
    {"camera": "top"}, {"camera": 1},
    {"url": "rtsp://10.0.0.5:8890"}, {"url": "srt://10.0.0.5:8890?mode=listener"},
    {"url": "srt://10.0.0.5"}, {"url": "srt://;rm -rf /:1"}, {"url": "srt://h:70000"},
    {"passphrase": "short"}, {"passphrase": "x" * 80}, {"passphrase": "有中文的口令口令口令口令"},
    {"passphrase": "a&mode=listener00"},
    {"ttl_ms": VIDEO_TTL_MIN_MS - 1}, {"ttl_ms": VIDEO_TTL_MAX_MS + 1}, {"ttl_ms": "10000"},
    {"ttl_ms": True},
])
def test_坏的都拒(bad):
    with pytest.raises(ContractError):
        parse_video_payload(_ok(**bad))


def test_推流地址由代理拼_口令进参数():
    r = parse_video_payload(_ok())
    url = srt_push_url(r, latency_ms=200)
    assert url.startswith("srt://10.0.0.5:8890?")
    assert "passphrase=" + "a" * 32 in url and "pbkeylen=16" in url and "latency=200000" in url
    assert "mode=listener" not in url


def test_停止也是一条video命令_stop字段只认布尔():
    r = parse_video_payload(_ok(stop=True))
    assert r.stop is True and parse_video_payload(r.to_payload()) == r
    assert parse_video_payload(_ok()).stop is False
    with pytest.raises(ContractError):
        parse_video_payload(_ok(stop="yes"))


def test_抹掉口令():
    from d1max_contract.video import scrub
    assert scrub("srt://h:1?passphrase=abcDEF123456&pbkeylen=16: refused") == \
        "srt://h:1?passphrase=***&pbkeylen=16: refused"
