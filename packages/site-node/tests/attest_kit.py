"""W63 测试辅助:给测试站点配一把临时出厂钥匙;造「原厂机器人」(每个序列号一把设备钥匙)答站点的题。"""

from __future__ import annotations

import json
from pathlib import Path

from d1max_contract import device_id as di
from d1max_contract import ed25519


class 出厂:
    def __init__(self, home: Path) -> None:
        self.seed = ed25519.new_seed()
        pub = home / "test-factory-pub.pem"
        pub.write_bytes(ed25519.public_pem(ed25519.public_key(self.seed)))
        cfg_path = home / "site.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["factory_pubkey"] = str(pub)
        cfg_path.write_text(json.dumps(cfg))
        self.devices: dict[str, bytes] = {}

    def 证书(self, serial: str, hw: str = "e" * 64) -> tuple[bytes, dict]:
        dev = self.devices.setdefault(serial, ed25519.new_seed())
        req = di.make_request(serial=serial, model="d1max", device_pub=ed25519.public_key(dev),
                              hw_id=hw)
        return dev, di.sign_device(self.seed, req, now_ms=1)

    def 答(self, s, serial: str, purpose: str) -> dict:
        """向站点 ``s``(test_site_api 的 站)要题,用这台的设备钥匙答。"""
        code, ch = s.req("GET", "/api/attest/challenge")
        assert code == 200, ch
        dev, cert = self.证书(serial)
        msg = di.attest_message(purpose=purpose, site_id=ch["site_id"], nonce=ch["nonce"],
                                serial=serial, hw_id=cert["hw_id"])
        return {"cert": cert, "hw_id": cert["hw_id"], "nonce": ch["nonce"],
                "sig": di.sign_attest(dev, msg)}
