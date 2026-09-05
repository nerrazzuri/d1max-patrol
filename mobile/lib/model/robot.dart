/// 名册里的一条：这是哪只狗，怎么连上它。
///
/// **为什么主键是 SN 而不是地址。** 每只 D1 Max 的内网地址都一模一样
/// （RK3588 `192.168.168.168`、Orin `192.168.168.100`），这是机器里写死的，
/// 不是我们配的。所以地址分不出谁是谁，只能认机身。服务端那边同样的道理写在
/// `src/d1max_patrol/app/identity.py` 里。
///
/// **PIN 不在这里。** 见 [Robot.toJson] 的注释。
library;

/// 兜底 SN 的前缀。服务端查不到真序列号时拿网卡 MAC 凑一个，形如
/// `mac:1c697a8b2f30`。这种 SN 会跟着机器换主板而变，认得出来才好在界面上
/// 标一下，别让人当成厂商给的号。
const String provisionalSnPrefix = 'mac:';

/// 名册里的一只狗。
///
/// 不可变。改一条就 [copyWith] 出新的，省得两处引用同一个对象互相打架。
class Robot {
  /// 机身序列号。名册的主键，不能为空 —— 空 SN 等于"不知道是哪只"，
  /// 而不知道是哪只的条目留在名册里只会让人按错。
  final String sn;

  /// 现场喊的名字，比如"三号"。可以为空，空的时候界面退回显示 SN。
  final String name;

  /// 这只狗热点的名字，比如 `XG2WIFI_C40221`。手机靠它知道该连哪个 WiFi。
  final String ssid;

  /// 热点的 BSSID（AP 的 MAC）。**连上之前就看得见**，所以它是唯一一个
  /// 不用解锁就能拿到的身份线索 —— 手机可以先用它认出面前是哪只狗，
  /// 再决定拿哪个 PIN 去解锁。可以为空（没记过）。
  final String bssid;

  /// 服务端在哪。默认是 Orin 上那个 app。
  final String host;
  final int port;

  const Robot({
    required this.sn,
    this.name = '',
    this.ssid = '',
    this.bssid = '',
    this.host = defaultHost,
    this.port = defaultPort,
  });

  /// Orin NX 的内网地址。app 装在它上面（厂商 SDK 指南 p.25 允许）。
  static const String defaultHost = '192.168.168.100';

  /// 服务端默认端口，跟 `server.py` 里的 `DEFAULT_PORT` 对齐。
  static const int defaultPort = 8095;

  /// 这个 SN 是不是兜底凑出来的。
  bool get provisional => sn.startsWith(provisionalSnPrefix);

  /// 界面上显示成什么。有名字用名字，没名字只能报 SN。
  String get label => name.isEmpty ? sn : '$name（$sn）';

  /// 服务端的根地址。
  String get baseUrl => 'http://$host:$port';

  Robot copyWith({
    String? sn,
    String? name,
    String? ssid,
    String? bssid,
    String? host,
    int? port,
  }) {
    return Robot(
      sn: sn ?? this.sn,
      name: name ?? this.name,
      ssid: ssid ?? this.ssid,
      bssid: bssid ?? this.bssid,
      host: host ?? this.host,
      port: port ?? this.port,
    );
  }

  /// 落盘用的形状。
  ///
  /// **这里没有 PIN，而且不能有。** PIN 是控制这台机器的钥匙，跟名字、SSID
  /// 这些不是一个东西：后者丢了不过是要重填一遍，前者丢了等于把狗交出去。
  /// 所以 PIN 走单独的密钥库（[PinVault]），名册文件里从头到尾看不见它。
  /// `test/robot_test.dart` 里有一条测试专门盯着这件事。
  Map<String, dynamic> toJson() => {
        'sn': sn,
        'name': name,
        'ssid': ssid,
        'bssid': bssid,
        'host': host,
        'port': port,
      };

  /// 从盘上读回来。
  ///
  /// 认不出的字段一律忽略，缺的字段用默认值 —— 名册是长期存的，日后加了
  /// 新字段，旧手机上的旧文件还得读得动。
  factory Robot.fromJson(Map<String, dynamic> raw) {
    final sn = (raw['sn'] as String? ?? '').trim();
    if (sn.isEmpty) {
      throw const FormatException('名册里有条目没有 SN，读不出是哪只狗');
    }
    return Robot(
      sn: sn,
      name: (raw['name'] as String? ?? '').trim(),
      ssid: (raw['ssid'] as String? ?? '').trim(),
      bssid: normalizeBssid(raw['bssid'] as String? ?? ''),
      host: (raw['host'] as String? ?? '').trim().isEmpty
          ? defaultHost
          : (raw['host'] as String).trim(),
      port: raw['port'] is int ? raw['port'] as int : defaultPort,
    );
  }

  @override
  bool operator ==(Object other) =>
      other is Robot &&
      other.sn == sn &&
      other.name == name &&
      other.ssid == ssid &&
      other.bssid == bssid &&
      other.host == host &&
      other.port == port;

  @override
  int get hashCode => Object.hash(sn, name, ssid, bssid, host, port);

  @override
  String toString() => 'Robot($sn, $name, $ssid)';
}

/// 把 BSSID 统一成小写冒号分隔。
///
/// Android 各家给的写法不一样：有大写的、有横杠分隔的。不统一的话
/// "同一个热点"会被当成两个，按 BSSID 认狗就认不出来。
String normalizeBssid(String raw) {
  final value = raw.trim().toLowerCase().replaceAll('-', ':');
  return value;
}
