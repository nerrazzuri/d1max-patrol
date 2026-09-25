"""W00c5d 第三部分:发布经站点的契约。"""

from __future__ import annotations

import pytest

from d1max_contract.errors import ContractError
from d1max_contract.releases import ReleaseRef, check_release_name


def test_往返与坏载荷():
    r = ReleaseRef.from_wire({"name": "2026-09-25-77b2de", "sha256": "a" * 64, "size": 10})
    assert ReleaseRef.from_wire(r.to_wire()) == r
    for bad in ({"name": "../x", "sha256": "a" * 64, "size": 1},
                {"name": "2026-09-25-77b2de", "sha256": "A" * 64, "size": 1},
                {"name": "2026-09-25-77b2de", "sha256": "a" * 64, "size": 0},
                {"name": "2026-09-25-77b2de", "sha256": "a" * 64, "size": True}, []):
        with pytest.raises(ContractError):
            ReleaseRef.from_wire(bad)
    for bad in ("2026-9-25-77b2de", "2026-09-25-77b2de\n"):   # 带换行的也不认(fullmatch)
        with pytest.raises(ContractError):
            check_release_name(bad)
