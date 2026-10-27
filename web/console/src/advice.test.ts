// W44 第二期:技术提醒说人话(设计 §5.0「要你处理的事」):每条写这是什么事、该做什么、按钮去哪。
import { beforeEach, describe, expect, it } from "vitest";
import { advice } from "./advice";
import { lang, t } from "./i18n";

beforeEach(() => {
  lang.value = "zh";
});

describe("技术提醒说人话", () => {
  it("证书快到期:请联系工程师", () => {
    const a = advice("cert_expiring")!;
    expect(t(a.todo)).toContain("工程师");
    expect(a.go).toBeUndefined();
  });
  it("备份:去系统页看", () => {
    expect(advice("backup_stale")!.go).toBe("admin/system");
    expect(advice("backup_verify_failed")!.go).toBe("admin/system");
  });
  it("硬盘、证据、版本、推送、后台、定位、巡检照片都有说法", () => {
    for (const k of ["site_disk_low", "disk_80", "evidence_plain", "evidence_seal_failed", "agent_too_old", "site_too_old", "push_failed",
      "watchdog_died", "schedule_died", "loc_degraded", "finding"]) {
      expect(advice(k), k).not.toBeNull();
    }
    expect(advice("loc_degraded")!.go).toBe("admin/localization");
  });
  it("没写的回 null", () => {
    expect(advice("something_new")).toBeNull();
  });
  it("说法里不写狗", () => {
    for (const k of ["cert_expiring", "site_disk_low", "evidence_plain", "agent_too_old", "loc_degraded", "finding"]) {
      const a = advice(k)!;
      expect(t(a.say) + t(a.todo)).not.toContain("狗");
    }
  });
});
