"""本机人员桥,代理这一头(W24)。跟障碍桥同一套规矩(:class:`d1max_agent.obs_server.ObsBridgeServer`),
只换报文(:mod:`d1max_contract.persbridge`)、套接字(``persons.sock``)、交给 :class:`PersonView`。"""

from __future__ import annotations

from typing import Any

from d1max_agent.obs_server import ObsBridgeServer
from d1max_contract import persbridge


class PersonBridgeServer(ObsBridgeServer):
    BRIDGE: Any = persbridge
    DATA: Any = persbridge.Persons
    DATA_CB = "on_persons"
    WHAT = "本机人员桥"
    PEER = "人员检测节点"
