// 技术提醒说人话(W44 第二期,设计 §5.0「要你处理的事」):给业主的那一类告警,每种写这是什么事、该做什么、按钮去哪。
// 没写的种类,页面用告警标题 + 「请联系工程师」。
import type { Key } from "./i18n";
import type { AdminPage } from "./shell";

export interface Advice { say: Key; todo: Key; go?: AdminPage }

const TABLE: Record<string, Advice> = {
  cert_expiring: { say: "advCert", todo: "advCallEngineer" },
  site_disk_low: { say: "advDisk", todo: "advDiskTodo", go: "admin/system" },
  disk_80: { say: "advDisk", todo: "advDiskTodo", go: "admin/system" },
  backup_stale: { say: "advBackup", todo: "advBackupTodo", go: "admin/system" },
  backup_verify_failed: { say: "advBackupBad", todo: "advBackupTodo", go: "admin/system" },
  evidence_plain: { say: "advEvidence", todo: "advCallEngineer" },
  evidence_seal_failed: { say: "advEvidence", todo: "advCallEngineer" },
  evidence_unopened: { say: "advEvidence", todo: "advCallEngineer" },
  agent_too_old: { say: "advRobotOld", todo: "advUpgrade" },
  site_too_old: { say: "advSiteOld", todo: "advUpgrade" },
  push_failed: { say: "advPush", todo: "advPushTodo" },
  watchdog_died: { say: "advStuck", todo: "advStuckTodo" },
  schedule_died: { say: "advStuck", todo: "advStuckTodo" },
  lane_stuck: { say: "advStuck", todo: "advStuckTodo" },
  loc_degraded: { say: "advLoc", todo: "advLocTodo", go: "admin/localization" },
  finding: { say: "advFinding", todo: "advFindingTodo" },
  recording_failed: { say: "advRecording", todo: "advCallEngineer" },
  map_failed: { say: "advMap", todo: "advCallEngineer", go: "admin/maps" },
};

export function advice(kind: string): Advice | null {
  return TABLE[kind] ?? null;
}
