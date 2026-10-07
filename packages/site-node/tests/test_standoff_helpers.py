"""W25 测试用:给真代理喂一帧局部栅格(四周空、都看得见)和一帧人员检测(人在正前方 ``person`` 米)。"""

from __future__ import annotations

from d1max_contract.obsbridge import Grid, pack_bits
from d1max_contract.persbridge import Person, Persons

_seq = [0]


def 喂(agent, *, person: float | None) -> None:
    _seq[0] += 1
    n = 60
    agent.obs_view.on_grid(Grid(seq=_seq[0], stamp_ns=0, res=0.1, size=n,
                                occ=pack_bits([False] * n * n, n),
                                known=pack_bits([True] * n * n, n), rear=True, rear_cal=True))
    people = () if person is None else (Person(bearing_deg=0.0, range_m=person, score=0.9),)
    agent.person_view.on_persons(Persons(seq=_seq[0], stamp_ns=0, camera="front",
                                         people=people))
