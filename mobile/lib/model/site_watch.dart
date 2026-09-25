/// 站点的值守汇总（W00c5a，`GET /api/watch/summary`）。每台狗一行，站点自己一行。
///
/// **缺的一律 null，不写 0。** `null` 是「不知道」，`0` 是「查过了，没有」——两句话在屏上长得一样，
/// 在现场差着一次事故。每个 null 在 [SiteWatchRobot.why] 里带一句人话，屏上照印。
library;

class SiteWatchRobot {
  final String robotId;
  final bool online;
  final bool fresh;
  final int? lastSeenMs;
  final double? batteryPct;
  final int? batteryAsOfMs;
  final double? clockSkewS;
  final Map<String, int> alerts;
  final double? diskUsedRatio;
  final int? uploadBacklog;
  final Object? bundleLag;
  final Object? backup;
  final Map<String, String> why;

  const SiteWatchRobot({
    required this.robotId,
    required this.online,
    required this.fresh,
    required this.lastSeenMs,
    required this.batteryPct,
    required this.batteryAsOfMs,
    required this.clockSkewS,
    required this.alerts,
    required this.diskUsedRatio,
    required this.uploadBacklog,
    required this.bundleLag,
    required this.backup,
    required this.why,
  });

  factory SiteWatchRobot.fromWire(Map<String, dynamic> m) => SiteWatchRobot(
        robotId: '${m['robot_id'] ?? ''}',
        online: m['online'] == true,
        fresh: m['fresh'] == true,
        lastSeenMs: (m['last_seen_ms'] as num?)?.toInt(),
        batteryPct: (m['battery_pct'] as num?)?.toDouble(),
        batteryAsOfMs: (m['battery_as_of_ms'] as num?)?.toInt(),
        clockSkewS: (m['clock_skew_s'] as num?)?.toDouble(),
        alerts: _counts(m['alerts']),
        diskUsedRatio: (m['disk_used_ratio'] as num?)?.toDouble(),
        uploadBacklog: (m['upload_backlog'] as num?)?.toInt(),
        bundleLag: m['bundle_lag'],
        backup: m['backup'],
        why: _why(m['why']),
      );

  /// 这一项为什么是「不知道」。没说理由的 null 也要印一句，不许留白。
  String whyFor(String field) => why[field] ?? '站点没给这一项，这一档是「不知道」';
}

class SiteWatchSummary {
  final int nowMs;
  final List<SiteWatchRobot> robots;

  /// 排程这一拍办成了没有；null = 这个站点没开排程。
  final bool? scheduleOk;
  final String scheduleError;
  final Map<String, int> siteAlerts;
  final Map<String, String> siteWhy;

  const SiteWatchSummary({
    required this.nowMs,
    required this.robots,
    required this.scheduleOk,
    required this.scheduleError,
    required this.siteAlerts,
    required this.siteWhy,
  });

  factory SiteWatchSummary.fromWire(Map<String, dynamic> m) {
    final site = (m['site'] as Map?)?.cast<String, dynamic>() ?? const <String, dynamic>{};
    return SiteWatchSummary(
      nowMs: (m['now_ms'] as num?)?.toInt() ?? 0,
      robots: (m['robots'] as List? ?? const [])
          .whereType<Map>()
          .map((r) => SiteWatchRobot.fromWire(r.cast<String, dynamic>()))
          .toList(),
      scheduleOk: site['schedule_ok'] as bool?,
      scheduleError: '${site['schedule_error'] ?? ''}',
      siteAlerts: _counts(site['alerts']),
      siteWhy: _why(site['why']),
    );
  }
}

Map<String, int> _counts(Object? raw) {
  final m = (raw as Map?) ?? const {};
  return {for (final k in const ['P1', 'P2', 'P3']) k: (m[k] as num?)?.toInt() ?? 0};
}

Map<String, String> _why(Object? raw) =>
    {for (final e in ((raw as Map?) ?? const {}).entries) '${e.key}': '${e.value}'};
